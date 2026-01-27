"""
Cocoris 商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 sucreyshopping.jp Cocoris 品牌所有商品
2. 計算材積重量 vs 實際重量，取大值
3. 上架到 Shopify（不重複上架）
4. 原價寫入成本價（Cost）
5. OpenAI 翻譯成繁體中文
"""

from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
from urllib.parse import urljoin
import threading
import base64

app = Flask(__name__)

# ========== 設定 ==========
SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""

BASE_URL = "https://sucreyshopping.jp"
LIST_BASE_URL = "https://sucreyshopping.jp/shop/c/c10/?brand=cocoris"
LIST_PAGE_URL_TEMPLATE = "https://sucreyshopping.jp/shop/c/c10_p{page}/?brand=cocoris"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

MIN_PRICE = 1000

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7',
    'Connection': 'keep-alive',
}

scrape_status = {
    "running": False,
    "progress": 0,
    "total": 0,
    "current_product": "",
    "products": [],
    "errors": [],
    "uploaded": 0,
    "skipped": 0,
    "skipped_exists": 0,
    "filtered_by_price": 0,
    "deleted": 0
}


def load_shopify_token():
    """載入 Shopify Access Token 和商店名稱"""
    global SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP
    
    env_token = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
    env_shop = os.environ.get('SHOPIFY_SHOP', '')
    
    if env_token and env_shop:
        SHOPIFY_ACCESS_TOKEN = env_token
        SHOPIFY_SHOP = env_shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
        print(f"[設定] 從環境變數載入 - 商店: {SHOPIFY_SHOP}")
        return True
    
    token_file = "shopify_token.json"
    if os.path.exists(token_file):
        with open(token_file, 'r') as f:
            data = json.load(f)
            SHOPIFY_ACCESS_TOKEN = data.get('access_token', '')
            shop = data.get('shop', '')
            if shop:
                SHOPIFY_SHOP = shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
            print(f"[設定] 從檔案載入 - 商店: {SHOPIFY_SHOP}")
            return True
    return False


def get_shopify_headers():
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }


def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


def calculate_selling_price(cost, weight):
    """售價 = [進貨價 + (重量 * 1250)] / 0.7"""
    if not cost or cost <= 0:
        return 0
    shipping_cost = weight * 1250 if weight else 0
    price = (cost + shipping_cost) / 0.7
    return round(price)


def clean_html_for_translation(html_text):
    """清除 HTML 標籤，只保留純文字"""
    if not html_text:
        return ""
    
    text = html_text
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'#[\w-]+\s*\{[^}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\.[\w-]+\s*\{[^}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'@media[^{]*\{[^}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\s*style\s*=\s*["\'][^"\']*["\']', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    
    return text.strip()


def translate_with_chatgpt(title, description):
    """使用 ChatGPT 翻譯商品名稱和說明"""
    clean_description = clean_html_for_translation(description)
    
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本甜點商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{clean_description[:1500]}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，簡潔有力，前面加上 Cocoris）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示）",
    "page_title": "SEO 頁面標題（繁體中文，包含 Cocoris 品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

重要規則：
1. 這是日本 Cocoris 的精緻烘焙甜點
2. 翻譯要自然流暢，不要生硬
3. 商品標題開頭必須是「Cocoris」（英文）
4. 【禁止使用任何日文】所有內容必須是繁體中文或英文，不可出現任何日文字
5. SEO 內容要包含：Cocoris、日本、甜點、伴手禮、送禮等關鍵字
6. 只回傳 JSON，不要其他文字"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。你的輸出必須完全使用繁體中文和英文，絕對禁止出現任何日文字元。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0,
                "max_tokens": 1000
            },
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            
            content = content.strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content.rsplit('```', 1)[0]
            content = content.strip()
            
            translated = json.loads(content)
            
            trans_title = translated.get('title', title)
            if not trans_title.startswith('Cocoris'):
                trans_title = f"Cocoris {trans_title}"
            
            return {
                'success': True,
                'title': trans_title,
                'description': translated.get('description', description),
                'page_title': translated.get('page_title', ''),
                'meta_description': translated.get('meta_description', '')
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': f"Cocoris {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"Cocoris {title}",
            'description': description,
            'page_title': '',
            'meta_description': ''
        }


def download_image_to_base64(img_url, max_retries=3):
    """下載圖片並轉換為 Base64"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
        'Referer': 'https://sucreyshopping.jp/',
        'Connection': 'keep-alive',
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.get(img_url, headers=headers, timeout=30)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', 'image/jpeg')
                if 'jpeg' in content_type or 'jpg' in content_type:
                    img_format = 'image/jpeg'
                elif 'png' in content_type:
                    img_format = 'image/png'
                elif 'webp' in content_type:
                    img_format = 'image/webp'
                elif 'gif' in content_type:
                    img_format = 'image/gif'
                else:
                    img_format = 'image/jpeg'
                
                img_base64 = base64.b64encode(response.content).decode('utf-8')
                return {
                    'success': True,
                    'base64': img_base64,
                    'content_type': img_format
                }
            else:
                print(f"[圖片下載] 第 {attempt+1} 次嘗試失敗: HTTP {response.status_code}")
        except Exception as e:
            print(f"[圖片下載] 第 {attempt+1} 次嘗試異常: {e}")
        
        time.sleep(1)
    
    return {'success': False}


def get_existing_products_map():
    """取得 Shopify 已存在的商品，回傳 {sku: product_id}"""
    products_map = {}
    url = shopify_api_url("products.json?limit=250")
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            print(f"Error fetching products: {response.status_code}")
            break
        
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    products_map[sku] = product_id
        
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    return products_map


def get_collection_products_map(collection_id):
    """取得特定 Collection 內的商品"""
    products_map = {}
    if not collection_id:
        return products_map
    
    url = shopify_api_url(f"collections/{collection_id}/products.json?limit=250")
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            break
        
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    products_map[sku] = product_id
        
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    print(f"[INFO] Collection 內有 {len(products_map)} 個商品")
    return products_map


def set_product_to_draft(product_id):
    """將商品設為草稿"""
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {"id": product_id, "status": "draft"}
    })
    if response.status_code == 200:
        print(f"[設為草稿] Product ID: {product_id}")
        return True
    return False


def get_or_create_collection(collection_title="Cocoris"):
    """取得或建立 Collection"""
    response = requests.get(
        shopify_api_url(f'custom_collections.json?title={collection_title}'),
        headers=get_shopify_headers()
    )
    
    if response.status_code == 200:
        collections = response.json().get('custom_collections', [])
        for col in collections:
            if col['title'] == collection_title:
                print(f"[INFO] 找到現有 Collection: {collection_title} (ID: {col['id']})")
                return col['id']
    
    response = requests.post(
        shopify_api_url('custom_collections.json'),
        headers=get_shopify_headers(),
        json={
            'custom_collection': {
                'title': collection_title,
                'published': True
            }
        }
    )
    
    if response.status_code == 201:
        collection_id = response.json()['custom_collection']['id']
        print(f"[INFO] 建立新 Collection: {collection_title} (ID: {collection_id})")
        return collection_id
    
    print(f"[ERROR] 無法建立 Collection: {response.text}")
    return None


def add_product_to_collection(product_id, collection_id):
    """將商品加入 Collection"""
    response = requests.post(
        shopify_api_url('collects.json'),
        headers=get_shopify_headers(),
        json={
            'collect': {
                'product_id': product_id,
                'collection_id': collection_id
            }
        }
    )
    return response.status_code == 201


def publish_to_all_channels(product_id):
    """發布到所有銷售渠道"""
    print(f"[發布] 正在發布商品 {product_id} 到所有渠道...")
    
    graphql_url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }
    
    query = """
    {
      publications(first: 20) {
        edges {
          node {
            id
            name
            supportsFuturePublishing
          }
        }
      }
    }
    """
    
    response = requests.post(graphql_url, headers=headers, json={'query': query})
    
    if response.status_code != 200:
        print(f"[發布] 無法取得渠道列表: {response.status_code}")
        return False
    
    result = response.json()
    publications = result.get('data', {}).get('publications', {}).get('edges', [])
    
    seen_names = set()
    unique_publications = []
    for pub in publications:
        name = pub['node']['name']
        if name not in seen_names:
            seen_names.add(name)
            unique_publications.append(pub['node'])
    
    print(f"[發布] 找到 {len(unique_publications)} 個銷售渠道")
    
    publication_inputs = [{"publicationId": pub['id']} for pub in unique_publications]
    
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount {
            count
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    
    variables = {
        "id": f"gid://shopify/Product/{product_id}",
        "input": publication_inputs
    }
    
    pub_response = requests.post(graphql_url, headers=headers, json={
        'query': mutation,
        'variables': variables
    })
    
    if pub_response.status_code == 200:
        pub_result = pub_response.json()
        data = pub_result.get('data') or {}
        publishable_publish = data.get('publishablePublish') or {}
        errors = publishable_publish.get('userErrors') or []
        
        if errors:
            real_errors = [e for e in errors if 'does not exist' not in e.get('message', '')]
            if real_errors:
                print(f"[發布] 錯誤: {real_errors}")
        
        return True
    else:
        print(f"[發布] GraphQL 請求失敗: {pub_response.status_code}")
        return False


def parse_box_size(text):
    """解析箱サイズ並計算材積重量"""
    text = text.replace('×', 'x').replace('Ｘ', 'x').replace('ｘ', 'x')
    text = text.replace('ｍｍ', 'mm').replace('ｇ', 'g').replace('ｋｇ', 'kg')
    text = text.replace(',', '')
    
    pattern = r'[Ww]?\s*(\d+(?:\.\d+)?)\s*[xX×]\s*[Dd]?\s*(\d+(?:\.\d+)?)\s*[xX×]\s*[Hh]?\s*(\d+(?:\.\d+)?)'
    match = re.search(pattern, text)
    
    if match:
        w, d, h = float(match.group(1)), float(match.group(2)), float(match.group(3))
        volume_weight = (w * d * h) / 6000000
        volume_weight = round(volume_weight, 2)
        print(f"[尺寸解析] {w}x{d}x{h}mm -> 材積重量: {volume_weight}kg")
        return {"width": w, "depth": d, "height": h, "volume_weight": volume_weight}
    
    simple_pattern = r'(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)'
    simple_match = re.search(simple_pattern, text)
    
    if simple_match:
        l, w, h = float(simple_match.group(1)), float(simple_match.group(2)), float(simple_match.group(3))
        volume_weight = (l * w * h) / 6000000
        volume_weight = round(volume_weight, 2)
        print(f"[尺寸解析] {l}x{w}x{h}mm -> 材積重量: {volume_weight}kg")
        return {"length": l, "width": w, "height": h, "volume_weight": volume_weight}
    
    return None


def scrape_product_list():
    """爬取商品列表"""
    products = []
    page_num = 1
    has_next_page = True
    
    while has_next_page:
        if page_num == 1:
            url = LIST_BASE_URL
        else:
            url = LIST_PAGE_URL_TEMPLATE.format(page=page_num)
        
        print(f"[INFO] 正在載入第 {page_num} 頁: {url}")
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            
            if response.status_code != 200:
                print(f"[ERROR] 載入頁面失敗: HTTP {response.status_code}")
                has_next_page = False
                continue
            
            soup = BeautifulSoup(response.text, 'html.parser')
            product_links = soup.find_all('a', href=re.compile(r'/shop/g/g[^/]+/?'))
            
            if not product_links:
                print(f"[INFO] 第 {page_num} 頁沒有找到商品，停止")
                has_next_page = False
                continue
            
            seen_skus = set()
            page_products = []
            
            for link in product_links:
                href = link.get('href', '')
                if not href or '/shop/g/g' not in href:
                    continue
                
                sku_match = re.search(r'/shop/g/g([^/]+)/?', href)
                if not sku_match:
                    continue
                
                sku = sku_match.group(1)
                
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)
                
                full_url = urljoin(BASE_URL, href)
                page_products.append({'url': full_url, 'sku': sku})
            
            print(f"[INFO] 第 {page_num} 頁找到 {len(page_products)} 個商品")
            products.extend(page_products)
            
            next_link = soup.find('a', href=re.compile(f'c10_p{page_num + 1}'))
            if next_link:
                page_num += 1
            else:
                has_next_page = False
                
        except Exception as e:
            print(f"[ERROR] 載入頁面失敗: {e}")
            has_next_page = False
    
    unique_products = []
    seen = set()
    for p in products:
        if p['sku'] not in seen:
            seen.add(p['sku'])
            unique_products.append(p)
    
    print(f"[INFO] 共收集 {len(unique_products)} 個不重複商品")
    return unique_products


def scrape_product_detail(url):
    """爬取單一商品詳細資訊"""
    product = {
        'url': url,
        'title': '',
        'price': 0,
        'description': '',
        'box_size_text': '',
        'weight': 0,
        'images': [],
        'in_stock': True,
        'is_point_product': False,
        'sku': '',
        'content': '',
        'allergens': '',
        'shelf_life': ''
    }
    
    sku_match = re.search(r'/shop/g/g([^/]+)/?', url)
    if sku_match:
        product['sku'] = sku_match.group(1)
    
    try:
        print(f"[載入] {url}")
        response = requests.get(url, headers=HEADERS, timeout=30)
        
        if response.status_code != 200:
            print(f"[ERROR] 載入頁面失敗: HTTP {response.status_code}")
            return product
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # 商品名稱
        title_el = soup.find('h1')
        if title_el:
            product['title'] = title_el.get_text(strip=True)
            print(f"[標題] {product['title']}")
        
        # 檢查是否為點數商品
        price_area = soup.find('div', class_='block-goods-price')
        if price_area:
            price_area_text = price_area.get_text()
            if 'ポイント' in price_area_text:
                product['is_point_product'] = True
                print(f"[點數商品] 偵測到ポイント商品")
        
        # 價格
        if not product['is_point_product']:
            price_el = soup.find('div', class_='block-goods-price--price')
            if price_el:
                price_text = price_el.get_text()
                price_match = re.search(r'(\d{1,3}(?:,\d{3})*)', price_text)
                if price_match:
                    price_str = price_match.group(1).replace(',', '')
                    product['price'] = int(price_str)
                    print(f"[價格] ¥{product['price']}")
            
            if not product['price']:
                price_match = re.search(r'(\d{1,3}(?:,\d{3})*)\s*円', page_text)
                if price_match:
                    price_str = price_match.group(1).replace(',', '')
                    product['price'] = int(price_str)
                    print(f"[價格-備用] ¥{product['price']}")
        
        # 商品資訊
        all_dt = soup.find_all('dt')
        all_dd = soup.find_all('dd')
        
        for i, dt in enumerate(all_dt):
            try:
                dt_text = dt.get_text(strip=True)
                if i < len(all_dd):
                    dd_text = all_dd[i].get_text(strip=True)
                    
                    if '内容' in dt_text:
                        product['content'] = dd_text
                        print(f"[內容] {dd_text[:50]}...")
                    elif '箱サイズ' in dt_text or 'サイズ' in dt_text:
                        product['box_size_text'] = dd_text
                        size_info = parse_box_size(dd_text)
                        if size_info:
                            product['weight'] = size_info.get('volume_weight', 0)
                        print(f"[尺寸] {dd_text} -> {product['weight']}kg")
                    elif '賞味期限' in dt_text:
                        product['shelf_life'] = dd_text
                    elif 'アレルギー' in dt_text or '特定原材料' in dt_text:
                        product['allergens'] = dd_text[:200]
            except Exception:
                continue
        
        # 商品說明
        desc_parts = []
        desc_selectors = ['item-description', 'product-description', 'detail-text']
        for class_name in desc_selectors:
            desc_el = soup.find('div', class_=class_name)
            if desc_el:
                desc_text = desc_el.get_text(strip=True)
                if desc_text and len(desc_text) > 20:
                    desc_parts.append(desc_text)
                    break
        
        if product['content']:
            desc_parts.append(f"內容：{product['content']}")
        if product['shelf_life']:
            desc_parts.append(f"賞味期限：{product['shelf_life']}")
        
        product['description'] = '\n\n'.join(desc_parts)
        
        # 圖片
        images = []
        sku = product['sku']
        
        image_prefixes = ['L', '2', '3', '4', 'D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7', 'D8']
        
        for prefix in image_prefixes:
            img_url = f"{BASE_URL}/img/goods/{prefix}/{sku}.jpg"
            try:
                head_response = requests.head(img_url, headers=HEADERS, timeout=5)
                if head_response.status_code == 200:
                    images.append(img_url)
                    print(f"[圖片] 找到: {prefix}/{sku}.jpg")
            except:
                pass
        
        if not images:
            img_tags = soup.find_all('img', src=re.compile(sku))
            for img in img_tags:
                src = img.get('src', '')
                if src and src not in images:
                    if not src.startswith('http'):
                        src = urljoin(BASE_URL, src)
                    images.append(src)
        
        product['images'] = images
        print(f"[圖片] 共找到 {len(images)} 張圖片")
        
        # 庫存狀態
        if '品切れ' in page_text or '在庫なし' in page_text or 'SOLD OUT' in page_text:
            product['in_stock'] = False
            print(f"[庫存] 無庫存")
        
    except Exception as e:
        print(f"[ERROR] 爬取商品詳細失敗: {e}")
        import traceback
        traceback.print_exc()
    
    return product


def upload_to_shopify(product, collection_id=None):
    """上傳商品到 Shopify"""
    
    original_title = product['title']
    
    print(f"[翻譯] 正在翻譯: {original_title[:30]}...")
    translated = translate_with_chatgpt(original_title, product.get('description', ''))
    
    if translated['success']:
        print(f"[翻譯成功] {translated['title'][:30]}...")
    else:
        print(f"[翻譯失敗] 使用原文")
    
    cost = product['price']
    weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    
    print(f"[價格計算] 進貨價: ¥{cost}, 重量: {weight}kg, 售價: ¥{selling_price}")
    
    # 下載圖片
    images_base64 = []
    img_urls = product.get('images', [])
    print(f"[圖片] 開始下載 {len(img_urls)} 張圖片...")
    
    for idx, img_url in enumerate(img_urls):
        if not img_url or not img_url.startswith('http'):
            continue
        
        print(f"[圖片] 下載中 ({idx+1}/{len(img_urls)}): {img_url[:60]}...")
        result = download_image_to_base64(img_url)
        
        if result['success']:
            images_base64.append({
                'attachment': result['base64'],
                'position': idx + 1,
                'filename': f"cocoris_{product['sku']}_{idx+1}.jpg"
            })
            print(f"[圖片] ✓ 下載成功 ({idx+1}/{len(img_urls)})")
        else:
            print(f"[圖片] ✗ 下載失敗 ({idx+1}/{len(img_urls)})")
        
        time.sleep(0.5)
    
    print(f"[圖片] 成功下載 {len(images_base64)}/{len(img_urls)} 張圖片")
    
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': 'Cocoris',
            'product_type': '烘焙甜點',
            'status': 'active',
            'published': True,
            'variants': [{
                'sku': product['sku'],
                'price': f"{selling_price:.2f}",
                'weight': product.get('weight', 0),
                'weight_unit': 'kg',
                'inventory_management': None,
                'inventory_policy': 'continue',
                'requires_shipping': True
            }],
            'images': images_base64,
            'tags': 'Cocoris, 日本, 烘焙甜點, 伴手禮, 日本代購, 送禮',
            'metafields_global_title_tag': translated['page_title'],
            'metafields_global_description_tag': translated['meta_description'],
            'metafields': [
                {
                    'namespace': 'custom',
                    'key': 'link',
                    'value': product['url'],
                    'type': 'url'
                }
            ]
        }
    }
    
    response = requests.post(
        shopify_api_url('products.json'),
        headers=get_shopify_headers(),
        json=shopify_product
    )
    
    print(f"[DEBUG] Shopify 回應: {response.status_code}")
    
    if response.status_code == 201:
        created_product = response.json()['product']
        product_id = created_product['id']
        variant_id = created_product['variants'][0]['id']
        
        created_images = created_product.get('images', [])
        print(f"[DEBUG] 商品建立成功: ID={product_id}")
        print(f"[DEBUG] Shopify 實際建立圖片: {len(created_images)}/{len(images_base64)} 張")
        
        # 更新成本價
        update_cost_response = requests.put(
            shopify_api_url(f'variants/{variant_id}.json'),
            headers=get_shopify_headers(),
            json={
                'variant': {
                    'id': variant_id,
                    'cost': f"{cost:.2f}"
                }
            }
        )
        print(f"[DEBUG] 更新 Cost 回應: {update_cost_response.status_code}")
        
        if collection_id:
            add_product_to_collection(product_id, collection_id)
        
        publish_to_all_channels(product_id)
        
        return {'success': True, 'product': created_product, 'translated': translated, 'selling_price': selling_price, 'cost': cost}
    else:
        print(f"[ERROR] Shopify 錯誤: {response.text}")
        return {'success': False, 'error': response.text}


# ========== Flask 路由 ==========

@app.route('/')
def index():
    """首頁"""
    token_loaded = load_shopify_token()
    token_status = '<span style="color: green;">✓ 已載入</span>' if token_loaded else '<span style="color: red;">✗ 未設定</span>'
    
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cocoris 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #8B4513; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #8B4513; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #A0522D; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #8B4513, #D2691E); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 100px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #8B4513; }}
        .stat-label {{ font-size: 12px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>🍪 Cocoris 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 sucreyshopping.jp Cocoris 品牌商品並上架到 Shopify</p>
        <p style="color: #666; font-size: 14px;">※ 成本價低於 ¥1000 的商品將自動跳過</p>
        <button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
        
        <div id="progressSection" style="display: none;">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill" style="width: 0%"></div>
            </div>
            <div class="status" id="statusText">準備中...</div>
            
            <div class="stats">
                <div class="stat">
                    <div class="stat-number" id="uploadedCount">0</div>
                    <div class="stat-label">已上架</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="skippedCount">0</div>
                    <div class="stat-label">已跳過</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="filteredCount">0</div>
                    <div class="stat-label">價格過濾</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="deletedCount" style="color: #e67e22;">0</div>
                    <div class="stat-label">設為草稿</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="errorCount">0</div>
                    <div class="stat-label">錯誤</div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="card">
        <h3>執行日誌</h3>
        <div class="log" id="logArea">等待開始...</div>
    </div>

    <script>
        let pollInterval = null;
        function log(msg, type = '') {{
            const logArea = document.getElementById('logArea');
            const time = new Date().toLocaleTimeString();
            const color = type === 'success' ? '#4ec9b0' : type === 'error' ? '#f14c4c' : '#d4d4d4';
            logArea.innerHTML += '<div style="color:' + color + '">[' + time + '] ' + msg + '</div>';
            logArea.scrollTop = logArea.scrollHeight;
        }}
        function clearLog() {{ document.getElementById('logArea').innerHTML = ''; }}
        async function testShopify() {{
            log('測試 Shopify 連線...');
            try {{
                const res = await fetch('/api/test-shopify');
                const data = await res.json();
                if (data.success) log('✓ 連線成功！', 'success');
                else log('✗ 連線失敗: ' + data.error, 'error');
            }} catch (e) {{ log('✗ 請求失敗: ' + e.message, 'error'); }}
        }}
        async function startScrape() {{
            clearLog(); log('開始爬取流程...');
            document.getElementById('startBtn').disabled = true;
            document.getElementById('progressSection').style.display = 'block';
            try {{
                const res = await fetch('/api/start-scrape', {{ method: 'POST' }});
                const data = await res.json();
                if (!data.success) {{ log('✗ ' + data.error, 'error'); document.getElementById('startBtn').disabled = false; return; }}
                log('✓ 爬取任務已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            }} catch (e) {{ log('✗ ' + e.message, 'error'); document.getElementById('startBtn').disabled = false; }}
        }}
        async function pollStatus() {{
            try {{
                const res = await fetch('/api/status');
                const data = await res.json();
                const percent = data.total > 0 ? (data.progress / data.total * 100) : 0;
                document.getElementById('progressFill').style.width = percent + '%';
                document.getElementById('statusText').textContent = data.current_product + ' (' + data.progress + '/' + data.total + ')';
                document.getElementById('uploadedCount').textContent = data.uploaded;
                document.getElementById('skippedCount').textContent = data.skipped;
                document.getElementById('filteredCount').textContent = data.filtered_by_price || 0;
                document.getElementById('deletedCount').textContent = data.deleted || 0;
                document.getElementById('errorCount').textContent = data.errors.length;
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval);
                    document.getElementById('startBtn').disabled = false;
                    log('========== 爬取完成 ==========', 'success');
                }}
            }} catch (e) {{ console.error(e); }}
        }}
    </script>
</body>
</html>'''


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/start-scrape', methods=['POST'])
def start_scrape():
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '找不到 shopify_token.json'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'success': True, 'message': '開始爬取'})


@app.route('/api/start', methods=['GET', 'POST'])
def api_start():
    """供 cron-job.org 外部觸發的 API"""
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'success': True, 'message': 'Cocoris 爬蟲已啟動'})


def run_scrape():
    """執行爬取流程"""
    global scrape_status
    
    try:
        scrape_status = {
            "running": True,
            "progress": 0,
            "total": 0,
            "current_product": "",
            "products": [],
            "errors": [],
            "uploaded": 0,
            "skipped": 0,
            "skipped_exists": 0,
            "filtered_by_price": 0,
            "deleted": 0
        }
        
        # 1. 取得或建立 Collection
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("Cocoris")
        print(f"[INFO] Collection ID: {collection_id}")
        
        # 2. 取得 Collection 內的商品
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        existing_skus = set(collection_products_map.keys())
        print(f"[INFO] Cocoris Collection 內有 {len(existing_skus)} 個商品")
        
        # 3. 爬取商品列表
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        website_skus = set(item['sku'] for item in product_list)
        print(f"[INFO] 官網 SKU 列表: {len(website_skus)} 個")
        
        # 4. 逐一處理商品
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理中: {item['sku']}"
            
            if item['sku'] in existing_skus:
                print(f"[跳過] 已存在: {item['sku']}")
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                continue
            
            print(f"[爬取] ({idx+1}/{len(product_list)}) {item['url']}")
            product = scrape_product_detail(item['url'])
            
            if not product.get('in_stock', True):
                print(f"[跳過] 無庫存: {product.get('title', item['sku'])}")
                scrape_status['skipped'] += 1
                continue
            
            if product.get('is_point_product', False):
                print(f"[跳過] 點數商品: {product.get('title', item['sku'])}")
                scrape_status['skipped'] += 1
                continue
            
            if product.get('price', 0) < MIN_PRICE:
                print(f"[跳過] 價格低於{MIN_PRICE}円: {product.get('title', item['sku'])} (¥{product.get('price', 0)})")
                scrape_status['filtered_by_price'] += 1
                scrape_status['skipped'] += 1
                continue
            
            if not product.get('title') or not product.get('price'):
                print(f"[跳過] 資訊不完整: {item['sku']}")
                scrape_status['errors'].append({'sku': item['sku'], 'error': '資訊不完整'})
                continue
            
            result = upload_to_shopify(product, collection_id)
            
            if result['success']:
                translated_title = result.get('translated', {}).get('title', product['title'])
                print(f"[成功] {translated_title}")
                existing_skus.add(product['sku'])  # 防止同一批次重複上架
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'sku': product['sku'],
                    'title': translated_title,
                    'original_title': product['title'],
                    'price': product['price'],
                    'selling_price': result.get('selling_price', 0),
                    'weight': product['weight'],
                    'status': 'success'
                })
            else:
                print(f"[失敗] {product['title']}: {result['error']}")
                scrape_status['errors'].append({
                    'sku': product['sku'],
                    'title': product['title'],
                    'error': result['error']
                })
                scrape_status['products'].append({
                    'sku': product['sku'],
                    'title': product['title'],
                    'status': 'failed',
                    'error': result['error']
                })
            
            time.sleep(1)
        
        # 5. 設為草稿
        scrape_status['current_product'] = "正在檢查已下架商品..."
        skus_to_draft = existing_skus - website_skus
        
        if skus_to_draft:
            print(f"[INFO] 發現 {len(skus_to_draft)} 個商品需要設為草稿")
            for sku in skus_to_draft:
                scrape_status['current_product'] = f"設為草稿: {sku}"
                product_id = collection_products_map.get(sku)
                if product_id and set_product_to_draft(product_id):
                    scrape_status['deleted'] += 1
                time.sleep(0.5)
        else:
            print(f"[INFO] 沒有需要設為草稿的商品")
        
        scrape_status['current_product'] = "完成！"
        
    except Exception as e:
        print(f"[ERROR] 爬取過程發生錯誤: {e}")
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '找不到 shopify_token.json'})
    
    response = requests.get(
        shopify_api_url('shop.json'),
        headers=get_shopify_headers()
    )
    
    if response.status_code == 200:
        return jsonify({'success': True, 'shop': response.json()['shop']})
    else:
        return jsonify({'success': False, 'error': response.text}), 400


@app.route('/api/test-scrape')
def test_scrape():
    """測試爬取單一商品"""
    test_url = "https://sucreyshopping.jp/shop/g/gcc03101/"
    product = scrape_product_detail(test_url)
    
    if product.get('price') and product.get('weight'):
        product['selling_price'] = calculate_selling_price(product['price'], product['weight'])
    
    return jsonify(product)


if __name__ == '__main__':
    print("=" * 50)
    print("Cocoris 爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

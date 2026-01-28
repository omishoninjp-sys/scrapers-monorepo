"""
本高砂屋商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 hontaka-shop.com 全站商品
2. 計算材積重量 vs 實際重量，取大值
3. 上架到 Shopify（不重複上架）
4. 原價寫入成本價（Cost）
5. OpenAI 翻譯成繁體中文
"""

from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
from urllib.parse import urljoin
import math
import threading
import base64

app = Flask(__name__)

# ========== 設定 ==========
SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""

BASE_URL = "https://www.hontaka-shop.com"
LIST_BASE_URL = "https://www.hontaka-shop.com/shopbrand/all_items/"
LIST_PAGE_URL_TEMPLATE = "https://www.hontaka-shop.com/shopbrand/all_items/page{page}/order/"

# OpenAI API 設定
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# 最低價格門檻
MIN_PRICE = 1000

# 請求 Headers
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7',
    'Accept-Charset': 'EUC-JP,utf-8;q=0.7,*;q=0.3',
    'Connection': 'keep-alive',
}

# 全域變數存儲爬取狀態
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
    "out_of_stock": 0,
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
    """取得 Shopify API Headers"""
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }

def shopify_api_url(endpoint):
    """建立 Shopify API URL"""
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"

def calculate_selling_price(cost, weight):
    """
    計算售價
    公式：[進貨價 + (重量 * 1250)] / 0.7 = 售價
    """
    if not cost or cost <= 0:
        return 0
    
    shipping_cost = weight * 1250 if weight else 0
    price = (cost + shipping_cost) / 0.7
    
    return round(price)

def clean_html_for_translation(html_text):
    """清除 HTML 中的 CSS、script 和多餘標籤，只保留純文字"""
    if not html_text:
        return ""
    
    text = html_text
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'#[\w-]+\s*\{[^}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\.[\w-]+\s*\{[^}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\s*style\s*=\s*["\'][^"\']*["\']', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    
    return text.strip()

def translate_with_chatgpt(title, description):
    """
    使用 ChatGPT 翻譯商品名稱和說明，並生成 SEO 內容
    """
    clean_description = clean_html_for_translation(description)
    
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本甜點商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{clean_description[:1500]}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，簡潔有力，前面加上「本高砂屋」）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示）",
    "page_title": "SEO 頁面標題（繁體中文，包含本高砂屋品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

重要規則：
1. 這是日本本高砂屋的高級洋菓子（エコルセ薄餅、マンデルチーゲル杏仁餅等）
2. エコルセ 是招牌商品，可翻譯為「薄餅捲」或「蛋捲」
3. マンデルチーゲル 可翻譯為「杏仁瓦片餅」
4. 翻譯要自然流暢，不要生硬
5. 商品標題開頭必須是「本高砂屋」
6. 【禁止使用任何日文】所有內容必須是繁體中文或英文
7. SEO 內容要包含：本高砂屋、神戶、日本、伴手禮、送禮等關鍵字
8. 只回傳 JSON，不要其他文字"""

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
            if not trans_title.startswith('本高砂屋'):
                trans_title = f"本高砂屋 {trans_title}"
            
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
                'title': f"本高砂屋 {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"本高砂屋 {title}",
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
        'Referer': 'https://www.hontaka-shop.com/',
        'Connection': 'keep-alive',
    }
    
    # 針對 akamaized.net CDN 調整 headers
    if 'akamaized.net' in img_url:
        headers['Referer'] = 'https://www.hontaka-shop.com/'
    
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
    """取得 Shopify 已存在的商品，回傳 {sku: product_id} 字典"""
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
            if match:
                url = match.group(1)
            else:
                url = None
        else:
            url = None
    
    return products_map

def get_collection_products_map(collection_id):
    """只取得特定 Collection 內的商品"""
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
            if match:
                url = match.group(1)
            else:
                url = None
        else:
            url = None
    
    print(f"[INFO] Collection 內有 {len(products_map)} 個商品")
    return products_map

def set_product_to_draft(product_id):
    """將 Shopify 商品設為草稿"""
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {"id": product_id, "status": "draft"}
    })
    if response.status_code == 200:
        print(f"[設為草稿] Product ID: {product_id}")
        return True
    return False

def get_or_create_collection(collection_title="本高砂屋"):
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

def parse_dimension_weight(text):
    """
    解析尺寸和重量
    尺寸格式: 248×248 ×121 mm 或 248× 248× 121 mm
    重量格式: 1477ｇ 或 1477g
    """
    result = {
        'dimension': None,
        'actual_weight': None,
        'volume_weight': 0,
        'final_weight': 0
    }
    
    # 標準化文字
    text = text.replace('×', 'x').replace('Ｘ', 'x').replace('ｘ', 'x')
    text = text.replace('ｍｍ', 'mm').replace('ｇ', 'g').replace('ｋｇ', 'kg')
    text = text.replace(',', '').replace('，', '')
    text = re.sub(r'\s+', ' ', text)
    
    # 解析尺寸 - 格式: 248x248 x121 mm 或 248x 248x 121 mm
    dim_pattern = r'(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]?\s*(\d+(?:\.\d+)?)\s*mm'
    dim_match = re.search(dim_pattern, text, re.IGNORECASE)
    
    if dim_match:
        l, w, h = float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))
        # 材積重量 = (長 × 寬 × 高) / 6000000 (mm³ 轉 kg)
        volume_weight = (l * w * h) / 6000000
        volume_weight = round(volume_weight, 2)
        result['dimension'] = {'length': l, 'width': w, 'height': h}
        result['volume_weight'] = volume_weight
        print(f"[尺寸] {l}x{w}x{h}mm -> 材積重量: {volume_weight}kg")
    
    # 解析重量 - 格式: 1477g 或 1.5kg
    weight_pattern = r'重量[：:\s]*(\d+(?:\.\d+)?)\s*(g|kg|ｇ|ｋｇ)'
    weight_match = re.search(weight_pattern, text, re.IGNORECASE)
    
    if weight_match:
        weight_val = float(weight_match.group(1))
        unit = weight_match.group(2).lower()
        if 'kg' in unit:
            actual_weight = weight_val
        else:
            actual_weight = weight_val / 1000
        result['actual_weight'] = round(actual_weight, 3)
        print(f"[重量] {result['actual_weight']}kg")
    
    # 計算最終重量（取較大值）
    if result['volume_weight'] and result['actual_weight']:
        result['final_weight'] = max(result['volume_weight'], result['actual_weight'])
    elif result['volume_weight']:
        result['final_weight'] = result['volume_weight']
    elif result['actual_weight']:
        result['final_weight'] = result['actual_weight']
    
    return result

def scrape_product_list():
    """爬取商品列表 - 支援多頁"""
    products = []
    page_num = 1
    max_pages = 20
    
    while page_num <= max_pages:
        if page_num == 1:
            url = LIST_BASE_URL
        else:
            url = LIST_PAGE_URL_TEMPLATE.format(page=page_num)
        
        print(f"[INFO] 正在載入第 {page_num} 頁: {url}")
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.encoding = 'euc-jp'
            
            if response.status_code != 200:
                print(f"[ERROR] 載入頁面失敗: HTTP {response.status_code}")
                break
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 尋找商品連結: /shopdetail/{12位數字}/
            product_links = soup.find_all('a', href=re.compile(r'/shopdetail/\d{12}/'))
            
            if not product_links:
                print(f"[INFO] 第 {page_num} 頁沒有找到商品，停止")
                break
            
            seen_skus = set()
            page_products = []
            
            for link in product_links:
                href = link.get('href', '')
                if not href or '/shopdetail/' not in href:
                    continue
                
                # 提取 SKU: /shopdetail/{12-digit}/
                sku_match = re.search(r'/shopdetail/(\d{12})/', href)
                if not sku_match:
                    continue
                
                sku = sku_match.group(1)
                
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)
                
                # 構建完整 URL（不含 all_items 路徑）
                full_url = f"{BASE_URL}/shopdetail/{sku}/"
                page_products.append({
                    'url': full_url,
                    'sku': sku
                })
            
            if not page_products:
                print(f"[INFO] 第 {page_num} 頁沒有新商品，停止")
                break
            
            print(f"[INFO] 第 {page_num} 頁找到 {len(page_products)} 個商品")
            products.extend(page_products)
            
            page_num += 1
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[ERROR] 載入頁面失敗: {e}")
            import traceback
            traceback.print_exc()
            break
    
    # 去重
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
        'weight': 0,
        'images': [],
        'in_stock': True,
        'sku': '',
        'product_code': '',
        'content': '',
        'allergens': '',
        'shelf_life': '',
        'size_text': '',
        'weight_text': ''
    }
    
    sku_match = re.search(r'/shopdetail/(\d{12})/', url)
    if sku_match:
        product['sku'] = sku_match.group(1)
    
    try:
        print(f"[載入] {url}")
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.encoding = 'euc-jp'
        
        if response.status_code != 200:
            print(f"[ERROR] 載入頁面失敗: HTTP {response.status_code}")
            return product
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # ========== 商品名稱 ==========
        # 從頁面標題或 h1 取得
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            # 格式: エコルセ E50 〔33050〕-本高砂屋
            title_parts = title_text.split('-')
            if title_parts:
                product['title'] = title_parts[0].strip()
        
        # 備用：從 meta 或 h2 取得
        if not product['title']:
            h2 = soup.find('h2')
            if h2:
                product['title'] = h2.get_text(strip=True)
        
        print(f"[標題] {product['title']}")
        
        # 提取商品編碼 (〔33050〕 格式)
        code_match = re.search(r'〔(\d+)〕', product['title'])
        if code_match:
            product['product_code'] = code_match.group(1)
            print(f"[商品編碼] {product['product_code']}")
        
        # ========== 價格 ==========
        # 優先從 hidden input 取得價格: <input type="hidden" name="price1" value="1188">
        price_input = soup.find('input', {'name': 'price1'})
        if price_input and price_input.get('value'):
            try:
                product['price'] = int(price_input.get('value').replace(',', ''))
            except:
                pass
        
        # 備用：從 M_price2 input 取得
        if not product['price']:
            price_input2 = soup.find('input', {'id': 'M_price2'})
            if price_input2 and price_input2.get('value'):
                try:
                    product['price'] = int(price_input2.get('value').replace(',', ''))
                except:
                    pass
        
        # 再備用：從頁面文字抓取
        if not product['price']:
            price_pattern = r'(\d{1,3}(?:,\d{3})*)\s*円'
            price_matches = re.findall(price_pattern, page_text)
            if price_matches:
                for pm in price_matches:
                    try:
                        price_val = int(pm.replace(',', ''))
                        if price_val >= 100:
                            product['price'] = price_val
                            break
                    except:
                        pass
        
        print(f"[價格] ¥{product['price']}")
        
        # ========== 庫存狀態 ==========
        if '売切れ' in page_text or '在庫なし' in page_text or 'SOLD OUT' in page_text:
            product['in_stock'] = False
            print(f"[庫存] 無庫存")
        
        # ========== 商品說明 ==========
        desc_parts = []
        
        # 尋找商品說明區塊 - 從頁面結構中提取
        # 格式通常是: 名称：焼菓子 --- 商品説明 ---
        desc_match = re.search(r'商品[説說]明[：:]\s*(.+?)(?=---|\n\n|内容量|賞味期限)', page_text, re.DOTALL)
        if desc_match:
            desc_parts.append(desc_match.group(1).strip())
        
        # 內容量
        content_match = re.search(r'内容量[：:]\s*(.+?)(?=---|\n|賞味期限|特定原材料)', page_text)
        if content_match:
            product['content'] = content_match.group(1).strip()
            desc_parts.append(f"內容量：{product['content']}")
        
        # 賞味期限
        shelf_match = re.search(r'賞味期限[：:]\s*(\d+日?)', page_text)
        if shelf_match:
            product['shelf_life'] = shelf_match.group(1)
            desc_parts.append(f"賞味期限：{product['shelf_life']}")
        
        # 過敏原
        allergen_match = re.search(r'特定原材料等?\d*品目[：:]\s*(.+?)(?=---|原材料名|\n\n)', page_text)
        if allergen_match:
            product['allergens'] = allergen_match.group(1).strip()
        
        # 尺寸
        size_match = re.search(r'サイズ[：:]\s*(.+?)(?=---|重量|\n)', page_text)
        if size_match:
            product['size_text'] = size_match.group(1).strip()
        
        # 重量
        weight_match = re.search(r'重量[：:]\s*(.+?)(?=---|保存|\n)', page_text)
        if weight_match:
            product['weight_text'] = weight_match.group(1).strip()
        
        product['description'] = '\n\n'.join(desc_parts) if desc_parts else ''
        
        # ========== 計算重量 ==========
        combined_text = f"サイズ：{product['size_text']} 重量：{product['weight_text']}"
        weight_info = parse_dimension_weight(combined_text)
        product['weight'] = weight_info['final_weight']
        print(f"[最終重量] {product['weight']}kg")
        
        # ========== 圖片 ==========
        images = []
        seen_images = set()
        
        # 從 makeshop-multi-images.akamaized.net 找圖片
        # 商品主圖格式: https://makeshop-multi-images.akamaized.net/shophontaka/shopimages/44/01/9_000000000144.jpg
        # 關聯商品格式: https://makeshop-multi-images.akamaized.net/shophontaka/itemimages/... (要排除)
        img_tags = soup.find_all('img', src=re.compile(r'makeshop-multi-images\.akamaized\.net'))
        for img in img_tags:
            src = img.get('src', '')
            if src and 'shophontaka' in src:
                # 排除關聯商品的圖片（itemimages 路徑）
                if '/itemimages/' in src:
                    continue
                
                # 只抓 shopimages 路徑的圖片
                if '/shopimages/' not in src:
                    continue
                
                # 過濾掉縮圖（以 s 開頭的檔名）
                # 縮圖格式: s9_000000000144.jpg
                # 主圖格式: 9_000000000144.jpg
                filename = src.split('/')[-1].split('?')[0]
                if filename.startswith('s') and filename[1].isdigit():
                    continue
                
                # 移除 query string 來去重
                clean_src = src.split('?')[0]
                if clean_src not in seen_images:
                    seen_images.add(clean_src)
                    images.append(src)
        
        # 備用：從整個 HTML 找圖片 URL（只找 shopimages）
        if not images:
            script_text = str(soup)
            img_pattern = r'(https://makeshop-multi-images\.akamaized\.net/shophontaka/shopimages/[^"\']+\.(?:jpg|jpeg|png|gif))'
            found_images = re.findall(img_pattern, script_text, re.IGNORECASE)
            for img_url in found_images:
                filename = img_url.split('/')[-1].split('?')[0]
                # 過濾縮圖
                if filename.startswith('s') and filename[1].isdigit():
                    continue
                clean_url = img_url.split('?')[0]
                if clean_url not in seen_images:
                    seen_images.add(clean_url)
                    images.append(img_url)
        
        product['images'] = images[:10]
        print(f"[圖片] 找到 {len(product['images'])} 張圖片")
        
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
    
    # 下載圖片並轉換為 Base64
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
                'filename': f"hontaka_{product['sku']}_{idx+1}.jpg"
            })
            print(f"[圖片] ✓ 下載成功 ({idx+1}/{len(img_urls)})")
        else:
            print(f"[圖片] ✗ 下載失敗 ({idx+1}/{len(img_urls)})")
        
        time.sleep(0.5)
    
    print(f"[圖片] 成功下載 {len(images_base64)}/{len(img_urls)} 張圖片")
    
    # 使用商品編碼作為 SKU（如果有的話）
    sku = product.get('product_code') or product['sku']
    
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': '本高砂屋',
            'product_type': '西式甜點',
            'status': 'active',
            'published': True,
            'variants': [{
                'sku': sku,
                'price': f"{selling_price:.2f}",
                'weight': product.get('weight', 0),
                'weight_unit': 'kg',
                'inventory_management': None,
                'inventory_policy': 'continue',
                'requires_shipping': True
            }],
            'images': images_base64,
            'tags': '本高砂屋, 日本, 神戶, 西式甜點, 伴手禮, 日本代購, 送禮, エコルセ, 薄餅',
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
    <title>本高砂屋 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #8B4513; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #8B4513; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #A0522D; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-secondary:hover {{ background: #2980b9; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #8B4513, #D2691E); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 80px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #8B4513; }}
        .stat-label {{ font-size: 12px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>🍪 本高砂屋 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 hontaka-shop.com 全站商品並上架到 Shopify</p>
        <p style="color: #666; font-size: 14px;">※ 成本價低於 ¥{MIN_PRICE} 的商品將自動跳過</p>
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
                    <div class="stat-label">已存在</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="filteredCount">0</div>
                    <div class="stat-label">價格過濾</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="outOfStockCount">0</div>
                    <div class="stat-label">無庫存</div>
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
                if (data.success) log('✓ 連線成功！商店: ' + data.shop.name, 'success');
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
                document.getElementById('skippedCount').textContent = data.skipped_exists || 0;
                document.getElementById('filteredCount').textContent = data.filtered_by_price || 0;
                document.getElementById('outOfStockCount').textContent = data.out_of_stock || 0;
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
    """取得爬取狀態"""
    return jsonify(scrape_status)

@app.route('/api/start-scrape', methods=['POST'])
def start_scrape():
    """開始爬取"""
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '找不到 Shopify 設定'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'success': True, 'message': '開始爬取'})

@app.route('/api/start', methods=['POST'])
def api_start():
    """Cron-job 觸發端點"""
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '找不到 Shopify 設定'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'success': True, 'message': '本高砂屋 爬蟲已啟動'})

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
            "out_of_stock": 0,
            "deleted": 0
        }
        
        # 1. 取得或建立 Collection
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("本高砂屋")
        print(f"[INFO] Collection ID: {collection_id}")
        
        # 2. 取得 Collection 內的商品
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        existing_skus = set(collection_products_map.keys())
        print(f"[INFO] 本高砂屋 Collection 內有 {len(existing_skus)} 個商品")
        
        # 3. 爬取商品列表
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        # 取得官網所有 SKU
        website_skus = set()
        
        # 4. 逐一處理商品
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理中: {item['sku']}"
            
            # 爬取詳細資訊
            print(f"[爬取] ({idx+1}/{len(product_list)}) {item['url']}")
            product = scrape_product_detail(item['url'])
            
            # 使用商品編碼作為 SKU
            actual_sku = product.get('product_code') or product['sku']
            website_skus.add(actual_sku)
            
            # 檢查庫存
            if not product.get('in_stock', True):
                print(f"[跳過] 無庫存: {product.get('title', item['sku'])}")
                scrape_status['out_of_stock'] += 1
                continue
            
            # 檢查價格門檻
            if product.get('price', 0) < MIN_PRICE:
                print(f"[跳過] 價格低於{MIN_PRICE}円: {product.get('title', item['sku'])} (¥{product.get('price', 0)})")
                scrape_status['filtered_by_price'] += 1
                continue
            
            # 檢查是否已存在
            if actual_sku in existing_skus:
                print(f"[跳過] 已存在: {actual_sku}")
                scrape_status['skipped_exists'] += 1
                continue
            
            # 檢查必要資訊
            if not product.get('title') or not product.get('price'):
                print(f"[跳過] 資訊不完整: {item['sku']}")
                scrape_status['errors'].append({
                    'sku': item['sku'],
                    'error': '資訊不完整'
                })
                continue
            
            # 上傳到 Shopify
            result = upload_to_shopify(product, collection_id)
            
            if result['success']:
                translated_title = result.get('translated', {}).get('title', product['title'])
                print(f"[成功] {translated_title}")
                existing_skus.add(actual_sku)
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'sku': actual_sku,
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
                    'sku': actual_sku,
                    'title': product['title'],
                    'error': result['error']
                })
            
            time.sleep(1)
        
        # 5. 設為草稿：Collection 內但官網已下架的商品
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
        import traceback
        traceback.print_exc()
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False

@app.route('/api/test-shopify')
def test_shopify():
    """測試 Shopify 連線"""
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '找不到 Shopify 設定'})
    
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
    test_url = "https://www.hontaka-shop.com/shopdetail/000000000006/"
    product = scrape_product_detail(test_url)
    
    if product.get('price') and product.get('weight'):
        product['selling_price'] = calculate_selling_price(product['price'], product['weight'])
    
    return jsonify(product)

if __name__ == '__main__':
    print("=" * 50)
    print("本高砂屋 爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

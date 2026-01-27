"""
ガトーフェスタ ハラダ (Gateau Festa Harada) 商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 shop.gateaufesta-harada.com 所有分類商品
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

BASE_URL = "https://shop.gateaufesta-harada.com"

# 所有分類頁面
CATEGORY_PATHS = [
    "/shop/c/croi/",
    "/shop/c/creine/",
    "/shop/c/ccacao/",
    "/shop/c/cleger/",
    "/shop/c/cwhite/",
    "/shop/c/cpremium/",
    "/shop/c/cex-pr/",
    "/shop/c/csoleil/",
    "/shop/c/cpr-ve/",
    "/shop/c/cpr-wz/",
    "/shop/c/crtb/",
    "/shop/c/crhw/",
    "/shop/c/csommelie/",
    "/shop/c/cmh/",
    "/shop/c/cgrt/",
    "/shop/c/cfromage/",
    "/shop/c/citalien/",
]

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
    if not cost or cost <= 0:
        return 0
    shipping_cost = weight * 1250 if weight else 0
    price = (cost + shipping_cost) / 0.7
    return round(price)


def translate_with_chatgpt(title, description):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本甜點商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{description[:1500] if description else ''}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，簡潔有力，前面加上 Gateau Festa Harada）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示，每個重點資訊用 <br> 換行）",
    "page_title": "SEO 頁面標題（繁體中文，包含品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

重要規則：
1. 這是日本 Gateau Festa Harada 的高級法式脆餅（ラスク）
2. グーテ・デ・ロワ 是招牌產品名，可翻譯為「王室脆餅」或保留原名
3. 翻譯要自然流暢，不要生硬
4. 商品標題開頭必須是「Gateau Festa Harada」（英文）
5. 【禁止使用任何日文】所有內容必須是繁體中文或英文
6. SEO 內容要包含：Gateau Festa Harada、日本、法式脆餅、伴手禮、送禮等關鍵字
7. description 中每個重點（內容量、賞味期限、尺寸、重量等）要用 <br> 換行，方便閱讀
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
            if not trans_title.startswith('Gateau Festa Harada'):
                trans_title = f"Gateau Festa Harada {trans_title}"
            
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
                'title': f"Gateau Festa Harada {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"Gateau Festa Harada {title}",
            'description': description,
            'page_title': '',
            'meta_description': ''
        }


def download_image_to_base64(img_url, max_retries=3):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
        'Referer': BASE_URL + '/',
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
                elif 'gif' in content_type:
                    img_format = 'image/gif'
                else:
                    img_format = 'image/jpeg'
                
                img_base64 = base64.b64encode(response.content).decode('utf-8')
                return {'success': True, 'base64': img_base64, 'content_type': img_format}
            else:
                print(f"[圖片下載] 第 {attempt+1} 次嘗試失敗: HTTP {response.status_code}")
        except Exception as e:
            print(f"[圖片下載] 第 {attempt+1} 次嘗試異常: {e}")
        time.sleep(1)
    
    return {'success': False}


def get_existing_products_map():
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
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {"id": product_id, "status": "draft"}
    })
    if response.status_code == 200:
        print(f"[設為草稿] Product ID: {product_id}")
        return True
    return False


def get_or_create_collection(collection_title="Gateau Festa Harada"):
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
        json={'custom_collection': {'title': collection_title, 'published': True}}
    )
    
    if response.status_code == 201:
        collection_id = response.json()['custom_collection']['id']
        print(f"[INFO] 建立新 Collection: {collection_title} (ID: {collection_id})")
        return collection_id
    
    print(f"[ERROR] 無法建立 Collection: {response.text}")
    return None


def add_product_to_collection(product_id, collection_id):
    response = requests.post(
        shopify_api_url('collects.json'),
        headers=get_shopify_headers(),
        json={'collect': {'product_id': product_id, 'collection_id': collection_id}}
    )
    return response.status_code == 201


def publish_to_all_channels(product_id):
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
          availablePublicationsCount { count }
        }
        userErrors { field message }
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
    
    return pub_response.status_code == 200


def parse_size_cm(size_text):
    if not size_text:
        return None
    
    # 格式：タテ23.8×ヨコ23.8×高さ14.7cm
    pattern = r'タテ\s*(\d+(?:\.\d+)?)\s*[×xX]\s*ヨコ\s*(\d+(?:\.\d+)?)\s*[×xX]\s*高さ\s*(\d+(?:\.\d+)?)\s*cm'
    match = re.search(pattern, size_text)
    
    if match:
        h, w, d = float(match.group(1)), float(match.group(2)), float(match.group(3))
        volume_weight = (h * w * d) / 6000
        volume_weight = round(volume_weight, 2)
        print(f"[尺寸解析] {h}x{w}x{d}cm -> 材積重量: {volume_weight}kg")
        return {"height": h, "width": w, "depth": d, "volume_weight": volume_weight}
    
    # 備用格式
    simple_pattern = r'(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)'
    simple_match = re.search(simple_pattern, size_text)
    
    if simple_match:
        a, b, c = float(simple_match.group(1)), float(simple_match.group(2)), float(simple_match.group(3))
        volume_weight = (a * b * c) / 6000
        volume_weight = round(volume_weight, 2)
        print(f"[尺寸解析] {a}x{b}x{c}cm -> 材積重量: {volume_weight}kg")
        return {"volume_weight": volume_weight}
    
    return None


def parse_weight(weight_text):
    if not weight_text:
        return 0
    
    kg_match = re.search(r'(\d+(?:\.\d+)?)\s*kg', weight_text, re.IGNORECASE)
    if kg_match:
        return float(kg_match.group(1))
    
    g_match = re.search(r'(\d+(?:\.\d+)?)\s*g', weight_text, re.IGNORECASE)
    if g_match:
        return float(g_match.group(1)) / 1000
    
    return 0


def scrape_product_list():
    products = []
    seen_skus = set()
    
    for category_path in CATEGORY_PATHS:
        url = BASE_URL + category_path
        print(f"[INFO] 正在爬取分類: {url}")
        
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            
            if response.status_code != 200:
                print(f"[ERROR] 載入頁面失敗: HTTP {response.status_code}")
                continue
            
            soup = BeautifulSoup(response.text, 'html.parser')
            product_blocks = soup.find_all('div', class_='block-goods-list-d--item-body')
            
            print(f"[INFO] 找到 {len(product_blocks)} 個商品區塊")
            
            for block in product_blocks:
                try:
                    # SKU
                    spec_goods = block.find('div', class_='block-goods-list-d--spec_goods')
                    sku = ''
                    if spec_goods:
                        sku_match = re.search(r'品番\s*[：:]\s*(\S+)', spec_goods.get_text())
                        if sku_match:
                            sku = sku_match.group(1)
                    
                    if not sku or sku in seen_skus:
                        continue
                    seen_skus.add(sku)
                    
                    # 商品名稱
                    title = ''
                    name_link = block.find('a', class_='js-enhanced-ecommerce-goods-name')
                    if name_link:
                        title = name_link.get_text(strip=True)
                    
                    # 價格
                    price = 0
                    price_el = block.find('div', class_='block-goods-list-d--price')
                    if price_el:
                        price_text = price_el.get_text()
                        price_match = re.search(r'[￥¥]\s*([\d,]+)', price_text)
                        if price_match:
                            price = int(price_match.group(1).replace(',', ''))
                    
                    # 商品屬性
                    shelf_life = ''
                    content = ''
                    size_text = ''
                    weight_text = ''
                    
                    attr_div = block.find('div', class_='att_')
                    if attr_div:
                        dls = attr_div.find_all('dl')
                        for dl in dls:
                            dt = dl.find('dt')
                            dd = dl.find('dd')
                            if dt and dd:
                                dt_text = dt.get_text(strip=True)
                                dd_text = dd.get_text(strip=True)
                                
                                if '賞味期間' in dt_text:
                                    shelf_life = dd_text
                                elif '内容量' in dt_text:
                                    content = dd_text
                                elif 'サイズ' in dt_text:
                                    size_text = dd_text
                                elif '重さ' in dt_text:
                                    weight_text = dd_text
                    
                    # 計算重量
                    actual_weight = parse_weight(weight_text)
                    size_info = parse_size_cm(size_text)
                    volume_weight = size_info.get('volume_weight', 0) if size_info else 0
                    final_weight = max(actual_weight, volume_weight)
                    
                    # 圖片 URL - 嘗試多種前綴
                    images = []
                    image_prefixes = ['L', '2', '3', '4', '5', '6', '7', '8']
                    for prefix in image_prefixes:
                        img_url = f"{BASE_URL}/img/goods/{prefix}/{sku}.jpg"
                        try:
                            head_resp = requests.head(img_url, headers=HEADERS, timeout=5)
                            if head_resp.status_code == 200:
                                images.append(img_url)
                        except:
                            pass
                    
                    # 如果沒找到，至少加入 L 圖
                    if not images:
                        images.append(f"{BASE_URL}/img/goods/L/{sku}.jpg")
                    
                    # 商品頁 URL
                    product_url = f"{BASE_URL}/shop/g/g{sku}/"
                    
                    # 組合描述（使用 HTML 換行）
                    description_parts = []
                    if content:
                        description_parts.append(f"內容量：{content}")
                    if shelf_life:
                        description_parts.append(f"賞味期間：{shelf_life}")
                    if size_text:
                        description_parts.append(f"尺寸：{size_text}")
                    if weight_text:
                        description_parts.append(f"重量：{weight_text}")
                    
                    product = {
                        'sku': sku,
                        'title': title,
                        'price': price,
                        'url': product_url,
                        'images': images,
                        'weight': round(final_weight, 2),
                        'actual_weight': actual_weight,
                        'volume_weight': volume_weight,
                        'description': '<br>'.join(description_parts),
                        'content': content,
                        'shelf_life': shelf_life,
                        'size_text': size_text,
                    }
                    
                    products.append(product)
                    print(f"[商品] SKU: {sku}, 價格: ¥{price}, 重量: {final_weight}kg, 圖片: {len(images)}張")
                    
                except Exception as e:
                    print(f"[ERROR] 解析商品區塊失敗: {e}")
                    continue
            
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[ERROR] 爬取分類失敗: {e}")
            continue
    
    print(f"[INFO] 共收集 {len(products)} 個不重複商品")
    return products


def upload_to_shopify(product, collection_id=None):
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
        
        print(f"[圖片] 下載中 ({idx+1}/{len(img_urls)}): {img_url}")
        result = download_image_to_base64(img_url)
        
        if result['success']:
            images_base64.append({
                'attachment': result['base64'],
                'position': idx + 1,
                'filename': f"harada_{product['sku']}_{idx+1}.jpg"
            })
            print(f"[圖片] ✓ 下載成功 ({idx+1}/{len(img_urls)})")
        else:
            print(f"[圖片] ✗ 下載失敗 ({idx+1}/{len(img_urls)})")
        
        time.sleep(0.3)
    
    print(f"[圖片] 成功下載 {len(images_base64)}/{len(img_urls)} 張圖片")
    
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': 'Gateau Festa Harada',
            'product_type': '法式脆餅',
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
            'tags': 'Gateau Festa Harada, 日本, 法式脆餅, 伴手禮, 日本代購, 送禮',
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
        
        print(f"[DEBUG] 商品建立成功: ID={product_id}")
        
        requests.put(
            shopify_api_url(f'variants/{variant_id}.json'),
            headers=get_shopify_headers(),
            json={'variant': {'id': variant_id, 'cost': f"{cost:.2f}"}}
        )
        
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
    token_loaded = load_shopify_token()
    token_status = '<span style="color: green;">✓ 已載入</span>' if token_loaded else '<span style="color: red;">✗ 未設定</span>'
    
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gateau Festa Harada 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #C9A050; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #C9A050; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #B8903F; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #C9A050, #E8C97A); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 100px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #C9A050; }}
        .stat-label {{ font-size: 12px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>🥖 Gateau Festa Harada 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 shop.gateaufesta-harada.com 所有商品並上架到 Shopify</p>
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
                const res = await fetch('/api/start', {{ method: 'POST' }});
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


@app.route('/api/start', methods=['GET', 'POST'])
def api_start():
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'success': True, 'message': 'Gateau Festa Harada 爬蟲已啟動'})


def run_scrape():
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
        
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("Gateau Festa Harada")
        print(f"[INFO] Collection ID: {collection_id}")
        
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        existing_skus = set(collection_products_map.keys())
        print(f"[INFO] Collection 內有 {len(existing_skus)} 個商品")
        
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        website_skus = set(p['sku'] for p in product_list)
        print(f"[INFO] 官網 SKU 列表: {len(website_skus)} 個")
        
        for idx, product in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理中: {product['sku']}"
            
            if product['sku'] in existing_skus:
                print(f"[跳過] 已存在: {product['sku']}")
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                continue
            
            if product.get('price', 0) < MIN_PRICE:
                print(f"[跳過] 價格低於{MIN_PRICE}円: {product['sku']} (¥{product.get('price', 0)})")
                scrape_status['filtered_by_price'] += 1
                scrape_status['skipped'] += 1
                continue
            
            if not product.get('title') or not product.get('price'):
                print(f"[跳過] 資訊不完整: {product['sku']}")
                scrape_status['errors'].append({'sku': product['sku'], 'error': '資訊不完整'})
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
            
            time.sleep(1)
        
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
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '環境變數未設定'})
    
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
    products = scrape_product_list()
    return jsonify({
        'count': len(products),
        'products': products[:5]
    })


if __name__ == '__main__':
    print("=" * 50)
    print("Gateau Festa Harada 爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

"""
Cocoris 商品爬蟲 + Shopify 上架工具 v2.3
功能：
1. 爬取 sucreyshopping.jp Cocoris 品牌所有商品
2. 計算材積重量 vs 實際重量，取大值
3. 上架到 Shopify（不重複上架）
4. 原價寫入成本價（Cost）
5. OpenAI 翻譯成繁體中文
6. 翻譯保護機制 - 翻譯失敗不上架、預檢、連續失敗自動停止
7. 日文商品掃描 - 找出並修復未翻譯的商品
8. 【v2.2】強化去重機制 - 多重 SKU 比對、handle 比對、上架前二次確認
9. 【v2.3】缺貨商品自動刪除 - 官網消失或缺貨皆直接刪除
"""

from flask import Flask, jsonify, request
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
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3

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
    "deleted": 0,
    "translation_failed": 0,
    "translation_stopped": False
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


def normalize_sku(sku):
    """★ v2.2 強化：統一 SKU 格式，去除所有可能的差異"""
    if not sku:
        return ""
    normalized = sku.strip().lower()
    normalized = re.sub(r'^cocoris[-_]?', '', normalized)
    return normalized


def extract_base_sku(sku):
    """★ v2.2 新增：提取 SKU 的基礎部分，用於模糊比對"""
    if not sku:
        return ""
    normalized = normalize_sku(sku)
    base = re.sub(r'[a-z]$', '', normalized)
    return base if base else normalized


def is_japanese_text(text):
    """判斷文字是否包含日文（平假名、片假名）"""
    if not text:
        return False
    check_text = text.replace('Cocoris', '').strip()
    if not check_text:
        return False
    japanese_chars = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', check_text))
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', check_text))
    total_chars = len(re.sub(r'[\s\d\W]', '', check_text))
    if total_chars == 0:
        return False
    if japanese_chars > 0 and (japanese_chars / total_chars > 0.3 or chinese_chars == 0):
        return True
    return False


def calculate_selling_price(cost):
    if not cost or cost <= 0: return 0
    if cost <= 5000:
        rate = 1.25
    elif cost <= 10000:
        rate = 1.22
    elif cost <= 20000:
        rate = 1.20
    elif cost <= 30000:
        rate = 1.18
    else:
        rate = 1.15
    fee = round(cost * (rate - 1))
    if fee < 300:
        fee = 300
    return round(cost + fee)


def clean_html_for_translation(html_text):
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
            error_msg = response.text[:200]
            print(f"[翻譯失敗] HTTP {response.status_code}: {error_msg}")
            return {
                'success': False,
                'error': f"HTTP {response.status_code}: {error_msg}",
                'title': f"Cocoris {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'error': str(e),
            'title': f"Cocoris {title}",
            'description': description,
            'page_title': '',
            'meta_description': ''
        }


def download_image_to_base64(img_url, max_retries=3):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Referer': 'https://sucreyshopping.jp/',
    }
    for attempt in range(max_retries):
        try:
            response = requests.get(img_url, headers=headers, timeout=30)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', 'image/jpeg')
                if 'png' in content_type:
                    img_format = 'image/png'
                elif 'webp' in content_type:
                    img_format = 'image/webp'
                elif 'gif' in content_type:
                    img_format = 'image/gif'
                else:
                    img_format = 'image/jpeg'
                img_base64 = base64.b64encode(response.content).decode('utf-8')
                return {'success': True, 'base64': img_base64, 'content_type': img_format}
        except Exception as e:
            print(f"[圖片下載] 第 {attempt+1} 次嘗試異常: {e}")
        time.sleep(1)
    return {'success': False}


def get_existing_products_map():
    """★ v2.2 強化：回傳多層去重 map，包含 SKU、normalized SKU、metafield URL"""
    products_map = {
        'by_sku': {},
        'by_raw_sku': {},
        'by_source_url': {},
        'by_title_hash': {},
        'by_variant': {},  # normalized_sku → {variant_id, price} 供售價同步用
    }
    
    url = shopify_api_url("products.json?limit=250&vendor=Cocoris")
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            break
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            title = product.get('title', '')
            
            title_key = re.sub(r'^cocoris\s*', '', title.lower()).strip()
            if title_key:
                products_map['by_title_hash'][title_key] = product_id
            
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    raw = sku.strip()
                    normalized = normalize_sku(sku)
                    products_map['by_sku'][normalized] = product_id
                    products_map['by_raw_sku'][raw] = product_id
                    products_map['by_raw_sku'][raw.lower()] = product_id
                    products_map['by_raw_sku'][raw.upper()] = product_id
                    products_map['by_variant'][normalized] = {
                        'variant_id': variant.get('id'),
                        'price': float(variant.get('price') or 0),
                    }
        
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    url = shopify_api_url("products.json?limit=250")
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
                    normalized = normalize_sku(sku)
                    if normalized not in products_map['by_sku']:
                        products_map['by_sku'][normalized] = product_id
                    raw = sku.strip()
                    if raw not in products_map['by_raw_sku']:
                        products_map['by_raw_sku'][raw] = product_id
        
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    return products_map


def sku_exists_in_map(sku, products_map):
    """★ v2.2 新增：多層檢查 SKU 是否已存在"""
    if not sku:
        return False
    
    normalized = normalize_sku(sku)
    raw = sku.strip()
    
    if normalized in products_map['by_sku']:
        print(f"[去重] SKU '{sku}' 已存在（normalized 比對）")
        return True
    
    if raw in products_map['by_raw_sku']:
        print(f"[去重] SKU '{sku}' 已存在（raw 比對）")
        return True
    if raw.lower() in products_map['by_raw_sku']:
        print(f"[去重] SKU '{sku}' 已存在（lower 比對）")
        return True
    if raw.upper() in products_map['by_raw_sku']:
        print(f"[去重] SKU '{sku}' 已存在（upper 比對）")
        return True
    
    return False


def check_sku_exists_realtime(sku):
    """★ v2.2 新增：上架前即時再查一次 Shopify"""
    if not sku:
        return False
    
    graphql_url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    
    query = """
    {
      productVariants(first: 10, query: "sku:%s") {
        edges {
          node {
            sku
            product {
              id
              title
            }
          }
        }
      }
    }
    """ % sku.replace('"', '\\"')
    
    try:
        response = requests.post(graphql_url, headers=headers, json={'query': query}, timeout=15)
        if response.status_code == 200:
            result = response.json()
            edges = result.get('data', {}).get('productVariants', {}).get('edges', [])
            for edge in edges:
                existing_sku = edge['node'].get('sku', '')
                if normalize_sku(existing_sku) == normalize_sku(sku):
                    product_title = edge['node'].get('product', {}).get('title', '')
                    print(f"[即時去重] SKU '{sku}' 已存在於商品: {product_title}")
                    return True
    except Exception as e:
        print(f"[即時去重] 查詢失敗: {e}")
    
    return False


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
                    normalized = normalize_sku(sku)
                    products_map[normalized] = product_id
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    return products_map


def set_product_to_draft(product_id):
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {"id": product_id, "status": "draft"}
    })
    return response.status_code == 200


def delete_product(product_id):
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.delete(url, headers=get_shopify_headers())
    return response.status_code == 200


def update_product(product_id, data):
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={"product": {"id": product_id, **data}})
    return response.status_code == 200, response


def get_or_create_collection(collection_title="Cocoris"):
    response = requests.get(
        shopify_api_url(f'custom_collections.json?title={collection_title}'),
        headers=get_shopify_headers()
    )
    if response.status_code == 200:
        collections = response.json().get('custom_collections', [])
        for col in collections:
            if col['title'] == collection_title:
                return col['id']
    response = requests.post(
        shopify_api_url('custom_collections.json'),
        headers=get_shopify_headers(),
        json={'custom_collection': {'title': collection_title, 'published': True}}
    )
    if response.status_code == 201:
        return response.json()['custom_collection']['id']
    return None


def add_product_to_collection(product_id, collection_id):
    response = requests.post(
        shopify_api_url('collects.json'),
        headers=get_shopify_headers(),
        json={'collect': {'product_id': product_id, 'collection_id': collection_id}}
    )
    return response.status_code == 201


def publish_to_all_channels(product_id):
    graphql_url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    query = """{ publications(first: 20) { edges { node { id name } } } }"""
    response = requests.post(graphql_url, headers=headers, json={'query': query})
    if response.status_code != 200:
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
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        userErrors { field message }
      }
    }
    """
    variables = {
        "id": f"gid://shopify/Product/{product_id}",
        "input": [{"publicationId": pub['id']} for pub in unique_publications]
    }
    requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables})
    return True


def parse_box_size(text):
    text = text.replace('×', 'x').replace('Ｘ', 'x').replace('ｘ', 'x')
    text = text.replace('ｍｍ', 'mm').replace('ｇ', 'g').replace('ｋｇ', 'kg')
    text = text.replace(',', '')
    pattern = r'[Ww]?\s*(\d+(?:\.\d+)?)\s*[xX×]\s*[Dd]?\s*(\d+(?:\.\d+)?)\s*[xX×]\s*[Hh]?\s*(\d+(?:\.\d+)?)'
    match = re.search(pattern, text)
    if match:
        w, d, h = float(match.group(1)), float(match.group(2)), float(match.group(3))
        volume_weight = round((w * d * h) / 6000000, 2)
        return {"width": w, "depth": d, "height": h, "volume_weight": volume_weight}
    simple_pattern = r'(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)'
    simple_match = re.search(simple_pattern, text)
    if simple_match:
        l, w, h = float(simple_match.group(1)), float(simple_match.group(2)), float(simple_match.group(3))
        volume_weight = round((l * w * h) / 6000000, 2)
        return {"length": l, "width": w, "height": h, "volume_weight": volume_weight}
    return None


def scrape_product_list():
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
                has_next_page = False
                continue
            soup = BeautifulSoup(response.text, 'html.parser')
            product_links = soup.find_all('a', href=re.compile(r'/shop/g/g[^/]+/?'))
            if not product_links:
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
                sku_raw = sku_match.group(1)
                sku = normalize_sku(sku_raw)
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)
                full_url = urljoin(BASE_URL, href)
                page_products.append({'url': full_url, 'sku': sku, 'sku_raw': sku_raw})
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
        key = normalize_sku(p['sku'])
        if key not in seen:
            seen.add(key)
            unique_products.append(p)
        else:
            print(f"[列表去重] 跳過重複 SKU: {p['sku']} (normalized: {key})")
    
    print(f"[INFO] 共找到 {len(unique_products)} 個不重複商品（原始 {len(products)} 個）")
    return unique_products


def scrape_product_detail(url):
    product = {
        'url': url, 'title': '', 'price': 0, 'description': '',
        'box_size_text': '', 'weight': 0, 'images': [], 'in_stock': True,
        'is_point_product': False, 'sku': '', 'content': '', 'allergens': '', 'shelf_life': ''
    }
    sku_match = re.search(r'/shop/g/g([^/]+)/?', url)
    if sku_match:
        product['sku'] = normalize_sku(sku_match.group(1))
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code != 200:
            return product
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        title_el = soup.find('h1')
        if title_el:
            product['title'] = title_el.get_text(strip=True)
        
        price_area = soup.find('div', class_='block-goods-price')
        if price_area:
            if 'ポイント' in price_area.get_text():
                product['is_point_product'] = True
        
        if not product['is_point_product']:
            price_el = soup.find('div', class_='block-goods-price--price')
            if price_el:
                price_match = re.search(r'(\d{1,3}(?:,\d{3})*)', price_el.get_text())
                if price_match:
                    product['price'] = int(price_match.group(1).replace(',', ''))
            if not product['price']:
                price_match = re.search(r'(\d{1,3}(?:,\d{3})*)\s*円', page_text)
                if price_match:
                    product['price'] = int(price_match.group(1).replace(',', ''))
        
        all_dt = soup.find_all('dt')
        all_dd = soup.find_all('dd')
        for i, dt in enumerate(all_dt):
            try:
                dt_text = dt.get_text(strip=True)
                if i < len(all_dd):
                    dd_text = all_dd[i].get_text(strip=True)
                    if '内容' in dt_text:
                        product['content'] = dd_text
                    elif '箱サイズ' in dt_text or 'サイズ' in dt_text:
                        product['box_size_text'] = dd_text
                        size_info = parse_box_size(dd_text)
                        if size_info:
                            product['weight'] = size_info.get('volume_weight', 0)
                    elif '賞味期限' in dt_text:
                        product['shelf_life'] = dd_text
                    elif 'アレルギー' in dt_text or '特定原材料' in dt_text:
                        product['allergens'] = dd_text[:200]
            except Exception:
                continue
        
        desc_parts = []
        for class_name in ['item-description', 'product-description', 'detail-text']:
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
        
        images = []
        sku_for_images = sku_match.group(1) if sku_match else product['sku']
        for prefix in ['L', '2', '3', '4', 'D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7', 'D8']:
            img_url = f"{BASE_URL}/img/goods/{prefix}/{sku_for_images}.jpg"
            try:
                head_response = requests.head(img_url, headers=HEADERS, timeout=5)
                if head_response.status_code == 200:
                    images.append(img_url)
            except:
                pass
        if not images:
            img_tags = soup.find_all('img', src=re.compile(sku_for_images, re.IGNORECASE))
            for img in img_tags:
                src = img.get('src', '')
                if src and src not in images:
                    if not src.startswith('http'):
                        src = urljoin(BASE_URL, src)
                    images.append(src)
        product['images'] = images
        
        if any(kw in page_text for kw in ['品切れ', '在庫なし', 'SOLD OUT']):
            product['in_stock'] = False
        
    except Exception as e:
        print(f"[ERROR] 爬取商品詳細失敗: {e}")
    
    return product


def upload_to_shopify(product, collection_id=None):
    """上傳商品到 Shopify（含翻譯保護 + v2.2 即時去重）"""
    
    if check_sku_exists_realtime(product['sku']):
        print(f"[跳過-即時去重] {product['sku']} 已存在")
        return {'success': False, 'error': 'already_exists_realtime', 'skipped': True}
    
    print(f"[翻譯] 正在翻譯: {product['title'][:30]}...")
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    
    if not translated['success']:
        print(f"[跳過-翻譯失敗] {product['sku']}: {translated.get('error', '未知錯誤')}")
        return {'success': False, 'error': 'translation_failed', 'translated': translated}
    
    print(f"[翻譯成功] {translated['title'][:30]}...")
    
    cost = product['price']
    selling_price = calculate_selling_price(cost)
    
    images_base64 = []
    img_urls = product.get('images', [])
    for idx, img_url in enumerate(img_urls):
        if not img_url or not img_url.startswith('http'):
            continue
        result = download_image_to_base64(img_url)
        if result['success']:
            images_base64.append({
                'attachment': result['base64'],
                'position': idx + 1,
                'filename': f"cocoris_{product['sku']}_{idx+1}.jpg"
            })
        time.sleep(0.5)
    
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
                'inventory_management': None,
                'inventory_policy': 'continue',
                'requires_shipping': True
            }],
            'images': images_base64,
            'tags': 'Cocoris, 日本, 烘焙甜點, 伴手禮, 日本代購, 送禮',
            'metafields_global_title_tag': translated['page_title'],
            'metafields_global_description_tag': translated['meta_description'],
            'metafields': [{'namespace': 'custom', 'key': 'link', 'value': product['url'], 'type': 'url'}]
        }
    }
    
    response = requests.post(shopify_api_url('products.json'), headers=get_shopify_headers(), json=shopify_product)
    
    if response.status_code == 201:
        created_product = response.json()['product']
        product_id = created_product['id']
        variant_id = created_product['variants'][0]['id']
        
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
        return {'success': False, 'error': response.text}


# ========== Flask 路由 ==========

@app.route('/')
def index():
    token_loaded = load_shopify_token()
    token_status = '✓ 已載入' if token_loaded else '✗ 未設定'
    token_color = 'green' if token_loaded else 'red'
    
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
        .btn {{ background: #8B4513; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; margin-bottom: 10px; text-decoration: none; display: inline-block; }}
        .btn:hover {{ background: #A0522D; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-secondary:hover {{ background: #2980b9; }}
        .btn-success {{ background: #27ae60; }}
        .btn-success:hover {{ background: #219a52; }}
        .btn-warning {{ background: #e67e22; }}
        .btn-warning:hover {{ background: #d35400; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #8B4513, #D2691E); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 80px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #8B4513; }}
        .stat-label {{ font-size: 11px; color: #666; margin-top: 5px; }}
        .nav {{ margin-bottom: 20px; }}
        .nav a {{ margin-right: 15px; color: #8B4513; text-decoration: none; font-weight: bold; }}
        .alert {{ padding: 12px 16px; border-radius: 5px; margin-bottom: 15px; }}
        .alert-danger {{ background: #fee; border: 1px solid #fcc; color: #c0392b; }}
    </style>
</head>
<body>
    <div class="nav">
        <a href="/">🏠 首頁</a>
        <a href="/japanese-scan">🇯🇵 日文商品掃描</a>
        <a href="/dedup-scan">🔍 重複商品掃描</a>
    </div>
    
    <h1>🍪 Cocoris 爬蟲工具 <small style="font-size: 14px; color: #999;">v2.3</small></h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: <span style="color: {token_color};">{token_status}</span></p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
        <button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
        <a href="/japanese-scan" class="btn btn-success">🇯🇵 掃描日文商品</a>
        <a href="/dedup-scan" class="btn btn-warning">🔍 掃描重複商品</a>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 sucreyshopping.jp Cocoris 品牌商品並上架到 Shopify</p>
        <p style="color: #666; font-size: 14px;">
            ※ 成本價低於 ¥{MIN_PRICE} 的商品將自動跳過<br>
            ※ <b style="color: #e74c3c;">翻譯保護</b> - 翻譯失敗不上架，連續失敗 {MAX_CONSECUTIVE_TRANSLATION_FAILURES} 次自動停止<br>
            ※ <b style="color: #e67e22;">v2.3 缺貨自動刪除</b> - 官網消失或缺貨的商品直接從 Shopify 刪除
        </p>
        <button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
        
        <div id="progressSection" style="display: none;">
            <div id="translationAlert" class="alert alert-danger" style="display: none;">
                ⚠️ 翻譯功能異常，爬取已自動停止！請檢查 OpenAI API Key 和餘額。
            </div>
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill" style="width: 0%"></div>
            </div>
            <div class="status" id="statusText">準備中...</div>
            <div class="stats">
                <div class="stat"><div class="stat-number" id="uploadedCount">0</div><div class="stat-label">已上架</div></div>
                <div class="stat"><div class="stat-number" id="skippedCount">0</div><div class="stat-label">已跳過</div></div>
                <div class="stat"><div class="stat-number" id="filteredCount">0</div><div class="stat-label">價格過濾</div></div>
                <div class="stat"><div class="stat-number" id="translationFailedCount" style="color: #e74c3c;">0</div><div class="stat-label">翻譯失敗</div></div>
                <div class="stat"><div class="stat-number" id="deletedCount" style="color: #e67e22;">0</div><div class="stat-label">已刪除</div></div>
                <div class="stat"><div class="stat-number" id="errorCount" style="color: #e74c3c;">0</div><div class="stat-label">錯誤</div></div>
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
            const colors = {{ success: '#4ec9b0', error: '#f14c4c', warning: '#dcdcaa' }};
            const color = colors[type] || '#d4d4d4';
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
        async function testTranslate() {{
            log('測試翻譯功能...');
            try {{
                const res = await fetch('/api/test-translate');
                const data = await res.json();
                if (data.error) log('✗ 翻譯失敗: ' + data.error, 'error');
                else if (data.success) log('✓ 翻譯成功！結果: ' + data.title, 'success');
                else log('✗ 翻譯失敗', 'error');
            }} catch (e) {{ log('✗ 請求失敗: ' + e.message, 'error'); }}
        }}
        async function startScrape() {{
            clearLog(); log('開始爬取流程...');
            document.getElementById('startBtn').disabled = true;
            document.getElementById('progressSection').style.display = 'block';
            document.getElementById('translationAlert').style.display = 'none';
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
                document.getElementById('translationFailedCount').textContent = data.translation_failed || 0;
                document.getElementById('deletedCount').textContent = data.deleted || 0;
                document.getElementById('errorCount').textContent = data.errors.length;
                if (data.translation_stopped) document.getElementById('translationAlert').style.display = 'block';
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval);
                    document.getElementById('startBtn').disabled = false;
                    if (data.translation_stopped) log('⚠️ 爬取因翻譯連續失敗而自動停止', 'error');
                    else log('========== 爬取完成 ==========', 'success');
                }}
            }} catch (e) {{ console.error(e); }}
        }}
    </script>
</body>
</html>'''


@app.route('/dedup-scan')
def dedup_scan_page():
    """★ v2.2 新增：重複商品掃描頁面"""
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>重複商品掃描 - Cocoris</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; border-bottom: 2px solid #e67e22; padding-bottom: 10px; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn { background: #8B4513; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 14px; margin-right: 10px; margin-bottom: 10px; }
        .btn:hover { background: #A0522D; }
        .btn:disabled { background: #ccc; cursor: not-allowed; }
        .btn-danger { background: #e74c3c; }
        .btn-sm { padding: 5px 10px; font-size: 12px; }
        .nav { margin-bottom: 20px; }
        .nav a { margin-right: 15px; color: #8B4513; text-decoration: none; font-weight: bold; }
        .dup-group { border: 2px solid #e74c3c; border-radius: 8px; margin-bottom: 15px; overflow: hidden; }
        .dup-group-header { background: #fee; padding: 10px 15px; font-weight: bold; color: #c0392b; }
        .dup-item { display: flex; align-items: center; padding: 10px 15px; border-top: 1px solid #eee; gap: 10px; }
        .dup-item img { width: 50px; height: 50px; object-fit: cover; border-radius: 4px; }
        .dup-item .info { flex: 1; font-size: 13px; }
        .dup-item .info .title { font-weight: bold; }
        .dup-item .keep { color: #27ae60; font-weight: bold; }
        .no-image { width: 50px; height: 50px; background: #eee; display: flex; align-items: center; justify-content: center; border-radius: 4px; color: #999; font-size: 10px; }
        .stats { display: flex; gap: 15px; margin: 20px 0; flex-wrap: wrap; }
        .stat { flex: 1; min-width: 150px; text-align: center; padding: 20px; background: #f8f9fa; border-radius: 8px; }
        .stat-number { font-size: 36px; font-weight: bold; }
        .stat-label { font-size: 14px; color: #666; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="nav">
        <a href="/">🏠 首頁</a>
        <a href="/japanese-scan">🇯🇵 日文商品掃描</a>
        <a href="/dedup-scan">🔍 重複商品掃描</a>
    </div>
    <h1>🔍 重複商品掃描 - Cocoris</h1>
    <div class="card">
        <p>掃描 Shopify 中 Cocoris 商品，找出 SKU 相同的重複商品。</p>
        <button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button>
        <span id="scanStatus"></span>
    </div>
    <div class="stats" id="statsSection" style="display: none;">
        <div class="stat"><div class="stat-number" id="totalProducts" style="color: #3498db;">0</div><div class="stat-label">Cocoris 商品數</div></div>
        <div class="stat"><div class="stat-number" id="dupGroups" style="color: #e74c3c;">0</div><div class="stat-label">重複組數</div></div>
        <div class="stat"><div class="stat-number" id="dupProducts" style="color: #e67e22;">0</div><div class="stat-label">可刪除數</div></div>
    </div>
    <div id="resultsCard" style="display: none;">
        <div class="card">
            <button class="btn btn-danger" id="deleteAllDupsBtn" onclick="deleteAllDups()" disabled>🗑️ 一鍵刪除所有重複（保留最舊的）</button>
            <span id="deleteProgress"></span>
        </div>
        <div id="results"></div>
    </div>
    <script>
        let dupData = [];
        async function startScan() {
            document.getElementById('scanBtn').disabled = true;
            document.getElementById('scanStatus').textContent = '掃描中...';
            try {
                const res = await fetch('/api/scan-duplicates');
                const data = await res.json();
                if (data.error) { alert('錯誤: ' + data.error); return; }
                dupData = data.duplicates;
                document.getElementById('totalProducts').textContent = data.total_products;
                document.getElementById('dupGroups').textContent = data.duplicate_groups;
                document.getElementById('dupProducts').textContent = data.deletable_count;
                document.getElementById('statsSection').style.display = 'flex';
                document.getElementById('resultsCard').style.display = 'block';
                document.getElementById('deleteAllDupsBtn').disabled = dupData.length === 0;
                renderDups(data.duplicates);
                document.getElementById('scanStatus').textContent = '掃描完成！';
            } catch (e) { alert(e.message); }
            finally { document.getElementById('scanBtn').disabled = false; }
        }
        function renderDups(groups) {
            const container = document.getElementById('results');
            if (groups.length === 0) {
                container.innerHTML = '<div class="card"><p style="text-align:center;color:#27ae60;font-size:18px;">✅ 沒有發現重複商品！</p></div>';
                return;
            }
            let html = '';
            groups.forEach((group, gi) => {
                html += `<div class="dup-group"><div class="dup-group-header">重複組 #${gi+1} — SKU: ${group.sku} (${group.products.length} 個商品)</div>`;
                group.products.forEach((p, pi) => {
                    const isKeep = pi === 0;
                    const img = p.image ? `<img src="${p.image}">` : `<div class="no-image">無圖</div>`;
                    const keepBadge = isKeep ? '<span class="keep">✓ 保留</span>' : `<button class="btn btn-danger btn-sm" onclick="deleteOne('${p.id}', this)">🗑️ 刪除</button>`;
                    html += `<div class="dup-item" id="dup-${p.id}">${img}<div class="info"><div class="title">${p.title}</div><div>ID: ${p.id} | 建立: ${p.created_at?.substring(0,10)||'?'} | 狀態: ${p.status}</div></div>${keepBadge}</div>`;
                });
                html += '</div>';
            });
            container.innerHTML = html;
        }
        async function deleteOne(id, btn) {
            if (!confirm('確定刪除？')) return;
            btn.disabled = true; btn.textContent = '刪除中...';
            try {
                const res = await fetch('/api/delete-product', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({product_id:id}) });
                const data = await res.json();
                if (data.success) { document.getElementById(`dup-${id}`).style.opacity = '0.3'; btn.textContent = '已刪除'; }
                else { alert('失敗'); btn.disabled = false; btn.textContent = '🗑️ 刪除'; }
            } catch(e) { alert(e.message); btn.disabled = false; }
        }
        async function deleteAllDups() {
            let toDelete = [];
            dupData.forEach(g => { g.products.slice(1).forEach(p => toDelete.push(p.id)); });
            if (!confirm(`確定刪除 ${toDelete.length} 個重複商品？`)) return;
            const btn = document.getElementById('deleteAllDupsBtn'); btn.disabled = true;
            let s=0, f=0;
            for (let i=0; i<toDelete.length; i++) {
                document.getElementById('deleteProgress').textContent = `${i+1}/${toDelete.length}`;
                try {
                    const res = await fetch('/api/delete-product', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({product_id:toDelete[i]}) });
                    const data = await res.json();
                    if (data.success) { s++; const el = document.getElementById(`dup-${toDelete[i]}`); if(el) el.style.opacity='0.3'; } else f++;
                } catch(e) { f++; }
                await new Promise(r=>setTimeout(r,300));
            }
            alert(`完成！刪除: ${s}, 失敗: ${f}`);
            btn.disabled = false; document.getElementById('deleteProgress').textContent = '';
        }
    </script>
</body>
</html>'''


@app.route('/japanese-scan')
def japanese_scan_page():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>日文商品掃描 - Cocoris</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; border-bottom: 2px solid #27ae60; padding-bottom: 10px; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn { background: #8B4513; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 14px; margin-right: 10px; margin-bottom: 10px; }
        .btn:hover { background: #A0522D; }
        .btn:disabled { background: #ccc; cursor: not-allowed; }
        .btn-danger { background: #e74c3c; }
        .btn-success { background: #27ae60; }
        .btn-sm { padding: 5px 10px; font-size: 12px; }
        .nav { margin-bottom: 20px; }
        .nav a { margin-right: 15px; color: #8B4513; text-decoration: none; font-weight: bold; }
        .stats { display: flex; gap: 15px; margin: 20px 0; flex-wrap: wrap; }
        .stat { flex: 1; min-width: 150px; text-align: center; padding: 20px; background: #f8f9fa; border-radius: 8px; }
        .stat-number { font-size: 36px; font-weight: bold; }
        .stat-label { font-size: 14px; color: #666; margin-top: 5px; }
        .product-item { display: flex; align-items: center; padding: 15px; border-bottom: 1px solid #eee; gap: 15px; }
        .product-item:last-child { border-bottom: none; }
        .product-item img { width: 60px; height: 60px; object-fit: cover; border-radius: 4px; }
        .product-item .info { flex: 1; }
        .product-item .info .title { font-weight: bold; margin-bottom: 5px; color: #c0392b; }
        .product-item .info .meta { font-size: 12px; color: #666; }
        .product-item .actions { display: flex; gap: 5px; }
        .no-image { width: 60px; height: 60px; background: #eee; display: flex; align-items: center; justify-content: center; border-radius: 4px; color: #999; font-size: 10px; }
        .retranslate-status { font-size: 12px; margin-top: 5px; }
        .action-bar { position: sticky; top: 0; background: white; padding: 15px; margin: -20px -20px 20px -20px; border-bottom: 1px solid #ddd; z-index: 100; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
    </style>
</head>
<body>
    <div class="nav">
        <a href="/">🏠 首頁</a>
        <a href="/japanese-scan">🇯🇵 日文商品掃描</a>
        <a href="/dedup-scan">🔍 重複商品掃描</a>
    </div>
    <h1>🇯🇵 日文商品掃描 - Cocoris</h1>
    <div class="card">
        <p>掃描 Shopify 商店中 Cocoris 的日文（未翻譯）商品，並提供重新翻譯功能。</p>
        <button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button>
        <span id="scanStatus"></span>
    </div>
    <div class="stats" id="statsSection" style="display: none;">
        <div class="stat"><div class="stat-number" id="totalProducts" style="color: #3498db;">0</div><div class="stat-label">Cocoris 商品數</div></div>
        <div class="stat"><div class="stat-number" id="japaneseCount" style="color: #e74c3c;">0</div><div class="stat-label">日文商品</div></div>
    </div>
    <div class="card" id="resultsCard" style="display: none;">
        <div class="action-bar">
            <div>
                <button class="btn btn-success" id="retranslateAllBtn" onclick="retranslateAll()" disabled>🔄 全部重新翻譯</button>
                <button class="btn btn-danger" id="deleteAllBtn" onclick="deleteAllJapanese()" disabled>🗑️ 全部刪除</button>
            </div>
            <div id="progressText"></div>
        </div>
        <div id="results"></div>
    </div>
    <script>
        let japaneseProducts = [];
        async function startScan() {
            document.getElementById('scanBtn').disabled = true;
            document.getElementById('scanStatus').textContent = '掃描中...';
            document.getElementById('statsSection').style.display = 'none';
            document.getElementById('resultsCard').style.display = 'none';
            try {
                const res = await fetch('/api/scan-japanese');
                const data = await res.json();
                if (data.error) { alert('錯誤: ' + data.error); return; }
                japaneseProducts = data.japanese_products;
                document.getElementById('totalProducts').textContent = data.total_products;
                document.getElementById('japaneseCount').textContent = data.japanese_count;
                document.getElementById('statsSection').style.display = 'flex';
                renderResults(data.japanese_products);
                document.getElementById('resultsCard').style.display = 'block';
                document.getElementById('retranslateAllBtn').disabled = japaneseProducts.length === 0;
                document.getElementById('deleteAllBtn').disabled = japaneseProducts.length === 0;
                document.getElementById('scanStatus').textContent = '掃描完成！';
            } catch (e) { alert('請求失敗: ' + e.message); }
            finally { document.getElementById('scanBtn').disabled = false; }
        }
        function renderResults(products) {
            const container = document.getElementById('results');
            if (products.length === 0) { container.innerHTML = '<p style="text-align:center;color:#27ae60;font-size:18px;">✅ 沒有發現日文商品。</p>'; return; }
            let html = '';
            products.forEach(item => {
                const img = item.image ? `<img src="${item.image}">` : `<div class="no-image">無圖片</div>`;
                html += `<div class="product-item" id="product-${item.id}">${img}<div class="info"><div class="title">${item.title}</div><div class="meta">SKU: ${item.sku||'無'} | 價格: ¥${item.price} | 狀態: ${item.status}</div><div class="retranslate-status" id="status-${item.id}"></div></div><div class="actions"><button class="btn btn-success btn-sm" onclick="retranslateOne('${item.id}')" id="retranslate-btn-${item.id}">🔄 翻譯</button><button class="btn btn-danger btn-sm" onclick="deleteOne('${item.id}')" id="delete-btn-${item.id}">🗑️ 刪除</button></div></div>`;
            });
            container.innerHTML = html;
        }
        async function retranslateOne(id) {
            const btn = document.getElementById(`retranslate-btn-${id}`); const st = document.getElementById(`status-${id}`);
            btn.disabled = true; btn.textContent = '翻譯中...'; st.innerHTML = '<span style="color:#f39c12;">⏳ 翻譯中...</span>';
            try {
                const res = await fetch('/api/retranslate-product', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({product_id:id}) });
                const data = await res.json();
                if (data.success) { st.innerHTML = `<span style="color:#27ae60;">✅ ${data.new_title}</span>`; document.querySelector(`#product-${id} .title`).textContent = data.new_title; document.querySelector(`#product-${id} .title`).style.color = '#27ae60'; btn.textContent = '✓ 完成'; }
                else { st.innerHTML = `<span style="color:#e74c3c;">❌ ${data.error}</span>`; btn.disabled = false; btn.textContent = '🔄 重試'; }
            } catch (e) { st.innerHTML = `<span style="color:#e74c3c;">❌ ${e.message}</span>`; btn.disabled = false; btn.textContent = '🔄 重試'; }
        }
        async function deleteOne(id) {
            if (!confirm('確定刪除？')) return;
            const btn = document.getElementById(`delete-btn-${id}`); btn.disabled = true;
            try { const res = await fetch('/api/delete-product', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({product_id:id}) }); const data = await res.json(); if (data.success) document.getElementById(`product-${id}`).remove(); else { alert('刪除失敗'); btn.disabled = false; } } catch(e) { alert(e.message); btn.disabled = false; }
        }
        async function retranslateAll() {
            if (!confirm(`重新翻譯全部 ${japaneseProducts.length} 個？`)) return;
            const btn = document.getElementById('retranslateAllBtn'); btn.disabled = true; btn.textContent = '翻譯中...';
            let s=0, f=0;
            for (let i=0; i<japaneseProducts.length; i++) {
                document.getElementById('progressText').textContent = `${i+1}/${japaneseProducts.length}`;
                try { const res = await fetch('/api/retranslate-product', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({product_id:japaneseProducts[i].id}) }); const data = await res.json(); const st = document.getElementById(`status-${japaneseProducts[i].id}`);
                    if (data.success) { s++; if(st) st.innerHTML=`<span style="color:#27ae60;">✅ ${data.new_title}</span>`; const t=document.querySelector(`#product-${japaneseProducts[i].id} .title`); if(t){t.textContent=data.new_title;t.style.color='#27ae60';} }
                    else { f++; if(st) st.innerHTML=`<span style="color:#e74c3c;">❌ ${data.error}</span>`; if(f>=3){alert('連續失敗，停止');break;} }
                } catch(e) { f++; }
                await new Promise(r=>setTimeout(r,1500));
            }
            alert(`完成！成功:${s} 失敗:${f}`); btn.textContent='🔄 全部重新翻譯'; btn.disabled=false; document.getElementById('progressText').textContent='';
        }
        async function deleteAllJapanese() {
            if (!confirm(`刪除全部 ${japaneseProducts.length} 個？無法復原！`)) return;
            const btn = document.getElementById('deleteAllBtn'); btn.disabled = true; btn.textContent = '刪除中...';
            let s=0, f=0;
            for (let i=0; i<japaneseProducts.length; i++) {
                document.getElementById('progressText').textContent = `${i+1}/${japaneseProducts.length}`;
                try { const res = await fetch('/api/delete-product', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({product_id:japaneseProducts[i].id}) }); const data = await res.json(); if(data.success){s++;const el=document.getElementById(`product-${japaneseProducts[i].id}`);if(el)el.remove();}else f++; } catch(e){f++;}
                await new Promise(r=>setTimeout(r,300));
            }
            alert(`完成！成功:${s} 失敗:${f}`); btn.textContent='🗑️ 全部刪除'; btn.disabled=false; document.getElementById('progressText').textContent='';
        }
    </script>
</body>
</html>'''


# ========== API 路由 ==========

@app.route('/api/scan-duplicates')
def api_scan_duplicates():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Shopify Token'}), 400
    
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=Cocoris")
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            break
        data = response.json()
        for p in data.get('products', []):
            sku = ''
            price = ''
            for v in p.get('variants', []):
                sku = v.get('sku', '')
                price = v.get('price', '')
                break
            products.append({
                'id': p.get('id'),
                'title': p.get('title', ''),
                'sku': sku,
                'price': price,
                'status': p.get('status', ''),
                'created_at': p.get('created_at', ''),
                'image': p.get('image', {}).get('src', '') if p.get('image') else ''
            })
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    sku_groups = {}
    for p in products:
        sku = normalize_sku(p.get('sku', ''))
        if not sku:
            continue
        if sku not in sku_groups:
            sku_groups[sku] = []
        sku_groups[sku].append(p)
    
    duplicates = []
    deletable_count = 0
    for sku, group in sku_groups.items():
        if len(group) >= 2:
            group.sort(key=lambda x: x.get('created_at', ''))
            duplicates.append({
                'sku': sku,
                'count': len(group),
                'products': group
            })
            deletable_count += len(group) - 1
    
    duplicates.sort(key=lambda x: x['count'], reverse=True)
    
    return jsonify({
        'total_products': len(products),
        'duplicate_groups': len(duplicates),
        'deletable_count': deletable_count,
        'duplicates': duplicates
    })


@app.route('/api/scan-japanese')
def api_scan_japanese():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Shopify Token'}), 400
    
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=Cocoris")
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            break
        data = response.json()
        for p in data.get('products', []):
            sku = ''
            price = ''
            for v in p.get('variants', []):
                sku = v.get('sku', '')
                price = v.get('price', '')
                break
            products.append({
                'id': p.get('id'),
                'title': p.get('title', ''),
                'handle': p.get('handle', ''),
                'sku': sku,
                'price': price,
                'vendor': p.get('vendor', ''),
                'status': p.get('status', ''),
                'created_at': p.get('created_at', ''),
                'image': p.get('image', {}).get('src', '') if p.get('image') else ''
            })
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    japanese_products = [p for p in products if is_japanese_text(p.get('title', ''))]
    return jsonify({
        'total_products': len(products),
        'japanese_count': len(japanese_products),
        'japanese_products': japanese_products
    })


@app.route('/api/retranslate-product', methods=['POST'])
def api_retranslate_product():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Shopify Token'}), 400
    data = request.get_json()
    product_id = data.get('product_id')
    if not product_id:
        return jsonify({'error': '缺少 product_id'}), 400
    
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.get(url, headers=get_shopify_headers())
    if response.status_code != 200:
        return jsonify({'error': f'無法取得商品: {response.status_code}'}), 400
    
    product = response.json().get('product', {})
    old_title = product.get('title', '')
    old_body = product.get('body_html', '')
    
    translated = translate_with_chatgpt(old_title, old_body)
    if not translated['success']:
        return jsonify({'success': False, 'error': f"翻譯失敗: {translated.get('error', '未知錯誤')}"})
    
    update_data = {
        'title': translated['title'],
        'body_html': translated['description'],
        'metafields_global_title_tag': translated['page_title'],
        'metafields_global_description_tag': translated['meta_description']
    }
    success, resp = update_product(product_id, update_data)
    if success:
        return jsonify({'success': True, 'old_title': old_title, 'new_title': translated['title'], 'product_id': product_id})
    else:
        return jsonify({'success': False, 'error': f'更新失敗: {resp.text[:200]}'})


@app.route('/api/delete-product', methods=['POST'])
def api_delete_product():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Shopify Token'}), 400
    data = request.get_json()
    product_id = data.get('product_id')
    if not product_id:
        return jsonify({'error': '缺少 product_id'}), 400
    success = delete_product(product_id)
    return jsonify({'success': success, 'product_id': product_id})


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/test-translate')
def test_translate():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({'error': 'OPENAI_API_KEY 環境變數未設定', 'key_exists': False})
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    result = translate_with_chatgpt("ココリス 焼き菓子詰合せ 12個入", "厳選素材で焼き上げた贅沢な焼き菓子の詰合せです")
    result['key_preview'] = key_preview
    result['key_length'] = len(api_key)
    return jsonify(result)


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '找不到 Token'})
    response = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    if response.status_code == 200:
        return jsonify({'success': True, 'shop': response.json()['shop']})
    else:
        return jsonify({'success': False, 'error': response.text}), 400


@app.route('/api/start-scrape', methods=['POST'])
def start_scrape():
    global scrape_status
    if scrape_status['running']:
        return jsonify({'success': False, 'error': '爬取正在進行中'})
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '找不到 Token'})
    
    test_result = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test_result['success']:
        error_msg = test_result.get('error', '未知錯誤')
        return jsonify({'success': False, 'error': f'翻譯功能異常，無法啟動: {error_msg}'})
    
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
    
    test_result = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test_result['success']:
        error_msg = test_result.get('error', '未知錯誤')
        return jsonify({'success': False, 'error': f'翻譯功能異常: {error_msg}'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    return jsonify({'success': True, 'message': 'Cocoris 爬蟲已啟動'})


def run_scrape():
    global scrape_status
    
    try:
        scrape_status = {
            "running": True, "progress": 0, "total": 0,
            "current_product": "", "products": [], "errors": [],
            "uploaded": 0, "skipped": 0, "skipped_exists": 0,
            "filtered_by_price": 0, "deleted": 0,
            "translation_failed": 0, "translation_stopped": False
        }
        
        scrape_status['current_product'] = "正在檢查 Shopify 已有商品（強化去重）..."
        products_map = get_existing_products_map()
        print(f"[去重] 已載入 {len(products_map['by_sku'])} 個 normalized SKU, {len(products_map['by_raw_sku'])} 個 raw SKU")
        
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("Cocoris")
        
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        collection_skus = set(collection_products_map.keys())
        
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)
        
        website_skus = set(item['sku'] for item in product_list)
        
        # === v2.3: 記錄缺貨的 SKU ===
        out_of_stock_skus = set()
        
        processed_skus_this_run = set()
        consecutive_translation_failures = 0
        
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理中: {item['sku']}"
            
            normalized_sku = normalize_sku(item['sku'])
            
            # 檢查本次是否已處理過
            if normalized_sku in processed_skus_this_run:
                print(f"[跳過-本次重複] {item['sku']}")
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                continue
            
            # 多層去重檢查 — 已存在於 Shopify
            if sku_exists_in_map(item['sku'], products_map):
                # 已存在的商品：爬詳情確認庫存 + 同步售價
                if normalized_sku in collection_skus:
                    product = scrape_product_detail(item['url'])
                    if product:
                        if not product.get('in_stock', True):
                            out_of_stock_skus.add(normalized_sku)
                            print(f"[缺貨偵測] {item['sku']} 官網缺貨，稍後刪除")
                        elif product.get('price', 0) >= MIN_PRICE:
                            new_selling_price = calculate_selling_price(product['price'])
                            variant_info = products_map['by_variant'].get(normalized_sku, {})
                            vid = variant_info.get('variant_id')
                            if vid and abs(new_selling_price - variant_info.get('price', 0)) >= 1:
                                requests.put(
                                    shopify_api_url(f'variants/{vid}.json'),
                                    headers=get_shopify_headers(),
                                    json={'variant': {'id': vid,
                                                      'price': f"{new_selling_price:.2f}",
                                                      'cost': f"{product['price']:.2f}"}}
                                )
                    time.sleep(0.5)
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                processed_skus_this_run.add(normalized_sku)
                continue
            
            product = scrape_product_detail(item['url'])
            
            # 用爬回來的 SKU 再檢查一次
            if sku_exists_in_map(product['sku'], products_map):
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                processed_skus_this_run.add(normalized_sku)
                processed_skus_this_run.add(normalize_sku(product['sku']))
                continue
            
            # === v2.3: 缺貨 → 不上架，記錄 SKU ===
            if not product.get('in_stock', True):
                out_of_stock_skus.add(normalized_sku)
                scrape_status['skipped'] += 1
                processed_skus_this_run.add(normalized_sku)
                continue
            
            if product.get('is_point_product', False):
                scrape_status['skipped'] += 1
                continue
            
            if product.get('price', 0) < MIN_PRICE:
                scrape_status['filtered_by_price'] += 1
                scrape_status['skipped'] += 1
                continue
            
            if not product.get('title') or not product.get('price'):
                scrape_status['errors'].append({'sku': item['sku'], 'error': '資訊不完整'})
                continue
            
            result = upload_to_shopify(product, collection_id)
            
            if result['success']:
                products_map['by_sku'][normalized_sku] = True
                products_map['by_sku'][normalize_sku(product['sku'])] = True
                products_map['by_raw_sku'][item['sku']] = True
                products_map['by_raw_sku'][product['sku']] = True
                processed_skus_this_run.add(normalized_sku)
                processed_skus_this_run.add(normalize_sku(product['sku']))
                scrape_status['uploaded'] += 1
                consecutive_translation_failures = 0
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1
                consecutive_translation_failures += 1
                if consecutive_translation_failures >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append({'error': f'翻譯連續失敗 {consecutive_translation_failures} 次，自動停止'})
                    break
            elif result.get('error') == 'already_exists_realtime':
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                processed_skus_this_run.add(normalized_sku)
            else:
                scrape_status['errors'].append({'sku': product['sku'], 'error': result['error']})
                consecutive_translation_failures = 0
            
            time.sleep(1)
        
        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "清理缺貨/下架商品..."
            
            # === v2.3: 合併需要刪除的 SKU ===
            # 1. 官網已消失的 SKU（collection 有但官網沒有）
            # 2. 官網還在但缺貨的 SKU
            skus_to_delete = (collection_skus - website_skus) | (collection_skus & out_of_stock_skus)
            
            if skus_to_delete:
                print(f"[v2.3] 準備刪除 {len(skus_to_delete)} 個商品（官網消失: {len(collection_skus - website_skus)}, 缺貨: {len(collection_skus & out_of_stock_skus)}）")
                for sku in skus_to_delete:
                    product_id = collection_products_map.get(sku)
                    if product_id:
                        if delete_product(product_id):
                            scrape_status['deleted'] += 1
                            print(f"[已刪除] SKU: {sku}, Product ID: {product_id}")
                        else:
                            scrape_status['errors'].append({'sku': sku, 'error': '刪除失敗'})
                    time.sleep(0.3)
        
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False
        scrape_status['current_product'] = "完成" if not scrape_status['translation_stopped'] else "翻譯異常停止"


if __name__ == '__main__':
    print("=" * 50)
    print("Cocoris 爬蟲工具 v2.3")
    print("新增: 缺貨商品自動刪除（官網消失或缺貨皆刪除）")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

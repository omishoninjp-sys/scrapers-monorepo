"""
The Maple Mania 楓糖男孩 商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 sucreyshopping.jp The Maple Mania 所有商品
2. 過濾 1000円以下商品、點數商品
3. 上架到 Shopify（不重複上架）
4. 原價寫入成本價（Cost）
5. OpenAI 翻譯成繁體中文
6. SEO 和 GEO 優化
7. 不設定庫存數量
8. 發布到所有銷售渠道
"""

from flask import Flask, jsonify, request
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
SHOPIFY_SHOP = ""  # 從環境變數讀取
SHOPIFY_ACCESS_TOKEN = ""  # 從環境變數讀取

BASE_URL = "https://sucreyshopping.jp"
# 商品列表頁面 (分頁)
LIST_PAGES = [
    "https://sucreyshopping.jp/shop/c/c10/?brand=themaplemania",
    "https://sucreyshopping.jp/shop/c/c10_p2/?brand=themaplemania",
    "https://sucreyshopping.jp/shop/c/c10_p3/?brand=themaplemania",
    "https://sucreyshopping.jp/shop/c/c10_p4/?brand=themaplemania",
]

# 品牌前綴
BRAND_PREFIX = "The maple mania 楓糖男孩"

# 最低價格門檻（日幣）
MIN_PRICE = 1000

# OpenAI API 設定 (從環境變數讀取)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# 模擬瀏覽器 Headers
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
}

# 建立 Session
session = requests.Session()
session.headers.update(BROWSER_HEADERS)

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
    "skipped_low_price": 0,
    "skipped_points": 0,
    "skipped_exists": 0,
    "filtered_by_price": 0,
    "deleted": 0
}


def load_shopify_token():
    """載入 Shopify Access Token 和商店名稱 (優先從環境變數讀取)"""
    global SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP
    
    # 優先從環境變數讀取
    env_token = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
    env_shop = os.environ.get('SHOPIFY_SHOP', '')
    
    if env_token and env_shop:
        SHOPIFY_ACCESS_TOKEN = env_token
        SHOPIFY_SHOP = env_shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
        print(f"[設定] 從環境變數載入 - 商店: {SHOPIFY_SHOP}")
        return True
    
    # 備用：從檔案讀取
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
    
    # 移除 style 標籤及內容
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    # 移除 script 標籤及內容
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    # 移除 CSS 樣式塊
    text = re.sub(r'#[\w-]+\s*\{[^}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\.[\w-]+\s*\{[^}]*\}', '', text, flags=re.DOTALL)
    text = re.sub(r'@media[^{]*\{[^}]*\}', '', text, flags=re.DOTALL)
    
    # 移除內聯 style 屬性
    text = re.sub(r'\s*style\s*=\s*["\'][^"\']*["\']', '', text, flags=re.IGNORECASE)
    
    # 移除 HTML 標籤，保留換行
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    
    # 移除多餘的空白和換行
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    
    return text.strip()


def translate_with_chatgpt(title, description):
    """
    使用 ChatGPT 翻譯商品名稱和說明，並生成 SEO 內容
    回傳：translated_title, translated_description, page_title, meta_description
    """
    # 清理描述中的 HTML/CSS
    clean_description = clean_html_for_translation(description)
    
    # 先移除描述中的價格資訊
    clean_description = re.sub(r'[\d,]+\s*円', '', clean_description)
    clean_description = re.sub(r'價格[：:]\s*[\d,]+\s*日圓', '', clean_description)
    clean_description = re.sub(r'価格[：:]\s*[\d,]+', '', clean_description)
    clean_description = re.sub(r'税込[\d,]+円', '', clean_description)
    clean_description = re.sub(r'[\d,]+円\s*（税込）', '', clean_description)
    
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本甜點商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{clean_description[:1500]}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，簡潔有力，前面加上 The maple mania 楓糖男孩）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示，使用 HTML 格式）",
    "page_title": "SEO 頁面標題（繁體中文，包含 The maple mania 楓糖男孩品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

注意：
1. 這是日本 The Maple Mania 楓糖男孩的楓糖甜點（餅乾、費南雪、年輪蛋糕）
2. 翻譯要自然流暢，不要生硬
3. 商品標題開頭必須是「The maple mania 楓糖男孩」
4. SEO 內容要包含：The maple mania、楓糖男孩、日本、東京伴手禮、送禮、楓糖餅乾等關鍵字
5. 楓糖男孩是東京車站最受歡迎的伴手禮之一
6. 描述中可以提到台灣代購、日本直送等關鍵字，增加 SEO 效果
7. **重要：說明文中不要包含任何價格資訊（如「xxx円」「xxx日圓」等）**
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
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家，專門處理日本高級甜點的中文翻譯。"},
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
            
            # 清理可能的 markdown 標記
            content = content.strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content.rsplit('```', 1)[0]
            content = content.strip()
            
            # 解析 JSON
            translated = json.loads(content)
            
            # 確保標題開頭有品牌名
            trans_title = translated.get('title', title)
            if not trans_title.startswith('The maple mania') and not trans_title.startswith('The Maple Mania'):
                trans_title = f"{BRAND_PREFIX} {trans_title}"
            
            # 清除描述中可能殘留的價格資訊
            trans_desc = translated.get('description', description)
            trans_desc = re.sub(r'[\d,]+\s*円', '', trans_desc)
            trans_desc = re.sub(r'[\d,]+\s*日圓', '', trans_desc)
            trans_desc = re.sub(r'價格[：:]\s*[\d,]+', '', trans_desc)
            trans_desc = re.sub(r'価格[：:]\s*[\d,]+', '', trans_desc)
            
            return {
                'success': True,
                'title': trans_title,
                'description': trans_desc,
                'page_title': translated.get('page_title', ''),
                'meta_description': translated.get('meta_description', '')
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': f"{BRAND_PREFIX} {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"{BRAND_PREFIX} {title}",
            'description': description,
            'page_title': '',
            'meta_description': ''
        }


def download_image_to_base64(img_url, max_retries=3):
    """
    下載圖片並轉換為 Base64
    使用與瀏覽器相同的 headers 來避免防盜連
    """
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
                # 取得圖片格式
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
                    img_format = 'image/jpeg'  # 預設
                
                # 轉換為 Base64
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
        
        time.sleep(1)  # 重試前等待
    
    return {'success': False}


def get_existing_skus():
    """取得 Shopify 已存在的 SKU 列表（只回傳 SKU set，向下相容）"""
    products_map = get_existing_products_map()
    return set(products_map.keys())

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
    """只取得特定 Collection 內的商品，回傳 {sku: product_id} 字典"""
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


def get_or_create_collection(collection_title="The maple mania 楓糖男孩"):
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
    
    # 不存在則建立
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
    """發布到所有銷售渠道（使用 GraphQL）"""
    print(f"[發布] 正在發布商品 {product_id} 到所有渠道...")
    
    graphql_url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }
    
    # 先用 GraphQL 查詢所有可發布的渠道
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
    
    # 過濾出唯一的渠道（去重）
    seen_names = set()
    unique_publications = []
    for pub in publications:
        name = pub['node']['name']
        if name not in seen_names:
            seen_names.add(name)
            unique_publications.append(pub['node'])
    
    print(f"[發布] 找到 {len(unique_publications)} 個唯一銷售渠道: {[p['name'] for p in unique_publications]}")
    
    # 建立發布請求
    publication_inputs = [{"publicationId": pub['id']} for pub in unique_publications]
    
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount {
            count
          }
          ... on Product {
            publishedOnCurrentPublication
          }
        }
        shop {
          publicationCount
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
        publishable = publishable_publish.get('publishable') or {}
        available_count_obj = publishable.get('availablePublicationsCount') or {}
        available_count = available_count_obj.get('count', 0)
        
        if errors:
            real_errors = [e for e in errors if 'does not exist' not in e.get('message', '')]
            if real_errors:
                print(f"[發布] 錯誤: {real_errors}")
        
        print(f"[發布] 成功發布到 {available_count} 個渠道")
        return True
    else:
        print(f"[發布] GraphQL 請求失敗: {pub_response.status_code}")
        return False


def parse_size_weight(text):
    """解析尺寸和重量"""
    dimension = None
    weight_kg = None
    
    # 標準化文字 (全形轉半形)
    text = text.replace('×', 'x').replace('Ｘ', 'x').replace('ｘ', 'x')
    text = text.replace('ｍｍ', 'mm').replace('ｇ', 'g').replace('ｋｇ', 'kg')
    text = text.replace('Φ', 'x')  # 圓形直徑符號
    text = text.replace(',', '')  # 移除千分位逗號
    text = text.replace('（', '(').replace('）', ')')  # 全形括號轉半形
    
    # 解析尺寸 (支援多種格式)
    dim_patterns = [
        # W277× D258× H48(mm) 格式
        r'W\s*(\d+(?:\.\d+)?)\s*[xX×]\s*D\s*(\d+(?:\.\d+)?)\s*[xX×]\s*H\s*(\d+(?:\.\d+)?)',
        # 標準立方體格式: 283x205x58mm
        r'(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:\(?\s*mm\s*\)?)?',
        r'(\d+)\s*[xX×]\s*(\d+)\s*[xX×]\s*(\d+)',
    ]
    
    for pattern in dim_patterns:
        dim_match = re.search(pattern, text, re.IGNORECASE)
        if dim_match:
            l, w, h = float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))
            volume_weight = (l * w * h) / 6000000
            volume_weight = round(volume_weight, 2)
            dimension = {"l": l, "w": w, "h": h, "volume_weight": volume_weight}
            print(f"[parse_size_weight] 尺寸: {l}x{w}x{h}mm -> 材積重量: {volume_weight}kg")
            break
    
    # 解析重量 (kg 或 g)
    weight_kg_match = re.search(r'(\d+(?:\.\d+)?)\s*kg', text, re.IGNORECASE)
    weight_g_match = re.search(r'(\d+(?:\.\d+)?)\s*g(?![\w])', text)
    
    if weight_kg_match:
        weight_kg = float(weight_kg_match.group(1))
        print(f"[parse_size_weight] 實際重量: {weight_kg}kg")
    elif weight_g_match:
        weight_kg = float(weight_g_match.group(1)) / 1000
        print(f"[parse_size_weight] 實際重量: {weight_g_match.group(1)}g = {weight_kg}kg")
    
    # 取較大值，如果沒有實際重量則用材積重量
    final_weight = 0
    if dimension and weight_kg:
        final_weight = max(dimension.get('volume_weight', 0), weight_kg)
        print(f"[parse_size_weight] 取較大值: max({dimension.get('volume_weight', 0)}, {weight_kg}) = {final_weight}kg")
    elif dimension:
        # 沒有實際重量，使用材積重量
        final_weight = dimension.get('volume_weight', 0)
        print(f"[parse_size_weight] 無實際重量，使用材積重量: {final_weight}kg")
    elif weight_kg:
        final_weight = weight_kg
    
    return {
        "dimension": dimension,
        "actual_weight": weight_kg,
        "final_weight": round(final_weight, 2)
    }


def scrape_product_list():
    """爬取商品列表（純 requests 版本）"""
    products = []
    seen_skus = set()
    
    for page_url in LIST_PAGES:
        print(f"[INFO] 正在載入頁面: {page_url}")
        
        try:
            response = session.get(page_url, timeout=30)
            if response.status_code != 200:
                print(f"[ERROR] 無法載入頁面: {page_url} (HTTP {response.status_code})")
                continue
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 找所有商品連結 /shop/g/g{sku}/
            product_links = soup.select('a[href*="/shop/g/g"]')
            print(f"[INFO] 頁面找到 {len(product_links)} 個連結")
            
            for link in product_links:
                try:
                    href = link.get('href', '')
                    if not href:
                        continue
                    
                    # 從 URL 提取 SKU
                    sku_match = re.search(r'/shop/g/g([^/]+)/', href)
                    if not sku_match:
                        continue
                    
                    sku = sku_match.group(1)
                    
                    # 跳過已處理的
                    if sku in seen_skus:
                        continue
                    seen_skus.add(sku)
                    
                    # 嘗試從列表頁面取得價格
                    price = 0
                    
                    # 找到父元素取得價格
                    parent = link.find_parent(['dl', 'div', 'li'])
                    if parent:
                        parent_text = parent.get_text()
                        
                        # 檢查是否為點數商品
                        if 'ポイント' in parent_text and '円' not in parent_text:
                            print(f"[跳過] 點數商品: {sku}")
                            continue
                        
                        # 提取價格
                        price_match = re.search(r'([\d,]+)円', parent_text)
                        if price_match:
                            price = int(price_match.group(1).replace(',', ''))
                        
                        # 檢查商品名稱是否包含「お急ぎ便」
                        if 'お急ぎ便' in parent_text:
                            print(f"[跳過] お急ぎ便商品: {sku}")
                            continue
                    
                    # 檢查最低價格
                    if price > 0 and price < MIN_PRICE:
                        print(f"[跳過] 價格過低: {sku} (¥{price})")
                        continue
                    
                    full_url = urljoin(BASE_URL, href)
                    products.append({
                        'url': full_url,
                        'sku': sku,
                        'list_price': price
                    })
                    print(f"[收集] {sku} - ¥{price}")
                    
                except Exception as e:
                    continue
            
            time.sleep(1)
            
        except Exception as e:
            print(f"[ERROR] 載入頁面失敗: {page_url} - {e}")
            continue
    
    print(f"[INFO] 共收集 {len(products)} 個商品")
    return products


def scrape_product_detail(url, max_retries=3):
    """爬取單一商品詳細資訊（純 requests 版本）"""
    product = {
        'url': url,
        'title': '',
        'price': 0,
        'description': '',
        'size_weight_text': '',
        'weight': 0,
        'images': [],
        'sku': '',
        'is_points': False
    }
    
    sku_match = re.search(r'/shop/g/g([^/]+)/', url)
    if sku_match:
        product['sku'] = sku_match.group(1)
    
    for attempt in range(max_retries):
        try:
            print(f"[載入] {url} (嘗試 {attempt+1}/{max_retries})")
            
            response = session.get(url, timeout=30)
            if response.status_code != 200:
                print(f"[ERROR] HTTP {response.status_code}")
                continue
            
            soup = BeautifulSoup(response.text, 'html.parser')
            page_text = soup.get_text()
            
            # 檢查是否為點數商品
            if 'ポイント' in page_text and re.search(r'\d+ポイント', page_text):
                if not re.search(r'[\d,]+円', page_text):
                    product['is_points'] = True
                    print(f"[跳過] 點數商品")
                    return product
            
            # 商品名稱
            title_selectors = ['h1.goods-name', 'h1[class*="goods"]', '.goods-detail h1', 'h1']
            for sel in title_selectors:
                title_el = soup.select_one(sel)
                if title_el:
                    title_text = title_el.get_text(strip=True)
                    if title_text and len(title_text) > 2:
                        product['title'] = title_text
                        print(f"[標題] {title_text}")
                        break
            
            # 價格
            price_selectors = ['.block-goods-price--price', '.js-enhanced-ecommerce-goods-price', '.price']
            for sel in price_selectors:
                price_el = soup.select_one(sel)
                if price_el:
                    price_text = price_el.get_text()
                    price_match = re.search(r'([\d,]+)', price_text)
                    if price_match:
                        product['price'] = int(price_match.group(1).replace(',', ''))
                        print(f"[價格] ¥{product['price']}")
                        break
            
            # 從頁面文字提取價格（備用）
            if not product['price']:
                price_match = re.search(r'([\d,]+)\s*円', page_text)
                if price_match:
                    product['price'] = int(price_match.group(1).replace(',', ''))
                    print(f"[價格] ¥{product['price']} (從頁面文字)")
            
            # 商品說明
            desc_selectors = ['.goods-description', '.item-description', '.product-description']
            for sel in desc_selectors:
                desc_el = soup.select_one(sel)
                if desc_el:
                    product['description'] = str(desc_el)
                    print(f"[描述] 已取得")
                    break
            
            # 重量和尺寸
            for dl in soup.select('dl'):
                dl_text = dl.get_text()
                if '箱サイズ' in dl_text or 'サイズ' in dl_text:
                    dd_el = dl.select_one('dd')
                    if dd_el:
                        dd_text = dd_el.get_text()
                        print(f"[尺寸] 找到: {dd_text}")
                        product['size_weight_text'] = dd_text
                        weight_info = parse_size_weight(dd_text)
                        if weight_info['final_weight'] > 0:
                            product['weight'] = weight_info['final_weight']
                            print(f"[重量] {product['weight']}kg")
                            break
            
            # 從整個頁面找尺寸格式（備用）
            if product['weight'] == 0:
                size_match = re.search(r'W\s*(\d+)\s*[×xX]\s*D\s*(\d+)\s*[×xX]\s*H\s*(\d+)', page_text)
                if size_match:
                    l, w, h = float(size_match.group(1)), float(size_match.group(2)), float(size_match.group(3))
                    product['weight'] = round((l * w * h) / 6000000, 2)
                    print(f"[重量] 材積重量: {product['weight']}kg")
            
            # 找純重量（備用）
            if product['weight'] == 0:
                weight_match = re.search(r'(\d+(?:,\d+)?)\s*[gG](?!ift)', page_text)
                if weight_match:
                    weight_str = weight_match.group(1).replace(',', '')
                    product['weight'] = round(float(weight_str) / 1000, 2)
                    print(f"[重量] {product['weight']}kg")
            
            # 預設重量
            if product['weight'] == 0:
                product['weight'] = 0.5
                print(f"[重量] 使用預設: 0.5kg")
            
            # 圖片
            images = []
            for img in soup.select('img[src*="/img/goods/"]'):
                src = img.get('src') or img.get('data-src')
                if src:
                    src = src.replace('/S/', '/L/').replace('/M/', '/L/')
                    if src.startswith('//'):
                        src = 'https:' + src
                    elif not src.startswith('http'):
                        src = urljoin(BASE_URL, src)
                    if src not in images:
                        images.append(src)
            
            # OG image 備用
            if not images:
                og_img = soup.select_one('meta[property="og:image"]')
                if og_img:
                    src = og_img.get('content')
                    if src:
                        if not src.startswith('http'):
                            src = urljoin(BASE_URL, src)
                        images.append(src)
            
            product['images'] = images[:10]
            print(f"[圖片] 取得 {len(product['images'])} 張")
            
            return product
            
        except Exception as e:
            print(f"[ERROR] 爬取失敗 (嘗試 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(3)
    
    return product


def upload_to_shopify(product, collection_id=None):
    """上傳商品到 Shopify"""
    
    # 翻譯商品名稱和說明
    original_title = product['title']
    
    print(f"[翻譯] 正在翻譯: {original_title[:30]}...")
    translated = translate_with_chatgpt(original_title, product.get('description', ''))
    
    if translated['success']:
        print(f"[翻譯成功] {translated['title'][:30]}...")
    else:
        print(f"[翻譯失敗] 使用原文")
    
    # 計算售價
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
                'filename': f"maple_mania_{product['sku']}_{idx+1}.jpg"
            })
            print(f"[圖片] ✓ 下載成功 ({idx+1}/{len(img_urls)})")
        else:
            print(f"[圖片] ✗ 下載失敗 ({idx+1}/{len(img_urls)})")
        
        time.sleep(0.5)  # 避免請求太快
    
    print(f"[圖片] 成功下載 {len(images_base64)}/{len(img_urls)} 張圖片")
    
    # 建立商品資料
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': 'The maple mania 楓糖男孩',
            'product_type': 'クッキー・洋菓子',
            'status': 'active',
            'published': True,
            'variants': [{
                'sku': product['sku'],
                'price': f"{selling_price:.2f}",
                'weight': product.get('weight', 0),
                'weight_unit': 'kg',
                'inventory_management': None,  # 不追蹤庫存
                'inventory_policy': 'continue',  # 允許超賣
                'requires_shipping': True
            }],
            'images': images_base64,
            'tags': 'The maple mania, 楓糖男孩, メープルマニア, 日本, 東京, 伴手禮, 東京土産, 日本代購, 楓糖餅乾',
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
    
    # 發送請求
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
        
        # 檢查實際建立了幾張圖片
        created_images = created_product.get('images', [])
        print(f"[DEBUG] 商品建立成功: ID={product_id}")
        print(f"[DEBUG] Shopify 實際建立圖片: {len(created_images)}/{len(images_base64)} 張")
        
        # 更新 variant 的 cost (成本價)
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
        
        # 加入 Collection
        if collection_id:
            add_product_to_collection(product_id, collection_id)
        
        # 發布到所有渠道
        publish_to_all_channels(product_id)
        
        return {'success': True, 'product': created_product, 'translated': translated, 'selling_price': selling_price, 'cost': cost}
    else:
        print(f"[ERROR] Shopify 錯誤: {response.text}")
        return {'success': False, 'error': response.text}


# ========== Flask 路由 ==========

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
            "skipped_low_price": 0,
            "skipped_points": 0,
            "skipped_exists": 0,
            "filtered_by_price": 0,
            "deleted": 0
        }
        
        # 1. 取得或建立 Collection
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("The maple mania 楓糖男孩")
        print(f"[INFO] Collection ID: {collection_id}")
        
        # 2. 取得 Collection 內的商品（只檢查這個 Collection）
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        existing_skus = set(collection_products_map.keys())
        print(f"[INFO] Collection 內有 {len(existing_skus)} 個商品")
        
        # 3. 爬取商品列表
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        # 取得官網所有 SKU
        website_skus = set(item['sku'] for item in product_list)
        print(f"[INFO] 官網 SKU 列表: {len(website_skus)} 個")
        
        # 4. 逐一處理商品
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理中: {item['sku']}"
            
            # 檢查是否已存在
            if item['sku'] in existing_skus:
                print(f"[跳過] 已存在: {item['sku']}")
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                continue
            
            # 爬取詳細資訊
            print(f"[爬取] ({idx+1}/{len(product_list)}) {item['url']}")
            product = scrape_product_detail(item['url'])
            
            # 檢查是否為點數商品
            if product.get('is_points'):
                print(f"[跳過] 點數商品: {product.get('sku', item['sku'])}")
                scrape_status['skipped_points'] += 1
                scrape_status['skipped'] += 1
                continue
            
            # 檢查是否為「お急ぎ便」商品
            if 'お急ぎ便' in product.get('title', ''):
                print(f"[跳過] お急ぎ便商品: {product.get('title', item['sku'])}")
                scrape_status['skipped'] += 1
                continue
            
            # 檢查價格門檻
            if product.get('price', 0) < MIN_PRICE:
                print(f"[跳過] 價格過低: {product.get('title', item['sku'])} (¥{product.get('price', 0)})")
                scrape_status['skipped_low_price'] += 1
                scrape_status['filtered_by_price'] += 1
                scrape_status['skipped'] += 1
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
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False


@app.route('/api/test-shopify')
def test_shopify():
    """測試 Shopify 連線"""
    if not load_shopify_token():
        return jsonify({'success': False, 'error': '未設定環境變數'})
    
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
    test_url = "https://sucreyshopping.jp/shop/g/gtmm01107/"
    product = scrape_product_detail(test_url)
    
    if product.get('price') and product.get('weight'):
        product['selling_price'] = calculate_selling_price(product['price'], product['weight'])
    
    return jsonify(product)


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
    <title>The Maple Mania 楓糖男孩 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #8B4513; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #8B4513; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #6B3510; }}
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
    <h1>🍁 The Maple Mania 楓糖男孩 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 sucreyshopping.jp The Maple Mania 商品並上架到 Shopify</p>
        <p style="color: #666; font-size: 14px;">※ 成本價低於 ¥1000、點數商品、お急ぎ便商品將自動跳過</p>
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
                document.getElementById('filteredCount').textContent = data.filtered_by_price || data.skipped_low_price || 0;
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
        return jsonify({'success': False, 'error': '未設定環境變數'})
    
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'success': True, 'message': '開始爬取'})


if __name__ == '__main__':
    print("=" * 50)
    print("The Maple Mania 楓糖男孩 爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

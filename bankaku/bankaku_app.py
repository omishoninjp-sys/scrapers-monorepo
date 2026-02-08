"""
坂角總本舖商品爬蟲 + Shopify 上架工具 v2.1
功能：
1. 爬取 bankaku.co.jp 指定分類的所有商品（ゆかり + ゆかり黄金缶）
2. 過濾無庫存商品
3. 計算材積重量 vs 實際重量，取大值
4. 上架到 Shopify（不重複上架）
5. 原價寫入成本價（Cost）
6. 【v2.1】翻譯保護機制 - 翻譯失敗不上架、預檢、連續失敗自動停止
7. 【v2.1】日文商品掃描 - 找出並修復未翻譯的商品
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

app = Flask(__name__)

# ========== 設定 ==========
SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""

BASE_URL = "https://www.bankaku.co.jp"
CATEGORY_URLS = [
    "https://www.bankaku.co.jp/shop/c/c1010/",
    "https://www.bankaku.co.jp/shop/c/c1020/",
]

MIN_COST_THRESHOLD = 1000
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7,zh;q=0.6',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Referer': 'https://www.bankaku.co.jp/',
}

session = requests.Session()
session.headers.update(BROWSER_HEADERS)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

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

def is_japanese_text(text):
    """判斷文字是否包含日文（平假名、片假名）"""
    if not text:
        return False
    check_text = text.strip()
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

def calculate_selling_price(cost, weight):
    if not cost or cost <= 0:
        return 0
    shipping_cost = weight * 1250 if weight else 0
    price = (cost + shipping_cost) / 0.7
    return round(price)

def translate_with_chatgpt(title, description):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本食品商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{description}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "翻譯後的商品名稱（繁體中文，簡潔有力）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示）",
    "page_title": "SEO 頁面標題（繁體中文，包含品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

注意：
1. 這是日本坂角總本舖的傳統海老煎餅（蝦餅）
2. ゆかり 是產品名稱，可保留日文或翻譯為「緣」
3. 翻譯要自然流暢，不要生硬
4. SEO 內容要包含：坂角總本舖、日本、海老煎餅、伴手禮等關鍵字
5. 只回傳 JSON，不要其他文字"""

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
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家，專門處理日本傳統食品的中文翻譯。"},
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
            return {
                'success': True,
                'title': translated.get('title', title),
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
                'title': title,
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'error': str(e),
            'title': title,
            'description': description,
            'page_title': '',
            'meta_description': ''
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
    "filtered_by_price": 0,
    "deleted": 0,
    "translation_failed": 0,
    "translation_stopped": False
}

def get_existing_skus():
    products_map = get_existing_products_map()
    return set(products_map.keys())

def get_existing_products_map():
    products_map = {}
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

def parse_dimension_weight(soup):
    dimension = None
    weight = None
    text = soup.get_text()
    
    dim_match = re.search(r'縦\s*(\d+(?:\.\d+)?)\s*[×xX]\s*横\s*(\d+(?:\.\d+)?)\s*[×xX]\s*高さ\s*(\d+(?:\.\d+)?)\s*cm', text)
    if dim_match:
        h, w, d = float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))
        dimension_weight = round((h * w * d) / 6000, 2)
        dimension = {"h": h, "w": w, "d": d, "volume_weight": dimension_weight}
    
    weight_match = re.search(r'重量[：:]\s*(\d+(?:\.\d+)?)\s*(?:kg|g)', text, re.IGNORECASE)
    if weight_match:
        weight_str = weight_match.group(1)
        unit = weight_match.group(0)
        if 'kg' in unit.lower():
            weight = float(weight_str)
        else:
            weight = float(weight_str) / 1000
    
    final_weight = 0
    if dimension and weight:
        final_weight = max(dimension['volume_weight'], weight)
    elif dimension:
        final_weight = dimension['volume_weight']
    elif weight:
        final_weight = weight
    else:
        content_match = re.search(r'(\d+)枚', text)
        if content_match:
            count = int(content_match.group(1))
            estimated = (count * 7 + 100) / 1000
            final_weight = round(estimated, 2)
    
    return {
        "dimension": dimension,
        "actual_weight": weight,
        "final_weight": round(final_weight, 2)
    }

def scrape_product_list(category_urls):
    products = []
    seen_skus = set()
    
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    for category_url in category_urls:
        page = 1
        max_pages = 10
        
        while page <= max_pages:
            if page == 1:
                url = category_url
            else:
                base = category_url.rstrip('/')
                url = f"{base}_p{page}/"
            
            print(f"[爬取] {url}")
            
            try:
                response = session.get(url, timeout=30)
                response.encoding = 'utf-8'
                
                if response.status_code != 200:
                    break
                
                if page > 1 and '_p' not in response.url:
                    break
                
                soup = BeautifulSoup(response.text, 'html.parser')
                all_links = soup.find_all('a', href=re.compile(r'/shop/g/g[A-Za-z0-9]+/'))
                
                new_count = 0
                seen_skus_this_page = set()
                
                for link in all_links:
                    href = link.get('href', '')
                    sku_match = re.search(r'/g/g([A-Za-z0-9]+)/', href)
                    if sku_match:
                        sku = sku_match.group(1)
                        
                        if sku in seen_skus_this_page:
                            continue
                        seen_skus_this_page.add(sku)
                        
                        if sku not in seen_skus:
                            full_url = urljoin(BASE_URL, href)
                            products.append({'url': full_url, 'sku': sku})
                            seen_skus.add(sku)
                            new_count += 1
                
                print(f"[進度] 新增 {new_count} 個商品，累計 {len(products)} 個")
                
                if new_count == 0:
                    break
                
                page += 1
                time.sleep(0.5)
                
            except Exception as e:
                print(f"[錯誤] 爬取失敗: {e}")
                break
    
    return products

def scrape_product_detail(url):
    try:
        response = session.get(url, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        title = ""
        title_elem = soup.select_one('h1')
        if title_elem:
            title = title_elem.get_text(strip=True)
        if not title:
            title_tag = soup.select_one('title')
            if title_tag:
                title = title_tag.get_text(strip=True).split(':')[0].split('|')[0].strip()
        
        description = ""
        for selector in ['.block-goods-comment', '.item-description', '.product-description']:
            desc_elem = soup.select_one(selector)
            if desc_elem:
                description = desc_elem.get_text(strip=True)
                break
        
        if not description:
            h1 = soup.select_one('h1')
            if h1:
                next_elem = h1.find_next_sibling()
                if next_elem:
                    description = next_elem.get_text(strip=True)[:200]
        
        price = 0
        price_text = soup.get_text()
        price_match = re.search(r'([\d,]+)円\s*\(?税込\)?', price_text)
        if price_match:
            price = int(price_match.group(1).replace(',', ''))
        
        sku = ""
        url_sku = re.search(r'/g/g([A-Za-z0-9]+)/', url)
        if url_sku:
            sku = url_sku.group(1)
        
        in_stock = True
        page_text = soup.get_text()
        if any(kw in page_text for kw in ['在庫がありません', '在庫切れ', '品切れ', 'SOLD OUT']):
            in_stock = False
        
        weight_info = parse_dimension_weight(soup)
        
        images = []
        seen_images = set()
        
        for img_link in soup.select('a[href*="/img/goods/"]'):
            href = img_link.get('href', '')
            if href and '/img/goods/' in href:
                full_src = urljoin(BASE_URL, href)
                if full_src not in seen_images:
                    seen_images.add(full_src)
                    images.append(full_src)
        
        if not images:
            for img in soup.select('img[src*="/img/goods/"]'):
                src = img.get('src', '')
                if src and '/img/goods/' in src and 'lazyload' not in src:
                    full_src = urljoin(BASE_URL, src)
                    if full_src not in seen_images:
                        seen_images.add(full_src)
                        images.append(full_src)
        
        specs = {}
        content_match = re.search(r'内容量[：:\s]+([^\n]+)', page_text)
        if content_match:
            specs['content'] = content_match.group(1).strip()
        
        return {
            'url': url,
            'sku': sku,
            'title': title,
            'price': price,
            'in_stock': in_stock,
            'description': description,
            'weight': weight_info['final_weight'],
            'weight_info': weight_info,
            'images': images[:10],
            'specs': specs
        }
        
    except Exception as e:
        print(f"[錯誤] 爬取商品失敗 {url}: {e}")
        return None

def get_or_create_collection(collection_title="坂角總本舖"):
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

def upload_to_shopify(product, collection_id=None):
    """上傳商品到 Shopify（含翻譯保護）"""
    
    print(f"[翻譯] 正在翻譯: {product['title'][:30]}...")
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    
    # ★ 翻譯保護：翻譯失敗就不上架
    if not translated['success']:
        print(f"[跳過-翻譯失敗] {product['sku']}: {translated.get('error', '未知錯誤')}")
        return {'success': False, 'error': 'translation_failed', 'translated': translated}
    
    print(f"[翻譯成功] {translated['title'][:30]}...")
    
    cost = product['price']
    weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    
    images = [{'src': img_url, 'position': idx + 1} for idx, img_url in enumerate(product.get('images', []))]
    
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': '坂角總本舖',
            'product_type': '海老煎餅',
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
            'images': images,
            'tags': '坂角總本舖, 日本, 海老煎餅, えびせんべい, ゆかり, 伴手禮, 日本零食',
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
    <title>坂角總本舖 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #D4AF37; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #D4AF37; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; margin-bottom: 10px; text-decoration: none; display: inline-block; }}
        .btn:hover {{ background: #B8972E; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-secondary:hover {{ background: #2980b9; }}
        .btn-success {{ background: #27ae60; }}
        .btn-success:hover {{ background: #219a52; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #D4AF37, #F0D078); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 80px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #D4AF37; }}
        .stat-label {{ font-size: 11px; color: #666; margin-top: 5px; }}
        .nav {{ margin-bottom: 20px; }}
        .nav a {{ margin-right: 15px; color: #D4AF37; text-decoration: none; font-weight: bold; }}
        .nav a:hover {{ text-decoration: underline; }}
        .alert {{ padding: 12px 16px; border-radius: 5px; margin-bottom: 15px; }}
        .alert-danger {{ background: #fee; border: 1px solid #fcc; color: #c0392b; }}
    </style>
</head>
<body>
    <div class="nav">
        <a href="/">🏠 首頁</a>
        <a href="/japanese-scan">🇯🇵 日文商品掃描</a>
    </div>
    
    <h1>🦐 坂角總本舖 爬蟲工具 <small style="font-size: 14px; color: #999;">v2.1</small></h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: <span style="color: {token_color};">{token_status}</span></p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
        <button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
        <a href="/japanese-scan" class="btn btn-success">🇯🇵 掃描日文商品</a>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 bankaku.co.jp 全站商品並上架到 Shopify</p>
        <p style="color: #666; font-size: 14px;">
            ※ 成本價低於 ¥1000 的商品將自動跳過<br>
            ※ <b style="color: #e74c3c;">翻譯保護</b> - 翻譯失敗不上架，連續失敗 {MAX_CONSECUTIVE_TRANSLATION_FAILURES} 次自動停止
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
                    <div class="stat-number" id="translationFailedCount" style="color: #e74c3c;">0</div>
                    <div class="stat-label">翻譯失敗</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="deletedCount" style="color: #e67e22;">0</div>
                    <div class="stat-label">設為草稿</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="errorCount" style="color: #e74c3c;">0</div>
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
                if (data.success) {{
                    log('✓ 連線成功！商店: ' + data.shop.name, 'success');
                }} else {{
                    log('✗ 連線失敗: ' + data.error, 'error');
                }}
            }} catch (e) {{
                log('✗ 請求失敗: ' + e.message, 'error');
            }}
        }}
        
        async function testTranslate() {{
            log('測試翻譯功能...');
            try {{
                const res = await fetch('/api/test-translate');
                const data = await res.json();
                if (data.error) {{
                    log('✗ 翻譯失敗: ' + data.error, 'error');
                }} else if (data.success) {{
                    log('✓ 翻譯成功！結果: ' + data.title, 'success');
                }} else {{
                    log('✗ 翻譯失敗（success=false）', 'error');
                }}
            }} catch (e) {{
                log('✗ 請求失敗: ' + e.message, 'error');
            }}
        }}
        
        async function startScrape() {{
            clearLog();
            log('開始爬取流程...');
            document.getElementById('startBtn').disabled = true;
            document.getElementById('progressSection').style.display = 'block';
            document.getElementById('translationAlert').style.display = 'none';
            
            try {{
                const res = await fetch('/api/start', {{ method: 'POST' }});
                const data = await res.json();
                if (data.error) {{
                    log('✗ 啟動失敗: ' + data.error, 'error');
                    document.getElementById('startBtn').disabled = false;
                    return;
                }}
                log('✓ 爬取任務已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            }} catch (e) {{
                log('✗ 請求失敗: ' + e.message, 'error');
                document.getElementById('startBtn').disabled = false;
            }}
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
                
                if (data.translation_stopped) {{
                    document.getElementById('translationAlert').style.display = 'block';
                }}
                
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval);
                    document.getElementById('startBtn').disabled = false;
                    if (data.translation_stopped) {{
                        log('⚠️ 爬取因翻譯連續失敗而自動停止', 'error');
                    }} else {{
                        log('========== 爬取完成 ==========', 'success');
                    }}
                }}
            }} catch (e) {{
                console.error('Poll error:', e);
            }}
        }}
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
    <title>日文商品掃描 - 坂角總本舖</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; border-bottom: 2px solid #27ae60; padding-bottom: 10px; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn { background: #D4AF37; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 14px; margin-right: 10px; margin-bottom: 10px; }
        .btn:hover { background: #B8972E; }
        .btn:disabled { background: #ccc; cursor: not-allowed; }
        .btn-danger { background: #e74c3c; }
        .btn-danger:hover { background: #c0392b; }
        .btn-success { background: #27ae60; }
        .btn-success:hover { background: #219a52; }
        .btn-secondary { background: #3498db; }
        .btn-sm { padding: 5px 10px; font-size: 12px; }
        .nav { margin-bottom: 20px; }
        .nav a { margin-right: 15px; color: #D4AF37; text-decoration: none; font-weight: bold; }
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
        .product-item .actions { display: flex; gap: 5px; flex-wrap: wrap; }
        .no-image { width: 60px; height: 60px; background: #eee; display: flex; align-items: center; justify-content: center; border-radius: 4px; color: #999; font-size: 10px; }
        .retranslate-status { font-size: 12px; margin-top: 5px; }
        .action-bar { position: sticky; top: 0; background: white; padding: 15px; margin: -20px -20px 20px -20px; border-bottom: 1px solid #ddd; z-index: 100; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
    </style>
</head>
<body>
    <div class="nav">
        <a href="/">🏠 首頁</a>
        <a href="/japanese-scan">🇯🇵 日文商品掃描</a>
    </div>
    
    <h1>🇯🇵 日文商品掃描 - 坂角總本舖</h1>
    
    <div class="card">
        <p>掃描 Shopify 商店中坂角總本舖的日文（未翻譯）商品，並提供重新翻譯功能。</p>
        <button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button>
        <span id="scanStatus"></span>
    </div>
    
    <div class="stats" id="statsSection" style="display: none;">
        <div class="stat">
            <div class="stat-number" id="totalProducts" style="color: #3498db;">0</div>
            <div class="stat-label">坂角總本舖商品數</div>
        </div>
        <div class="stat">
            <div class="stat-number" id="japaneseCount" style="color: #e74c3c;">0</div>
            <div class="stat-label">日文商品</div>
        </div>
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
            } catch (e) {
                alert('請求失敗: ' + e.message);
            } finally {
                document.getElementById('scanBtn').disabled = false;
            }
        }
        
        function renderResults(products) {
            const container = document.getElementById('results');
            if (products.length === 0) {
                container.innerHTML = '<p style="text-align: center; color: #27ae60; font-size: 18px;">✅ 太棒了！沒有發現日文商品。</p>';
                return;
            }
            let html = '';
            products.forEach((item) => {
                const imageHtml = item.image 
                    ? `<img src="${item.image}" alt="${item.title}">`
                    : `<div class="no-image">無圖片</div>`;
                html += `<div class="product-item" id="product-${item.id}">
                    ${imageHtml}
                    <div class="info">
                        <div class="title">${item.title}</div>
                        <div class="meta">SKU: ${item.sku || '無'} | 價格: ¥${item.price} | 狀態: ${item.status}</div>
                        <div class="retranslate-status" id="status-${item.id}"></div>
                    </div>
                    <div class="actions">
                        <button class="btn btn-success btn-sm" onclick="retranslateOne('${item.id}')" id="retranslate-btn-${item.id}">🔄 翻譯</button>
                        <button class="btn btn-danger btn-sm" onclick="deleteOne('${item.id}')" id="delete-btn-${item.id}">🗑️ 刪除</button>
                    </div>
                </div>`;
            });
            container.innerHTML = html;
        }
        
        async function retranslateOne(productId) {
            const btn = document.getElementById(`retranslate-btn-${productId}`);
            const statusEl = document.getElementById(`status-${productId}`);
            btn.disabled = true; btn.textContent = '翻譯中...';
            statusEl.innerHTML = '<span style="color: #f39c12;">⏳ 翻譯中...</span>';
            try {
                const res = await fetch('/api/retranslate-product', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ product_id: productId })
                });
                const data = await res.json();
                if (data.success) {
                    statusEl.innerHTML = `<span style="color: #27ae60;">✅ 已翻譯為: ${data.new_title}</span>`;
                    document.querySelector(`#product-${productId} .title`).textContent = data.new_title;
                    document.querySelector(`#product-${productId} .title`).style.color = '#27ae60';
                    btn.textContent = '✓ 完成';
                } else {
                    statusEl.innerHTML = `<span style="color: #e74c3c;">❌ 失敗: ${data.error}</span>`;
                    btn.disabled = false; btn.textContent = '🔄 重試';
                }
            } catch (e) {
                statusEl.innerHTML = `<span style="color: #e74c3c;">❌ 請求失敗: ${e.message}</span>`;
                btn.disabled = false; btn.textContent = '🔄 重試';
            }
        }
        
        async function deleteOne(productId) {
            if (!confirm('確定要刪除這個商品嗎？')) return;
            const btn = document.getElementById(`delete-btn-${productId}`);
            btn.disabled = true;
            try {
                const res = await fetch('/api/delete-product', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ product_id: productId })
                });
                const data = await res.json();
                if (data.success) { document.getElementById(`product-${productId}`).remove(); }
                else { alert('刪除失敗'); btn.disabled = false; }
            } catch (e) { alert('請求失敗: ' + e.message); btn.disabled = false; }
        }
        
        async function retranslateAll() {
            if (!confirm(`確定要重新翻譯全部 ${japaneseProducts.length} 個日文商品嗎？`)) return;
            const btn = document.getElementById('retranslateAllBtn');
            btn.disabled = true; btn.textContent = '翻譯中...';
            let success = 0, fail = 0;
            for (let i = 0; i < japaneseProducts.length; i++) {
                const item = japaneseProducts[i];
                document.getElementById('progressText').textContent = `進度: ${i + 1}/${japaneseProducts.length}`;
                try {
                    const res = await fetch('/api/retranslate-product', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ product_id: item.id })
                    });
                    const data = await res.json();
                    const statusEl = document.getElementById(`status-${item.id}`);
                    if (data.success) {
                        success++;
                        if (statusEl) statusEl.innerHTML = `<span style="color: #27ae60;">✅ ${data.new_title}</span>`;
                        const titleEl = document.querySelector(`#product-${item.id} .title`);
                        if (titleEl) { titleEl.textContent = data.new_title; titleEl.style.color = '#27ae60'; }
                    } else {
                        fail++;
                        if (statusEl) statusEl.innerHTML = `<span style="color: #e74c3c;">❌ ${data.error}</span>`;
                        if (fail >= 3) { alert('翻譯連續失敗，已自動停止。'); break; }
                    }
                } catch (e) { fail++; }
                await new Promise(r => setTimeout(r, 1500));
            }
            alert(`翻譯完成！\\n成功: ${success}\\n失敗: ${fail}`);
            btn.textContent = '🔄 全部重新翻譯'; btn.disabled = false;
            document.getElementById('progressText').textContent = '';
        }
        
        async function deleteAllJapanese() {
            if (!confirm(`確定要刪除全部 ${japaneseProducts.length} 個日文商品嗎？此操作無法復原！`)) return;
            const btn = document.getElementById('deleteAllBtn');
            btn.disabled = true; btn.textContent = '刪除中...';
            let success = 0, fail = 0;
            for (let i = 0; i < japaneseProducts.length; i++) {
                const item = japaneseProducts[i];
                document.getElementById('progressText').textContent = `進度: ${i + 1}/${japaneseProducts.length}`;
                try {
                    const res = await fetch('/api/delete-product', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ product_id: item.id })
                    });
                    const data = await res.json();
                    if (data.success) { success++; const el = document.getElementById(`product-${item.id}`); if (el) el.remove(); }
                    else { fail++; }
                } catch (e) { fail++; }
                await new Promise(r => setTimeout(r, 300));
            }
            alert(`刪除完成！\\n成功: ${success}\\n失敗: ${fail}`);
            btn.textContent = '🗑️ 全部刪除'; btn.disabled = false;
            document.getElementById('progressText').textContent = '';
        }
    </script>
</body>
</html>'''

# ========== API 路由 ==========

@app.route('/api/scan-japanese')
def api_scan_japanese():
    """掃描日文商品 API（只掃坂角總本舖）"""
    if not load_shopify_token():
        return jsonify({'error': '未設定 Shopify Token'}), 400
    
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=坂角總本舖")
    
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
    """重新翻譯單一商品 API"""
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
        return jsonify({
            'success': True,
            'old_title': old_title,
            'new_title': translated['title'],
            'product_id': product_id
        })
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
    """測試翻譯功能"""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({'error': 'OPENAI_API_KEY 環境變數未設定', 'key_exists': False})
    
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    
    result = translate_with_chatgpt("ゆかり 8枚入", "坂角總本舖の伝統的な海老煎餅です")
    result['key_preview'] = key_preview
    result['key_length'] = len(api_key)
    
    return jsonify(result)

@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token():
        return jsonify({'error': '未找到 Token'}), 400
    
    response = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    
    if response.status_code == 200:
        return jsonify({'success': True, 'shop': response.json()['shop']})
    else:
        return jsonify({'success': False, 'error': response.text}), 400

@app.route('/api/start', methods=['POST'])
def start_scrape():
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'error': '爬取已在進行中'}), 400
    
    scrape_status = {
        "running": True, "progress": 0, "total": 0,
        "current_product": "正在取得商品列表...",
        "products": [], "errors": [],
        "uploaded": 0, "skipped": 0,
        "filtered_by_price": 0, "deleted": 0,
        "translation_failed": 0, "translation_stopped": False
    }
    
    if not load_shopify_token():
        scrape_status['running'] = False
        return jsonify({'error': '請先設定 Shopify Token'}), 400
    
    # ★ 預檢：開始前先測試翻譯功能
    scrape_status['current_product'] = "正在測試翻譯功能..."
    test_result = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test_result['success']:
        scrape_status['running'] = False
        scrape_status['translation_stopped'] = True
        error_msg = test_result.get('error', '未知錯誤')
        return jsonify({'error': f'翻譯功能異常，無法啟動爬取: {error_msg}'}), 400
    
    import threading
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'message': '開始爬取'})

def run_scrape():
    global scrape_status
    
    try:
        scrape_status['current_product'] = "正在檢查 Shopify 已有商品..."
        existing_products_map = get_existing_products_map()
        existing_skus = set(existing_products_map.keys())
        
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("坂角總本舖")
        
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        collection_skus = set(collection_products_map.keys())
        
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list(CATEGORY_URLS)
        scrape_status['total'] = len(product_list)
        
        website_skus = set(item['sku'] for item in product_list)
        
        consecutive_translation_failures = 0  # ★ 連續翻譯失敗計數器
        
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['sku']}"
            
            if item['sku'] in existing_skus:
                scrape_status['skipped'] += 1
                continue
            
            product = scrape_product_detail(item['url'])
            if not product:
                scrape_status['errors'].append(f"無法爬取: {item['url']}")
                continue
            
            if product['price'] < MIN_COST_THRESHOLD:
                scrape_status['filtered_by_price'] += 1
                continue
            
            if not product['in_stock']:
                scrape_status['skipped'] += 1
                continue
            
            result = upload_to_shopify(product, collection_id)
            
            if result['success']:
                existing_skus.add(product['sku'])
                scrape_status['uploaded'] += 1
                consecutive_translation_failures = 0  # ★ 成功就重置
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1
                consecutive_translation_failures += 1
                
                # ★ 連續翻譯失敗超過閾值，自動停止
                if consecutive_translation_failures >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append(
                        f"翻譯連續失敗 {consecutive_translation_failures} 次，自動停止爬取。"
                    )
                    break
            else:
                scrape_status['errors'].append(f"上傳失敗 {product['sku']}")
                consecutive_translation_failures = 0
            
            time.sleep(1)
        
        # 設為草稿（只有在非翻譯停止的情況下才執行）
        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "正在檢查已下架商品..."
            skus_to_draft = collection_skus - website_skus
            
            for sku in skus_to_draft:
                product_id = collection_products_map.get(sku)
                if product_id and set_product_to_draft(product_id):
                    scrape_status['deleted'] += 1
                time.sleep(0.3)
        
    except Exception as e:
        scrape_status['errors'].append(str(e))
    
    finally:
        scrape_status['running'] = False
        scrape_status['current_product'] = "完成" if not scrape_status['translation_stopped'] else "翻譯異常停止"

if __name__ == '__main__':
    print("=" * 50)
    print("坂角總本舖爬蟲工具 v2.1")
    print("新增功能：翻譯保護、日文商品掃描")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

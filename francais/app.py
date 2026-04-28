"""
Francais フランセ 商品爬蟲 + Shopify 上架工具 v2.2
功能：
1. 爬取 sucreyshopping.jp フランセ品牌所有商品
2. 計算材積重量 vs 實際重量，取大值
3. 上架到 Shopify（不重複上架）
4. 原價寫入成本價（Cost）
5. OpenAI 翻譯成繁體中文
6. 【v2.1】翻譯保護機制 - 翻譯失敗不上架、預檢、連續失敗自動停止
7. 【v2.1】日文商品掃描 - 找出並修復未翻譯的商品
8. 【v2.2】缺貨商品自動刪除 - 官網消失、缺貨、お急ぎ便皆直接刪除
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
LIST_BASE_URL = "https://sucreyshopping.jp/shop/c/c10/?brand=francais"
LIST_PAGE_URL_TEMPLATE = "https://sucreyshopping.jp/shop/c/c10_p{page}/?brand=francais"

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MIN_PRICE = 1000
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3
SHIPPING_HTML = '<div style="margin-top:24px;border-top:1px solid #e8eaf0;padding-top:20px;"><h2 style="font-size:16px;font-weight:700;color:#1a1a2e;border-bottom:2px solid #e8eaf0;padding-bottom:8px;margin:0 0 16px;">國際運費（空運・包稅）</h2><p style="margin:0 0 6px;font-size:13px;color:#444;">✓ 含關稅\u3000✓ 含台灣配送費\u3000✓ 只收實重\u3000✓ 無材積費</p><p style="margin:0 0 12px;font-size:13px;color:#444;">起運 1 kg，未滿 1 kg 以 1 kg 計算，每增加 0.5 kg 加收 ¥500。</p><table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;"><tbody><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">≦ 1.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,000 <span style="color:#888;font-weight:400;">≈ NT$200</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.1 ～ 1.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,500 <span style="color:#888;font-weight:400;">≈ NT$300</span></td></tr><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.6 ～ 2.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,000 <span style="color:#888;font-weight:400;">≈ NT$400</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.1 ～ 2.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,500 <span style="color:#888;font-weight:400;">≈ NT$500</span></td></tr><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.6 ～ 3.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥3,000 <span style="color:#888;font-weight:400;">≈ NT$600</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">每增加 0.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">+¥500\u3000<span style="color:#888;">+≈ NT$100</span></td></tr></tbody></table><p style="margin:0 0 28px;font-size:12px;color:#999;">NT$ 匯率僅供參考，實際以下單當日匯率為準。運費於商品到倉後出貨前確認重量後統一請款。</p></div>'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7',
    'Connection': 'keep-alive',
}

scrape_status = {
    "running": False, "progress": 0, "total": 0,
    "current_product": "", "products": [], "errors": [],
    "uploaded": 0, "skipped": 0, "skipped_exists": 0,
    "filtered_by_price": 0, "deleted": 0,
    "translation_failed": 0, "translation_stopped": False
}


def load_shopify_token():
    global SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP
    env_token = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
    env_shop = os.environ.get('SHOPIFY_SHOP', '')
    if env_token and env_shop:
        SHOPIFY_ACCESS_TOKEN = env_token
        SHOPIFY_SHOP = env_shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
        return True
    token_file = "shopify_token.json"
    if os.path.exists(token_file):
        with open(token_file, 'r') as f:
            data = json.load(f)
            SHOPIFY_ACCESS_TOKEN = data.get('access_token', '')
            shop = data.get('shop', '')
            if shop:
                SHOPIFY_SHOP = shop.replace('https://', '').replace('http://', '').replace('.myshopify.com', '').strip('/')
            return True
    return False


def get_shopify_headers():
    return {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}


def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


def normalize_sku(sku):
    if not sku:
        return ""
    return sku.strip().lower()


def is_japanese_text(text):
    if not text:
        return False
    check_text = text.replace('Francais', '').strip()
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


def translate_with_chatgpt(title, description, retry=False):
    clean_description = clean_html_for_translation(description)
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。將以下日本商品資訊翻譯成繁體中文並優化 SEO。

商品名稱：{title}
商品說明：{clean_description[:1500]}

只回傳此 JSON 格式，不加 markdown、不加任何其他文字：
{"title":"翻譯後的商品名稱","description":"翻譯後的商品說明（HTML格式）","page_title":"SEO標題50字以內","meta_description":"SEO描述100字以內"}

規則：
1. 品牌背景：日本高級洋菓子品牌，主打千層派與精緻西式甜點
2. 標題開頭必須是「Francais」，後接繁體中文商品名，不得省略
3. 【強制禁止日文】所有輸出必須是繁體中文或英文，不可出現任何平假名或片假名
4. 詞彙對照：ミルフィユ→千層派；洋菓子→西式甜點；詰合せ→綜合禮盒；果実→水果；贅沢→奢華
5. SEO 關鍵字必須自然融入，包含：Francais、日本、千層派、西式甜點、伴手禮、送禮
6. 只回傳 JSON，不得有任何其他文字"""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。你的輸出必須完全使用繁體中文和英文，絕對禁止出現任何日文字元。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0, "max_tokens": 1000
            },
            timeout=60
        )
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content'].strip()
            if content.startswith('```'):
                content = content.split('\n', 1)[1]
            if content.endswith('```'):
                content = content.rsplit('```', 1)[0]
            content = content.strip()
            translated = json.loads(content)
            trans_title = translated.get('title', title)
            if not trans_title.startswith('Francais'):
                trans_title = f"Francais {trans_title}"
            return {
                'success': True, 'title': trans_title,
                'description': translated.get('description', description),
                'page_title': translated.get('page_title', ''),
                'meta_description': translated.get('meta_description', '')
            }
        else:
            error_msg = response.text[:200]
            print(f"[翻譯失敗] HTTP {response.status_code}: {error_msg}")
            return {'success': False, 'error': f"HTTP {response.status_code}: {error_msg}",
                    'title': f"Francais {title}", 'description': description, 'page_title': '', 'meta_description': ''}
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {'success': False, 'error': str(e),
                'title': f"Francais {title}", 'description': description, 'page_title': '', 'meta_description': ''}


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
                if 'png' in content_type: img_format = 'image/png'
                elif 'webp' in content_type: img_format = 'image/webp'
                elif 'gif' in content_type: img_format = 'image/gif'
                else: img_format = 'image/jpeg'
                return {'success': True, 'base64': base64.b64encode(response.content).decode('utf-8'), 'content_type': img_format}
        except Exception as e:
            print(f"[圖片下載] 第 {attempt+1} 次異常: {e}")
        time.sleep(1)
    return {'success': False}


def get_existing_products_map():
    products_map = {}
    url = shopify_api_url("products.json?limit=250")
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200: break
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    normalized = normalize_sku(sku)
                    products_map[normalized] = product_id
                    if sku != normalized:
                        products_map[sku] = product_id
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else: url = None
    return products_map


def get_collection_products_map(collection_id):
    products_map = {}
    if not collection_id: return products_map
    url = shopify_api_url(f"collections/{collection_id}/products.json?limit=250")
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200: break
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    products_map[normalize_sku(sku)] = product_id
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else: url = None
    return products_map


def set_product_to_draft(product_id):
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={"product": {"id": product_id, "status": "draft"}})
    return response.status_code == 200


def delete_product(product_id):
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.delete(url, headers=get_shopify_headers())
    return response.status_code == 200


def update_product(product_id, data):
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={"product": {"id": product_id, **data}})
    return response.status_code == 200, response


def get_or_create_collection(collection_title="Francais"):
    response = requests.get(shopify_api_url(f'custom_collections.json?title={collection_title}'), headers=get_shopify_headers())
    if response.status_code == 200:
        for col in response.json().get('custom_collections', []):
            if col['title'] == collection_title: return col['id']
    response = requests.post(shopify_api_url('custom_collections.json'), headers=get_shopify_headers(),
                             json={'custom_collection': {'title': collection_title, 'published': True}})
    if response.status_code == 201: return response.json()['custom_collection']['id']
    return None


def add_product_to_collection(product_id, collection_id):
    response = requests.post(shopify_api_url('collects.json'), headers=get_shopify_headers(),
                             json={'collect': {'product_id': product_id, 'collection_id': collection_id}})
    return response.status_code == 201


def publish_to_all_channels(product_id):
    graphql_url = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    query = """{ publications(first: 20) { edges { node { id name } } } }"""
    response = requests.post(graphql_url, headers=headers, json={'query': query})
    if response.status_code != 200: return False
    publications = response.json().get('data', {}).get('publications', {}).get('edges', [])
    seen = set()
    unique = []
    for pub in publications:
        name = pub['node']['name']
        if name not in seen: seen.add(name); unique.append(pub['node'])
    mutation = """mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) { userErrors { field message } } }"""
    variables = {"id": f"gid://shopify/Product/{product_id}", "input": [{"publicationId": p['id']} for p in unique]}
    requests.post(graphql_url, headers=headers, json={'query': mutation, 'variables': variables})
    return True


def parse_box_size(text):
    text = text.replace('×', 'x').replace('Ｘ', 'x').replace('ｘ', 'x')
    text = text.replace('ｍｍ', 'mm').replace('ｇ', 'g').replace('ｋｇ', 'kg').replace(',', '')
    pattern = r'[Ww]?\s*(\d+(?:\.\d+)?)\s*[xX×]\s*[Dd]?\s*(\d+(?:\.\d+)?)\s*[xX×]\s*[Hh]?\s*(\d+(?:\.\d+)?)'
    match = re.search(pattern, text)
    if match:
        w, d, h = float(match.group(1)), float(match.group(2)), float(match.group(3))
        return {"width": w, "depth": d, "height": h, "volume_weight": round((w * d * h) / 6000000, 2)}
    simple = re.search(r'(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)', text)
    if simple:
        l, w, h = float(simple.group(1)), float(simple.group(2)), float(simple.group(3))
        return {"length": l, "width": w, "height": h, "volume_weight": round((l * w * h) / 6000000, 2)}
    return None


def scrape_product_list():
    products = []
    page_num = 1
    has_next_page = True
    while has_next_page:
        url = LIST_BASE_URL if page_num == 1 else LIST_PAGE_URL_TEMPLATE.format(page=page_num)
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            if response.status_code != 200: has_next_page = False; continue
            soup = BeautifulSoup(response.text, 'html.parser')
            product_links = soup.find_all('a', href=re.compile(r'/shop/g/g[^/]+/?'))
            if not product_links: has_next_page = False; continue
            seen_skus = set()
            page_products = []
            for link in product_links:
                href = link.get('href', '')
                if '/shop/g/g' not in href: continue
                sku_match = re.search(r'/shop/g/g([^/]+)/?', href)
                if not sku_match: continue
                sku_raw = sku_match.group(1)
                sku = normalize_sku(sku_raw)
                if sku in seen_skus: continue
                seen_skus.add(sku)
                link_title = link.get('title', '') or link.get_text(strip=True)
                is_express = 'お急ぎ便' in link_title
                page_products.append({'url': urljoin(BASE_URL, href), 'sku': sku, 'sku_raw': sku_raw, 'is_express': is_express})
            products.extend(page_products)
            next_link = soup.find('a', href=re.compile(f'c10_p{page_num + 1}'))
            if next_link: page_num += 1
            else: has_next_page = False
        except Exception as e:
            print(f"[ERROR] {e}"); has_next_page = False
    unique = []; seen = set()
    for p in products:
        if p['sku'] not in seen: seen.add(p['sku']); unique.append(p)
    return unique


def scrape_product_detail(url):
    product = {
        'url': url, 'title': '', 'price': 0, 'description': '', 'box_size_text': '',
        'weight': 0, 'images': [], 'in_stock': True, 'is_point_product': False, 'is_express': False,
        'sku': '', 'sku_raw': '', 'content': '', 'allergens': '', 'shelf_life': ''
    }
    sku_match = re.search(r'/shop/g/g([^/]+)/?', url)
    if sku_match:
        product['sku_raw'] = sku_match.group(1)
        product['sku'] = normalize_sku(product['sku_raw'])
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        if response.status_code != 200: return product
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        title_el = soup.find('h1')
        if title_el: product['title'] = title_el.get_text(strip=True)
        if 'お急ぎ便' in product['title']:
            product['is_express'] = True
        price_area = soup.find('div', class_='block-goods-price')
        if price_area and 'ポイント' in price_area.get_text():
            product['is_point_product'] = True
        if not product['is_point_product']:
            price_el = soup.find('div', class_='block-goods-price--price')
            if price_el:
                pm = re.search(r'(\d{1,3}(?:,\d{3})*)', price_el.get_text())
                if pm: product['price'] = int(pm.group(1).replace(',', ''))
            if not product['price']:
                pm = re.search(r'(\d{1,3}(?:,\d{3})*)\s*円', page_text)
                if pm: product['price'] = int(pm.group(1).replace(',', ''))
        all_dt = soup.find_all('dt'); all_dd = soup.find_all('dd')
        for i, dt in enumerate(all_dt):
            try:
                dt_text = dt.get_text(strip=True)
                if i < len(all_dd):
                    dd_text = all_dd[i].get_text(strip=True)
                    if '内容' in dt_text: product['content'] = dd_text
                    elif '箱サイズ' in dt_text or 'サイズ' in dt_text:
                        product['box_size_text'] = dd_text
                        size_info = parse_box_size(dd_text)
                        if size_info: product['weight'] = size_info.get('volume_weight', 0)
                    elif '賞味期限' in dt_text: product['shelf_life'] = dd_text
                    elif 'アレルギー' in dt_text or '特定原材料' in dt_text: product['allergens'] = dd_text[:200]
            except: continue
        desc_parts = []
        for cn in ['item-description', 'product-description', 'detail-text']:
            el = soup.find('div', class_=cn)
            if el:
                t = el.get_text(strip=True)
                if t and len(t) > 20: desc_parts.append(t); break
        if product['content']: desc_parts.append(f"內容：{product['content']}")
        if product['shelf_life']: desc_parts.append(f"賞味期限：{product['shelf_life']}")
        product['description'] = '\n\n'.join(desc_parts)
        images = []; sku_raw = product['sku_raw']
        for prefix in ['L', '2', '3', '4', 'D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7', 'D8']:
            img_url = f"{BASE_URL}/img/goods/{prefix}/{sku_raw}.jpg"
            try:
                if requests.head(img_url, headers=HEADERS, timeout=5).status_code == 200: images.append(img_url)
            except: pass
        if not images:
            for img in soup.find_all('img', src=re.compile(sku_raw)):
                src = img.get('src', '')
                if src and src not in images:
                    images.append(urljoin(BASE_URL, src) if not src.startswith('http') else src)
        product['images'] = images
        if any(kw in page_text for kw in ['品切れ', '在庫なし', 'SOLD OUT']): product['in_stock'] = False
    except Exception as e:
        print(f"[ERROR] 爬取商品詳細失敗: {e}")
    return product


def upload_to_shopify(product, collection_id=None):
    print(f"[翻譯] 正在翻譯: {product['title'][:30]}...")
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))

    if not translated['success']:
        print(f"[跳過-翻譯失敗] {product['sku']}: {translated.get('error', '未知錯誤')}")
        return {'success': False, 'error': 'translation_failed', 'translated': translated}

    if is_japanese_text(translated['title']):
        print(f"[翻譯驗證] 標題仍含日文，重試加強翻譯: {translated['title']}")
        retry_result = translate_with_chatgpt(product['title'], product.get('description', ''), retry=True)
        if retry_result['success'] and not is_japanese_text(retry_result['title']):
            translated = retry_result
            print(f"[翻譯驗證] 重試成功: {translated['title']}")
        else:
            print(f"[翻譯驗證] 重試仍含日文，視為失敗")
            return {'success': False, 'error': 'translation_failed', 'translated': translated}

    print(f"[翻譯成功] {translated['title'][:30]}...")

    cost = product['price']
    selling_price = calculate_selling_price(cost)

    images_base64 = []
    for idx, img_url in enumerate(product.get('images', [])):
        if not img_url or not img_url.startswith('http'): continue
        result = download_image_to_base64(img_url)
        if result['success']:
            images_base64.append({'attachment': result['base64'], 'position': idx + 1, 'filename': f"francais_{product['sku']}_{idx+1}.jpg"})
        time.sleep(0.5)

    shopify_product = {
        'product': {
            'title': translated['title'], 'body_html': translated['description'] + SHIPPING_HTML,
            'vendor': 'Francais', 'product_type': '千層派・西式甜點',
            'status': 'active', 'published': True,
            'variants': [{'sku': product['sku'], 'price': f"{selling_price:.2f}",
                          'inventory_management': None, 'inventory_policy': 'continue', 'requires_shipping': True}],
            'images': images_base64,
            'tags': 'Francais, 日本, 西式甜點, 千層派, 伴手禮, 日本代購, 送禮',
            'metafields_global_title_tag': translated['page_title'],
            'metafields_global_description_tag': translated['meta_description'],
            'metafields': [{'namespace': 'custom', 'key': 'link', 'value': product['url'], 'type': 'url'}]
        }
    }

    response = requests.post(shopify_api_url('products.json'), headers=get_shopify_headers(), json=shopify_product)
    if response.status_code == 201:
        created = response.json()['product']
        product_id = created['id']
        variant_id = created['variants'][0]['id']
        requests.put(shopify_api_url(f'variants/{variant_id}.json'), headers=get_shopify_headers(),
                     json={'variant': {'id': variant_id, 'cost': f"{cost:.2f}"}})
        if collection_id: add_product_to_collection(product_id, collection_id)
        publish_to_all_channels(product_id)
        return {'success': True, 'product': created, 'translated': translated, 'selling_price': selling_price, 'cost': cost}
    else:
        return {'success': False, 'error': response.text}


# ========== Flask 路由 ==========


# ========== 運費 HTML 批次更新 ==========

update_shipping_status = {"running": False, "done": 0, "total": 0, "skipped": 0, "errors": []}


def run_update_shipping():
    global update_shipping_status
    update_shipping_status = {"running": True, "done": 0, "total": 0, "skipped": 0, "errors": []}
    try:
        collection_id = get_or_create_collection("Francais")
        cpm = get_collection_products_map(collection_id)
        pids = list(set(cpm.values()))
        update_shipping_status["total"] = len(pids)
        for pid in pids:
            try:
                r = requests.get(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers())
                if r.status_code != 200:
                    update_shipping_status["errors"].append(f"取得失敗 {pid}")
                    continue
                product = r.json().get("product", {})
                body = product.get("body_html", "") or ""
                if "國際運費" in body:
                    update_shipping_status["skipped"] += 1
                    continue
                ru = requests.put(
                    shopify_api_url(f"products/{pid}.json"),
                    headers=get_shopify_headers(),
                    json={"product": {"id": pid, "body_html": body + SHIPPING_HTML}}
                )
                if ru.status_code == 200:
                    update_shipping_status["done"] += 1
                else:
                    update_shipping_status["errors"].append(f"更新失敗 {pid}: {ru.status_code}")
            except Exception as e:
                update_shipping_status["errors"].append(str(e))
    except Exception as e:
        update_shipping_status["errors"].append(str(e))
    finally:
        update_shipping_status["running"] = False


@app.route("/api/update-shipping", methods=["POST"])
def api_update_shipping():
    if not load_shopify_token():
        return jsonify({"error": "未設定 Token"}), 400
    if update_shipping_status.get("running"):
        return jsonify({"error": "更新已在進行中"}), 400
    import threading
    threading.Thread(target=run_update_shipping, daemon=True).start()
    return jsonify({"message": "開始更新運費說明，請輪詢 /api/update-shipping-status"})


@app.route("/api/update-shipping-status")
def api_update_shipping_status():
    return jsonify(update_shipping_status)


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
    <title>Francais 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #E91E63; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #E91E63; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; margin-bottom: 10px; text-decoration: none; display: inline-block; }}
        .btn:hover {{ background: #C2185B; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-secondary:hover {{ background: #2980b9; }}
        .btn-success {{ background: #27ae60; }}
        .btn-success:hover {{ background: #219a52; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #E91E63, #FF80AB); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 80px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #E91E63; }}
        .stat-label {{ font-size: 11px; color: #666; margin-top: 5px; }}
        .nav {{ margin-bottom: 20px; }}
        .nav a {{ margin-right: 15px; color: #E91E63; text-decoration: none; font-weight: bold; }}
        .alert {{ padding: 12px 16px; border-radius: 5px; margin-bottom: 15px; }}
        .alert-danger {{ background: #fee; border: 1px solid #fcc; color: #c0392b; }}
    </style>
</head>
<body>
    <div class="nav">
        <a href="/">🏠 首頁</a>
        <a href="/japanese-scan">🇯🇵 日文商品掃描</a>
    </div>
    <h1>🍰 Francais 爬蟲工具 <small style="font-size: 14px; color: #999;">v2.2</small></h1>
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: <span style="color: {token_color};">{token_status}</span></p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
        <button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
        <a href="/japanese-scan" class="btn btn-success">🇯🇵 掃描日文商品</a>
    </div>
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 sucreyshopping.jp Francais 品牌商品並上架到 Shopify</p>
        <p style="color: #666; font-size: 14px;">
            ※ 成本價低於 ¥{MIN_PRICE} 的商品將自動跳過<br>
            ※ <b style="color: #e74c3c;">翻譯保護</b> - 翻譯失敗不上架，連續失敗 {MAX_CONSECUTIVE_TRANSLATION_FAILURES} 次自動停止<br>
            ※ <b style="color: #e67e22;">v2.2 缺貨自動刪除</b> - 官網消失、缺貨、お急ぎ便皆直接刪除
        </p>
        <button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
        <div id="progressSection" style="display: none;">
            <div id="translationAlert" class="alert alert-danger" style="display: none;">⚠️ 翻譯功能異常，爬取已自動停止！</div>
            <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width: 0%"></div></div>
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
    <div class="card"><h3>執行日誌</h3><div class="log" id="logArea">等待開始...</div></div>
    <script>
        let pollInterval = null;
        function log(msg, type='') {{
            const logArea = document.getElementById('logArea'); const time = new Date().toLocaleTimeString();
            const colors = {{ success:'#4ec9b0', error:'#f14c4c', warning:'#dcdcaa' }};
            logArea.innerHTML += '<div style="color:'+(colors[type]||'#d4d4d4')+'">['+time+'] '+msg+'</div>';
            logArea.scrollTop = logArea.scrollHeight;
        }}
        function clearLog() {{ document.getElementById('logArea').innerHTML = ''; }}
        async function testShopify() {{
            log('測試 Shopify 連線...');
            try {{ const res = await fetch('/api/test-shopify'); const data = await res.json();
                if (data.success) log('✓ 連線成功！', 'success'); else log('✗ '+data.error, 'error');
            }} catch(e) {{ log('✗ '+e.message, 'error'); }}
        }}
        async function testTranslate() {{
            log('測試翻譯功能...');
            try {{ const res = await fetch('/api/test-translate'); const data = await res.json();
                if (data.error) log('✗ '+data.error, 'error');
                else if (data.success) log('✓ 翻譯成功！'+data.title, 'success');
                else log('✗ 翻譯失敗', 'error');
            }} catch(e) {{ log('✗ '+e.message, 'error'); }}
        }}
        async function startScrape() {{
            clearLog(); log('開始爬取流程...');
            document.getElementById('startBtn').disabled = true;
            document.getElementById('progressSection').style.display = 'block';
            document.getElementById('translationAlert').style.display = 'none';
            try {{
                const res = await fetch('/api/start-scrape', {{ method: 'POST' }}); const data = await res.json();
                if (!data.success) {{ log('✗ '+data.error, 'error'); document.getElementById('startBtn').disabled = false; return; }}
                log('✓ 爬取任務已啟動', 'success'); pollInterval = setInterval(pollStatus, 1000);
            }} catch(e) {{ log('✗ '+e.message, 'error'); document.getElementById('startBtn').disabled = false; }}
        }}
        async function pollStatus() {{
            try {{
                const res = await fetch('/api/status'); const data = await res.json();
                const pct = data.total > 0 ? (data.progress/data.total*100) : 0;
                document.getElementById('progressFill').style.width = pct+'%';
                document.getElementById('statusText').textContent = data.current_product+' ('+data.progress+'/'+data.total+')';
                document.getElementById('uploadedCount').textContent = data.uploaded;
                document.getElementById('skippedCount').textContent = data.skipped;
                document.getElementById('filteredCount').textContent = data.filtered_by_price||0;
                document.getElementById('translationFailedCount').textContent = data.translation_failed||0;
                document.getElementById('deletedCount').textContent = data.deleted||0;
                document.getElementById('errorCount').textContent = data.errors.length;
                if (data.translation_stopped) document.getElementById('translationAlert').style.display = 'block';
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval); document.getElementById('startBtn').disabled = false;
                    if (data.translation_stopped) log('⚠️ 翻譯連續失敗，自動停止', 'error');
                    else log('========== 爬取完成 ==========', 'success');
                }}
            }} catch(e) {{ console.error(e); }}
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
    <title>日文商品掃描 - Francais</title>
    <style>
        * { box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; border-bottom: 2px solid #27ae60; padding-bottom: 10px; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn { background: #E91E63; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 14px; margin-right: 10px; margin-bottom: 10px; }
        .btn:hover { background: #C2185B; }
        .btn:disabled { background: #ccc; cursor: not-allowed; }
        .btn-danger { background: #e74c3c; }
        .btn-success { background: #27ae60; }
        .btn-sm { padding: 5px 10px; font-size: 12px; }
        .nav { margin-bottom: 20px; }
        .nav a { margin-right: 15px; color: #E91E63; text-decoration: none; font-weight: bold; }
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
    <div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文商品掃描</a></div>
    <h1>🇯🇵 日文商品掃描 - Francais</h1>
    <div class="card">
        <p>掃描 Shopify 商店中 Francais 的日文（未翻譯）商品。</p>
        <button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button>
        <span id="scanStatus"></span>
    </div>
    <div class="stats" id="statsSection" style="display: none;">
        <div class="stat"><div class="stat-number" id="totalProducts" style="color: #3498db;">0</div><div class="stat-label">Francais 商品數</div></div>
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
            try {
                const res = await fetch('/api/scan-japanese'); const data = await res.json();
                if (data.error) { alert(data.error); return; }
                japaneseProducts = data.japanese_products;
                document.getElementById('totalProducts').textContent = data.total_products;
                document.getElementById('japaneseCount').textContent = data.japanese_count;
                document.getElementById('statsSection').style.display = 'flex';
                renderResults(data.japanese_products);
                document.getElementById('resultsCard').style.display = 'block';
                document.getElementById('retranslateAllBtn').disabled = japaneseProducts.length === 0;
                document.getElementById('deleteAllBtn').disabled = japaneseProducts.length === 0;
                document.getElementById('scanStatus').textContent = '掃描完成！';
            } catch(e) { alert(e.message); }
            finally { document.getElementById('scanBtn').disabled = false; }
        }
        function renderResults(products) {
            const c = document.getElementById('results');
            if (!products.length) { c.innerHTML = '<p style="text-align:center;color:#27ae60;font-size:18px;">✅ 沒有日文商品</p>'; return; }
            let h = '';
            products.forEach(item => {
                const img = item.image ? `<img src="${item.image}">` : `<div class="no-image">無圖</div>`;
                h += `<div class="product-item" id="product-${item.id}">${img}<div class="info"><div class="title">${item.title}</div><div class="meta">SKU: ${item.sku||'無'} | ¥${item.price} | ${item.status}</div><div class="retranslate-status" id="status-${item.id}"></div></div><div class="actions"><button class="btn btn-success btn-sm" onclick="retranslateOne('${item.id}')" id="retranslate-btn-${item.id}">🔄 翻譯</button><button class="btn btn-danger btn-sm" onclick="deleteOne('${item.id}')" id="delete-btn-${item.id}">🗑️ 刪除</button></div></div>`;
            });
            c.innerHTML = h;
        }
        async function retranslateOne(id) {
            const btn = document.getElementById(`retranslate-btn-${id}`); const st = document.getElementById(`status-${id}`);
            btn.disabled = true; btn.textContent = '翻譯中...'; st.innerHTML = '<span style="color:#f39c12;">⏳</span>';
            try {
                const res = await fetch('/api/retranslate-product', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});
                const data = await res.json();
                if (data.success) { st.innerHTML=`<span style="color:#27ae60;">✅ ${data.new_title}</span>`; const t=document.querySelector(`#product-${id} .title`);if(t){t.textContent=data.new_title;t.style.color='#27ae60';} btn.textContent='✓'; }
                else { st.innerHTML=`<span style="color:#e74c3c;">❌ ${data.error}</span>`; btn.disabled=false; btn.textContent='🔄 重試'; }
            } catch(e) { st.innerHTML=`<span style="color:#e74c3c;">❌ ${e.message}</span>`; btn.disabled=false; btn.textContent='🔄 重試'; }
        }
        async function deleteOne(id) {
            if (!confirm('確定刪除？')) return;
            const btn = document.getElementById(`delete-btn-${id}`); btn.disabled = true;
            try { const res = await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})}); const data = await res.json(); if(data.success) document.getElementById(`product-${id}`).remove(); else { alert('失敗'); btn.disabled=false; } } catch(e) { alert(e.message); btn.disabled=false; }
        }
        async function retranslateAll() {
            if (!confirm(`翻譯全部 ${japaneseProducts.length} 個？`)) return;
            const btn = document.getElementById('retranslateAllBtn'); btn.disabled=true; btn.textContent='翻譯中...';
            let s=0,f=0;
            for (let i=0;i<japaneseProducts.length;i++) {
                document.getElementById('progressText').textContent=`${i+1}/${japaneseProducts.length}`;
                try { const res=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:japaneseProducts[i].id})}); const data=await res.json(); const st=document.getElementById(`status-${japaneseProducts[i].id}`);
                    if(data.success){s++;if(st)st.innerHTML=`<span style="color:#27ae60;">✅ ${data.new_title}</span>`;const t=document.querySelector(`#product-${japaneseProducts[i].id} .title`);if(t){t.textContent=data.new_title;t.style.color='#27ae60';}}
                    else{f++;if(st)st.innerHTML=`<span style="color:#e74c3c;">❌ ${data.error}</span>`;if(f>=3){alert('連續失敗，停止');break;}}
                } catch(e){f++;}
                await new Promise(r=>setTimeout(r,1500));
            }
            alert(`成功:${s} 失敗:${f}`); btn.textContent='🔄 全部重新翻譯'; btn.disabled=false; document.getElementById('progressText').textContent='';
        }
        async function deleteAllJapanese() {
            if (!confirm(`刪除全部 ${japaneseProducts.length} 個？無法復原！`)) return;
            const btn=document.getElementById('deleteAllBtn'); btn.disabled=true; btn.textContent='刪除中...';
            let s=0,f=0;
            for (let i=0;i<japaneseProducts.length;i++) {
                document.getElementById('progressText').textContent=`${i+1}/${japaneseProducts.length}`;
                try { const res=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:japaneseProducts[i].id})}); const data=await res.json(); if(data.success){s++;const el=document.getElementById(`product-${japaneseProducts[i].id}`);if(el)el.remove();}else f++; } catch(e){f++;}
                await new Promise(r=>setTimeout(r,300));
            }
            alert(`成功:${s} 失敗:${f}`); btn.textContent='🗑️ 全部刪除'; btn.disabled=false; document.getElementById('progressText').textContent='';
        }
    </script>
</body>
</html>'''


# ========== API 路由 ==========

@app.route('/api/scan-japanese')
def api_scan_japanese():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Shopify Token'}), 400
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=Francais")
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200: break
        data = response.json()
        for p in data.get('products', []):
            sku = ''; price = ''
            for v in p.get('variants', []):
                sku = v.get('sku', ''); price = v.get('price', ''); break
            products.append({
                'id': p.get('id'), 'title': p.get('title', ''), 'handle': p.get('handle', ''),
                'sku': sku, 'price': price, 'vendor': p.get('vendor', ''),
                'status': p.get('status', ''), 'created_at': p.get('created_at', ''),
                'image': p.get('image', {}).get('src', '') if p.get('image') else ''
            })
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else: url = None
    japanese_products = [p for p in products if is_japanese_text(p.get('title', ''))]
    return jsonify({'total_products': len(products), 'japanese_count': len(japanese_products), 'japanese_products': japanese_products})


@app.route('/api/retranslate-product', methods=['POST'])
def api_retranslate_product():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    data = request.get_json()
    product_id = data.get('product_id')
    if not product_id: return jsonify({'error': '缺少 product_id'}), 400
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.get(url, headers=get_shopify_headers())
    if response.status_code != 200: return jsonify({'error': f'無法取得商品: {response.status_code}'}), 400
    product = response.json().get('product', {})
    old_title = product.get('title', ''); old_body = product.get('body_html', '')
    translated = translate_with_chatgpt(old_title, old_body)
    if not translated['success']:
        return jsonify({'success': False, 'error': f"翻譯失敗: {translated.get('error', '未知')}"})
    if is_japanese_text(translated['title']):
        retry_result = translate_with_chatgpt(old_title, old_body, retry=True)
        if retry_result['success'] and not is_japanese_text(retry_result['title']):
            translated = retry_result
        else:
            return jsonify({'success': False, 'error': '翻譯後仍含日文，請手動修改'})
    success, resp = update_product(product_id, {
        'title': translated['title'], 'body_html': translated['description'] + SHIPPING_HTML,
        'metafields_global_title_tag': translated['page_title'],
        'metafields_global_description_tag': translated['meta_description']
    })
    if success: return jsonify({'success': True, 'old_title': old_title, 'new_title': translated['title'], 'product_id': product_id})
    else: return jsonify({'success': False, 'error': f'更新失敗: {resp.text[:200]}'})


@app.route('/api/delete-product', methods=['POST'])
def api_delete_product():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    data = request.get_json()
    product_id = data.get('product_id')
    if not product_id: return jsonify({'error': '缺少 product_id'}), 400
    return jsonify({'success': delete_product(product_id), 'product_id': product_id})


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/test-translate')
def test_translate():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key: return jsonify({'error': 'OPENAI_API_KEY 未設定'})
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    result = translate_with_chatgpt("ミルフィユ 12個入", "サクサクのパイ生地にクリームをはさんだ贅沢なミルフィユです")
    result['key_preview'] = key_preview; result['key_length'] = len(api_key)
    return jsonify(result)


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token(): return jsonify({'success': False, 'error': '找不到 Token'})
    response = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    if response.status_code == 200: return jsonify({'success': True, 'shop': response.json()['shop']})
    else: return jsonify({'success': False, 'error': response.text}), 400


@app.route('/api/start-scrape', methods=['POST'])
def start_scrape():
    global scrape_status
    if scrape_status['running']: return jsonify({'success': False, 'error': '爬取正在進行中'})
    if not load_shopify_token(): return jsonify({'success': False, 'error': '找不到 Token'})
    test_result = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test_result['success']:
        return jsonify({'success': False, 'error': f"翻譯功能異常: {test_result.get('error', '未知')}"})
    thread = threading.Thread(target=run_scrape); thread.start()
    return jsonify({'success': True, 'message': '開始爬取'})


@app.route('/api/start', methods=['POST'])
def api_start():
    global scrape_status
    if scrape_status['running']: return jsonify({'success': False, 'error': '爬取正在進行中'})
    if not load_shopify_token(): return jsonify({'success': False, 'error': '找不到設定'})
    test_result = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test_result['success']:
        return jsonify({'success': False, 'error': f"翻譯功能異常: {test_result.get('error', '未知')}"})
    thread = threading.Thread(target=run_scrape); thread.start()
    return jsonify({'success': True, 'message': 'Francais 爬蟲已啟動'})


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

        scrape_status['current_product'] = "正在檢查 Shopify 已有商品..."
        all_products_map = get_existing_products_map()
        existing_skus = set(all_products_map.keys())

        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("Francais")

        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        collection_skus = set(collection_products_map.keys())

        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)

        website_skus = set(item['sku'] for item in product_list)
        express_skus = set(item['sku'] for item in product_list if item.get('is_express'))

        # === v2.2: 記錄缺貨的 SKU ===
        out_of_stock_skus = set()

        consecutive_translation_failures = 0

        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理中: {item['sku']}"

            # お急ぎ便商品：跳過不上架，記錄為需刪除
            if item.get('is_express'):
                scrape_status['skipped'] += 1
                continue

            # 已存在於 Shopify
            if item['sku'] in existing_skus:
                # === v2.2: 已上架商品檢查庫存 ===
                if item['sku'] in collection_skus:
                    product = scrape_product_detail(item['url'])
                    if product and not product.get('in_stock', True):
                        out_of_stock_skus.add(item['sku'])
                        print(f"[缺貨偵測] {item['sku']} 官網缺貨，稍後刪除")
                    time.sleep(0.5)
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                continue

            product = scrape_product_detail(item['url'])

            # === v2.2: 缺貨 → 不上架，記錄 SKU ===
            if not product.get('in_stock', True):
                out_of_stock_skus.add(item['sku'])
                scrape_status['skipped'] += 1
                continue

            if product.get('is_point_product', False): scrape_status['skipped'] += 1; continue
            if product.get('price', 0) < MIN_PRICE:
                scrape_status['filtered_by_price'] += 1; scrape_status['skipped'] += 1; continue
            if not product.get('title') or not product.get('price'):
                scrape_status['errors'].append({'sku': item['sku'], 'error': '資訊不完整'}); continue

            result = upload_to_shopify(product, collection_id)

            if result['success']:
                existing_skus.add(product['sku']); existing_skus.add(item['sku'])
                scrape_status['uploaded'] += 1
                consecutive_translation_failures = 0
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1
                consecutive_translation_failures += 1
                if consecutive_translation_failures >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append({'error': f'翻譯連續失敗 {consecutive_translation_failures} 次，自動停止'})
                    break
            else:
                scrape_status['errors'].append({'sku': product['sku'], 'error': result['error']})
                consecutive_translation_failures = 0

            time.sleep(1)

        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "清理缺貨/下架/お急ぎ便商品..."

            # === v2.2: 合併需要刪除的 SKU ===
            # 1. 官網已消失的 SKU（collection 有但官網沒有）
            # 2. 官網還在但缺貨的 SKU
            # 3. お急ぎ便商品
            skus_to_delete = (collection_skus - website_skus) | (collection_skus & out_of_stock_skus) | (collection_skus & express_skus)

            # 也檢查 all_products_map 中的お急ぎ便（可能不在 collection 裡）
            for sku in express_skus:
                pid = all_products_map.get(sku)
                if pid and sku not in skus_to_delete:
                    skus_to_delete.add(sku)

            if skus_to_delete:
                print(f"[v2.2] 準備刪除 {len(skus_to_delete)} 個商品")
                for sku in skus_to_delete:
                    pid = collection_products_map.get(sku) or all_products_map.get(sku)
                    if pid:
                        if delete_product(pid):
                            scrape_status['deleted'] += 1
                            print(f"[已刪除] SKU: {sku}, Product ID: {pid}")
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
    print("Francais 爬蟲工具 v2.2")
    print("新增: 缺貨商品自動刪除（官網消失、缺貨、お急ぎ便皆刪除）")
    print("=" * 50)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

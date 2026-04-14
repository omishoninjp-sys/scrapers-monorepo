"""
本高砂屋商品爬蟲 + Shopify 上架工具 v2.3
v2.1: 翻譯保護機制、日文商品掃描、測試翻譯
v2.2: 缺貨商品自動刪除 - 官網消失或缺貨皆直接刪除
v2.3: 修復同步 Bug
  - 修復: existing_skus 改用全店 SKU 比對（不依賴 collection，解決每次重新上架問題）
  - 新增: get_hontaka_products_map() 用 vendor 篩選虎屋商品
  - 新增: /api/sync-delete 手動觸發僅刪除（背景執行）
  - 新增: /api/sync-status 輪詢同步進度
  - 新增: 每日自動同步排程（預設 JST 10:00）
  - 新增: 安全閾值防爬蟲異常誤刪
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

SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""
BASE_URL = "https://www.hontaka-shop.com"
LIST_BASE_URL = "https://www.hontaka-shop.com/shopbrand/all_items/"
LIST_PAGE_URL_TEMPLATE = "https://www.hontaka-shop.com/shopbrand/all_items/page{page}/order/"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MIN_PRICE = 1000
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3

# === v2.3: 安全閾值 ===
MIN_SCRAPED_PRODUCTS_FOR_DELETE = 5

# === v2.3: 自動同步排程 ===
AUTO_SYNC_ENABLED = os.environ.get("AUTO_SYNC_ENABLED", "true").lower() == "true"
AUTO_SYNC_HOUR = int(os.environ.get("AUTO_SYNC_HOUR", "10"))  # JST 10:00

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7',
    'Accept-Charset': 'EUC-JP,utf-8;q=0.7,*;q=0.3',
}

scrape_status = {
    "running": False, "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "uploaded": 0, "skipped": 0,
    "skipped_exists": 0, "filtered_by_price": 0, "out_of_stock": 0, "deleted": 0,
    "translation_failed": 0, "translation_stopped": False
}

# === v2.3: 同步狀態 ===
sync_status = {"running": False, "current_step": "", "finished": False}
last_sync_log = {"last_run": None, "website_skus_count": 0, "shopify_skus_count": 0, "deleted_skus": [], "errors": []}


def is_japanese_text(text):
    if not text: return False
    check = text.replace('\u672c\u9ad8\u7802\u5c4b', '').strip()
    if not check: return False
    jp = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', check))
    cn = len(re.findall(r'[\u4e00-\u9fff]', check))
    total = len(re.sub(r'[\s\d\W]', '', check))
    if total == 0: return False
    return jp > 0 and (jp / total > 0.3 or cn == 0)


def load_shopify_token():
    global SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP
    env_token = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')
    env_shop = os.environ.get('SHOPIFY_SHOP', '')
    if env_token and env_shop:
        SHOPIFY_ACCESS_TOKEN = env_token
        SHOPIFY_SHOP = env_shop.replace('https://','').replace('http://','').replace('.myshopify.com','').strip('/')
        return True
    tf = "shopify_token.json"
    if os.path.exists(tf):
        with open(tf, 'r') as f:
            d = json.load(f)
            SHOPIFY_ACCESS_TOKEN = d.get('access_token', '')
            s = d.get('shop', '')
            if s: SHOPIFY_SHOP = s.replace('https://','').replace('http://','').replace('.myshopify.com','').strip('/')
            return True
    return False


def get_shopify_headers():
    return {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}


def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


def calculate_selling_price(cost, weight):
    if not cost or cost <= 0: return 0
    return round((cost + (weight * 1250 if weight else 0)) / 0.7)


def clean_html_for_translation(html_text):
    if not html_text: return ""
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
    return text.strip()


def translate_with_chatgpt(title, description, retry=False):
    clean_desc = clean_html_for_translation(description)
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。翻譯成繁體中文並優化 SEO。

商品名稱：{title}
商品說明：{clean_desc[:1500]}

回傳 JSON（不加 markdown）：
{{"title":"名稱（前加「本高砂屋」）","description":"說明","page_title":"SEO標題50-60字","meta_description":"SEO描述100字內"}}

規則：1.本高砂屋洋菓子 2.エコルセ→薄餅捲 3.マンデルチーゲル→杏仁瓦片餅 4.開頭「本高砂屋」5.禁日文 6.只回傳JSON"
    if retry:
        prompt += "\n\n【嚴重警告】上次翻譯結果仍然包含日文字元！這次你必須：\n1. 將所有日文平假名、片假名完全翻譯成繁體中文\n2. うす皮→薄皮、金鍔→金鍔餅、詰合せ→綜合禮盒、迎春→迎春、翔ける→飛翔\n3. 絕對不可以出現任何 ひらがな 或 カタカナ\n4. 商品名中的日文必須全部意譯成中文"""
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。禁止輸出日文。"},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1000}, timeout=60)
        if r.status_code == 200:
            c = r.json()['choices'][0]['message']['content'].strip()
            if c.startswith('```'): c = c.split('\n', 1)[1]
            if c.endswith('```'): c = c.rsplit('```', 1)[0]
            t = json.loads(c.strip())
            tt = t.get('title', title)
            if not tt.startswith('本高砂屋'): tt = f"本高砂屋 {tt}"
            return {'success': True, 'title': tt, 'description': t.get('description', description),
                    'page_title': t.get('page_title', ''), 'meta_description': t.get('meta_description', '')}
        else:
            return {'success': False, 'error': f"HTTP {r.status_code}: {r.text[:200]}",
                    'title': f"本高砂屋 {title}", 'description': description, 'page_title': '', 'meta_description': ''}
    except Exception as e:
        return {'success': False, 'error': str(e),
                'title': f"本高砂屋 {title}", 'description': description, 'page_title': '', 'meta_description': ''}


def download_image_to_base64(img_url, max_retries=3):
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*', 'Referer': 'https://www.hontaka-shop.com/'}
    for attempt in range(max_retries):
        try:
            r = requests.get(img_url, headers=headers, timeout=30)
            if r.status_code == 200:
                ct = r.headers.get('Content-Type', 'image/jpeg')
                fmt = 'image/png' if 'png' in ct else 'image/gif' if 'gif' in ct else 'image/jpeg'
                return {'success': True, 'base64': base64.b64encode(r.content).decode('utf-8'), 'content_type': fmt}
        except: pass
        time.sleep(1)
    return {'success': False}


def get_existing_products_map():
    pm = {}
    url = shopify_api_url("products.json?limit=250")
    while url:
        r = requests.get(url, headers=get_shopify_headers())
        if r.status_code != 200: break
        for p in r.json().get('products', []):
            pid = p.get('id')
            for v in p.get('variants', []):
                sk = v.get('sku')
                if sk and pid: pm[sk] = pid
        lh = r.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    return pm


def get_collection_products_map(collection_id):
    pm = {}
    if not collection_id: return pm
    url = shopify_api_url(f"collections/{collection_id}/products.json?limit=250")
    while url:
        r = requests.get(url, headers=get_shopify_headers())
        if r.status_code != 200: break
        for p in r.json().get('products', []):
            pid = p.get('id')
            for v in p.get('variants', []):
                sk = v.get('sku')
                if sk and pid: pm[sk] = pid
        lh = r.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    return pm


# === v2.3: 用 vendor 篩選取得所有本高砂屋商品 ===
def get_hontaka_products_map():
    """取得 Shopify 上所有本高砂屋商品 {sku: product_id}，不依賴 collection"""
    pm = {}
    url = shopify_api_url("products.json?limit=250&vendor=本高砂屋")
    while url:
        r = requests.get(url, headers=get_shopify_headers())
        if r.status_code != 200: break
        for p in r.json().get('products', []):
            pid = p.get('id')
            for v in p.get('variants', []):
                sk = v.get('sku')
                if sk and pid: pm[sk] = pid
        lh = r.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    print(f"[v2.3] Shopify 本高砂屋商品: {len(pm)} 筆")
    return pm


def delete_product(pid):
    return requests.delete(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers()).status_code == 200


def update_product(pid, data):
    r = requests.put(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers(),
        json={"product": {"id": pid, **data}})
    return r.status_code == 200, r


def get_or_create_collection(ct="本高砂屋"):
    r = requests.get(shopify_api_url(f'custom_collections.json?title={ct}'), headers=get_shopify_headers())
    if r.status_code == 200:
        for c in r.json().get('custom_collections', []):
            if c['title'] == ct: return c['id']
    r = requests.post(shopify_api_url('custom_collections.json'), headers=get_shopify_headers(),
        json={'custom_collection': {'title': ct, 'published': True}})
    if r.status_code == 201: return r.json()['custom_collection']['id']
    return None


def add_product_to_collection(pid, cid):
    return requests.post(shopify_api_url('collects.json'), headers=get_shopify_headers(),
        json={'collect': {'product_id': pid, 'collection_id': cid}}).status_code == 201


def publish_to_all_channels(pid):
    gu = f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/graphql.json"
    hd = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    r = requests.post(gu, headers=hd, json={'query': '{ publications(first:20){ edges{ node{ id name }}}}'})
    if r.status_code != 200: return False
    pubs = r.json().get('data', {}).get('publications', {}).get('edges', [])
    seen = set(); uq = []
    for p in pubs:
        if p['node']['name'] not in seen: seen.add(p['node']['name']); uq.append(p['node'])
    mut = """mutation publishablePublish($id:ID!,$input:[PublicationInput!]!){publishablePublish(id:$id,input:$input){userErrors{field message}}}"""
    requests.post(gu, headers=hd, json={'query': mut, 'variables': {"id": f"gid://shopify/Product/{pid}", "input": [{"publicationId": p['id']} for p in uq]}})
    return True


def parse_dimension_weight(text):
    result = {'dimension': None, 'actual_weight': None, 'volume_weight': 0, 'final_weight': 0}
    text = text.replace('×', 'x').replace('Ｘ', 'x').replace('ｘ', 'x')
    text = text.replace('ｍｍ', 'mm').replace('ｇ', 'g').replace('ｋｇ', 'kg').replace(',', '')
    dm = re.search(r'(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]?\s*(\d+(?:\.\d+)?)\s*mm', text, re.IGNORECASE)
    if dm:
        l, w, h = float(dm.group(1)), float(dm.group(2)), float(dm.group(3))
        result['volume_weight'] = round((l * w * h) / 6000000, 2)
        result['dimension'] = {'length': l, 'width': w, 'height': h}
    wm = re.search(r'重量[：:\s]*(\d+(?:\.\d+)?)\s*(g|kg)', text, re.IGNORECASE)
    if wm:
        wv = float(wm.group(1))
        result['actual_weight'] = round(wv / 1000 if wm.group(2).lower() == 'g' else wv, 3)
    if result['volume_weight'] and result['actual_weight']:
        result['final_weight'] = max(result['volume_weight'], result['actual_weight'])
    elif result['volume_weight']: result['final_weight'] = result['volume_weight']
    elif result['actual_weight']: result['final_weight'] = result['actual_weight']
    return result


def scrape_product_list():
    products = []; page_num = 1
    while page_num <= 20:
        url = LIST_BASE_URL if page_num == 1 else LIST_PAGE_URL_TEMPLATE.format(page=page_num)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30); r.encoding = 'euc-jp'
            if r.status_code != 200: break
            soup = BeautifulSoup(r.text, 'html.parser')
            pls = soup.find_all('a', href=re.compile(r'/shopdetail/\d{12}/'))
            if not pls: break
            seen = set(); pp = []
            for l in pls:
                sm = re.search(r'/shopdetail/(\d{12})/', l.get('href', ''))
                if sm and sm.group(1) not in seen:
                    seen.add(sm.group(1))
                    pp.append({'url': f"{BASE_URL}/shopdetail/{sm.group(1)}/", 'sku': sm.group(1)})
            if not pp: break
            products.extend(pp); page_num += 1; time.sleep(0.5)
        except: break
    unique = []; us = set()
    for p in products:
        if p['sku'] not in us: us.add(p['sku']); unique.append(p)
    return unique


def scrape_product_detail(url):
    product = {'url': url, 'title': '', 'price': 0, 'description': '', 'weight': 0, 'images': [],
        'in_stock': True, 'sku': '', 'product_code': '', 'size_text': '', 'weight_text': ''}
    sm = re.search(r'/shopdetail/(\d{12})/', url)
    if sm: product['sku'] = sm.group(1)
    try:
        r = requests.get(url, headers=HEADERS, timeout=30); r.encoding = 'euc-jp'
        if r.status_code != 200: return product
        soup = BeautifulSoup(r.text, 'html.parser'); pt = soup.get_text()
        tt = soup.find('title')
        if tt:
            tp = tt.get_text(strip=True).split('-')
            if tp: product['title'] = tp[0].strip()
        if not product['title']:
            h2 = soup.find('h2')
            if h2: product['title'] = h2.get_text(strip=True)
        cm = re.search(r'〔(\d+)〕', product['title'])
        if cm: product['product_code'] = cm.group(1)
        pi = soup.find('input', {'name': 'price1'})
        if pi and pi.get('value'):
            try: product['price'] = int(pi.get('value').replace(',', ''))
            except: pass
        if not product['price']:
            pi2 = soup.find('input', {'id': 'M_price2'})
            if pi2 and pi2.get('value'):
                try: product['price'] = int(pi2.get('value').replace(',', ''))
                except: pass
        if not product['price']:
            for pm in re.findall(r'(\d{1,3}(?:,\d{3})*)\s*円', pt):
                try:
                    pv = int(pm.replace(',', ''))
                    if pv >= 100: product['price'] = pv; break
                except: pass
        if any(kw in pt for kw in ['売切れ', '在庫なし', 'SOLD OUT']): product['in_stock'] = False
        desc_parts = []
        dm = re.search(r'商品[説說]明[：:]\s*(.+?)(?=---|\n\n|内容量|賞味期限)', pt, re.DOTALL)
        if dm: desc_parts.append(dm.group(1).strip())
        ctm = re.search(r'内容量[：:]\s*(.+?)(?=---|\n|賞味期限|特定原材料)', pt)
        if ctm: desc_parts.append(f"內容量：{ctm.group(1).strip()}")
        slm = re.search(r'賞味期限[：:]\s*(\d+日?)', pt)
        if slm: desc_parts.append(f"賞味期限：{slm.group(1)}")
        szm = re.search(r'サイズ[：:]\s*(.+?)(?=---|重量|\n)', pt)
        if szm: product['size_text'] = szm.group(1).strip()
        wtm = re.search(r'重量[：:]\s*(.+?)(?=---|保存|\n)', pt)
        if wtm: product['weight_text'] = wtm.group(1).strip()
        product['description'] = '\n\n'.join(desc_parts) if desc_parts else ''
        wi = parse_dimension_weight(f"サイズ：{product['size_text']} 重量：{product['weight_text']}")
        product['weight'] = wi['final_weight']
        images = []; seen_img = set()
        for img in soup.find_all('img', src=re.compile(r'makeshop-multi-images\.akamaized\.net')):
            src = img.get('src', '')
            if src and 'shophontaka' in src and '/shopimages/' in src and '/itemimages/' not in src:
                fn = src.split('/')[-1].split('?')[0]
                if not (fn.startswith('s') and len(fn) > 1 and fn[1].isdigit()):
                    cs = src.split('?')[0]
                    if cs not in seen_img: seen_img.add(cs); images.append(src)
        if not images:
            for iu in re.findall(r'(https://makeshop-multi-images\.akamaized\.net/shophontaka/shopimages/[^"\']+\.(?:jpg|jpeg|png|gif))', str(soup), re.IGNORECASE):
                fn = iu.split('/')[-1].split('?')[0]
                if not (fn.startswith('s') and len(fn) > 1 and fn[1].isdigit()):
                    cu = iu.split('?')[0]
                    if cu not in seen_img: seen_img.add(cu); images.append(iu)
        product['images'] = images[:10]
    except Exception as e:
        print(f"[ERROR] {e}")
    return product


def upload_to_shopify(product, collection_id=None):
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    if not translated['success']:
        return {'success': False, 'error': 'translation_failed', 'translated': translated}
    if is_japanese_text(translated['title']):
        retry = translate_with_chatgpt(product['title'], product.get('description', ''), retry=True)
        if retry['success'] and not is_japanese_text(retry['title']):
            translated = retry
        else:
            return {'success': False, 'error': 'translation_failed', 'translated': translated}
    cost = product['price']; weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    images_b64 = []
    for idx, iu in enumerate(product.get('images', [])):
        if not iu or not iu.startswith('http'): continue
        result = download_image_to_base64(iu)
        if result['success']:
            images_b64.append({'attachment': result['base64'], 'position': idx+1, 'filename': f"hontaka_{product['sku']}_{idx+1}.jpg"})
        time.sleep(0.3)
    sku = product.get('product_code') or product['sku']
    sp = {'product': {
        'title': translated['title'], 'body_html': translated['description'],
        'vendor': '本高砂屋', 'product_type': '西式甜點', 'status': 'active', 'published': True,
        'variants': [{'sku': sku, 'price': f"{selling_price:.2f}", 'weight': weight,
            'weight_unit': 'kg', 'inventory_management': None, 'inventory_policy': 'continue', 'requires_shipping': True}],
        'images': images_b64, 'tags': '本高砂屋, 日本, 神戶, 西式甜點, 伴手禮, 日本代購, 送禮, エコルセ, 薄餅',
        'metafields_global_title_tag': translated['page_title'],
        'metafields_global_description_tag': translated['meta_description'],
        'metafields': [{'namespace': 'custom', 'key': 'link', 'value': product['url'], 'type': 'url'}]
    }}
    r = requests.post(shopify_api_url('products.json'), headers=get_shopify_headers(), json=sp)
    if r.status_code == 201:
        cp = r.json()['product']; pid = cp['id']; vid = cp['variants'][0]['id']
        requests.put(shopify_api_url(f'variants/{vid}.json'), headers=get_shopify_headers(),
            json={'variant': {'id': vid, 'cost': f"{cost:.2f}"}})
        if collection_id: add_product_to_collection(pid, collection_id)
        publish_to_all_channels(pid)
        return {'success': True, 'product': cp, 'translated': translated, 'selling_price': selling_price, 'cost': cost}
    return {'success': False, 'error': r.text}


# === v2.3: 獨立的同步刪除函式（背景執行）===
def sync_delete_stale_products():
    """比對官網商品清單，刪除已下架商品（僅 SKU 比對，不爬詳情頁）"""
    global last_sync_log, sync_status
    sync_status.update({"running": True, "current_step": "開始同步...", "finished": False})
    log = {"last_run": time.strftime("%Y-%m-%d %H:%M:%S"), "website_skus_count": 0,
           "shopify_skus_count": 0, "deleted_skus": [], "errors": []}
    try:
        if not load_shopify_token():
            log["errors"].append("Shopify Token 未設定"); last_sync_log = log; return log

        sync_status['current_step'] = "爬取官網商品列表..."
        product_list = scrape_product_list()
        website_skus = set(item['sku'] for item in product_list)
        log["website_skus_count"] = len(website_skus)
        print(f"[v2.3 sync] 官網商品: {len(website_skus)} 筆")

        if len(website_skus) < MIN_SCRAPED_PRODUCTS_FOR_DELETE:
            msg = f"官網只爬到 {len(website_skus)} 筆（低於安全閾值 {MIN_SCRAPED_PRODUCTS_FOR_DELETE}），跳過刪除"
            log["errors"].append(msg); last_sync_log = log; return log

        sync_status['current_step'] = "取得 Shopify 本高砂屋商品..."
        hontaka_pm = get_hontaka_products_map()
        log["shopify_skus_count"] = len(hontaka_pm)
        print(f"[v2.3 sync] Shopify 本高砂屋商品: {len(hontaka_pm)} 筆")

        # SKU 格式橋接：Shopify 可能存短碼 product_code，官網用 12 位數
        website_skus_normalized = set()
        for ws in website_skus:
            website_skus_normalized.add(ws)
            website_skus_normalized.add(ws.lstrip('0') or '0')

        skus_to_delete = set()
        for sku in hontaka_pm:
            if sku not in website_skus_normalized:
                skus_to_delete.add(sku)

        print(f"[v2.3 sync] 準備刪除 {len(skus_to_delete)} 筆: {skus_to_delete}")
        sync_status['current_step'] = f"刪除 {len(skus_to_delete)} 筆下架商品..."

        for i, sku in enumerate(skus_to_delete):
            sync_status['current_step'] = f"刪除 ({i+1}/{len(skus_to_delete)}): {sku}"
            pid = hontaka_pm.get(sku)
            if pid:
                if delete_product(pid):
                    log["deleted_skus"].append(sku)
                    print(f"[v2.3 sync] ✓ 已刪除 {sku} (ID: {pid})")
                else:
                    log["errors"].append(f"刪除失敗: {sku}")
            time.sleep(0.3)

        print(f"[v2.3 sync] 完成，共刪除 {len(log['deleted_skus'])} 筆")
        sync_status['current_step'] = f"完成，刪除 {len(log['deleted_skus'])} 筆"
    except Exception as e:
        log["errors"].append(str(e))
        sync_status['current_step'] = f"錯誤: {e}"
    finally:
        sync_status['running'] = False; sync_status['finished'] = True
    last_sync_log = log
    return log


# === v2.3: 自動同步排程 ===
def start_auto_sync_scheduler():
    def scheduler_loop():
        import datetime as dt
        while True:
            try:
                now_jst = dt.datetime.utcnow() + dt.timedelta(hours=9)
                target_jst = now_jst.replace(hour=AUTO_SYNC_HOUR, minute=0, second=0, microsecond=0)
                if now_jst >= target_jst: target_jst += dt.timedelta(days=1)
                wait = (target_jst - now_jst).total_seconds()
                print(f"[v2.3 scheduler] 下次自動同步: {target_jst.strftime('%Y-%m-%d %H:%M')} JST（{int(wait)}秒後）")
                time.sleep(wait)
                print(f"[v2.3 scheduler] 開始自動同步刪除...")
                result = sync_delete_stale_products()
                print(f"[v2.3 scheduler] 完成: 刪除 {len(result.get('deleted_skus', []))} 筆")
            except Exception as e:
                print(f"[v2.3 scheduler] 錯誤: {e}"); time.sleep(3600)
    t = threading.Thread(target=scheduler_loop, daemon=True); t.start()
    print(f"[v2.3] 自動同步排程已啟動（每日 JST {AUTO_SYNC_HOUR}:00）")


def run_scrape():
    global scrape_status
    try:
        scrape_status.update({"running": True, "progress": 0, "total": 0, "current_product": "",
            "products": [], "errors": [], "uploaded": 0, "skipped": 0, "skipped_exists": 0,
            "filtered_by_price": 0, "out_of_stock": 0, "deleted": 0,
            "translation_failed": 0, "translation_stopped": False})

        scrape_status['current_product'] = "設定 Collection..."
        collection_id = get_or_create_collection("本高砂屋")

        # === v2.3: 用全店 SKU 比對（不依賴 collection），解決重複上架 ===
        scrape_status['current_product'] = "取得 Shopify 現有商品..."
        existing_map = get_existing_products_map()
        existing_skus = set(existing_map.keys())

        scrape_status['current_product'] = "取得本高砂屋商品（用於刪除比對）..."
        hontaka_pm = get_hontaka_products_map()
        hontaka_skus = set(hontaka_pm.keys())

        scrape_status['current_product'] = "爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)

        website_skus = set()
        # === v2.2: 記錄缺貨的 SKU ===
        out_of_stock_skus = set()
        ctf = 0

        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['sku']}"

            product = scrape_product_detail(item['url'])
            actual_sku = product.get('product_code') or product['sku']
            website_skus.add(actual_sku)

            # === v2.2: 缺貨 → 記錄 SKU，不上架 ===
            if not product.get('in_stock', True):
                out_of_stock_skus.add(actual_sku)
                scrape_status['out_of_stock'] += 1
                continue

            if product.get('price', 0) < MIN_PRICE:
                scrape_status['filtered_by_price'] += 1; continue

            if actual_sku in existing_skus:
                scrape_status['skipped_exists'] += 1; scrape_status['skipped'] += 1; continue

            if not product.get('title') or not product.get('price'):
                scrape_status['errors'].append({'sku': item['sku'], 'error': '資訊不完整'}); continue

            result = upload_to_shopify(product, collection_id)
            if result['success']:
                existing_skus.add(actual_sku); scrape_status['uploaded'] += 1; ctf = 0
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1; ctf += 1
                if ctf >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append({'error': f'翻譯連續失敗 {ctf} 次，自動停止'}); break
            else:
                scrape_status['errors'].append({'sku': actual_sku, 'error': result.get('error','')}); ctf = 0
            time.sleep(1)

        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "清理缺貨/下架商品..."

            # === v2.3: 安全檢查 + 用 hontaka_pm 比對 ===
            if len(website_skus) >= MIN_SCRAPED_PRODUCTS_FOR_DELETE:
                # SKU 格式橋接：Shopify 可能存 product_code（短碼），官網用 12 位數
                # 建立反查表：短碼 → 有對應的官網 SKU
                website_skus_padded = set()
                for ws in website_skus:
                    website_skus_padded.add(ws)
                    website_skus_padded.add(ws.lstrip('0') or '0')  # 去前導零版本

                skus_to_delete = set()
                for sku in hontaka_skus:
                    # 檢查 SKU 是否存在於官網（精確 or 去前導零比對）
                    if sku not in website_skus_padded:
                        skus_to_delete.add(sku)
                # 加上缺貨的
                for sku in hontaka_skus & out_of_stock_skus:
                    skus_to_delete.add(sku)

                if skus_to_delete:
                    print(f"[v2.3] 準備刪除 {len(skus_to_delete)} 個商品")
                    for sku in skus_to_delete:
                        scrape_status['current_product'] = f"刪除: {sku}"
                        pid = hontaka_pm.get(sku)
                        if pid:
                            if delete_product(pid):
                                scrape_status['deleted'] += 1
                                print(f"[已刪除] SKU: {sku}, Product ID: {pid}")
                            else:
                                scrape_status['errors'].append({'sku': sku, 'error': '刪除失敗'})
                        time.sleep(0.3)
            else:
                msg = f"⚠️ 官網只爬到 {len(website_skus)} 筆（安全閾值 {MIN_SCRAPED_PRODUCTS_FOR_DELETE}），跳過刪除"
                scrape_status['errors'].append({'error': msg})
                print(f"[v2.3] {msg}")

        scrape_status['current_product'] = "完成" if not scrape_status['translation_stopped'] else "翻譯異常停止"
    except Exception as e:
        scrape_status['errors'].append({'error': str(e)})
    finally:
        scrape_status['running'] = False


# ========== Flask 路由 ==========

@app.route('/')
def index():
    token_loaded = load_shopify_token()
    tc = 'green' if token_loaded else 'red'
    ts = '✓ 已載入' if token_loaded else '✗ 未設定'
    html = """<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>本高砂屋 爬蟲工具</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #8B4513;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1);}.btn{background:#8B4513;color:white;border:none;padding:12px 24px;border-radius:5px;cursor:pointer;font-size:16px;margin-right:10px;margin-bottom:10px;text-decoration:none;display:inline-block}.btn:hover{background:#A0522D}.btn:disabled{background:#ccc}.btn-secondary{background:#3498db}.btn-success{background:#27ae60}.progress-bar{width:100%;height:20px;background:#eee;border-radius:10px;overflow:hidden;margin:10px 0}.progress-fill{height:100%;background:linear-gradient(90deg,#8B4513,#D2691E);transition:width 0.3s}.status{padding:10px;background:#f8f9fa;border-radius:5px;margin-top:10px}.log{max-height:300px;overflow-y:auto;font-family:monospace;font-size:13px;background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:5px}.stats{display:flex;gap:15px;margin-top:15px;flex-wrap:wrap}.stat{flex:1;min-width:70px;text-align:center;padding:15px;background:#f8f9fa;border-radius:5px}.stat-number{font-size:24px;font-weight:bold;color:#8B4513}.stat-label{font-size:10px;color:#666;margin-top:5px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}.alert{padding:12px 16px;border-radius:5px;margin-bottom:15px}.alert-danger{background:#fee;border:1px solid #fcc;color:#c0392b}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🍪 本高砂屋 爬蟲工具 <small style="font-size:14px;color:#999">v2.3</small></h1>
<div class="card"><h3>Shopify 連線</h3><p>Token: <span style="color:__TC__;">__TS__</span></p>
<button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
<button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
<a href="/japanese-scan" class="btn btn-success">🇯🇵 日文掃描</a></div>
<div class="card"><h3>🔄 同步清理（僅刪除下架商品）</h3>
<p style="color:#666;font-size:14px">※ 比對官網，刪除已下架商品。不會上架新品。</p>
<button class="btn" style="background:#e67e22" id="syncBtn" onclick="syncDelete()">🧹 立即同步清理</button>
<div id="syncResult" style="display:none;margin-top:15px"></div></div>
<div class="card"><h3>🚀 完整爬取（含上架新品）</h3>
<p>爬取 hontaka-shop.com 所有商品並上架到 Shopify</p>
<p style="color:#666;font-size:14px">※ &lt;¥__MIN_COST__ 跳過 | <b style="color:#e74c3c">翻譯保護</b> 連續失敗 __MAX_FAIL__ 次停止 | <b style="color:#e67e22">缺貨自動刪除</b></p>
<button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
<div id="progressSection" style="display:none">
<div id="translationAlert" class="alert alert-danger" style="display:none">⚠️ 翻譯功能異常，已自動停止！</div>
<div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
<div class="status" id="statusText">準備中...</div>
<div class="stats">
<div class="stat"><div class="stat-number" id="uploadedCount">0</div><div class="stat-label">已上架</div></div>
<div class="stat"><div class="stat-number" id="skippedCount">0</div><div class="stat-label">已存在</div></div>
<div class="stat"><div class="stat-number" id="translationFailedCount" style="color:#e74c3c">0</div><div class="stat-label">翻譯失敗</div></div>
<div class="stat"><div class="stat-number" id="filteredCount">0</div><div class="stat-label">價格過濾</div></div>
<div class="stat"><div class="stat-number" id="outOfStockCount">0</div><div class="stat-label">無庫存</div></div>
<div class="stat"><div class="stat-number" id="deletedCount" style="color:#e67e22">0</div><div class="stat-label">已刪除</div></div>
<div class="stat"><div class="stat-number" id="errorCount" style="color:#e74c3c">0</div><div class="stat-label">錯誤</div></div>
</div></div></div>
<div class="card"><h3>執行日誌</h3><div class="log" id="logArea">等待開始...</div></div>
<script>let pollInterval=null;function log(m,t){const l=document.getElementById('logArea');const tm=new Date().toLocaleTimeString();const c={success:'#4ec9b0',error:'#f14c4c',warning:'#e67e22'}[t]||'#d4d4d4';l.innerHTML+='<div style="color:'+c+'">['+tm+'] '+m+'</div>';l.scrollTop=l.scrollHeight}function clearLog(){document.getElementById('logArea').innerHTML=''}async function testShopify(){log('測試連線...');try{const r=await fetch('/api/test-shopify');const d=await r.json();if(d.success)log('✓ '+d.shop.name,'success');else log('✗ '+d.error,'error')}catch(e){log('✗ '+e.message,'error')}}async function testTranslate(){log('測試翻譯...');try{const r=await fetch('/api/test-translate');const d=await r.json();if(d.error)log('✗ '+d.error,'error');else if(d.success)log('✓ '+d.title,'success');else log('✗ 翻譯失敗','error')}catch(e){log('✗ '+e.message,'error')}}async function syncDelete(){const b=document.getElementById('syncBtn');const rd=document.getElementById('syncResult');b.disabled=true;b.textContent='同步中...';rd.style.display='none';log('開始同步清理...','warning');try{const r=await fetch('/api/sync-delete',{method:'POST'});const d=await r.json();if(d.error){log('✗ '+d.error,'error');b.disabled=false;b.textContent='🧹 立即同步清理';return}log('✓ 同步已啟動','success');const poll=setInterval(async()=>{try{const sr=await fetch('/api/sync-status');const sd=await sr.json();rd.innerHTML='<div style="padding:10px;background:#e8f4fd;border-radius:5px">⏳ '+sd.current_step+'</div>';rd.style.display='block';if(!sd.running&&sd.finished){clearInterval(poll);const del_count=sd.deleted_skus?sd.deleted_skus.length:0;const msg=`✓ 同步完成：官網 ${sd.website_skus_count} 筆 / Shopify ${sd.shopify_skus_count} 筆 / 刪除 ${del_count} 筆`;log(msg,'success');if(sd.deleted_skus&&sd.deleted_skus.length>0)sd.deleted_skus.forEach(s=>log('  刪除: '+s,'warning'));rd.innerHTML='<div style="padding:10px;background:#e8f4fd;border-radius:5px">'+msg+'</div>';b.disabled=false;b.textContent='🧹 立即同步清理'}}catch(e){console.error(e)}},2000)}catch(e){log('✗ '+e.message,'error');b.disabled=false;b.textContent='🧹 立即同步清理'}}async function startScrape(){clearLog();log('開始爬取...');document.getElementById('startBtn').disabled=true;document.getElementById('progressSection').style.display='block';document.getElementById('translationAlert').style.display='none';try{const r=await fetch('/api/start',{method:'POST'});const d=await r.json();if(d.error){log('✗ '+d.error,'error');document.getElementById('startBtn').disabled=false;return}log('✓ 已啟動','success');pollInterval=setInterval(pollStatus,1000)}catch(e){log('✗ '+e.message,'error');document.getElementById('startBtn').disabled=false}}async function pollStatus(){try{const r=await fetch('/api/status');const d=await r.json();const p=d.total>0?(d.progress/d.total*100):0;document.getElementById('progressFill').style.width=p+'%';document.getElementById('statusText').textContent=d.current_product+' ('+d.progress+'/'+d.total+')';document.getElementById('uploadedCount').textContent=d.uploaded;document.getElementById('skippedCount').textContent=d.skipped_exists||d.skipped||0;document.getElementById('translationFailedCount').textContent=d.translation_failed||0;document.getElementById('filteredCount').textContent=d.filtered_by_price||0;document.getElementById('outOfStockCount').textContent=d.out_of_stock||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors.length;if(d.translation_stopped)document.getElementById('translationAlert').style.display='block';if(!d.running&&d.progress>0){clearInterval(pollInterval);document.getElementById('startBtn').disabled=false;if(d.translation_stopped)log('⚠️ 翻譯異常停止','error');else log('========== 完成 ==========','success')}}catch(e){console.error(e)}}</script></body></html>"""
    return html.replace('__TC__', tc).replace('__TS__', ts).replace('__MIN_COST__', str(MIN_PRICE)).replace('__MAX_FAIL__', str(MAX_CONSECUTIVE_TRANSLATION_FAILURES))


@app.route('/japanese-scan')
def japanese_scan_page():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>日文商品掃描 - 本高砂屋</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #27ae60;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#8B4513;color:white;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-size:14px;margin-right:10px;margin-bottom:10px}.btn:disabled{background:#ccc}.btn-danger{background:#e74c3c}.btn-success{background:#27ae60}.btn-sm{padding:5px 10px;font-size:12px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}.stats{display:flex;gap:15px;margin:20px 0;flex-wrap:wrap}.stat{flex:1;min-width:150px;text-align:center;padding:20px;background:#f8f9fa;border-radius:8px}.stat-number{font-size:36px;font-weight:bold}.stat-label{font-size:14px;color:#666;margin-top:5px}.product-item{display:flex;align-items:center;padding:15px;border-bottom:1px solid #eee;gap:15px}.product-item:last-child{border-bottom:none}.product-item img{width:60px;height:60px;object-fit:cover;border-radius:4px}.product-item .info{flex:1}.product-item .info .title{font-weight:bold;margin-bottom:5px;color:#c0392b}.product-item .info .meta{font-size:12px;color:#666}.no-image{width:60px;height:60px;background:#eee;display:flex;align-items:center;justify-content:center;border-radius:4px;color:#999;font-size:10px}.retranslate-status{font-size:12px;margin-top:5px}.action-bar{position:sticky;top:0;background:white;padding:15px;margin:-20px -20px 20px -20px;border-bottom:1px solid #ddd;z-index:100;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🇯🇵 日文商品掃描 - 本高砂屋</h1>
<div class="card"><p>掃描 Shopify 中 本高砂屋 的日文（未翻譯）商品。</p><button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button><span id="scanStatus"></span></div>
<div class="stats" id="statsSection" style="display:none"><div class="stat"><div class="stat-number" id="totalProducts" style="color:#3498db">0</div><div class="stat-label">本高砂屋商品數</div></div><div class="stat"><div class="stat-number" id="japaneseCount" style="color:#e74c3c">0</div><div class="stat-label">日文商品</div></div></div>
<div class="card" id="resultsCard" style="display:none"><div class="action-bar"><div><button class="btn btn-success" id="retranslateAllBtn" onclick="retranslateAll()" disabled>🔄 全部翻譯</button><button class="btn btn-danger" id="deleteAllBtn" onclick="deleteAllJP()" disabled>🗑️ 全部刪除</button></div><div id="progressText"></div></div><div id="results"></div></div>
<script>let jp=[];async function startScan(){document.getElementById('scanBtn').disabled=true;document.getElementById('scanStatus').textContent='掃描中...';try{const r=await fetch('/api/scan-japanese');const d=await r.json();if(d.error){alert(d.error);return}jp=d.japanese_products;document.getElementById('totalProducts').textContent=d.total_products;document.getElementById('japaneseCount').textContent=d.japanese_count;document.getElementById('statsSection').style.display='flex';renderResults(d.japanese_products);document.getElementById('resultsCard').style.display='block';document.getElementById('retranslateAllBtn').disabled=jp.length===0;document.getElementById('deleteAllBtn').disabled=jp.length===0;document.getElementById('scanStatus').textContent='完成！'}catch(e){alert(e.message)}finally{document.getElementById('scanBtn').disabled=false}}function renderResults(p){const c=document.getElementById('results');if(!p.length){c.innerHTML='<p style="text-align:center;color:#27ae60;font-size:18px">✅ 沒有日文商品</p>';return}let h='';p.forEach(i=>{const img=i.image?`<img src="${i.image}">`:`<div class="no-image">無圖</div>`;h+=`<div class="product-item" id="product-${i.id}">${img}<div class="info"><div class="title">${i.title}</div><div class="meta">SKU:${i.sku||'無'}|¥${i.price}|${i.status}</div><div class="retranslate-status" id="status-${i.id}"></div></div><div class="actions"><button class="btn btn-success btn-sm" onclick="rt1('${i.id}')" id="rt-${i.id}">🔄</button><button class="btn btn-danger btn-sm" onclick="del1('${i.id}')" id="del-${i.id}">🗑️</button></div></div>`});c.innerHTML=h}async function rt1(id){const b=document.getElementById(`rt-${id}`);const s=document.getElementById(`status-${id}`);b.disabled=true;b.textContent='...';try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success){s.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}b.textContent='✓'}else{s.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;b.disabled=false;b.textContent='🔄'}}catch(e){s.innerHTML=`<span style="color:#e74c3c">❌ ${e.message}</span>`;b.disabled=false;b.textContent='🔄'}}async function del1(id){if(!confirm('確定刪除？'))return;const b=document.getElementById(`del-${id}`);b.disabled=true;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success)document.getElementById(`product-${id}`).remove();else{alert('失敗');b.disabled=false}}catch(e){alert(e.message);b.disabled=false}}async function retranslateAll(){if(!confirm(`翻譯全部 ${jp.length} 個？`))return;const b=document.getElementById('retranslateAllBtn');b.disabled=true;b.textContent='翻譯中...';let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();const st=document.getElementById(`status-${jp[i].id}`);if(d.success){s++;if(st)st.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${jp[i].id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}}else{f++;if(st)st.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;if(f>=3){alert('連續失敗');break}}}catch(e){f++}await new Promise(r=>setTimeout(r,1500))}alert(`成功:${s} 失敗:${f}`);b.textContent='🔄 全部翻譯';b.disabled=false;document.getElementById('progressText').textContent=''}async function deleteAllJP(){if(!confirm(`刪除全部 ${jp.length} 個？`))return;const b=document.getElementById('deleteAllBtn');b.disabled=true;let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();if(d.success){s++;const el=document.getElementById(`product-${jp[i].id}`);if(el)el.remove()}else f++}catch(e){f++}await new Promise(r=>setTimeout(r,300))}alert(`成功:${s} 失敗:${f}`);b.textContent='🗑️ 全部刪除';b.disabled=false;document.getElementById('progressText').textContent=''}</script></body></html>'''


@app.route('/api/scan-japanese')
def api_scan_japanese():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Token'}), 400
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=本高砂屋")
    while url:
        r = requests.get(url, headers=get_shopify_headers())
        if r.status_code != 200: break
        for p in r.json().get('products', []):
            sku = ''; price = ''
            for v in p.get('variants', []): sku = v.get('sku', ''); price = v.get('price', ''); break
            products.append({'id': p.get('id'), 'title': p.get('title', ''), 'handle': p.get('handle', ''),
                'sku': sku, 'price': price, 'vendor': p.get('vendor', ''), 'status': p.get('status', ''),
                'created_at': p.get('created_at', ''), 'image': p.get('image', {}).get('src', '') if p.get('image') else ''})
        lh = r.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    jp = [p for p in products if is_japanese_text(p.get('title', ''))]
    return jsonify({'total_products': len(products), 'japanese_count': len(jp), 'japanese_products': jp})


@app.route('/api/retranslate-product', methods=['POST'])
def api_retranslate_product():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Token'}), 400
    data = request.get_json()
    pid = data.get('product_id')
    if not pid:
        return jsonify({'error': '缺少 product_id'}), 400
    resp = requests.get(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers())
    if resp.status_code != 200:
        return jsonify({'error': f'無法取得: {resp.status_code}'}), 400
    product = resp.json().get('product', {})
    translated = translate_with_chatgpt(product.get('title', ''), product.get('body_html', ''))
    if not translated['success']:
        return jsonify({'success': False, 'error': f"翻譯失敗: {translated.get('error', '未知')}"})
    if is_japanese_text(translated['title']):
        retry = translate_with_chatgpt(product.get('title', ''), product.get('body_html', ''), retry=True)
        if retry['success'] and not is_japanese_text(retry['title']):
            translated = retry
        else:
            return jsonify({'success': False, 'error': '翻譯後仍含日文，請手動修改'})
    ok, r = update_product(pid, {
        'title': translated['title'], 'body_html': translated['description'],
        'metafields_global_title_tag': translated['page_title'],
        'metafields_global_description_tag': translated['meta_description']
    })
    if ok:
        return jsonify({'success': True, 'old_title': product.get('title', ''), 'new_title': translated['title'], 'product_id': pid})
    return jsonify({'success': False, 'error': f'更新失敗: {r.text[:200]}'})


@app.route('/api/delete-product', methods=['POST'])
def api_delete_product():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Token'}), 400
    data = request.get_json()
    pid = data.get('product_id')
    if not pid:
        return jsonify({'error': '缺少 product_id'}), 400
    return jsonify({'success': delete_product(pid), 'product_id': pid})


# === v2.3: 同步刪除 API ===
@app.route('/api/sync-delete', methods=['POST'])
def api_sync_delete():
    if scrape_status.get('running'): return jsonify({'error': '完整爬取正在進行中'}), 400
    if sync_status.get('running'): return jsonify({'error': '同步已在進行中'}), 400
    threading.Thread(target=sync_delete_stale_products, daemon=True).start()
    return jsonify({'message': '同步清理已啟動'})


@app.route('/api/sync-status')
def api_sync_status():
    return jsonify({**sync_status, **last_sync_log})


@app.route('/api/sync-log')
def api_sync_log():
    return jsonify(last_sync_log)


@app.route('/api/test-translate')
def api_test_translate():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({'error': 'OPENAI_API_KEY 未設定'})
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    result = translate_with_chatgpt("エコルセ E50", "本高砂屋の代表的な焼き菓子の詰め合わせです")
    result['key_preview'] = key_preview
    result['key_length'] = len(api_key)
    return jsonify(result)


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/start', methods=['POST', 'GET'])
@app.route('/api/start-scrape', methods=['POST', 'GET'])
def api_start():
    global scrape_status
    if scrape_status['running']: return jsonify({'error': '爬取正在進行中'}), 400
    if not load_shopify_token(): return jsonify({'error': '未設定 Shopify Token'}), 400
    test = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test['success']:
        return jsonify({'error': f"翻譯功能異常: {test.get('error', '未知')}"}), 400
    threading.Thread(target=run_scrape).start()
    return jsonify({'message': '本高砂屋 爬蟲已啟動'})


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token(): return jsonify({'success': False, 'error': '未設定 Token'})
    r = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    if r.status_code == 200: return jsonify({'success': True, 'shop': r.json()['shop']})
    return jsonify({'success': False, 'error': r.text}), 400


if __name__ == '__main__':
    print("=" * 50)
    print("本高砂屋 爬蟲工具 v2.3")
    print("修復: 重複上架 / 安全檢查 / 自動排程")
    print("=" * 50)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

# === v2.3: 在模組載入時啟動排程（gunicorn 也會觸發）===
if AUTO_SYNC_ENABLED:
    start_auto_sync_scheduler()

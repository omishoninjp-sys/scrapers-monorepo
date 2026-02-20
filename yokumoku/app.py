"""
YOKUMOKU 商品爬蟲 + Shopify 上架工具 v2.2
v2.1: 翻譯保護機制、日文商品掃描、翻譯驗證重試、環境變數、Docker/Zeabur 部署
v2.2: 缺貨商品自動刪除 - 官網消失或缺貨皆直接刪除
"""

from flask import Flask, jsonify, request
import requests
import re
import json
import os
import time
from urllib.parse import urljoin
import math
from playwright.sync_api import sync_playwright
import threading
import base64

app = Flask(__name__)

SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""
BASE_URL = "https://www.yokumoku.jp"
SEARCH_URL = "https://www.yokumoku.jp/search?including_oos=1"
BRAND_PREFIX = "YOKUMOKU"
MIN_PRICE = 1000
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

scrape_status = {
    "running": False, "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "uploaded": 0, "skipped": 0,
    "skipped_frozen": 0, "skipped_oos": 0, "skipped_exists": 0,
    "skipped_low_price": 0, "filtered_by_price": 0,
    "out_of_stock": 0, "deleted": 0,
    "translation_failed": 0, "translation_stopped": False
}


def is_japanese_text(text):
    if not text: return False
    check = text.replace('YOKUMOKU', '').strip()
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


def normalize_sku(sku):
    if not sku: return ""
    return sku.strip().lower()


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
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本甜點商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{clean_description[:1500]}

請回傳 JSON 格式（不要加 markdown 標記）：
{{"title":"翻譯後的商品名稱（前面加上 YOKUMOKU）","description":"翻譯後的商品說明（HTML）","page_title":"SEO標題50-60字","meta_description":"SEO描述100字內"}}

規則：1.YOKUMOKU高級洋菓子 2.開頭「YOKUMOKU」3.シガール→雪茄蛋捲（非香菸蛋捲）4.禁日文 5.只回傳JSON"""
    if retry:
        prompt += "\n\n【嚴重警告】上次翻譯結果仍然包含日文字元（平假名/片假名）！這次你必須：\n1. 將所有日文平假名、片假名完全翻譯成繁體中文\n2. シガール→雪茄蛋捲、サンクデリス→五味精選、ビエ→薄餅、ショコラ→巧克力、ラングドシャー→貓舌餅乾、オレ→牛奶、詰合せ→綜合禮盒\n3. 絕對不可以出現任何 ひらがな 或 カタカナ\n4. 商品名中的日文必須全部意譯成中文"
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。你的輸出必須完全使用繁體中文和英文，絕對禁止出現任何日文字元。"},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1000}, timeout=60)
        if r.status_code == 200:
            c = r.json()['choices'][0]['message']['content'].strip()
            if c.startswith('```'): c = c.split('\n', 1)[1]
            if c.endswith('```'): c = c.rsplit('```', 1)[0]
            t = json.loads(c.strip())
            tt = t.get('title', title)
            if not tt.startswith('YOKUMOKU'): tt = f"YOKUMOKU {tt}"
            return {'success': True, 'title': tt, 'description': t.get('description', description),
                    'page_title': t.get('page_title', ''), 'meta_description': t.get('meta_description', '')}
        else:
            return {'success': False, 'error': f"HTTP {r.status_code}: {r.text[:200]}",
                    'title': f"YOKUMOKU {title}", 'description': description, 'page_title': '', 'meta_description': ''}
    except Exception as e:
        return {'success': False, 'error': str(e),
                'title': f"YOKUMOKU {title}", 'description': description, 'page_title': '', 'meta_description': ''}


def download_image_to_base64(img_url, max_retries=3):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
               'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8', 'Referer': 'https://www.yokumoku.jp/'}
    for attempt in range(max_retries):
        try:
            r = requests.get(img_url, headers=headers, timeout=30)
            if r.status_code == 200:
                ct = r.headers.get('Content-Type', 'image/jpeg')
                fmt = 'image/png' if 'png' in ct else 'image/gif' if 'gif' in ct else 'image/webp' if 'webp' in ct else 'image/jpeg'
                return {'success': True, 'base64': base64.b64encode(r.content).decode('utf-8'), 'content_type': fmt}
        except Exception as e:
            print(f"[圖片下載] 第 {attempt+1} 次異常: {e}")
        time.sleep(1)
    return {'success': False}


# ========== Shopify 工具函數 ==========

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
                if sk and pid:
                    n = normalize_sku(sk)
                    pm[n] = pid
                    if sk != n: pm[sk] = pid
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
                if sk and pid: pm[normalize_sku(sk)] = pid
        lh = r.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    return pm


def delete_product(pid):
    return requests.delete(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers()).status_code == 200


def update_product(pid, data):
    r = requests.put(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers(),
        json={"product": {"id": pid, **data}})
    return r.status_code == 200, r


def get_or_create_collection(ct="YOKUMOKU"):
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


def parse_size_weight(text):
    text = text.replace('×', 'x').replace('Ｘ', 'x').replace('ｘ', 'x')
    text = text.replace('ｍｍ', 'mm').replace('ｇ', 'g').replace('ｋｇ', 'kg')
    text = text.replace('Φ', 'x').replace(',', '')
    dimension = None; weight_kg = None
    for pat in [r'(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*mm',
                r'(\d+)\s*[xX]\s*(\d+)\s*[xX]\s*(\d+)']:
        dm = re.search(pat, text, re.IGNORECASE)
        if dm:
            l, w, h = float(dm.group(1)), float(dm.group(2)), float(dm.group(3))
            dimension = {"l": l, "w": w, "h": h, "volume_weight": round((l*w*h)/6000000, 2)}
            break
    if not dimension:
        cm = re.search(r'(\d+(?:\.\d+)?)\s*[xX]\s*(\d+(?:\.\d+)?)\s*mm', text, re.IGNORECASE)
        if cm:
            d, h = float(cm.group(1)), float(cm.group(2))
            vol = math.pi * (d/2)**2 * h
            dimension = {"diameter": d, "height": h, "volume_weight": round(vol/6000000, 2)}
    wm = re.search(r'(\d+(?:\.\d+)?)\s*kg', text, re.IGNORECASE)
    gm = re.search(r'(\d+(?:\.\d+)?)\s*g(?![\w])', text)
    if wm: weight_kg = float(wm.group(1))
    elif gm: weight_kg = float(gm.group(1)) / 1000
    final = 0
    if dimension and weight_kg: final = max(dimension.get('volume_weight', 0), weight_kg)
    elif dimension: final = dimension.get('volume_weight', 0)
    elif weight_kg: final = weight_kg
    return {"dimension": dimension, "actual_weight": weight_kg, "final_weight": round(final, 2)}


# ========== Playwright 爬蟲 ==========

def scrape_product_list():
    products = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        page = context.new_page()
        print("[INFO] 正在載入商品列表頁面...")
        page.goto(SEARCH_URL, wait_until='networkidle', timeout=60000)
        time.sleep(3)
        last_height = 0; scroll_attempts = 0
        while scroll_attempts < 50:
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            time.sleep(1.5)
            new_height = page.evaluate('document.body.scrollHeight')
            if new_height == last_height:
                scroll_attempts += 1
                if scroll_attempts >= 3: break
            else: scroll_attempts = 0
            last_height = new_height
            current_count = len(page.query_selector_all('a[href*="/products/"]'))
            print(f"[進度] 已載入約 {current_count // 2} 個商品...")
        all_links = page.query_selector_all('a[href*="/products/"]')
        seen_skus = set()
        for link in all_links:
            try:
                href = link.get_attribute('href')
                if not href or '/products/' not in href: continue
                sku_match = re.search(r'/products/([a-f0-9]+)/', href)
                if not sku_match: continue
                sku = sku_match.group(1)
                if sku in seen_skus: continue
                seen_skus.add(sku)
                is_frozen = False
                try:
                    card = link.evaluate_handle('el => el.closest(".p-product-list__item") || el.closest("article") || el.closest("div")')
                    card_html = card.evaluate('el => el.innerHTML')
                    if '冷凍' in card_html: is_frozen = True
                except: pass
                if is_frozen:
                    print(f"[跳過] 冷凍商品: {sku}"); continue
                products.append({'url': urljoin(BASE_URL, href), 'sku': sku})
            except: continue
        browser.close()
    print(f"[INFO] 共收集 {len(products)} 個商品")
    return products


def check_product_in_stock(url):
    """v2.2: 快速檢查商品庫存狀態（不抓完整詳情）"""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            page = context.new_page()
            page.goto(url, wait_until='networkidle', timeout=30000)
            time.sleep(2)
            oos_btn = page.query_selector('button.oos') or page.query_selector('.oos')
            in_stock = True
            if oos_btn:
                oos_text = oos_btn.inner_text()
                if '品切れ' in oos_text or '在庫なし' in oos_text:
                    in_stock = False
            # 也檢查頁面文字
            if in_stock:
                pt = page.inner_text('body')
                if any(k in pt for k in ['品切れ', '在庫がありません', '在庫切れ', 'SOLD OUT', '売り切れ', '完売', '販売終了']):
                    in_stock = False
            browser.close()
            return in_stock
    except Exception as e:
        print(f"[庫存檢查錯誤] {url}: {e}")
        return True  # 錯誤時預設有庫存，避免誤刪


def scrape_product_detail(url):
    product = {'url': url, 'title': '', 'subtitle': '', 'price': 0, 'description': '',
               'size_weight_text': '', 'weight': 0, 'images': [], 'in_stock': True, 'is_frozen': False, 'sku': ''}
    sku_match = re.search(r'/products/([a-f0-9]+)/', url)
    if sku_match: product['sku'] = sku_match.group(1)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                                      viewport={'width': 1920, 'height': 1080})
        page = context.new_page()
        try:
            page.goto(url, wait_until='networkidle', timeout=60000)
            try: page.wait_for_selector('.p-details', timeout=10000)
            except: pass
            time.sleep(5)
            page.evaluate('window.scrollTo(0, document.body.scrollHeight / 2)')
            time.sleep(1)
            page.evaluate('window.scrollTo(0, 0)')
            time.sleep(1)
            oos_btn = page.query_selector('button.oos') or page.query_selector('.oos')
            if oos_btn:
                oos_text = oos_btn.inner_text()
                if '品切れ' in oos_text or '在庫なし' in oos_text: product['in_stock'] = False
            # === v2.2: 擴充缺貨偵測 ===
            if product['in_stock']:
                pt = page.inner_text('body')
                if any(k in pt for k in ['品切れ', '在庫がありません', '在庫切れ', 'SOLD OUT', '売り切れ', '完売', '販売終了']):
                    product['in_stock'] = False
            for sel in ['h1.h3.u-weight-bold', 'h1.u-weight-bold', '.p-details__title h1', '.p-details h1', 'h1']:
                el = page.query_selector(sel)
                if el:
                    t = el.inner_text().strip()
                    if t and len(t) > 2: product['title'] = t; break
            for sel in ['p.u-color-gray', '.p-details__subtitle', '.u-color-gray']:
                el = page.query_selector(sel)
                if el:
                    t = el.inner_text().strip()
                    if t and len(t) > 2: product['subtitle'] = t; break
            for sel in ['.p-price__price', '.p-price', '[class*="price"]', '.price']:
                el = page.query_selector(sel)
                if el:
                    pm = re.search(r'([\d,]+)', el.inner_text().replace('¥', '').replace('￥', ''))
                    if pm: product['price'] = int(pm.group(1).replace(',', '')); break
            for sel in ['.p-details__description', '.description', '[class*="description"]']:
                el = page.query_selector(sel)
                if el: product['description'] = el.inner_html(); break
            for dd in page.query_selector_all('dd'):
                text = dd.inner_text()
                has_size = (re.search(r'\d+\s*[×xXΦ]\s*\d+\s*[×xX]\s*\d+', text) or
                            re.search(r'\d+\s*Φ\s*[×xX]\s*\d+', text) or
                            re.search(r'\d+\s*[×xX]\s*\d+(?:\.\d+)?\s*mm', text))
                has_weight = re.search(r'\d+(?:,\d+)?\s*[gG]', text)
                if has_size or has_weight:
                    product['size_weight_text'] = text
                    wi = parse_size_weight(text)
                    product['weight'] = wi['final_weight']; break
            if not product['weight']:
                page_text = page.inner_text('body')
                wm = re.search(r'(\d+(?:,\d+)?)\s*[gG](?![\w])', page_text)
                if wm: product['weight'] = round(float(wm.group(1).replace(',', '')) / 1000, 2)
            if product['weight'] == 0: product['weight'] = 0.5
            images = []
            skip_patterns = ['dummy_product_thumbnail', 'play_button', 'details/caution/', 'about_clack', 'about_shopper', 'data:image/png;base64']
            try:
                for _ in range(3):
                    nb = page.query_selector('.slick-next')
                    if nb: nb.click(); time.sleep(0.3)
            except: pass
            tc = page.query_selector('.p-details__thumbnails')
            if tc:
                for img in tc.query_selector_all('img'):
                    src = img.get_attribute('data-src') or img.get_attribute('src')
                    if not src or any(pat in src for pat in skip_patterns): continue
                    src = re.sub(r'/ex/[\d.]+/[\d.]+/', '/full/', src)
                    src = re.sub(r'/ex/[\d.]+/', '/full/', src)
                    if src.startswith('//'): src = 'https:' + src
                    elif not src.startswith('http'): src = urljoin(BASE_URL, src)
                    if src not in images: images.append(src)
            if len(images) < 3:
                sc = page.query_selector('.p-details__mainimage')
                if sc:
                    for img in sc.query_selector_all('.slick-slide:not(.slick-cloned) img'):
                        src = img.get_attribute('data-src') or img.get_attribute('src')
                        if not src or any(pat in src for pat in skip_patterns): continue
                        src = re.sub(r'/ex/[\d.]+/', '/full/', src)
                        if src.startswith('//'): src = 'https:' + src
                        elif not src.startswith('http'): src = urljoin(BASE_URL, src)
                        if src not in images: images.append(src)
            if len(images) < 3:
                for img in page.query_selector_all('img[src*="cloudfront.net/full/goods/"], img[data-src*="cloudfront.net/full/goods/"]'):
                    src = img.get_attribute('data-src') or img.get_attribute('src')
                    if src and src not in images:
                        if src.startswith('//'): src = 'https:' + src
                        images.append(src)
            if not images:
                og = page.query_selector('meta[property="og:image"]')
                if og:
                    src = og.get_attribute('content')
                    if src: images.append(src)
            product['images'] = images[:10]
        except Exception as e:
            print(f"[ERROR] 爬取商品詳細失敗: {e}")
        finally:
            browser.close()
    return product


def upload_to_shopify(product, collection_id=None):
    original_title = product['title']
    if product.get('subtitle'): original_title = f"{product['title']} - {product['subtitle']}"
    translated = translate_with_chatgpt(original_title, product.get('description', ''))
    if not translated['success']:
        return {'success': False, 'error': 'translation_failed', 'translated': translated}
    if is_japanese_text(translated['title']):
        print(f"[翻譯驗證] 標題仍含日文，重試加強翻譯: {translated['title']}")
        retry_result = translate_with_chatgpt(original_title, product.get('description', ''), retry=True)
        if retry_result['success'] and not is_japanese_text(retry_result['title']):
            translated = retry_result
            print(f"[翻譯驗證] 重試成功: {translated['title']}")
        else:
            print(f"[翻譯驗證] 重試仍含日文，視為失敗")
            return {'success': False, 'error': 'translation_failed', 'translated': translated}
    cost = product['price']; weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    images_b64 = []
    for idx, iu in enumerate(product.get('images', [])):
        if not iu or not iu.startswith('http'): continue
        result = download_image_to_base64(iu)
        if result['success']:
            images_b64.append({'attachment': result['base64'], 'position': idx+1,
                               'filename': f"yokumoku_{product['sku']}_{idx+1}.jpg"})
        time.sleep(0.5)
    sp = {'product': {
        'title': translated['title'], 'body_html': translated['description'],
        'vendor': 'YOKUMOKU', 'product_type': 'クッキー・洋菓子',
        'status': 'active', 'published': True,
        'variants': [{'sku': product['sku'], 'price': f"{selling_price:.2f}", 'weight': weight,
                      'weight_unit': 'kg', 'inventory_management': None, 'inventory_policy': 'continue', 'requires_shipping': True}],
        'images': images_b64,
        'tags': 'YOKUMOKU, ヨックモック, 日本, 洋菓子, クッキー, シガール, 伴手禮, 日本代購, 雪茄蛋捲',
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


def run_scrape():
    global scrape_status
    try:
        scrape_status.update({"running": True, "progress": 0, "total": 0, "current_product": "",
            "products": [], "errors": [], "uploaded": 0, "skipped": 0,
            "skipped_frozen": 0, "skipped_oos": 0, "skipped_exists": 0,
            "skipped_low_price": 0, "filtered_by_price": 0,
            "out_of_stock": 0, "deleted": 0,
            "translation_failed": 0, "translation_stopped": False})

        scrape_status['current_product'] = "檢查 Shopify 已有商品..."
        all_pm = get_existing_products_map()
        existing_skus = set(all_pm.keys())

        scrape_status['current_product'] = "設定 Collection..."
        collection_id = get_or_create_collection("YOKUMOKU")

        scrape_status['current_product'] = "取得 Collection 商品..."
        cpm = get_collection_products_map(collection_id)
        collection_skus = set(cpm.keys())

        scrape_status['current_product'] = "爬取商品列表（需要時間）..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)

        website_skus = set(item['sku'] for item in product_list)
        # === v2.2: 記錄缺貨的 SKU ===
        out_of_stock_skus = set()
        ctf = 0

        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['sku']}"

            if item['sku'] in existing_skus:
                # === v2.2: 已上架商品檢查庫存 ===
                if item['sku'] in collection_skus:
                    if not check_product_in_stock(item['url']):
                        out_of_stock_skus.add(item['sku'])
                        scrape_status['out_of_stock'] += 1
                    time.sleep(0.5)
                scrape_status['skipped_exists'] += 1
                scrape_status['skipped'] += 1
                continue

            product = scrape_product_detail(item['url'])

            if product.get('is_frozen'):
                scrape_status['skipped_frozen'] += 1
                scrape_status['skipped'] += 1; continue

            # === v2.2: 缺貨 → 記錄 SKU，不上架 ===
            if not product.get('in_stock', True):
                out_of_stock_skus.add(item['sku'])
                scrape_status['skipped_oos'] += 1
                scrape_status['out_of_stock'] += 1
                continue

            if product.get('price', 0) < MIN_PRICE:
                scrape_status['skipped_low_price'] += 1
                scrape_status['filtered_by_price'] += 1
                scrape_status['skipped'] += 1; continue

            if not product.get('title') or not product.get('price'):
                scrape_status['errors'].append({'sku': item['sku'], 'error': '資訊不完整'}); continue

            result = upload_to_shopify(product, collection_id)
            if result['success']:
                existing_skus.add(product['sku'])
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'sku': product['sku'], 'title': result.get('translated', {}).get('title', product['title']),
                    'price': product['price'], 'selling_price': result.get('selling_price', 0),
                    'weight': product['weight'], 'status': 'success'})
                ctf = 0
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1; ctf += 1
                scrape_status['errors'].append({'sku': product['sku'], 'error': '翻譯失敗'})
                if ctf >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append({'error': f'翻譯連續失敗 {ctf} 次，自動停止'}); break
            else:
                scrape_status['errors'].append({'sku': product['sku'], 'error': result.get('error', '')}); ctf = 0
            time.sleep(1)

        # === v2.2: 合併需要刪除的 SKU ===
        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "清理缺貨/下架商品..."
            skus_to_delete = (collection_skus - website_skus) | (collection_skus & out_of_stock_skus)
            if skus_to_delete:
                print(f"[v2.2] 準備刪除 {len(skus_to_delete)} 個商品")
                for sku in skus_to_delete:
                    scrape_status['current_product'] = f"刪除: {sku}"
                    pid = cpm.get(sku)
                    if pid:
                        if delete_product(pid):
                            scrape_status['deleted'] += 1
                            print(f"[已刪除] SKU: {sku}, Product ID: {pid}")
                        else:
                            scrape_status['errors'].append({'sku': sku, 'error': '刪除失敗'})
                    time.sleep(0.3)

        scrape_status['current_product'] = "完成！" if not scrape_status['translation_stopped'] else "翻譯異常停止"
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
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>YOKUMOKU 爬蟲工具</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #1a1a2e;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#e94560;color:white;border:none;padding:12px 24px;border-radius:5px;cursor:pointer;font-size:16px;margin-right:10px;margin-bottom:10px;text-decoration:none;display:inline-block}.btn:hover{background:#c2185b}.btn:disabled{background:#ccc}.btn-secondary{background:#3498db}.btn-success{background:#27ae60}.progress-bar{width:100%;height:20px;background:#eee;border-radius:10px;overflow:hidden;margin:10px 0}.progress-fill{height:100%;background:linear-gradient(90deg,#e94560,#ff6b6b);transition:width 0.3s}.status{padding:10px;background:#f8f9fa;border-radius:5px;margin-top:10px}.log{max-height:300px;overflow-y:auto;font-family:monospace;font-size:13px;background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:5px}.stats{display:flex;gap:15px;margin-top:15px;flex-wrap:wrap}.stat{flex:1;min-width:70px;text-align:center;padding:15px;background:#f8f9fa;border-radius:5px}.stat-number{font-size:24px;font-weight:bold;color:#e94560}.stat-label{font-size:10px;color:#666;margin-top:5px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#e94560;text-decoration:none;font-weight:bold}.alert{padding:12px 16px;border-radius:5px;margin-bottom:15px}.alert-danger{background:#fee;border:1px solid #fcc;color:#c0392b}.formula{background:#1a1a2e;color:#4ade80;padding:15px;border-radius:8px;font-family:monospace;margin:10px 0;font-size:13px}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🍪 YOKUMOKU 爬蟲工具 <small style="font-size:14px;color:#999">v2.2</small></h1>
<div class="card"><h3>Shopify 連線</h3><p>Token: <span style="color:__TC__;">__TS__</span></p>
<div class="formula">售價 = (成本價 + 重量 × 1250) / 0.7<br>重量 = max(材積重量, 實際重量)</div>
<button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
<button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
<a href="/japanese-scan" class="btn btn-success">🇯🇵 日文掃描</a></div>
<div class="card"><h3>開始爬取</h3>
<p style="color:#666;font-size:14px">※ 排除冷凍商品 | &lt;¥__MIN_COST__ 跳過 | <b style="color:#e74c3c">翻譯保護</b> 連續失敗 __MAX_FAIL__ 次停止 | <b style="color:#e67e22">缺貨自動刪除</b><br>⚠️ 使用 Playwright 無頭瀏覽器，商品列表載入需要較長時間</p>
<button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
<div id="progressSection" style="display:none">
<div id="translationAlert" class="alert alert-danger" style="display:none">⚠️ 翻譯功能異常，已自動停止！</div>
<div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
<div class="status" id="statusText">準備中...</div>
<div class="stats">
<div class="stat"><div class="stat-number" id="uploadedCount">0</div><div class="stat-label">已上架</div></div>
<div class="stat"><div class="stat-number" id="skippedCount">0</div><div class="stat-label">已跳過</div></div>
<div class="stat"><div class="stat-number" id="translationFailedCount" style="color:#e74c3c">0</div><div class="stat-label">翻譯失敗</div></div>
<div class="stat"><div class="stat-number" id="filteredCount">0</div><div class="stat-label">價格過濾</div></div>
<div class="stat"><div class="stat-number" id="outOfStockCount">0</div><div class="stat-label">無庫存</div></div>
<div class="stat"><div class="stat-number" id="deletedCount" style="color:#e67e22">0</div><div class="stat-label">已刪除</div></div>
<div class="stat"><div class="stat-number" id="errorCount" style="color:#e74c3c">0</div><div class="stat-label">錯誤</div></div>
</div></div></div>
<div class="card"><h3>執行日誌</h3><div class="log" id="logArea">等待開始...</div></div>
<script>let pollInterval=null;function log(m,t){const l=document.getElementById('logArea');const tm=new Date().toLocaleTimeString();const c={success:'#4ec9b0',error:'#f14c4c'}[t]||'#d4d4d4';l.innerHTML+='<div style="color:'+c+'">['+tm+'] '+m+'</div>';l.scrollTop=l.scrollHeight}function clearLog(){document.getElementById('logArea').innerHTML=''}async function testShopify(){log('測試連線...');try{const r=await fetch('/api/test-shopify');const d=await r.json();if(d.success)log('✓ '+d.shop.name,'success');else log('✗ '+d.error,'error')}catch(e){log('✗ '+e.message,'error')}}async function testTranslate(){log('測試翻譯...');try{const r=await fetch('/api/test-translate');const d=await r.json();if(d.error)log('✗ '+d.error,'error');else if(d.success)log('✓ '+d.title,'success');else log('✗ 翻譯失敗','error')}catch(e){log('✗ '+e.message,'error')}}async function startScrape(){clearLog();log('開始爬取...');document.getElementById('startBtn').disabled=true;document.getElementById('progressSection').style.display='block';document.getElementById('translationAlert').style.display='none';try{const r=await fetch('/api/start-scrape',{method:'POST'});const d=await r.json();if(!d.success){log('✗ '+d.error,'error');document.getElementById('startBtn').disabled=false;return}log('✓ 已啟動（商品列表載入需要時間）','success');pollInterval=setInterval(pollStatus,2000)}catch(e){log('✗ '+e.message,'error');document.getElementById('startBtn').disabled=false}}async function pollStatus(){try{const r=await fetch('/api/status');const d=await r.json();const p=d.total>0?(d.progress/d.total*100):0;document.getElementById('progressFill').style.width=p+'%';document.getElementById('statusText').textContent=d.current_product+' ('+d.progress+'/'+d.total+')';document.getElementById('uploadedCount').textContent=d.uploaded;document.getElementById('skippedCount').textContent=d.skipped;document.getElementById('translationFailedCount').textContent=d.translation_failed||0;document.getElementById('filteredCount').textContent=d.filtered_by_price||0;document.getElementById('outOfStockCount').textContent=d.out_of_stock||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors.length;if(d.translation_stopped)document.getElementById('translationAlert').style.display='block';if(!d.running&&d.progress>0){clearInterval(pollInterval);document.getElementById('startBtn').disabled=false;if(d.translation_stopped)log('⚠️ 翻譯異常停止','error');else log('========== 完成 ==========','success')}}catch(e){console.error(e)}}</script></body></html>"""
    return html.replace('__TC__', tc).replace('__TS__', ts).replace('__MIN_COST__', str(MIN_PRICE)).replace('__MAX_FAIL__', str(MAX_CONSECUTIVE_TRANSLATION_FAILURES))


@app.route('/japanese-scan')
def japanese_scan_page():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>日文商品掃描 - YOKUMOKU</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #27ae60;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#e94560;color:white;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-size:14px;margin-right:10px;margin-bottom:10px}.btn:disabled{background:#ccc}.btn-danger{background:#e74c3c}.btn-success{background:#27ae60}.btn-sm{padding:5px 10px;font-size:12px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#e94560;text-decoration:none;font-weight:bold}.stats{display:flex;gap:15px;margin:20px 0;flex-wrap:wrap}.stat{flex:1;min-width:150px;text-align:center;padding:20px;background:#f8f9fa;border-radius:8px}.stat-number{font-size:36px;font-weight:bold}.stat-label{font-size:14px;color:#666;margin-top:5px}.product-item{display:flex;align-items:center;padding:15px;border-bottom:1px solid #eee;gap:15px}.product-item:last-child{border-bottom:none}.product-item img{width:60px;height:60px;object-fit:cover;border-radius:4px}.product-item .info{flex:1}.product-item .info .title{font-weight:bold;margin-bottom:5px;color:#c0392b}.product-item .info .meta{font-size:12px;color:#666}.no-image{width:60px;height:60px;background:#eee;display:flex;align-items:center;justify-content:center;border-radius:4px;color:#999;font-size:10px}.retranslate-status{font-size:12px;margin-top:5px}.action-bar{position:sticky;top:0;background:white;padding:15px;margin:-20px -20px 20px -20px;border-bottom:1px solid #ddd;z-index:100;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🇯🇵 日文商品掃描 - YOKUMOKU</h1>
<div class="card"><p>掃描 Shopify 中 YOKUMOKU 的日文（未翻譯）商品。</p><button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button><span id="scanStatus"></span></div>
<div class="stats" id="statsSection" style="display:none"><div class="stat"><div class="stat-number" id="totalProducts" style="color:#3498db">0</div><div class="stat-label">YOKUMOKU 商品數</div></div><div class="stat"><div class="stat-number" id="japaneseCount" style="color:#e74c3c">0</div><div class="stat-label">日文商品</div></div></div>
<div class="card" id="resultsCard" style="display:none"><div class="action-bar"><div><button class="btn btn-success" id="retranslateAllBtn" onclick="retranslateAll()" disabled>🔄 全部翻譯</button><button class="btn btn-danger" id="deleteAllBtn" onclick="deleteAllJP()" disabled>🗑️ 全部刪除</button></div><div id="progressText"></div></div><div id="results"></div></div>
<script>let jp=[];async function startScan(){document.getElementById('scanBtn').disabled=true;document.getElementById('scanStatus').textContent='掃描中...';try{const r=await fetch('/api/scan-japanese');const d=await r.json();if(d.error){alert(d.error);return}jp=d.japanese_products;document.getElementById('totalProducts').textContent=d.total_products;document.getElementById('japaneseCount').textContent=d.japanese_count;document.getElementById('statsSection').style.display='flex';renderResults(d.japanese_products);document.getElementById('resultsCard').style.display='block';document.getElementById('retranslateAllBtn').disabled=jp.length===0;document.getElementById('deleteAllBtn').disabled=jp.length===0;document.getElementById('scanStatus').textContent='完成！'}catch(e){alert(e.message)}finally{document.getElementById('scanBtn').disabled=false}}function renderResults(p){const c=document.getElementById('results');if(!p.length){c.innerHTML='<p style="text-align:center;color:#27ae60;font-size:18px">✅ 沒有日文商品</p>';return}let h='';p.forEach(i=>{const img=i.image?`<img src="${i.image}">`:`<div class="no-image">無圖</div>`;h+=`<div class="product-item" id="product-${i.id}">${img}<div class="info"><div class="title">${i.title}</div><div class="meta">SKU:${i.sku||'無'}|¥${i.price}|${i.status}</div><div class="retranslate-status" id="status-${i.id}"></div></div><div class="actions"><button class="btn btn-success btn-sm" onclick="rt1('${i.id}')" id="rt-${i.id}">🔄</button><button class="btn btn-danger btn-sm" onclick="del1('${i.id}')" id="del-${i.id}">🗑️</button></div></div>`});c.innerHTML=h}async function rt1(id){const b=document.getElementById(`rt-${id}`);const s=document.getElementById(`status-${id}`);b.disabled=true;b.textContent='...';try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success){s.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}b.textContent='✓'}else{s.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;b.disabled=false;b.textContent='🔄'}}catch(e){s.innerHTML=`<span style="color:#e74c3c">❌ ${e.message}</span>`;b.disabled=false;b.textContent='🔄'}}async function del1(id){if(!confirm('確定刪除？'))return;const b=document.getElementById(`del-${id}`);b.disabled=true;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success)document.getElementById(`product-${id}`).remove();else{alert('失敗');b.disabled=false}}catch(e){alert(e.message);b.disabled=false}}async function retranslateAll(){if(!confirm(`翻譯全部 ${jp.length} 個？`))return;const b=document.getElementById('retranslateAllBtn');b.disabled=true;b.textContent='翻譯中...';let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();const st=document.getElementById(`status-${jp[i].id}`);if(d.success){s++;if(st)st.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${jp[i].id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}}else{f++;if(st)st.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;if(f>=3){alert('連續失敗');break}}}catch(e){f++}await new Promise(r=>setTimeout(r,1500))}alert(`成功:${s} 失敗:${f}`);b.textContent='🔄 全部翻譯';b.disabled=false;document.getElementById('progressText').textContent=''}async function deleteAllJP(){if(!confirm(`刪除全部 ${jp.length} 個？`))return;const b=document.getElementById('deleteAllBtn');b.disabled=true;let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();if(d.success){s++;const el=document.getElementById(`product-${jp[i].id}`);if(el)el.remove()}else f++}catch(e){f++}await new Promise(r=>setTimeout(r,300))}alert(`成功:${s} 失敗:${f}`);b.textContent='🗑️ 全部刪除';b.disabled=false;document.getElementById('progressText').textContent=''}</script></body></html>'''


# ========== API 路由 ==========

@app.route('/api/scan-japanese')
def api_scan_japanese():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=YOKUMOKU")
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
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    data = request.get_json(); pid = data.get('product_id')
    if not pid: return jsonify({'error': '缺少 product_id'}), 400
    resp = requests.get(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers())
    if resp.status_code != 200: return jsonify({'error': f'無法取得: {resp.status_code}'}), 400
    product = resp.json().get('product', {})
    translated = translate_with_chatgpt(product.get('title', ''), product.get('body_html', ''))
    if not translated['success']:
        return jsonify({'success': False, 'error': f"翻譯失敗: {translated.get('error', '未知')}"})
    if is_japanese_text(translated['title']):
        retry_result = translate_with_chatgpt(product.get('title', ''), product.get('body_html', ''), retry=True)
        if retry_result['success'] and not is_japanese_text(retry_result['title']): translated = retry_result
        else: return jsonify({'success': False, 'error': '翻譯後仍含日文，請手動修改'})
    ok, r = update_product(pid, {'title': translated['title'], 'body_html': translated['description'],
        'metafields_global_title_tag': translated['page_title'], 'metafields_global_description_tag': translated['meta_description']})
    if ok: return jsonify({'success': True, 'old_title': product.get('title', ''), 'new_title': translated['title'], 'product_id': pid})
    return jsonify({'success': False, 'error': f'更新失敗: {r.text[:200]}'})


@app.route('/api/delete-product', methods=['POST'])
def api_delete_product():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    data = request.get_json(); pid = data.get('product_id')
    if not pid: return jsonify({'error': '缺少 product_id'}), 400
    return jsonify({'success': delete_product(pid), 'product_id': pid})


@app.route('/api/test-translate')
def api_test_translate():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key: return jsonify({'error': 'OPENAI_API_KEY 未設定'})
    kp = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    result = translate_with_chatgpt("【5種 40個入り】サンク デリス", "シガールと季節限定クッキーの詰め合わせです")
    result['key_preview'] = kp; result['key_length'] = len(api_key)
    return jsonify(result)


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/start-scrape', methods=['POST'])
def start_scrape():
    global scrape_status
    if scrape_status['running']: return jsonify({'success': False, 'error': '爬取正在進行中'})
    if not load_shopify_token(): return jsonify({'success': False, 'error': '未設定環境變數'})
    test = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test['success']:
        return jsonify({'success': False, 'error': f"翻譯功能異常: {test.get('error', '未知')}"})
    threading.Thread(target=run_scrape).start()
    return jsonify({'success': True, 'message': '開始爬取'})


@app.route('/api/start', methods=['POST'])
def cron_trigger():
    global scrape_status
    if scrape_status['running']: return jsonify({'success': False, 'error': '爬取正在進行中'}), 409
    if not load_shopify_token(): return jsonify({'success': False, 'error': '未設定環境變數'}), 500
    test = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test['success']:
        return jsonify({'success': False, 'error': f"翻譯功能異常: {test.get('error', '未知')}"}), 400
    threading.Thread(target=run_scrape).start()
    return jsonify({'success': True, 'message': 'Cron job triggered', 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')})


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token(): return jsonify({'success': False, 'error': '未設定環境變數'})
    r = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    if r.status_code == 200: return jsonify({'success': True, 'shop': r.json()['shop']})
    return jsonify({'success': False, 'error': r.text}), 400


@app.route('/api/test-scrape')
def test_scrape():
    product = scrape_product_detail("https://www.yokumoku.jp/products/5d70b5f1dbbfdc006fd21f3c/%E3%80%905%E7%A8%AE-40%E5%80%8B%E5%85%A5%E3%82%8A%E3%80%91%E3%82%B5%E3%83%B3%E3%82%AF-%E3%83%87%E3%83%AA%E3%82%B9")
    if product.get('price') and product.get('weight'):
        product['selling_price'] = calculate_selling_price(product['price'], product['weight'])
    return jsonify(product)


if __name__ == '__main__':
    print("=" * 50)
    print("YOKUMOKU 爬蟲工具 v2.2")
    print("新增: 缺貨商品自動刪除")
    print("=" * 50)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

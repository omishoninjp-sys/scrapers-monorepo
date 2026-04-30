"""
The Maple Mania 楓糖男孩 商品爬蟲 + Shopify 上架工具 v2.2
v2.1: 翻譯保護機制、日文商品掃描、測試翻譯
v2.2: 缺貨商品自動刪除 - 官網消失或缺貨皆直接刪除
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
BASE_URL = "https://sucreyshopping.jp"
LIST_PAGES = [
    "https://sucreyshopping.jp/shop/c/c10/?brand=themaplemania",
    "https://sucreyshopping.jp/shop/c/c10_p2/?brand=themaplemania",
    "https://sucreyshopping.jp/shop/c/c10_p3/?brand=themaplemania",
    "https://sucreyshopping.jp/shop/c/c10_p4/?brand=themaplemania",
]
BRAND_PREFIX = "The maple mania 楓糖男孩"
MIN_PRICE = 1000
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3
SHIPPING_HTML = '<div style="margin-top:24px;border-top:1px solid #e8eaf0;padding-top:20px;"><h2 style="font-size:16px;font-weight:700;color:#1a1a2e;border-bottom:2px solid #e8eaf0;padding-bottom:8px;margin:0 0 16px;">國際運費（空運・包稅）</h2><p style="margin:0 0 6px;font-size:13px;color:#444;">✓ 含關稅\u3000✓ 含台灣配送費\u3000✓ 只收實重\u3000✓ 無材積費</p><p style="margin:0 0 12px;font-size:13px;color:#444;">起運 1 kg，未滿 1 kg 以 1 kg 計算，每增加 0.5 kg 加收 ¥500。</p><table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;"><tbody><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">≦ 1.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,000 <span style="color:#888;font-weight:400;">≈ NT$200</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.1 ～ 1.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥1,500 <span style="color:#888;font-weight:400;">≈ NT$300</span></td></tr><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">1.6 ～ 2.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,000 <span style="color:#888;font-weight:400;">≈ NT$400</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.1 ～ 2.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥2,500 <span style="color:#888;font-weight:400;">≈ NT$500</span></td></tr><tr style="background:#f0f4ff;"><td style="padding:9px 14px;border:1px solid #dde3f0;">2.6 ～ 3.0 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;font-weight:600;">¥3,000 <span style="color:#888;font-weight:400;">≈ NT$600</span></td></tr><tr style="background:#fff;"><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">每增加 0.5 kg</td><td style="padding:9px 14px;border:1px solid #dde3f0;color:#555;">+¥500\u3000<span style="color:#888;">+≈ NT$100</span></td></tr></tbody></table><p style="margin:0 0 28px;font-size:12px;color:#999;">NT$ 匯率僅供參考，實際以下單當日匯率為準。運費於商品到倉後出貨前確認重量後統一請款。</p></div>'
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
}
session = requests.Session()
session.headers.update(BROWSER_HEADERS)

scrape_status = {
    "running": False, "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "uploaded": 0, "skipped": 0,
    "skipped_low_price": 0, "skipped_points": 0, "skipped_exists": 0,
    "filtered_by_price": 0, "out_of_stock": 0, "deleted": 0,
    "translation_failed": 0, "translation_stopped": False
}


def is_japanese_text(text):
    if not text: return False
    check = text.replace('The maple mania', '').replace('楓糖男孩', '').strip()
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
    token_file = "shopify_token.json"
    if os.path.exists(token_file):
        with open(token_file, 'r') as f:
            data = json.load(f)
            SHOPIFY_ACCESS_TOKEN = data.get('access_token', '')
            shop = data.get('shop', '')
            if shop: SHOPIFY_SHOP = shop.replace('https://','').replace('http://','').replace('.myshopify.com','').strip('/')
            return True
    return False


def get_shopify_headers():
    return {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}


def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


def normalize_sku(sku):
    if not sku: return ""
    return sku.strip().lower()


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
    clean_desc = clean_html_for_translation(description)
    clean_desc = re.sub(r'[\d,]+\s*円', '', clean_desc)
    clean_desc = re.sub(r'價格[：:]\s*[\d,]+\s*日圓', '', clean_desc)
    clean_desc = re.sub(r'税込[\d,]+円', '', clean_desc)
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。將以下日本商品資訊翻譯成繁體中文並優化 SEO。

商品名稱：{title}
商品說明：{clean_desc[:1500]}

只回傳此 JSON 格式，不加 markdown、不加任何其他文字：
{"title":"翻譯後的商品名稱","description":"翻譯後的商品說明（HTML格式）","page_title":"SEO標題50字以內","meta_description":"SEO描述100字以內"}

規則：
1. 品牌背景：日本東京人氣伴手禮品牌，主打楓糖風味餅乾
2. 標題開頭必須是「The maple mania 楓糖男孩」，後接繁體中文商品名，不得省略
3. 【強制禁止日文】所有輸出必須是繁體中文或英文，不可出現任何平假名或片假名
4. 詞彙對照：メープルバタークッキー→楓糖奶油餅乾；詰合せ→綜合禮盒；說明禁止含任何價格資訊
5. SEO 關鍵字必須自然融入，包含：The maple mania、楓糖男孩、日本、東京、伴手禮、楓糖餅乾
6. 只回傳 JSON，不得有任何其他文字"""
    if retry:
        prompt += "\n\n【重要警告】前次翻譯輸出仍含有日文字元（平假名或片假名），請這次嚴格執行：\n1. 所有日文必須完整翻譯成繁體中文，不得保留任何假名\n2. 若不確定翻譯，請意譯其含義，絕對不可直接保留日文\n3. 商品名稱中的日文單字全部必須翻譯"
    try:
        response = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。"},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1000}, timeout=60)
        if response.status_code == 200:
            content = response.json()['choices'][0]['message']['content'].strip()
            if content.startswith('```'): content = content.split('\n', 1)[1]
            if content.endswith('```'): content = content.rsplit('```', 1)[0]
            translated = json.loads(content.strip())
            t = translated.get('title', title)
            if not t.startswith('The maple mania') and not t.startswith('The Maple Mania'):
                t = f"{BRAND_PREFIX} {t}"
            desc = translated.get('description', description)
            desc = re.sub(r'[\d,]+\s*円', '', desc)
            desc = re.sub(r'[\d,]+\s*日圓', '', desc)
            return {'success': True, 'title': t, 'description': desc,
                    'page_title': translated.get('page_title', ''), 'meta_description': translated.get('meta_description', '')}
        else:
            return {'success': False, 'error': f"HTTP {response.status_code}: {response.text[:200]}",
                    'title': f"{BRAND_PREFIX} {title}", 'description': description, 'page_title': '', 'meta_description': ''}
    except Exception as e:
        return {'success': False, 'error': str(e),
                'title': f"{BRAND_PREFIX} {title}", 'description': description, 'page_title': '', 'meta_description': ''}


def download_image_to_base64(img_url, max_retries=3):
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'image/*', 'Referer': 'https://sucreyshopping.jp/'}
    for attempt in range(max_retries):
        try:
            r = requests.get(img_url, headers=headers, timeout=30)
            if r.status_code == 200:
                ct = r.headers.get('Content-Type', 'image/jpeg')
                fmt = 'image/png' if 'png' in ct else 'image/gif' if 'gif' in ct else 'image/webp' if 'webp' in ct else 'image/jpeg'
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
                if sk and pid:
                    n = normalize_sku(sk)
                    pm[n] = {'product_id': pid, 'variant_id': v.get('id'), 'price': float(v.get('price') or 0)}
                    if sk != n: pm[sk] = pm[n]
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


def get_or_create_collection(ct="The maple mania 楓糖男孩"):
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
    text = text.replace('Φ', 'x').replace(',', '').replace('（', '(').replace('）', ')')
    dimension = None; weight_kg = None
    for pat in [r'W\s*(\d+(?:\.\d+)?)\s*[xX×]\s*D\s*(\d+(?:\.\d+)?)\s*[xX×]\s*H\s*(\d+(?:\.\d+)?)',
                r'(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(?:\(?\s*mm\s*\)?)?']:
        dm = re.search(pat, text, re.IGNORECASE)
        if dm:
            l, w, h = float(dm.group(1)), float(dm.group(2)), float(dm.group(3))
            dimension = {"l": l, "w": w, "h": h, "volume_weight": round((l*w*h)/6000000, 2)}; break
    wm = re.search(r'(\d+(?:\.\d+)?)\s*kg', text, re.IGNORECASE)
    gm = re.search(r'(\d+(?:\.\d+)?)\s*g(?![\w])', text)
    if wm: weight_kg = float(wm.group(1))
    elif gm: weight_kg = float(gm.group(1)) / 1000
    final = 0
    if dimension and weight_kg: final = max(dimension.get('volume_weight', 0), weight_kg)
    elif dimension: final = dimension.get('volume_weight', 0)
    elif weight_kg: final = weight_kg
    return {"dimension": dimension, "actual_weight": weight_kg, "final_weight": round(final, 2)}


def scrape_product_list():
    products = []; seen_skus = set()
    for page_url in LIST_PAGES:
        try:
            r = session.get(page_url, timeout=30)
            if r.status_code != 200: continue
            soup = BeautifulSoup(r.text, 'html.parser')
            for link in soup.select('a[href*="/shop/g/g"]'):
                href = link.get('href', '')
                sm = re.search(r'/shop/g/g([^/]+)/', href)
                if not sm: continue
                sku = normalize_sku(sm.group(1))
                if sku in seen_skus: continue
                seen_skus.add(sku)
                parent = link.find_parent(['dl', 'div', 'li'])
                if parent:
                    pt = parent.get_text()
                    if 'ポイント' in pt and '円' not in pt: continue
                    if 'お急ぎ便' in pt: continue
                    pm = re.search(r'([\d,]+)円', pt)
                    price = int(pm.group(1).replace(',', '')) if pm else 0
                    if 0 < price < MIN_PRICE: continue
                else: price = 0
                products.append({'url': urljoin(BASE_URL, href), 'sku': sku, 'sku_raw': sm.group(1), 'list_price': price})
            time.sleep(1)
        except Exception as e:
            print(f"[ERROR] {e}"); continue
    return products


def scrape_product_detail(url, max_retries=3):
    product = {'url': url, 'title': '', 'price': 0, 'description': '', 'size_weight_text': '',
        'weight': 0, 'images': [], 'sku': '', 'sku_raw': '', 'is_points': False, 'in_stock': True}
    sm = re.search(r'/shop/g/g([^/]+)/', url)
    if sm: product['sku_raw'] = sm.group(1); product['sku'] = normalize_sku(sm.group(1))
    for attempt in range(max_retries):
        try:
            r = session.get(url, timeout=30)
            if r.status_code != 200: continue
            soup = BeautifulSoup(r.text, 'html.parser'); pt = soup.get_text()
            if 'ポイント' in pt and re.search(r'\d+ポイント', pt) and not re.search(r'[\d,]+円', pt):
                product['is_points'] = True; return product

            # === v2.2: 缺貨偵測 ===
            if any(kw in pt for kw in ['品切れ', '在庫なし', 'SOLD OUT', '在庫がありません', '完売', '売り切れ']):
                product['in_stock'] = False

            for sel in ['h1.goods-name', 'h1[class*="goods"]', '.goods-detail h1', 'h1']:
                el = soup.select_one(sel)
                if el:
                    t = el.get_text(strip=True)
                    if t and len(t) > 2: product['title'] = t; break
            for sel in ['.block-goods-price--price', '.js-enhanced-ecommerce-goods-price', '.price']:
                el = soup.select_one(sel)
                if el:
                    pm = re.search(r'([\d,]+)', el.get_text())
                    if pm: product['price'] = int(pm.group(1).replace(',', '')); break
            if not product['price']:
                pm = re.search(r'([\d,]+)\s*円', pt)
                if pm: product['price'] = int(pm.group(1).replace(',', ''))
            for sel in ['.goods-description', '.item-description', '.product-description']:
                el = soup.select_one(sel)
                if el: product['description'] = str(el); break
            for dl in soup.select('dl'):
                if '箱サイズ' in dl.get_text() or 'サイズ' in dl.get_text():
                    dd = dl.select_one('dd')
                    if dd:
                        product['size_weight_text'] = dd.get_text()
                        wi = parse_size_weight(dd.get_text())
                        if wi['final_weight'] > 0: product['weight'] = wi['final_weight']; break
            if product['weight'] == 0:
                sm2 = re.search(r'W\s*(\d+)\s*[×xX]\s*D\s*(\d+)\s*[×xX]\s*H\s*(\d+)', pt)
                if sm2: product['weight'] = round((float(sm2.group(1))*float(sm2.group(2))*float(sm2.group(3)))/6000000, 2)
            if product['weight'] == 0:
                wm = re.search(r'(\d+(?:,\d+)?)\s*[gG](?!ift)', pt)
                if wm: product['weight'] = round(float(wm.group(1).replace(',',''))/1000, 2)
            if product['weight'] == 0: product['weight'] = 0.5
            images = []
            for img in soup.select('img[src*="/img/goods/"]'):
                src = img.get('src') or img.get('data-src')
                if src:
                    src = src.replace('/S/', '/L/').replace('/M/', '/L/')
                    if src.startswith('//'): src = 'https:' + src
                    elif not src.startswith('http'): src = urljoin(BASE_URL, src)
                    if src not in images: images.append(src)
            if not images:
                og = soup.select_one('meta[property="og:image"]')
                if og and og.get('content'):
                    s = og.get('content')
                    if not s.startswith('http'): s = urljoin(BASE_URL, s)
                    images.append(s)
            product['images'] = images[:10]
            return product
        except Exception as e:
            if attempt < max_retries - 1: time.sleep(3)
    return product


def check_product_in_stock(sku):
    """v2.2: 爬取商品頁面確認庫存狀態"""
    url = f"{BASE_URL}/shop/g/g{sku}/"
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            return True  # 預設有庫存（安全預設）
        pt = BeautifulSoup(r.text, 'html.parser').get_text()
        if any(kw in pt for kw in ['品切れ', '在庫なし', 'SOLD OUT', '在庫がありません', '完売', '売り切れ']):
            return False
        return True
    except:
        return True  # 錯誤時預設有庫存


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
    cost = product['price']
    selling_price = calculate_selling_price(cost)
    images_b64 = []
    for idx, iu in enumerate(product.get('images', [])):
        if not iu or not iu.startswith('http'): continue
        result = download_image_to_base64(iu)
        if result['success']:
            images_b64.append({'attachment': result['base64'], 'position': idx+1, 'filename': f"maple_mania_{product['sku']}_{idx+1}.jpg"})
        time.sleep(0.5)
    sp = {'product': {
        'title': translated['title'], 'body_html': translated['description'] + SHIPPING_HTML,
        'vendor': 'The maple mania 楓糖男孩', 'product_type': 'クッキー・洋菓子',
        'status': 'active', 'published': True,
        'variants': [{'sku': product['sku'], 'price': f"{selling_price:.2f}",
            'inventory_management': None, 'inventory_policy': 'continue', 'requires_shipping': True}],
        'images': images_b64,
        'tags': 'The maple mania, 楓糖男孩, メープルマニア, 日本, 東京, 伴手禮, 東京土産, 日本代購, 楓糖餅乾',
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
            "skipped_low_price": 0, "skipped_points": 0, "skipped_exists": 0,
            "filtered_by_price": 0, "out_of_stock": 0, "deleted": 0,
            "translation_failed": 0, "translation_stopped": False})

        scrape_status['current_product'] = "檢查 Shopify 已有商品..."
        all_pm = get_existing_products_map()
        existing_skus = set(all_pm.keys())

        scrape_status['current_product'] = "設定 Collection..."
        collection_id = get_or_create_collection("The maple mania 楓糖男孩")

        scrape_status['current_product'] = "取得 Collection 商品..."
        cpm = get_collection_products_map(collection_id); collection_skus = set(cpm.keys())

        scrape_status['current_product'] = "爬取商品列表..."
        product_list = scrape_product_list(); scrape_status['total'] = len(product_list)

        website_skus = set(item['sku'] for item in product_list)

        # === v2.2: 記錄缺貨的 SKU ===
        out_of_stock_skus = set()

        ctf = 0

        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['sku']}"

            if item['sku'] in existing_skus:
                # 已上架商品：爬詳情取得現在售價 + 缺貨偵測
                if item['sku'] in collection_skus:
                    product = scrape_product_detail(item['url'])
                    if product:
                        if not product.get('in_stock', True):
                            out_of_stock_skus.add(item['sku'])
                            print(f"[缺貨偵測] {item['sku']} 官網缺貨，稍後刪除")
                        elif product.get('price', 0) >= MIN_PRICE:
                            new_selling_price = calculate_selling_price(product['price'])
                            variant_info = all_pm.get(item['sku'], {})
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
                scrape_status['skipped_exists'] += 1; scrape_status['skipped'] += 1; continue

            product = scrape_product_detail(item['url'])
            if not product: scrape_status['errors'].append({'sku': item['sku'], 'error': '爬取失敗'}); continue

            if product.get('sku') and product['sku'] in existing_skus:
                scrape_status['skipped_exists'] += 1; scrape_status['skipped'] += 1; continue
            if product.get('is_points'):
                scrape_status['skipped_points'] += 1; scrape_status['skipped'] += 1; continue
            if 'お急ぎ便' in product.get('title', ''):
                scrape_status['skipped'] += 1; continue

            # === v2.2: 缺貨 → 記錄 SKU，不上架 ===
            if not product.get('in_stock', True):
                out_of_stock_skus.add(product['sku'])
                scrape_status['out_of_stock'] += 1
                continue

            if product.get('price', 0) < MIN_PRICE:
                scrape_status['skipped_low_price'] += 1; scrape_status['filtered_by_price'] += 1; scrape_status['skipped'] += 1; continue
            if not product.get('title') or not product.get('price'):
                scrape_status['errors'].append({'sku': item['sku'], 'error': '資訊不完整'}); continue

            result = upload_to_shopify(product, collection_id)
            if result['success']:
                existing_skus.add(product['sku']); scrape_status['uploaded'] += 1
                scrape_status['products'].append({'sku': product['sku'],
                    'title': result.get('translated', {}).get('title', product['title']),
                    'price': product['price'], 'selling_price': result.get('selling_price', 0),
                    'weight': product['weight'], 'status': 'success'})
                ctf = 0
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1; ctf += 1
                scrape_status['errors'].append({'sku': product['sku'], 'error': '翻譯失敗'})
                if ctf >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append({'error': f'翻譯連續失敗 {ctf} 次，自動停止'})
                    break
            else:
                scrape_status['errors'].append({'sku': product['sku'], 'error': result.get('error', '')})
                ctf = 0
            time.sleep(1)

        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "清理缺貨/下架商品..."

            # === v2.2: 合併需要刪除的 SKU ===
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


# ========== 運費 HTML 批次更新 ==========

update_shipping_status = {"running": False, "done": 0, "total": 0, "skipped": 0, "errors": []}


def run_update_shipping():
    global update_shipping_status
    update_shipping_status = {"running": True, "done": 0, "total": 0, "skipped": 0, "errors": []}
    try:
        collection_id = get_or_create_collection("The maple mania 楓糖男孩")
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
    tc = 'green' if token_loaded else 'red'
    ts = '✓ 已載入' if token_loaded else '✗ 未設定'
    html = """<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>楓糖男孩 爬蟲工具</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #8B4513;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1);}.btn{background:#8B4513;color:white;border:none;padding:12px 24px;border-radius:5px;cursor:pointer;font-size:16px;margin-right:10px;margin-bottom:10px;text-decoration:none;display:inline-block}.btn:hover{background:#6B3510}.btn:disabled{background:#ccc}.btn-secondary{background:#3498db}.btn-success{background:#27ae60}.progress-bar{width:100%;height:20px;background:#eee;border-radius:10px;overflow:hidden;margin:10px 0}.progress-fill{height:100%;background:linear-gradient(90deg,#8B4513,#D2691E);transition:width 0.3s}.status{padding:10px;background:#f8f9fa;border-radius:5px;margin-top:10px}.log{max-height:300px;overflow-y:auto;font-family:monospace;font-size:13px;background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:5px}.stats{display:flex;gap:15px;margin-top:15px;flex-wrap:wrap}.stat{flex:1;min-width:70px;text-align:center;padding:15px;background:#f8f9fa;border-radius:5px}.stat-number{font-size:24px;font-weight:bold;color:#8B4513}.stat-label{font-size:10px;color:#666;margin-top:5px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}.alert{padding:12px 16px;border-radius:5px;margin-bottom:15px}.alert-danger{background:#fee;border:1px solid #fcc;color:#c0392b}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🍁 楓糖男孩 爬蟲工具 <small style="font-size:14px;color:#999">v2.2</small></h1>
<div class="card"><h3>Shopify 連線</h3><p>Token: <span style="color:__TC__;">__TS__</span></p>
<button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
<button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
<a href="/japanese-scan" class="btn btn-success">🇯🇵 日文掃描</a> <button class="btn" style="background:#2ecc71" onclick="updateShipping()">📦 更新運費說明</button></div>
<div class="card"><h3>開始爬取</h3>
<p style="color:#666;font-size:14px">※ &lt;¥1000、點數、お急ぎ便跳過 | <b style="color:#e74c3c">翻譯保護</b> 連續失敗 __MAX_FAIL__ 次停止 | <b style="color:#e67e22">缺貨自動刪除</b></p>
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
<script>let pollInterval=null;function log(m,t){const l=document.getElementById('logArea');const tm=new Date().toLocaleTimeString();const c={success:'#4ec9b0',error:'#f14c4c'}[t]||'#d4d4d4';l.innerHTML+='<div style="color:'+c+'">['+tm+'] '+m+'</div>';l.scrollTop=l.scrollHeight}function clearLog(){document.getElementById('logArea').innerHTML=''}async function testShopify(){log('測試連線...');try{const r=await fetch('/api/test-shopify');const d=await r.json();if(d.success)log('✓ '+d.shop.name,'success');else log('✗ '+d.error,'error')}catch(e){log('✗ '+e.message,'error')}}async function testTranslate(){log('測試翻譯...');try{const r=await fetch('/api/test-translate');const d=await r.json();if(d.error)log('✗ '+d.error,'error');else if(d.success)log('✓ '+d.title,'success');else log('✗ 翻譯失敗','error')}catch(e){log('✗ '+e.message,'error')}}async function startScrape(){clearLog();log('開始爬取...');document.getElementById('startBtn').disabled=true;document.getElementById('progressSection').style.display='block';document.getElementById('translationAlert').style.display='none';try{const r=await fetch('/api/start-scrape',{method:'POST'});const d=await r.json();if(!d.success){log('✗ '+d.error,'error');document.getElementById('startBtn').disabled=false;return}log('✓ 已啟動','success');pollInterval=setInterval(pollStatus,1000)}catch(e){log('✗ '+e.message,'error');document.getElementById('startBtn').disabled=false}}async function pollStatus(){try{const r=await fetch('/api/status');const d=await r.json();const p=d.total>0?(d.progress/d.total*100):0;document.getElementById('progressFill').style.width=p+'%';document.getElementById('statusText').textContent=d.current_product+' ('+d.progress+'/'+d.total+')';document.getElementById('uploadedCount').textContent=d.uploaded;document.getElementById('skippedCount').textContent=d.skipped;document.getElementById('translationFailedCount').textContent=d.translation_failed||0;document.getElementById('filteredCount').textContent=d.filtered_by_price||0;document.getElementById('outOfStockCount').textContent=d.out_of_stock||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors.length;if(d.translation_stopped)document.getElementById('translationAlert').style.display='block';if(!d.running&&d.progress>0){clearInterval(pollInterval);document.getElementById('startBtn').disabled=false;if(d.translation_stopped)log('⚠️ 翻譯異常停止','error');else log('========== 完成 ==========','success')}}catch(e){console.error(e)}}
        async function updateShipping() {
            const b = document.querySelector('[onclick=\"updateShipping()\"]');
            b.disabled = true; b.textContent = '更新中...';
            try {
                const r = await fetch('/api/update-shipping', {method: 'POST'});
                const d = await r.json();
                if (d.error) { alert('錯誤: ' + d.error); b.disabled=false; b.textContent='📦 更新運費說明'; return; }
                const poll = setInterval(async () => {
                    const sr = await fetch('/api/update-shipping-status');
                    const sd = await sr.json();
                    b.textContent = '更新中 ' + sd.done + '/' + sd.total + ' (跳過' + sd.skipped + ')';
                    if (!sd.running) {
                        clearInterval(poll);
                        b.disabled = false;
                        b.textContent = '✓ 完成 更新' + sd.done + ' 跳過' + sd.skipped + ' 錯誤' + sd.errors.length;
                    }
                }, 1500);
            } catch(e) { alert(e.message); b.disabled=false; b.textContent='📦 更新運費說明'; }
        }
        </script></body></html>"""
    return html.replace('__TC__', tc).replace('__TS__', ts).replace('__MIN_COST__', str(MIN_PRICE)).replace('__MAX_FAIL__', str(MAX_CONSECUTIVE_TRANSLATION_FAILURES))


@app.route('/japanese-scan')
def japanese_scan_page():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>日文商品掃描 - 楓糖男孩</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #27ae60;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#8B4513;color:white;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-size:14px;margin-right:10px;margin-bottom:10px}.btn:disabled{background:#ccc}.btn-danger{background:#e74c3c}.btn-success{background:#27ae60}.btn-sm{padding:5px 10px;font-size:12px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}.stats{display:flex;gap:15px;margin:20px 0;flex-wrap:wrap}.stat{flex:1;min-width:150px;text-align:center;padding:20px;background:#f8f9fa;border-radius:8px}.stat-number{font-size:36px;font-weight:bold}.stat-label{font-size:14px;color:#666;margin-top:5px}.product-item{display:flex;align-items:center;padding:15px;border-bottom:1px solid #eee;gap:15px}.product-item:last-child{border-bottom:none}.product-item img{width:60px;height:60px;object-fit:cover;border-radius:4px}.product-item .info{flex:1}.product-item .info .title{font-weight:bold;margin-bottom:5px;color:#c0392b}.product-item .info .meta{font-size:12px;color:#666}.no-image{width:60px;height:60px;background:#eee;display:flex;align-items:center;justify-content:center;border-radius:4px;color:#999;font-size:10px}.retranslate-status{font-size:12px;margin-top:5px}.action-bar{position:sticky;top:0;background:white;padding:15px;margin:-20px -20px 20px -20px;border-bottom:1px solid #ddd;z-index:100;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🇯🇵 日文商品掃描 - 楓糖男孩</h1>
<div class="card"><p>掃描 Shopify 中楓糖男孩的日文（未翻譯）商品。</p><button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button><span id="scanStatus"></span></div>
<div class="stats" id="statsSection" style="display:none"><div class="stat"><div class="stat-number" id="totalProducts" style="color:#3498db">0</div><div class="stat-label">楓糖男孩商品數</div></div><div class="stat"><div class="stat-number" id="japaneseCount" style="color:#e74c3c">0</div><div class="stat-label">日文商品</div></div></div>
<div class="card" id="resultsCard" style="display:none"><div class="action-bar"><div><button class="btn btn-success" id="retranslateAllBtn" onclick="retranslateAll()" disabled>🔄 全部翻譯</button><button class="btn btn-danger" id="deleteAllBtn" onclick="deleteAllJP()" disabled>🗑️ 全部刪除</button></div><div id="progressText"></div></div><div id="results"></div></div>
<script>let jp=[];async function startScan(){document.getElementById('scanBtn').disabled=true;document.getElementById('scanStatus').textContent='掃描中...';try{const r=await fetch('/api/scan-japanese');const d=await r.json();if(d.error){alert(d.error);return}jp=d.japanese_products;document.getElementById('totalProducts').textContent=d.total_products;document.getElementById('japaneseCount').textContent=d.japanese_count;document.getElementById('statsSection').style.display='flex';renderResults(d.japanese_products);document.getElementById('resultsCard').style.display='block';document.getElementById('retranslateAllBtn').disabled=jp.length===0;document.getElementById('deleteAllBtn').disabled=jp.length===0;document.getElementById('scanStatus').textContent='完成！'}catch(e){alert(e.message)}finally{document.getElementById('scanBtn').disabled=false}}function renderResults(p){const c=document.getElementById('results');if(!p.length){c.innerHTML='<p style="text-align:center;color:#27ae60;font-size:18px">✅ 沒有日文商品</p>';return}let h='';p.forEach(i=>{const img=i.image?`<img src="${i.image}">`:`<div class="no-image">無圖</div>`;h+=`<div class="product-item" id="product-${i.id}">${img}<div class="info"><div class="title">${i.title}</div><div class="meta">SKU:${i.sku||'無'}|¥${i.price}|${i.status}</div><div class="retranslate-status" id="status-${i.id}"></div></div><div class="actions"><button class="btn btn-success btn-sm" onclick="rt1('${i.id}')" id="rt-${i.id}">🔄</button><button class="btn btn-danger btn-sm" onclick="del1('${i.id}')" id="del-${i.id}">🗑️</button></div></div>`});c.innerHTML=h}async function rt1(id){const b=document.getElementById(`rt-${id}`);const s=document.getElementById(`status-${id}`);b.disabled=true;b.textContent='...';try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success){s.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}b.textContent='✓'}else{s.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;b.disabled=false;b.textContent='🔄'}}catch(e){s.innerHTML=`<span style="color:#e74c3c">❌ ${e.message}</span>`;b.disabled=false;b.textContent='🔄'}}async function del1(id){if(!confirm('確定刪除？'))return;const b=document.getElementById(`del-${id}`);b.disabled=true;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success)document.getElementById(`product-${id}`).remove();else{alert('失敗');b.disabled=false}}catch(e){alert(e.message);b.disabled=false}}async function retranslateAll(){if(!confirm(`翻譯全部 ${jp.length} 個？`))return;const b=document.getElementById('retranslateAllBtn');b.disabled=true;b.textContent='翻譯中...';let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();const st=document.getElementById(`status-${jp[i].id}`);if(d.success){s++;if(st)st.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${jp[i].id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}}else{f++;if(st)st.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;if(f>=3){alert('連續失敗');break}}}catch(e){f++}await new Promise(r=>setTimeout(r,1500))}alert(`成功:${s} 失敗:${f}`);b.textContent='🔄 全部翻譯';b.disabled=false;document.getElementById('progressText').textContent=''}async function deleteAllJP(){if(!confirm(`刪除全部 ${jp.length} 個？`))return;const b=document.getElementById('deleteAllBtn');b.disabled=true;let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();if(d.success){s++;const el=document.getElementById(`product-${jp[i].id}`);if(el)el.remove()}else f++}catch(e){f++}await new Promise(r=>setTimeout(r,300))}alert(`成功:${s} 失敗:${f}`);b.textContent='🗑️ 全部刪除';b.disabled=false;document.getElementById('progressText').textContent=''}</script></body></html>'''


@app.route('/api/scan-japanese')
def api_scan_japanese():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=The+maple+mania+楓糖男孩")
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
        retry = translate_with_chatgpt(product.get('title', ''), product.get('body_html', ''), retry=True)
        if retry['success'] and not is_japanese_text(retry['title']): translated = retry
        else: return jsonify({'success': False, 'error': '翻譯後仍含日文'})
    ok, r = update_product(pid, {'title': translated['title'], 'body_html': translated['description'] + SHIPPING_HTML,
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
    result = translate_with_chatgpt("メープルバタークッキー 9枚入", "メープルシュガーをたっぷり使った焼き菓子です")
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
    product = scrape_product_detail("https://sucreyshopping.jp/shop/g/gtmm01107/")
    if product.get('price') and product.get('weight'):
        product['selling_price'] = calculate_selling_price(product['price'], product['weight'])
    return jsonify(product)


if __name__ == '__main__':
    print("=" * 50)
    print("The Maple Mania 楓糖男孩 爬蟲工具 v2.2")
    print("新增: 缺貨商品自動刪除")
    print("=" * 50)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

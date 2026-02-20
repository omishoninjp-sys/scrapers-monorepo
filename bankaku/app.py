"""
坂角總本舖商品爬蟲 + Shopify 上架工具 v2.2
v2.2: 缺貨商品自動刪除（官網消失或缺貨皆刪除）
v2.1: 翻譯保護機制、日文商品掃描、測試翻譯
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

app = Flask(__name__)

SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""
BASE_URL = "https://www.bankaku.co.jp"
CATEGORY_URLS = [
    "https://www.bankaku.co.jp/shop/c/c1010/",
    "https://www.bankaku.co.jp/shop/c/c1020/",
]
MIN_COST_THRESHOLD = 1000
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7',
    'Referer': 'https://www.bankaku.co.jp/',
}
session = requests.Session()
session.headers.update(BROWSER_HEADERS)

scrape_status = {
    "running": False, "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "uploaded": 0, "skipped": 0,
    "filtered_by_price": 0, "deleted": 0,
    "translation_failed": 0, "translation_stopped": False
}


def is_japanese_text(text):
    if not text: return False
    check = text.strip()
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


def translate_with_chatgpt(title, description):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。翻譯成繁體中文並優化 SEO。

商品名稱：{title}
商品說明：{description}

回傳 JSON（不加 markdown）：
{{"title":"翻譯名稱","description":"翻譯說明","page_title":"SEO標題50-60字","meta_description":"SEO描述100字內"}}

規則：1.坂角總本舖海老煎餅 2.ゆかり可保留或譯「緣」3.自然流暢 4.含關鍵字：坂角總本舖、日本、海老煎餅、伴手禮 5.只回傳JSON"""
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini", "messages": [
                {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家。"},
                {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1000}, timeout=60)
        if r.status_code == 200:
            c = r.json()['choices'][0]['message']['content'].strip()
            if c.startswith('```'): c = c.split('\n', 1)[1]
            if c.endswith('```'): c = c.rsplit('```', 1)[0]
            t = json.loads(c.strip())
            return {'success': True, 'title': t.get('title', title), 'description': t.get('description', description),
                    'page_title': t.get('page_title', ''), 'meta_description': t.get('meta_description', '')}
        else:
            return {'success': False, 'error': f"HTTP {r.status_code}: {r.text[:200]}",
                    'title': title, 'description': description, 'page_title': '', 'meta_description': ''}
    except Exception as e:
        return {'success': False, 'error': str(e),
                'title': title, 'description': description, 'page_title': '', 'meta_description': ''}


def get_existing_skus():
    return set(get_existing_products_map().keys())


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


def set_product_to_draft(pid):
    return requests.put(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers(),
        json={"product": {"id": pid, "status": "draft"}}).status_code == 200


def delete_product(pid):
    return requests.delete(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers()).status_code == 200


def update_product(pid, data):
    r = requests.put(shopify_api_url(f"products/{pid}.json"), headers=get_shopify_headers(),
        json={"product": {"id": pid, **data}})
    return r.status_code == 200, r


def get_or_create_collection(ct="坂角總本舖"):
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


def parse_dimension_weight(soup):
    dimension = None; weight = None; text = soup.get_text()
    dm = re.search(r'縦\s*(\d+(?:\.\d+)?)\s*[×xX]\s*横\s*(\d+(?:\.\d+)?)\s*[×xX]\s*高さ\s*(\d+(?:\.\d+)?)\s*cm', text)
    if dm:
        h, w, d = float(dm.group(1)), float(dm.group(2)), float(dm.group(3))
        dimension = {"h": h, "w": w, "d": d, "volume_weight": round((h * w * d) / 6000, 2)}
    wm = re.search(r'重量[：:]\s*(\d+(?:\.\d+)?)\s*(?:kg|g)', text, re.IGNORECASE)
    if wm:
        wv = float(wm.group(1)); unit = wm.group(0)
        weight = wv if 'kg' in unit.lower() else wv / 1000
    final = 0
    if dimension and weight: final = max(dimension['volume_weight'], weight)
    elif dimension: final = dimension['volume_weight']
    elif weight: final = weight
    else:
        cm = re.search(r'(\d+)枚', text)
        if cm: final = round((int(cm.group(1)) * 7 + 100) / 1000, 2)
    return {"dimension": dimension, "actual_weight": weight, "final_weight": round(final, 2)}


def scrape_product_list(category_urls):
    products = []; seen_skus = set()
    session.get(BASE_URL, timeout=30); time.sleep(0.5)
    for cat_url in category_urls:
        page = 1
        while page <= 10:
            url = cat_url if page == 1 else f"{cat_url.rstrip('/')}_p{page}/"
            try:
                r = session.get(url, timeout=30); r.encoding = 'utf-8'
                if r.status_code != 200: break
                if page > 1 and '_p' not in r.url: break
                soup = BeautifulSoup(r.text, 'html.parser')
                links = soup.find_all('a', href=re.compile(r'/shop/g/g[A-Za-z0-9]+/'))
                new_count = 0; seen_page = set()
                for link in links:
                    sm = re.search(r'/g/g([A-Za-z0-9]+)/', link.get('href', ''))
                    if sm:
                        sku = sm.group(1)
                        if sku in seen_page: continue
                        seen_page.add(sku)
                        if sku not in seen_skus:
                            products.append({'url': urljoin(BASE_URL, link.get('href','')), 'sku': sku})
                            seen_skus.add(sku); new_count += 1
                if new_count == 0: break
                page += 1; time.sleep(0.5)
            except: break
    return products


def scrape_product_detail(url):
    try:
        r = session.get(url, timeout=30); r.encoding = 'utf-8'
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.text, 'html.parser')
        title = ""
        h1 = soup.select_one('h1')
        if h1: title = h1.get_text(strip=True)
        if not title:
            tt = soup.select_one('title')
            if tt: title = tt.get_text(strip=True).split(':')[0].split('|')[0].strip()
        desc = ""
        for sel in ['.block-goods-comment', '.item-description', '.product-description']:
            de = soup.select_one(sel)
            if de: desc = de.get_text(strip=True); break
        if not desc and h1:
            ne = h1.find_next_sibling()
            if ne: desc = ne.get_text(strip=True)[:200]
        price = 0; pt = soup.get_text()
        pm = re.search(r'([\d,]+)円\s*\(?税込\)?', pt)
        if pm: price = int(pm.group(1).replace(',', ''))
        sku = ""; um = re.search(r'/g/g([A-Za-z0-9]+)/', url)
        if um: sku = um.group(1)
        in_stock = not any(k in pt for k in ['在庫がありません', '在庫切れ', '品切れ', 'SOLD OUT'])
        wi = parse_dimension_weight(soup)
        images = []; seen = set()
        for il in soup.select('a[href*="/img/goods/"]'):
            href = il.get('href', '')
            if href and '/img/goods/' in href:
                fs = urljoin(BASE_URL, href)
                if fs not in seen: seen.add(fs); images.append(fs)
        if not images:
            for img in soup.select('img[src*="/img/goods/"]'):
                src = img.get('src', '')
                if src and '/img/goods/' in src and 'lazyload' not in src:
                    fs = urljoin(BASE_URL, src)
                    if fs not in seen: seen.add(fs); images.append(fs)
        return {'url': url, 'sku': sku, 'title': title, 'price': price, 'in_stock': in_stock,
                'description': desc, 'weight': wi['final_weight'], 'weight_info': wi, 'images': images[:10]}
    except Exception as e:
        print(f"[錯誤] {url}: {e}"); return None


def upload_to_shopify(product, collection_id=None):
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    if not translated['success']:
        return {'success': False, 'error': 'translation_failed', 'translated': translated}
    cost = product['price']; weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    images = [{'src': u, 'position': i+1} for i, u in enumerate(product.get('images', []))]
    sp = {'product': {
        'title': translated['title'], 'body_html': translated['description'],
        'vendor': '坂角總本舖', 'product_type': '海老煎餅',
        'status': 'active', 'published': True,
        'variants': [{'sku': product['sku'], 'price': f"{selling_price:.2f}", 'weight': weight,
            'weight_unit': 'kg', 'inventory_management': None, 'inventory_policy': 'continue', 'requires_shipping': True}],
        'images': images, 'tags': '坂角總本舖, 日本, 海老煎餅, えびせんべい, ゆかり, 伴手禮, 日本零食',
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
            "filtered_by_price": 0, "deleted": 0,
            "translation_failed": 0, "translation_stopped": False})
        scrape_status['current_product'] = "檢查 Shopify 商品..."
        existing_skus = get_existing_skus()
        scrape_status['current_product'] = "設定 Collection..."
        collection_id = get_or_create_collection("坂角總本舖")
        scrape_status['current_product'] = "取得 Collection 商品..."
        cpm = get_collection_products_map(collection_id)
        collection_skus = set(cpm.keys())
        scrape_status['current_product'] = "爬取商品列表..."
        product_list = scrape_product_list(CATEGORY_URLS)
        scrape_status['total'] = len(product_list)
        website_skus = set(item['sku'] for item in product_list)

        # === v2.2: 記錄缺貨的 SKU ===
        out_of_stock_skus = set()

        ctf = 0
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['sku']}"

            # 已存在於 Shopify → 需要檢查庫存狀態
            if item['sku'] in existing_skus:
                # 如果這個 SKU 也在 collection 裡，爬詳情確認是否缺貨
                if item['sku'] in collection_skus:
                    product = scrape_product_detail(item['url'])
                    if product and not product['in_stock']:
                        out_of_stock_skus.add(item['sku'])
                    time.sleep(0.5)
                scrape_status['skipped'] += 1
                continue

            product = scrape_product_detail(item['url'])
            if not product: scrape_status['errors'].append(f"無法爬取: {item['url']}"); continue
            if product['price'] < MIN_COST_THRESHOLD: scrape_status['filtered_by_price'] += 1; continue

            # 缺貨 → 不上架，記錄 SKU
            if not product['in_stock']:
                out_of_stock_skus.add(item['sku'])
                scrape_status['skipped'] += 1
                continue

            result = upload_to_shopify(product, collection_id)
            if result['success']:
                existing_skus.add(product['sku']); scrape_status['uploaded'] += 1; ctf = 0
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1; ctf += 1
                if ctf >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append(f'翻譯連續失敗 {ctf} 次，自動停止'); break
            else:
                scrape_status['errors'].append(f"上傳失敗 {product['sku']}"); ctf = 0
            time.sleep(1)

        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "清理缺貨/下架商品..."

            # === v2.2: 合併需要刪除的 SKU ===
            # 1. 官網已消失的 SKU（collection 有但官網沒有）
            # 2. 官網還在但缺貨的 SKU
            skus_to_delete = (collection_skus - website_skus) | (collection_skus & out_of_stock_skus)

            for sku in skus_to_delete:
                pid = cpm.get(sku)
                if pid:
                    if delete_product(pid):
                        scrape_status['deleted'] += 1
                    else:
                        scrape_status['errors'].append(f"刪除失敗: {sku}")
                time.sleep(0.3)

        scrape_status['current_product'] = "完成" if not scrape_status['translation_stopped'] else "翻譯異常停止"
    except Exception as e:
        scrape_status['errors'].append(str(e))
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
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>坂角總本舖 爬蟲工具</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #D4AF37;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#D4AF37;color:white;border:none;padding:12px 24px;border-radius:5px;cursor:pointer;font-size:16px;margin-right:10px;margin-bottom:10px;text-decoration:none;display:inline-block}.btn:hover{background:#B8972E}.btn:disabled{background:#ccc}.btn-secondary{background:#3498db}.btn-success{background:#27ae60}.progress-bar{width:100%;height:20px;background:#eee;border-radius:10px;overflow:hidden;margin:10px 0}.progress-fill{height:100%;background:linear-gradient(90deg,#D4AF37,#F0D078);transition:width 0.3s}.status{padding:10px;background:#f8f9fa;border-radius:5px;margin-top:10px}.log{max-height:300px;overflow-y:auto;font-family:monospace;font-size:13px;background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:5px}.stats{display:flex;gap:15px;margin-top:15px;flex-wrap:wrap}.stat{flex:1;min-width:70px;text-align:center;padding:15px;background:#f8f9fa;border-radius:5px}.stat-number{font-size:24px;font-weight:bold;color:#D4AF37}.stat-label{font-size:10px;color:#666;margin-top:5px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#D4AF37;text-decoration:none;font-weight:bold}.alert{padding:12px 16px;border-radius:5px;margin-bottom:15px}.alert-danger{background:#fee;border:1px solid #fcc;color:#c0392b}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🦐 坂角總本舖 爬蟲工具 <small style="font-size:14px;color:#999">v2.2</small></h1>
<div class="card"><h3>Shopify 連線</h3><p>Token: <span style="color:__TC__;">__TS__</span></p>
<button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
<button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
<a href="/japanese-scan" class="btn btn-success">🇯🇵 日文掃描</a></div>
<div class="card"><h3>開始爬取</h3>
<p>爬取 bankaku.co.jp 全站商品並上架到 Shopify</p>
<p style="color:#666;font-size:14px">※ &lt;¥__MIN_COST__ 跳過 | <b style="color:#e74c3c">翻譯保護</b> 連續失敗 __MAX_FAIL__ 次停止 | <b style="color:#e67e22">缺貨自動刪除</b></p>
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
<div class="stat"><div class="stat-number" id="deletedCount" style="color:#e67e22">0</div><div class="stat-label">已刪除</div></div>
<div class="stat"><div class="stat-number" id="errorCount" style="color:#e74c3c">0</div><div class="stat-label">錯誤</div></div>
</div></div></div>
<div class="card"><h3>執行日誌</h3><div class="log" id="logArea">等待開始...</div></div>
<script>let pollInterval=null;function log(m,t){const l=document.getElementById('logArea');const tm=new Date().toLocaleTimeString();const c={success:'#4ec9b0',error:'#f14c4c'}[t]||'#d4d4d4';l.innerHTML+='<div style="color:'+c+'">['+tm+'] '+m+'</div>';l.scrollTop=l.scrollHeight}function clearLog(){document.getElementById('logArea').innerHTML=''}async function testShopify(){log('測試連線...');try{const r=await fetch('/api/test-shopify');const d=await r.json();if(d.success)log('✓ '+d.shop.name,'success');else log('✗ '+d.error,'error')}catch(e){log('✗ '+e.message,'error')}}async function testTranslate(){log('測試翻譯...');try{const r=await fetch('/api/test-translate');const d=await r.json();if(d.error)log('✗ '+d.error,'error');else if(d.success)log('✓ '+d.title,'success');else log('✗ 翻譯失敗','error')}catch(e){log('✗ '+e.message,'error')}}async function startScrape(){clearLog();log('開始爬取...');document.getElementById('startBtn').disabled=true;document.getElementById('progressSection').style.display='block';document.getElementById('translationAlert').style.display='none';try{const r=await fetch('/api/start',{method:'POST'});const d=await r.json();if(d.error){log('✗ '+d.error,'error');document.getElementById('startBtn').disabled=false;return}log('✓ 已啟動','success');pollInterval=setInterval(pollStatus,1000)}catch(e){log('✗ '+e.message,'error');document.getElementById('startBtn').disabled=false}}async function pollStatus(){try{const r=await fetch('/api/status');const d=await r.json();const p=d.total>0?(d.progress/d.total*100):0;document.getElementById('progressFill').style.width=p+'%';document.getElementById('statusText').textContent=d.current_product+' ('+d.progress+'/'+d.total+')';document.getElementById('uploadedCount').textContent=d.uploaded;document.getElementById('skippedCount').textContent=d.skipped;document.getElementById('translationFailedCount').textContent=d.translation_failed||0;document.getElementById('filteredCount').textContent=d.filtered_by_price||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors.length;if(d.translation_stopped)document.getElementById('translationAlert').style.display='block';if(!d.running&&d.progress>0){clearInterval(pollInterval);document.getElementById('startBtn').disabled=false;if(d.translation_stopped)log('⚠️ 翻譯異常停止','error');else log('========== 完成 ==========','success')}}catch(e){console.error(e)}}</script></body></html>"""
    return html.replace('__TC__', tc).replace('__TS__', ts).replace('__MIN_COST__', str(MIN_COST_THRESHOLD)).replace('__MAX_FAIL__', str(MAX_CONSECUTIVE_TRANSLATION_FAILURES))



@app.route('/japanese-scan')
def japanese_scan_page():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>日文商品掃描 - 坂角總本舖</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #27ae60;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#D4AF37;color:white;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-size:14px;margin-right:10px;margin-bottom:10px}.btn:disabled{background:#ccc}.btn-danger{background:#e74c3c}.btn-success{background:#27ae60}.btn-sm{padding:5px 10px;font-size:12px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}.stats{display:flex;gap:15px;margin:20px 0;flex-wrap:wrap}.stat{flex:1;min-width:150px;text-align:center;padding:20px;background:#f8f9fa;border-radius:8px}.stat-number{font-size:36px;font-weight:bold}.stat-label{font-size:14px;color:#666;margin-top:5px}.product-item{display:flex;align-items:center;padding:15px;border-bottom:1px solid #eee;gap:15px}.product-item:last-child{border-bottom:none}.product-item img{width:60px;height:60px;object-fit:cover;border-radius:4px}.product-item .info{flex:1}.product-item .info .title{font-weight:bold;margin-bottom:5px;color:#c0392b}.product-item .info .meta{font-size:12px;color:#666}.no-image{width:60px;height:60px;background:#eee;display:flex;align-items:center;justify-content:center;border-radius:4px;color:#999;font-size:10px}.retranslate-status{font-size:12px;margin-top:5px}.action-bar{position:sticky;top:0;background:white;padding:15px;margin:-20px -20px 20px -20px;border-bottom:1px solid #ddd;z-index:100;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🇯🇵 日文商品掃描 - 坂角總本舖</h1>
<div class="card"><p>掃描 Shopify 中坂角總本舖 的日文（未翻譯）商品。</p><button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button><span id="scanStatus"></span></div>
<div class="stats" id="statsSection" style="display:none"><div class="stat"><div class="stat-number" id="totalProducts" style="color:#3498db">0</div><div class="stat-label">坂角總本舖商品數</div></div><div class="stat"><div class="stat-number" id="japaneseCount" style="color:#e74c3c">0</div><div class="stat-label">日文商品</div></div></div>
<div class="card" id="resultsCard" style="display:none"><div class="action-bar"><div><button class="btn btn-success" id="retranslateAllBtn" onclick="retranslateAll()" disabled>🔄 全部翻譯</button><button class="btn btn-danger" id="deleteAllBtn" onclick="deleteAllJP()" disabled>🗑️ 全部刪除</button></div><div id="progressText"></div></div><div id="results"></div></div>
<script>let jp=[];async function startScan(){document.getElementById('scanBtn').disabled=true;document.getElementById('scanStatus').textContent='掃描中...';try{const r=await fetch('/api/scan-japanese');const d=await r.json();if(d.error){alert(d.error);return}jp=d.japanese_products;document.getElementById('totalProducts').textContent=d.total_products;document.getElementById('japaneseCount').textContent=d.japanese_count;document.getElementById('statsSection').style.display='flex';renderResults(d.japanese_products);document.getElementById('resultsCard').style.display='block';document.getElementById('retranslateAllBtn').disabled=jp.length===0;document.getElementById('deleteAllBtn').disabled=jp.length===0;document.getElementById('scanStatus').textContent='完成！'}catch(e){alert(e.message)}finally{document.getElementById('scanBtn').disabled=false}}function renderResults(p){const c=document.getElementById('results');if(!p.length){c.innerHTML='<p style="text-align:center;color:#27ae60;font-size:18px">✅ 沒有日文商品</p>';return}let h='';p.forEach(i=>{const img=i.image?`<img src="${i.image}">`:`<div class="no-image">無圖</div>`;h+=`<div class="product-item" id="product-${i.id}">${img}<div class="info"><div class="title">${i.title}</div><div class="meta">SKU:${i.sku||'無'}|¥${i.price}|${i.status}</div><div class="retranslate-status" id="status-${i.id}"></div></div><div class="actions"><button class="btn btn-success btn-sm" onclick="rt1('${i.id}')" id="rt-${i.id}">🔄</button><button class="btn btn-danger btn-sm" onclick="del1('${i.id}')" id="del-${i.id}">🗑️</button></div></div>`});c.innerHTML=h}async function rt1(id){const b=document.getElementById(`rt-${id}`);const s=document.getElementById(`status-${id}`);b.disabled=true;b.textContent='...';try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success){s.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}b.textContent='✓'}else{s.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;b.disabled=false;b.textContent='🔄'}}catch(e){s.innerHTML=`<span style="color:#e74c3c">❌ ${e.message}</span>`;b.disabled=false;b.textContent='🔄'}}async function del1(id){if(!confirm('確定刪除？'))return;const b=document.getElementById(`del-${id}`);b.disabled=true;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success)document.getElementById(`product-${id}`).remove();else{alert('失敗');b.disabled=false}}catch(e){alert(e.message);b.disabled=false}}async function retranslateAll(){if(!confirm(`翻譯全部 ${jp.length} 個？`))return;const b=document.getElementById('retranslateAllBtn');b.disabled=true;b.textContent='翻譯中...';let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();const st=document.getElementById(`status-${jp[i].id}`);if(d.success){s++;if(st)st.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${jp[i].id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}}else{f++;if(st)st.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;if(f>=3){alert('連續失敗');break}}}catch(e){f++}await new Promise(r=>setTimeout(r,1500))}alert(`成功:${s} 失敗:${f}`);b.textContent='🔄 全部翻譯';b.disabled=false;document.getElementById('progressText').textContent=''}async function deleteAllJP(){if(!confirm(`刪除全部 ${jp.length} 個？`))return;const b=document.getElementById('deleteAllBtn');b.disabled=true;let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();if(d.success){s++;const el=document.getElementById(`product-${jp[i].id}`);if(el)el.remove()}else f++}catch(e){f++}await new Promise(r=>setTimeout(r,300))}alert(`成功:${s} 失敗:${f}`);b.textContent='🗑️ 全部刪除';b.disabled=false;document.getElementById('progressText').textContent=''}</script></body></html>'''




@app.route('/api/scan-japanese')
def api_scan_japanese():
    if not load_shopify_token():
        return jsonify({'error': '未設定 Token'}), 400
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=坂角總本舖")
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
    ok, r = update_product(pid, {
        'title': translated['title'],
        'body_html': translated['description'],
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




@app.route('/api/test-translate')
def api_test_translate():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({'error': 'OPENAI_API_KEY 未設定'})
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    result = translate_with_chatgpt("ゆかり 8枚入", "坂角總本舖の伝統的な海老煎餅です")
    result['key_preview'] = key_preview
    result['key_length'] = len(api_key)
    return jsonify(result)




@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/start', methods=['POST'])
def start_scrape():
    global scrape_status
    if scrape_status['running']: return jsonify({'error': '爬取已在進行中'}), 400
    if not load_shopify_token(): return jsonify({'error': '未設定 Shopify Token'}), 400
    test = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test['success']:
        return jsonify({'error': f"翻譯功能異常: {test.get('error', '未知')}"}), 400
    threading.Thread(target=run_scrape).start()
    return jsonify({'message': '開始爬取'})


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token(): return jsonify({'success': False, 'error': '未設定 Token'})
    r = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    if r.status_code == 200: return jsonify({'success': True, 'shop': r.json()['shop']})
    return jsonify({'success': False, 'error': r.text}), 400


if __name__ == '__main__':
    print("=" * 50)
    print("坂角總本舖爬蟲工具 v2.2")
    print("新增: 缺貨商品自動刪除")
    print("=" * 50)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

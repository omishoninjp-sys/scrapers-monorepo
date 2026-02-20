"""
神戶風月堂商品爬蟲 + Shopify 上架工具 (修正版 v2.2)

修正項目：
1. 新增「標題重複檢查」- 避免翻譯後標題相同的商品重複上架
2. 新增「重複商品診斷」頁面
3. 改進 SKU 標準化邏輯
4. 【v2.1】翻譯保護機制
5. 【v2.1】日文商品掃描
6. 【v2.2】缺貨商品自動刪除 - 官網消失或缺貨皆直接刪除
"""

from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import sys
import time
from urllib.parse import urljoin, urlencode
from collections import defaultdict
import math
import threading

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    TEMPLATE_DIR = os.path.join(sys._MEIPASS, 'templates')
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)

SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""
BASE_URL = "https://shop.fugetsudo-kobe.jp"
LIST_URL_TEMPLATE = "https://shop.fugetsudo-kobe.jp/shop/shopbrand.html?page={page}&search=&sort=&money1=&money2=&prize1=&company1=&content1=&originalcode1=&category=&subcategory="
MIN_COST_THRESHOLD = 1000
MAX_CONSECUTIVE_TRANSLATION_FAILURES = 3

BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7,zh;q=0.6',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Referer': 'https://shop.fugetsudo-kobe.jp/',
}

session = requests.Session()
session.headers.update(BROWSER_HEADERS)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

scrape_status = {
    "running": False, "progress": 0, "total": 0, "current_product": "",
    "products": [], "errors": [], "uploaded": 0, "skipped": 0,
    "skipped_by_title": 0, "filtered_by_price": 0, "deleted": 0,
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
    token_file = os.path.join(BASE_DIR, "shopify_token.json")
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


def normalize_sku(sku_or_brandcode):
    if not sku_or_brandcode: return ""
    brandcode = sku_or_brandcode[4:] if sku_or_brandcode.startswith('FGT-') else sku_or_brandcode
    try: return f"FGT-{str(int(brandcode))}"
    except ValueError: return sku_or_brandcode


def normalize_title(title):
    if not title: return ""
    n = title.strip()
    n = re.sub(r'\s+', '', n).replace('　', '').replace('・', '').replace('‧', '').replace('·', '')
    return n.lower()


def is_japanese_text(text):
    if not text: return False
    check_text = text.replace('神戶風月堂', '').strip()
    if not check_text: return False
    jp = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', check_text))
    cn = len(re.findall(r'[\u4e00-\u9fff]', check_text))
    total = len(re.sub(r'[\s\d\W]', '', check_text))
    if total == 0: return False
    return jp > 0 and (jp / total > 0.3 or cn == 0)


def calculate_selling_price(cost, weight):
    if not cost or cost <= 0: return 0
    return round((cost + (weight * 1250 if weight else 0)) / 0.7)


def translate_with_chatgpt(title, description):
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本食品商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{description}

請回傳 JSON 格式（不要加 markdown 標記）：
{{"title":"翻譯後的商品名稱（前加「神戶風月堂」）","description":"翻譯後的商品說明","page_title":"SEO標題50-60字","meta_description":"SEO描述100字內"}}

規則：1.神戶風月堂高級法蘭酥 2.開頭「神戶風月堂」3.ゴーフル→法蘭酥 4.プティーゴーフル→迷你法蘭酥 5.ミニゴーフル→小法蘭酥 6.神戸ぶっせ→神戶布雪 7.レスポワール→雷斯波瓦 8.規格需標示 9.只回傳JSON"""
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
            tt = t.get('title', title)
            if not tt.startswith('神戶風月堂'): tt = f"神戶風月堂 {tt}"
            return {'success': True, 'title': tt, 'description': t.get('description', description),
                    'page_title': t.get('page_title', ''), 'meta_description': t.get('meta_description', '')}
        else:
            return {'success': False, 'error': f"HTTP {r.status_code}: {r.text[:200]}",
                    'title': title, 'description': description, 'page_title': '', 'meta_description': ''}
    except Exception as e:
        return {'success': False, 'error': str(e), 'title': title, 'description': description, 'page_title': '', 'meta_description': ''}


def get_all_products_detailed():
    products = []
    url = shopify_api_url("products.json?limit=250")
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
    return products


def get_existing_products_full():
    result = {'by_sku': {}, 'by_title': {}, 'by_handle': {}}
    url = shopify_api_url("products.json?limit=250&fields=id,title,handle,variants")
    while url:
        r = requests.get(url, headers=get_shopify_headers())
        if r.status_code != 200: break
        for p in r.json().get('products', []):
            pid = p.get('id'); title = p.get('title', ''); handle = p.get('handle', '')
            nt = normalize_title(title)
            if nt: result['by_title'][nt] = pid
            if handle: result['by_handle'][handle] = pid
            for v in p.get('variants', []):
                sku = v.get('sku')
                if sku and pid:
                    n = normalize_sku(sku)
                    result['by_sku'][n] = pid
                    if sku != n: result['by_sku'][sku] = pid
        lh = r.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    return result


def get_existing_skus():
    return set(get_existing_products_full()['by_sku'].keys())


def get_existing_products_map():
    return get_existing_products_full()['by_sku']


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
                sku = v.get('sku')
                if sku and pid: pm[normalize_sku(sku)] = pid
        lh = r.headers.get('Link', '')
        m = re.search(r'<([^>]+)>; rel="next"', lh)
        url = m.group(1) if m and 'rel="next"' in lh else None
    return pm


def delete_product(product_id):
    return requests.delete(shopify_api_url(f"products/{product_id}.json"), headers=get_shopify_headers()).status_code == 200


def update_product(product_id, data):
    r = requests.put(shopify_api_url(f"products/{product_id}.json"), headers=get_shopify_headers(),
        json={"product": {"id": product_id, **data}})
    return r.status_code == 200, r


def get_or_create_collection(ct="神戶風月堂"):
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


def parse_dimension_weight(soup, page_text):
    dimension = None
    detail_txt = soup.select_one('.detailTxt')
    if detail_txt:
        for row in detail_txt.select('.row'):
            cells = row.select('.cell')
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True)
                if 'サイズ' in label:
                    sm = re.search(r'([\d.]+)\s*[×xX]\s*([\d.]+)\s*[×xX]\s*([\d.]+)\s*cm', cells[1].get_text(strip=True))
                    if sm:
                        d1, d2, d3 = float(sm.group(1)), float(sm.group(2)), float(sm.group(3))
                        dimension = {"d1": d1, "d2": d2, "d3": d3, "volume_weight": round((d1*d2*d3)/6000, 2)}
                    break
    if not dimension:
        for pat in [r'サイズ[^\d]*([\d.]+)\s*[×xX]\s*([\d.]+)\s*[×xX]\s*([\d.]+)\s*cm',
                    r'([\d.]+)\s*[×xX]\s*([\d.]+)\s*[×xX]\s*([\d.]+)\s*cm']:
            sm = re.search(pat, page_text)
            if sm:
                d1, d2, d3 = float(sm.group(1)), float(sm.group(2)), float(sm.group(3))
                dimension = {"d1": d1, "d2": d2, "d3": d3, "volume_weight": round((d1*d2*d3)/6000, 2)}
                break
    return {"dimension": dimension, "final_weight": round(dimension['volume_weight'], 2) if dimension else 0}


def scrape_product_list():
    products = []; seen_skus = set()
    session.get(BASE_URL, timeout=30); time.sleep(0.5)
    page = 1
    while page <= 20:
        url = LIST_URL_TEMPLATE.format(page=page)
        try:
            r = session.get(url, timeout=30); r.encoding = 'euc-jp'
            if r.status_code != 200: break
            soup = BeautifulSoup(r.text, 'html.parser')
            pls = [l for l in soup.find_all('a') if 'shopdetail' in l.get('href', '') and 'brandcode=' in l.get('href', '')]
            new_count = 0; seen_bc = set()
            for l in pls:
                sm = re.search(r'brandcode=(\d+)', l.get('href', ''))
                if sm:
                    bc_raw = sm.group(1); bc_n = str(int(bc_raw))
                    if bc_n in seen_bc: continue
                    seen_bc.add(bc_n)
                    sku = f"FGT-{bc_n}"
                    if sku not in seen_skus:
                        products.append({'url': f"{BASE_URL}/shopdetail/{bc_raw}/", 'sku': sku, 'brandcode': bc_n, 'brandcode_raw': bc_raw})
                        seen_skus.add(sku); new_count += 1
            if new_count == 0: break
            if not soup.find('a', href=re.compile(rf'page={page+1}')) and not soup.find('a', string=re.compile(r'次|next', re.IGNORECASE)): break
            page += 1; time.sleep(0.5)
        except: break
    return products


def scrape_product_detail(url):
    try:
        r = session.get(url, timeout=30); r.encoding = 'euc-jp'
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.text, 'html.parser'); pt = soup.get_text()

        title = ""
        te = soup.select_one('#itemInfo h2')
        if te: title = te.get_text(strip=True)
        if not title:
            og = soup.find('meta', property='og:title')
            if og: title = og.get('content', '').split('－')[0].strip()

        description = ""
        de = soup.select_one('.detailTxt')
        if de:
            fp = de.find('p')
            description = fp.get_text(strip=True) if fp else de.get_text(strip=True)[:500]

        price = 0
        pm = soup.find('meta', property='product:price:amount')
        if pm:
            try: price = int(pm.get('content', '0'))
            except: pass
        if not price:
            pm2 = re.search(r'税込\s*([\d,]+)\s*円', pt)
            if pm2: price = int(pm2.group(1).replace(',', ''))

        sku = ""
        bm = re.search(r'/shopdetail/(\d+)/', url)
        if bm: sku = f"FGT-{str(int(bm.group(1)))}"

        in_stock = not any(kw in pt for kw in ['在庫がありません', '在庫切れ', '品切れ', 'SOLD OUT'])
        weight_info = parse_dimension_weight(soup, pt)

        images = []; seen_img = set()
        for img in soup.select('.M_imageMain img'):
            src = img.get('src', '')
            if src and 'noimage' not in src.lower():
                fs = re.sub(r'/s(\d)_', r'/\1_', src)
                if fs not in seen_img: seen_img.add(fs); images.append(fs)
        for img in soup.select('.M_imageCatalog img'):
            src = img.get('src', '')
            if src and 'noimage' not in src.lower():
                fs = re.sub(r'/s(\d)_', r'/\1_', src)
                if fs not in seen_img: seen_img.add(fs); images.append(fs)
        if not images:
            og = soup.find('meta', property='og:image')
            if og and og.get('content'): images.append(og.get('content'))

        return {'url': url, 'sku': sku, 'title': title, 'price': price, 'in_stock': in_stock,
                'description': description, 'weight': weight_info['final_weight'], 'images': images[:10]}
    except Exception as e:
        print(f"[ERROR] {url}: {e}"); return None


def upload_to_shopify(product, collection_id=None, existing_titles=None):
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    if not translated['success']:
        return {'success': False, 'error': 'translation_failed', 'translated': translated}

    if existing_titles is not None:
        if normalize_title(translated['title']) in existing_titles:
            return {'success': False, 'error': 'title_duplicate', 'translated': translated}

    cost = product['price']; weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    images = [{'src': u, 'position': i+1} for i, u in enumerate(product.get('images', []))]

    sp = {'product': {
        'title': translated['title'], 'body_html': translated['description'],
        'vendor': '神戶風月堂', 'product_type': '法蘭酥', 'status': 'active', 'published': True,
        'variants': [{'sku': product['sku'], 'price': f"{selling_price:.2f}", 'weight': weight,
            'weight_unit': 'kg', 'inventory_management': None, 'inventory_policy': 'continue', 'requires_shipping': True}],
        'images': images, 'tags': '神戶風月堂, 日本, 法蘭酥, ゴーフル, 伴手禮, 日本零食, 神戶',
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
        scrape_status['current_product'] = "正在檢查 Shopify 已有商品..."
        existing_data = get_existing_products_full()
        existing_skus = set(existing_data['by_sku'].keys())
        existing_titles = set(existing_data['by_title'].keys())

        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("神戶風月堂")

        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        collection_skus = set(collection_products_map.keys())

        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)

        website_skus = set(item['sku'] for item in product_list)

        # === v2.2: 記錄缺貨的 SKU ===
        out_of_stock_skus = set()

        consecutive_translation_failures = 0

        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['sku']}"

            # 已存在於 Shopify
            if item['sku'] in existing_skus:
                # === v2.2: 已上架商品檢查庫存 ===
                if item['sku'] in collection_skus:
                    product = scrape_product_detail(item['url'])
                    if product and not product.get('in_stock', True):
                        out_of_stock_skus.add(item['sku'])
                        print(f"[缺貨偵測] {item['sku']} 官網缺貨，稍後刪除")
                    time.sleep(0.5)
                scrape_status['skipped'] += 1
                continue

            product = scrape_product_detail(item['url'])
            if not product:
                scrape_status['errors'].append(f"無法爬取: {item['url']}"); continue

            if product['sku'] in existing_skus:
                scrape_status['skipped'] += 1; continue

            if product['price'] < MIN_COST_THRESHOLD:
                scrape_status['filtered_by_price'] += 1; continue

            # === v2.2: 缺貨 → 記錄 SKU，不上架 ===
            if not product['in_stock']:
                out_of_stock_skus.add(product['sku'])
                scrape_status['skipped'] += 1
                continue

            result = upload_to_shopify(product, collection_id, existing_titles)

            if result['success']:
                existing_skus.add(product['sku'])
                new_title = result.get('translated', {}).get('title', '')
                if new_title: existing_titles.add(normalize_title(new_title))
                scrape_status['uploaded'] += 1
                consecutive_translation_failures = 0
            elif result.get('error') == 'title_duplicate':
                scrape_status['skipped_by_title'] += 1
                consecutive_translation_failures = 0
            elif result.get('error') == 'translation_failed':
                scrape_status['translation_failed'] += 1
                consecutive_translation_failures += 1
                if consecutive_translation_failures >= MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                    scrape_status['translation_stopped'] = True
                    scrape_status['errors'].append(f'翻譯連續失敗 {consecutive_translation_failures} 次，自動停止')
                    break
            else:
                scrape_status['errors'].append(f"上傳失敗 {product['sku']}")
                consecutive_translation_failures = 0

            time.sleep(1)

        if not scrape_status['translation_stopped']:
            scrape_status['current_product'] = "清理缺貨/下架商品..."

            # === v2.2: 合併需要刪除的 SKU ===
            skus_to_delete = (collection_skus - website_skus) | (collection_skus & out_of_stock_skus)

            if skus_to_delete:
                print(f"[v2.2] 準備刪除 {len(skus_to_delete)} 個商品")
                for sku in skus_to_delete:
                    scrape_status['current_product'] = f"刪除: {sku}"
                    pid = collection_products_map.get(sku)
                    if pid:
                        if delete_product(pid):
                            scrape_status['deleted'] += 1
                            print(f"[已刪除] SKU: {sku}, Product ID: {pid}")
                        else:
                            scrape_status['errors'].append(f"刪除失敗: {sku}")
                    time.sleep(0.3)

    except Exception as e:
        scrape_status['errors'].append(str(e))
    finally:
        scrape_status['running'] = False
        scrape_status['current_product'] = "完成" if not scrape_status['translation_stopped'] else "翻譯異常停止"


# ========== Flask 路由 ==========

@app.route('/')
def index():
    token_loaded = load_shopify_token()
    tc = 'green' if token_loaded else 'red'
    ts = '✓ 已載入' if token_loaded else '✗ 未設定'
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>神戶風月堂 爬蟲工具</title>
<style>*{{box-sizing:border-box}}body{{font-family:-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:20px;background:#f5f5f5}}h1{{color:#333;border-bottom:2px solid #8B4513;padding-bottom:10px}}.card{{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}}.btn{{background:#8B4513;color:white;border:none;padding:12px 24px;border-radius:5px;cursor:pointer;font-size:16px;margin-right:10px;margin-bottom:10px;text-decoration:none;display:inline-block}}.btn:hover{{background:#6B3510}}.btn:disabled{{background:#ccc}}.btn-secondary{{background:#3498db}}.btn-warning{{background:#f39c12}}.btn-success{{background:#27ae60}}.progress-bar{{width:100%;height:20px;background:#eee;border-radius:10px;overflow:hidden;margin:10px 0}}.progress-fill{{height:100%;background:linear-gradient(90deg,#8B4513,#D2691E);transition:width 0.3s}}.status{{padding:10px;background:#f8f9fa;border-radius:5px;margin-top:10px}}.log{{max-height:300px;overflow-y:auto;font-family:monospace;font-size:13px;background:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:5px}}.stats{{display:flex;gap:15px;margin-top:15px;flex-wrap:wrap}}.stat{{flex:1;min-width:80px;text-align:center;padding:15px;background:#f8f9fa;border-radius:5px}}.stat-number{{font-size:24px;font-weight:bold;color:#8B4513}}.stat-label{{font-size:11px;color:#666;margin-top:5px}}.nav{{margin-bottom:20px}}.nav a{{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}}.alert{{padding:12px 16px;border-radius:5px;margin-bottom:15px}}.alert-danger{{background:#fee;border:1px solid #fcc;color:#c0392b}}</style>
</head><body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/diagnose">🔍 重複診斷</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🍪 神戶風月堂 爬蟲工具 <small style="font-size:14px;color:#999">v2.2</small></h1>
<div class="card"><h3>Shopify 連線</h3><p>Token: <span style="color:{tc}">{ts}</span></p>
<button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
<button class="btn btn-secondary" onclick="testTranslate()">測試翻譯</button>
<a href="/diagnose" class="btn btn-warning">🔍 重複診斷</a>
<a href="/japanese-scan" class="btn btn-success">🇯🇵 日文掃描</a></div>
<div class="card"><h3>開始爬取</h3>
<p>爬取 shop.fugetsudo-kobe.jp 全站商品並上架到 Shopify</p>
<p style="color:#666;font-size:14px">※ &lt;¥{MIN_COST_THRESHOLD} 跳過 | 標題重複檢查 | <b style="color:#e74c3c">翻譯保護</b> 連續失敗 {MAX_CONSECUTIVE_TRANSLATION_FAILURES} 次停止 | <b style="color:#e67e22">缺貨自動刪除</b></p>
<button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取</button>
<div id="progressSection" style="display:none">
<div id="translationAlert" class="alert alert-danger" style="display:none">⚠️ 翻譯功能異常，已自動停止！</div>
<div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
<div class="status" id="statusText">準備中...</div>
<div class="stats">
<div class="stat"><div class="stat-number" id="uploadedCount">0</div><div class="stat-label">已上架</div></div>
<div class="stat"><div class="stat-number" id="skippedCount">0</div><div class="stat-label">SKU重複</div></div>
<div class="stat"><div class="stat-number" id="titleSkippedCount" style="color:#9b59b6">0</div><div class="stat-label">標題重複</div></div>
<div class="stat"><div class="stat-number" id="filteredCount">0</div><div class="stat-label">價格過濾</div></div>
<div class="stat"><div class="stat-number" id="translationFailedCount" style="color:#e74c3c">0</div><div class="stat-label">翻譯失敗</div></div>
<div class="stat"><div class="stat-number" id="deletedCount" style="color:#e67e22">0</div><div class="stat-label">已刪除</div></div>
<div class="stat"><div class="stat-number" id="errorCount" style="color:#e74c3c">0</div><div class="stat-label">錯誤</div></div>
</div></div></div>
<div class="card"><h3>執行日誌</h3><div class="log" id="logArea">等待開始...</div></div>
<script>let pollInterval=null;function log(m,t){{const l=document.getElementById('logArea');const tm=new Date().toLocaleTimeString();const c={{success:'#4ec9b0',error:'#f14c4c'}}[t]||'#d4d4d4';l.innerHTML+='<div style="color:'+c+'">['+tm+'] '+m+'</div>';l.scrollTop=l.scrollHeight}}function clearLog(){{document.getElementById('logArea').innerHTML=''}}async function testShopify(){{log('測試連線...');try{{const r=await fetch('/api/test-shopify');const d=await r.json();if(d.success)log('✓ '+d.shop.name,'success');else log('✗ '+d.error,'error')}}catch(e){{log('✗ '+e.message,'error')}}}}async function testTranslate(){{log('測試翻譯...');try{{const r=await fetch('/api/test-translate');const d=await r.json();if(d.error)log('✗ '+d.error,'error');else if(d.success)log('✓ '+d.title,'success');else log('✗ 失敗','error')}}catch(e){{log('✗ '+e.message,'error')}}}}async function startScrape(){{clearLog();log('開始...');document.getElementById('startBtn').disabled=true;document.getElementById('progressSection').style.display='block';document.getElementById('translationAlert').style.display='none';try{{const r=await fetch('/api/start',{{method:'POST'}});const d=await r.json();if(d.error){{log('✗ '+d.error,'error');document.getElementById('startBtn').disabled=false;return}}log('✓ 已啟動','success');pollInterval=setInterval(pollStatus,1000)}}catch(e){{log('✗ '+e.message,'error');document.getElementById('startBtn').disabled=false}}}}async function pollStatus(){{try{{const r=await fetch('/api/status');const d=await r.json();const p=d.total>0?(d.progress/d.total*100):0;document.getElementById('progressFill').style.width=p+'%';document.getElementById('statusText').textContent=d.current_product+' ('+d.progress+'/'+d.total+')';document.getElementById('uploadedCount').textContent=d.uploaded;document.getElementById('skippedCount').textContent=d.skipped;document.getElementById('titleSkippedCount').textContent=d.skipped_by_title||0;document.getElementById('filteredCount').textContent=d.filtered_by_price||0;document.getElementById('translationFailedCount').textContent=d.translation_failed||0;document.getElementById('deletedCount').textContent=d.deleted||0;document.getElementById('errorCount').textContent=d.errors.length;if(d.translation_stopped)document.getElementById('translationAlert').style.display='block';if(!d.running&&d.progress>0){{clearInterval(pollInterval);document.getElementById('startBtn').disabled=false;if(d.translation_stopped)log('⚠️ 翻譯異常停止','error');else log('========== 完成 ==========','success')}}}}catch(e){{console.error(e)}}}}</script>
</body></html>'''


@app.route('/japanese-scan')
def japanese_scan_page():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>日文商品掃描 - 神戶風月堂</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #27ae60;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#8B4513;color:white;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-size:14px;margin-right:10px;margin-bottom:10px}.btn:disabled{background:#ccc}.btn-danger{background:#e74c3c}.btn-success{background:#27ae60}.btn-sm{padding:5px 10px;font-size:12px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}.stats{display:flex;gap:15px;margin:20px 0;flex-wrap:wrap}.stat{flex:1;min-width:150px;text-align:center;padding:20px;background:#f8f9fa;border-radius:8px}.stat-number{font-size:36px;font-weight:bold}.stat-label{font-size:14px;color:#666;margin-top:5px}.product-item{display:flex;align-items:center;padding:15px;border-bottom:1px solid #eee;gap:15px}.product-item:last-child{border-bottom:none}.product-item img{width:60px;height:60px;object-fit:cover;border-radius:4px}.product-item .info{flex:1}.product-item .info .title{font-weight:bold;margin-bottom:5px;color:#c0392b}.product-item .info .meta{font-size:12px;color:#666}.no-image{width:60px;height:60px;background:#eee;display:flex;align-items:center;justify-content:center;border-radius:4px;color:#999;font-size:10px}.retranslate-status{font-size:12px;margin-top:5px}.action-bar{position:sticky;top:0;background:white;padding:15px;margin:-20px -20px 20px -20px;border-bottom:1px solid #ddd;z-index:100;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/diagnose">🔍 重複診斷</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🇯🇵 日文商品掃描 - 神戶風月堂</h1>
<div class="card"><p>掃描 Shopify 中神戶風月堂的日文（未翻譯）商品。</p><button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button><span id="scanStatus"></span></div>
<div class="stats" id="statsSection" style="display:none"><div class="stat"><div class="stat-number" id="totalProducts" style="color:#3498db">0</div><div class="stat-label">總商品數</div></div><div class="stat"><div class="stat-number" id="japaneseCount" style="color:#e74c3c">0</div><div class="stat-label">日文商品</div></div></div>
<div class="card" id="resultsCard" style="display:none"><div class="action-bar"><div><button class="btn btn-success" id="retranslateAllBtn" onclick="retranslateAll()" disabled>🔄 全部翻譯</button><button class="btn btn-danger" id="deleteAllBtn" onclick="deleteAllJP()" disabled>🗑️ 全部刪除</button></div><div id="progressText"></div></div><div id="results"></div></div>
<script>let jp=[];async function startScan(){document.getElementById('scanBtn').disabled=true;document.getElementById('scanStatus').textContent='掃描中...';try{const r=await fetch('/api/scan-japanese');const d=await r.json();if(d.error){alert(d.error);return}jp=d.japanese_products;document.getElementById('totalProducts').textContent=d.total_products;document.getElementById('japaneseCount').textContent=d.japanese_count;document.getElementById('statsSection').style.display='flex';renderResults(d.japanese_products);document.getElementById('resultsCard').style.display='block';document.getElementById('retranslateAllBtn').disabled=jp.length===0;document.getElementById('deleteAllBtn').disabled=jp.length===0;document.getElementById('scanStatus').textContent='完成！'}catch(e){alert(e.message)}finally{document.getElementById('scanBtn').disabled=false}}function renderResults(p){const c=document.getElementById('results');if(!p.length){c.innerHTML='<p style="text-align:center;color:#27ae60;font-size:18px">✅ 沒有日文商品</p>';return}let h='';p.forEach(i=>{const img=i.image?`<img src="${i.image}">`:`<div class="no-image">無圖</div>`;h+=`<div class="product-item" id="product-${i.id}">${img}<div class="info"><div class="title">${i.title}</div><div class="meta">SKU:${i.sku||'無'}|¥${i.price}|${i.status}</div><div class="retranslate-status" id="status-${i.id}"></div></div><div class="actions"><button class="btn btn-success btn-sm" onclick="rt1('${i.id}')" id="rt-${i.id}">🔄</button><button class="btn btn-danger btn-sm" onclick="del1('${i.id}')" id="del-${i.id}">🗑️</button></div></div>`});c.innerHTML=h}async function rt1(id){const b=document.getElementById(`rt-${id}`);const s=document.getElementById(`status-${id}`);b.disabled=true;b.textContent='...';try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success){s.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}b.textContent='✓'}else{s.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;b.disabled=false;b.textContent='🔄'}}catch(e){s.innerHTML=`<span style="color:#e74c3c">❌ ${e.message}</span>`;b.disabled=false;b.textContent='🔄'}}async function del1(id){if(!confirm('確定刪除？'))return;const b=document.getElementById(`del-${id}`);b.disabled=true;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success)document.getElementById(`product-${id}`).remove();else{alert('失敗');b.disabled=false}}catch(e){alert(e.message);b.disabled=false}}async function retranslateAll(){if(!confirm(`翻譯全部 ${jp.length} 個？`))return;const b=document.getElementById('retranslateAllBtn');b.disabled=true;b.textContent='翻譯中...';let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/retranslate-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();const st=document.getElementById(`status-${jp[i].id}`);if(d.success){s++;if(st)st.innerHTML=`<span style="color:#27ae60">✅ ${d.new_title}</span>`;const t=document.querySelector(`#product-${jp[i].id} .title`);if(t){t.textContent=d.new_title;t.style.color='#27ae60'}}else{f++;if(st)st.innerHTML=`<span style="color:#e74c3c">❌ ${d.error}</span>`;if(f>=3){alert('連續失敗');break}}}catch(e){f++}await new Promise(r=>setTimeout(r,1500))}alert(`成功:${s} 失敗:${f}`);b.textContent='🔄 全部翻譯';b.disabled=false;document.getElementById('progressText').textContent=''}async function deleteAllJP(){if(!confirm(`刪除全部 ${jp.length} 個？`))return;const b=document.getElementById('deleteAllBtn');b.disabled=true;let s=0,f=0;for(let i=0;i<jp.length;i++){document.getElementById('progressText').textContent=`${i+1}/${jp.length}`;try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:jp[i].id})});const d=await r.json();if(d.success){s++;const el=document.getElementById(`product-${jp[i].id}`);if(el)el.remove()}else f++}catch(e){f++}await new Promise(r=>setTimeout(r,300))}alert(`成功:${s} 失敗:${f}`);b.textContent='🗑️ 全部刪除';b.disabled=false;document.getElementById('progressText').textContent=''}</script></body></html>'''


@app.route('/diagnose')
def diagnose_page():
    return '''<!DOCTYPE html>
<html lang="zh-TW">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>重複商品診斷 - 神戶風月堂</title>
<style>*{box-sizing:border-box}body{font-family:-apple-system,sans-serif;max-width:1200px;margin:0 auto;padding:20px;background:#f5f5f5}h1{color:#333;border-bottom:2px solid #e74c3c;padding-bottom:10px}.card{background:white;border-radius:8px;padding:20px;margin-bottom:20px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}.btn{background:#8B4513;color:white;border:none;padding:10px 20px;border-radius:5px;cursor:pointer;font-size:14px;margin-right:10px;margin-bottom:10px}.btn:disabled{background:#ccc}.btn-danger{background:#e74c3c}.btn-secondary{background:#3498db}.btn-sm{padding:5px 10px;font-size:12px}.nav{margin-bottom:20px}.nav a{margin-right:15px;color:#8B4513;text-decoration:none;font-weight:bold}.stats{display:flex;gap:15px;margin:20px 0;flex-wrap:wrap}.stat{flex:1;min-width:150px;text-align:center;padding:20px;background:#f8f9fa;border-radius:8px}.stat-number{font-size:36px;font-weight:bold}.stat-label{font-size:14px;color:#666;margin-top:5px}.duplicate-group{border:1px solid #e74c3c;border-radius:8px;margin-bottom:15px;overflow:hidden}.duplicate-header{background:#fee;padding:15px;border-bottom:1px solid #e74c3c;display:flex;justify-content:space-between;align-items:center}.duplicate-header h4{margin:0;color:#c0392b}.duplicate-item{display:flex;align-items:center;padding:12px 15px;border-bottom:1px solid #eee;gap:15px}.duplicate-item:last-child{border-bottom:none}.duplicate-item.keep{background:#e8f5e9}.duplicate-item.delete{background:#ffebee}.duplicate-item img{width:60px;height:60px;object-fit:cover;border-radius:4px}.duplicate-item .info{flex:1}.duplicate-item .info .title{font-weight:bold;margin-bottom:5px}.duplicate-item .info .meta{font-size:12px;color:#666}.badge{display:inline-block;padding:3px 8px;border-radius:3px;font-size:11px;font-weight:bold}.badge-keep{background:#27ae60;color:white}.badge-delete{background:#e74c3c;color:white}.no-image{width:60px;height:60px;background:#eee;display:flex;align-items:center;justify-content:center;border-radius:4px;color:#999;font-size:10px}.action-bar{position:sticky;top:0;background:white;padding:15px;margin:-20px -20px 20px -20px;border-bottom:1px solid #ddd;z-index:100;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}</style></head>
<body>
<div class="nav"><a href="/">🏠 首頁</a><a href="/diagnose">🔍 重複診斷</a><a href="/japanese-scan">🇯🇵 日文掃描</a></div>
<h1>🔍 重複商品診斷</h1>
<div class="card"><p>掃描重複商品（相同標題），提供一鍵清理。</p><button class="btn" id="scanBtn" onclick="startScan()">🔍 開始掃描</button><span id="scanStatus"></span></div>
<div class="stats" id="statsSection" style="display:none"><div class="stat"><div class="stat-number" id="totalProducts" style="color:#3498db">0</div><div class="stat-label">總商品數</div></div><div class="stat"><div class="stat-number" id="duplicateGroups" style="color:#e74c3c">0</div><div class="stat-label">重複群組</div></div><div class="stat"><div class="stat-number" id="duplicateCount" style="color:#e67e22">0</div><div class="stat-label">建議刪除</div></div></div>
<div class="card" id="resultsCard" style="display:none"><div class="action-bar"><div><button class="btn btn-danger" id="deleteSelectedBtn" onclick="deleteSelected()" disabled>🗑️ 刪除選中</button><button class="btn btn-secondary" onclick="selectAll()">全選建議刪除</button><button class="btn btn-secondary" onclick="deselectAll()">取消全選</button></div><div id="selectedCount">已選擇: 0</div></div><div id="results"></div></div>
<script>let dd=[],sel=new Set();async function startScan(){document.getElementById('scanBtn').disabled=true;document.getElementById('scanStatus').textContent='掃描中...';try{const r=await fetch('/api/diagnose');const d=await r.json();if(d.error){alert(d.error);return}dd=d.duplicates;document.getElementById('totalProducts').textContent=d.total_products;document.getElementById('duplicateGroups').textContent=d.duplicate_groups;document.getElementById('duplicateCount').textContent=d.to_delete_count;document.getElementById('statsSection').style.display='flex';renderResults(d.duplicates);document.getElementById('resultsCard').style.display='block';document.getElementById('scanStatus').textContent='完成！'}catch(e){alert(e.message)}finally{document.getElementById('scanBtn').disabled=false}}function renderResults(dups){const c=document.getElementById('results');if(!dups.length){c.innerHTML='<p style="text-align:center;color:#27ae60;font-size:18px">✅ 沒有重複商品</p>';return}let h='';dups.forEach(g=>{h+=`<div class="duplicate-group"><div class="duplicate-header"><h4>📦 ${g.title} (${g.items.length}個)</h4></div><div>`;g.items.forEach((i,idx)=>{const keep=idx===0;const img=i.image?`<img src="${i.image}">`:`<div class="no-image">無圖</div>`;h+=`<div class="duplicate-item ${keep?'keep':'delete'}">${!keep?`<label><input type="checkbox" class="dc" data-id="${i.id}" onchange="upd()"></label>`:'<div style="width:20px"></div>'}${img}<div class="info"><div class="title">${i.title}</div><div class="meta">SKU:${i.sku||'無'}|Handle:${i.handle}|$${i.price}|${new Date(i.created_at).toLocaleDateString('zh-TW')}</div></div><span class="badge ${keep?'badge-keep':'badge-delete'}">${keep?'保留':'建議刪除'}</span></div>`});h+='</div></div>'});c.innerHTML=h}function upd(){sel.clear();document.querySelectorAll('.dc:checked').forEach(c=>sel.add(c.dataset.id));document.getElementById('selectedCount').textContent='已選擇: '+sel.size;document.getElementById('deleteSelectedBtn').disabled=sel.size===0}function selectAll(){document.querySelectorAll('.dc').forEach(c=>c.checked=true);upd()}function deselectAll(){document.querySelectorAll('.dc').forEach(c=>c.checked=false);upd()}async function deleteSelected(){if(!sel.size||!confirm(`刪除 ${sel.size} 個？`))return;const b=document.getElementById('deleteSelectedBtn');b.disabled=true;b.textContent='刪除中...';const ids=Array.from(sel);let s=0,f=0;for(const id of ids){try{const r=await fetch('/api/delete-product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id})});const d=await r.json();if(d.success){s++;const cb=document.querySelector(`.dc[data-id="${id}"]`);if(cb)cb.closest('.duplicate-item').remove()}else f++}catch(e){f++}}alert(`成功:${s} 失敗:${f}`);sel.clear();upd();b.textContent='🗑️ 刪除選中';startScan()}</script></body></html>'''


# ========== API 路由 ==========

@app.route('/api/scan-japanese')
def api_scan_japanese():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    products = []
    url = shopify_api_url("products.json?limit=250&vendor=神戶風月堂")
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
    ok, r = update_product(pid, {'title': translated['title'], 'body_html': translated['description'],
        'metafields_global_title_tag': translated['page_title'], 'metafields_global_description_tag': translated['meta_description']})
    if ok: return jsonify({'success': True, 'old_title': product.get('title', ''), 'new_title': translated['title'], 'product_id': pid})
    return jsonify({'success': False, 'error': f'更新失敗: {r.text[:200]}'})


@app.route('/api/diagnose')
def api_diagnose():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    products = get_all_products_detailed()
    by_title = defaultdict(list)
    for p in products:
        t = p.get('title', '')
        if t: by_title[t].append(p)
    duplicates = []; tdc = 0
    for title, items in by_title.items():
        if len(items) > 1:
            si = sorted(items, key=lambda x: x['created_at'])
            duplicates.append({'title': title, 'count': len(items), 'items': si})
            tdc += len(items) - 1
    duplicates.sort(key=lambda x: -x['count'])
    return jsonify({'total_products': len(products), 'duplicate_groups': len(duplicates), 'to_delete_count': tdc, 'duplicates': duplicates})


@app.route('/api/delete-product', methods=['POST'])
def api_delete_product():
    if not load_shopify_token(): return jsonify({'error': '未設定 Token'}), 400
    data = request.get_json(); pid = data.get('product_id')
    if not pid: return jsonify({'error': '缺少 product_id'}), 400
    return jsonify({'success': delete_product(pid), 'product_id': pid})


@app.route('/api/status')
def get_status():
    return jsonify(scrape_status)


@app.route('/api/test-translate')
def test_translate():
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key: return jsonify({'error': 'OPENAI_API_KEY 未設定'})
    kp = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    result = translate_with_chatgpt("ゴーフル10S", "神戸の銘菓ゴーフルの詰め合わせです")
    result['key_preview'] = kp; result['key_length'] = len(api_key)
    return jsonify(result)


@app.route('/api/test-shopify')
def test_shopify():
    if not load_shopify_token(): return jsonify({'error': '未找到 Token'}), 400
    r = requests.get(shopify_api_url('shop.json'), headers=get_shopify_headers())
    if r.status_code == 200: return jsonify({'success': True, 'shop': r.json()['shop']})
    return jsonify({'success': False, 'error': r.text}), 400


@app.route('/api/start', methods=['POST'])
def start_scrape():
    global scrape_status
    if scrape_status['running']: return jsonify({'error': '爬取已在進行中'}), 400
    scrape_status = {"running": True, "progress": 0, "total": 0, "current_product": "測試翻譯...",
        "products": [], "errors": [], "uploaded": 0, "skipped": 0, "skipped_by_title": 0,
        "filtered_by_price": 0, "deleted": 0, "translation_failed": 0, "translation_stopped": False}
    if not load_shopify_token():
        scrape_status['running'] = False
        return jsonify({'error': '請先設定 Shopify Token'}), 400
    test = translate_with_chatgpt("テスト商品", "テスト説明")
    if not test['success']:
        scrape_status['running'] = False; scrape_status['translation_stopped'] = True
        return jsonify({'error': f"翻譯功能異常: {test.get('error', '未知')}"}), 400
    threading.Thread(target=run_scrape).start()
    return jsonify({'message': '開始爬取'})


if __name__ == '__main__':
    print("=" * 50)
    print("神戶風月堂爬蟲工具 v2.2")
    print("新增: 缺貨商品自動刪除")
    print("=" * 50)
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

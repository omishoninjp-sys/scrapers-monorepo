"""
資生堂パーラー（Shiseido Parlour）商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 parlour.shiseido.co.jp 指定分類的所有商品
2. 過濾 1000 円以下商品
3. 計算材積重量 vs 實際重量，取大值
4. 上架到 Shopify（不重複上架）
5. 原價寫入成本價（Cost）
"""

from flask import Flask, render_template, jsonify, request
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import time
from urllib.parse import urljoin, urlparse, parse_qs
import math

import sys

# 支援 PyInstaller 打包
if getattr(sys, 'frozen', False):
    BASE_DIR = sys._MEIPASS
    template_folder = os.path.join(BASE_DIR, 'templates')
    app = Flask(__name__, template_folder=template_folder)
else:
    app = Flask(__name__)

# ========== 設定 ==========
SHOPIFY_SHOP = ""
SHOPIFY_ACCESS_TOKEN = ""

BASE_URL = "https://parlour.shiseido.co.jp"

# 要爬取的分類頁面
CATEGORY_URLS = [
    "https://parlour.shiseido.co.jp/food_products/onlineshop/recommend.html",
    "https://parlour.shiseido.co.jp/food_products/onlineshop/category.html?cat_id=002",
    "https://parlour.shiseido.co.jp/food_products/onlineshop/category.html?cat_id=003",
    "https://parlour.shiseido.co.jp/food_products/onlineshop/category.html?cat_id=004",
    "https://parlour.shiseido.co.jp/food_products/onlineshop/category.html?cat_id=005",
    "https://parlour.shiseido.co.jp/food_products/onlineshop/category.html?cat_id=007",
    "https://parlour.shiseido.co.jp/food_products/onlineshop/category.html?cat_id=008",
]

# 模擬瀏覽器 Headers
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7,zh;q=0.6',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Referer': 'https://parlour.shiseido.co.jp/',
}

# 建立 Session 保持 cookies
session = requests.Session()
session.headers.update(BROWSER_HEADERS)

# OpenAI API 設定 (從環境變數讀取)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# 最低價格門檻
MIN_PRICE = 1000


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
            
            print(f"[設定] 商店: {SHOPIFY_SHOP}")
            print(f"[設定] Token: {SHOPIFY_ACCESS_TOKEN[:20]}..." if SHOPIFY_ACCESS_TOKEN else "[設定] Token: 未設定")
            return True
    return False


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


def translate_with_chatgpt(title, description):
    """
    使用 ChatGPT 翻譯商品名稱和說明，並生成 SEO 內容
    """
    prompt = f"""你是專業的日本商品翻譯和 SEO 專家。請將以下日本食品商品資訊翻譯成繁體中文，並優化 SEO。

商品名稱（日文）：{title}
商品說明（日文）：{description}

請回傳 JSON 格式（不要加 markdown 標記）：
{{
    "title": "資生堂PARLOUR 翻譯後的商品名稱（繁體中文，簡潔有力）",
    "description": "翻譯後的商品說明（繁體中文，保留原意但更流暢，適合電商展示）",
    "page_title": "SEO 頁面標題（繁體中文，包含品牌和商品特色，50-60字以內）",
    "meta_description": "SEO 描述（繁體中文，吸引點擊，包含關鍵字，100字以內）"
}}

注意：
1. 這是日本東京銀座「資生堂パーラー」（資生堂PARLOUR）的高級洋菓子
2. 資生堂パーラー創立於1902年，是日本最具歷史的西洋料理店之一
3. 翻譯要自然流暢，不要生硬
4. SEO 內容要包含：資生堂PARLOUR、銀座、日本、伴手禮等關鍵字
5. 只回傳 JSON，不要其他文字
6. 【重要】商品標題必須以「資生堂PARLOUR」開頭，例如：「資生堂PARLOUR 起司蛋糕 3入」"""

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
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家，專門處理日本洋菓子的中文翻譯。"},
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
            
            # 確保標題以「資生堂PARLOUR」開頭
            translated_title = translated.get('title', title)
            if not translated_title.startswith('資生堂PARLOUR'):
                translated_title = f"資生堂PARLOUR {translated_title}"
            
            return {
                'success': True,
                'title': translated_title,
                'description': translated.get('description', description),
                'page_title': translated.get('page_title', ''),
                'meta_description': translated.get('meta_description', '')
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            fallback_title = title if title.startswith('資生堂PARLOUR') else f"資生堂PARLOUR {title}"
            return {
                'success': False,
                'title': fallback_title,
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        fallback_title = title if title.startswith('資生堂PARLOUR') else f"資生堂PARLOUR {title}"
        return {
            'success': False,
            'title': fallback_title,
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
    "deleted": 0
}


def get_shopify_headers():
    """取得 Shopify API Headers"""
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }


def shopify_api_url(endpoint):
    """建立 Shopify API URL"""
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"


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
        print("[WARNING] 沒有 Collection ID，跳過")
        return products_map
    
    url = shopify_api_url(f"collections/{collection_id}/products.json?limit=250")
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            print(f"Error fetching collection products: {response.status_code}")
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
    """將 Shopify 商品設為草稿（而非刪除）"""
    url = shopify_api_url(f"products/{product_id}.json")
    
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {
            "id": product_id,
            "status": "draft"
        }
    })
    
    if response.status_code == 200:
        print(f"[設為草稿] Product ID: {product_id}")
        return True
    else:
        print(f"[設為草稿失敗] Product ID: {product_id}, 錯誤: {response.status_code}")
        return False


def parse_dimension_weight(size_text):
    """
    解析商品尺寸和重量
    格式：62mm×183mm×35mm*108g
    或：91㎜×173㎜×21㎜*129g（全形㎜）
    
    材積重量計算：長*寬*高/6000 (cm為單位)
    取材積重量和實際重量的較大值
    """
    dimension = None
    weight = None
    final_weight = 0
    
    if not size_text:
        return {'dimension': None, 'actual_weight': None, 'final_weight': 0}
    
    # 統一將全形 ㎜ 轉換為 mm
    size_text = size_text.replace('㎜', 'mm')
    
    print(f"[DEBUG] 解析尺寸文字: {size_text}")
    
    # 解析尺寸 - 格式：62mm×183mm×35mm 或 78mm×120mm×23mm
    # 支援 mm 單位在每個數字後面或只在最後
    dim_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:mm)?\s*[×xX]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[×xX]\s*(\d+(?:\.\d+)?)\s*mm', size_text)
    if dim_match:
        # 單位是 mm，轉換為 cm
        w_mm = float(dim_match.group(1))
        d_mm = float(dim_match.group(2))
        h_mm = float(dim_match.group(3))
        
        w_cm = w_mm / 10  # mm -> cm
        d_cm = d_mm / 10
        h_cm = h_mm / 10
        
        # 材積重量 = 長*寬*高/6000 (cm為單位)，結果為 kg
        volume_weight = (w_cm * d_cm * h_cm) / 6000
        
        print(f"[DEBUG] 尺寸: {w_mm}mm × {d_mm}mm × {h_mm}mm = {w_cm}cm × {d_cm}cm × {h_cm}cm")
        print(f"[DEBUG] 材積重量: {w_cm} × {d_cm} × {h_cm} / 6000 = {volume_weight:.4f} kg")
        
        dimension = {"w": w_cm, "d": d_cm, "h": h_cm, "volume_weight": round(volume_weight, 4)}
    
    # 解析重量 (g) - 用 * 或空格分隔
    weight_match = re.search(r'[*\s](\d+(?:\.\d+)?)\s*g', size_text)
    if weight_match:
        weight_g = float(weight_match.group(1))
        weight = weight_g / 1000  # 轉換為 kg
        print(f"[DEBUG] 實際重量: {weight_g}g = {weight:.4f} kg")
    
    # 計算最終重量（取較大值）
    if dimension and weight:
        final_weight = max(dimension['volume_weight'], weight)
        print(f"[DEBUG] 取較大值: max({dimension['volume_weight']:.4f}, {weight:.4f}) = {final_weight:.4f} kg")
    elif dimension:
        final_weight = dimension['volume_weight']
    elif weight:
        final_weight = weight
    
    return {
        "dimension": dimension,
        "actual_weight": weight,
        "final_weight": round(final_weight, 3)
    }


def scrape_product_list(category_urls):
    """爬取所有分類頁面的商品列表"""
    products = []
    seen_prod_ids = set()
    
    # 先訪問首頁取得 cookies
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    for category_url in category_urls:
        print(f"\n[爬取分類] {category_url}")
        
        try:
            response = session.get(category_url, timeout=30)
            
            if response.status_code != 200:
                print(f"[錯誤] 狀態碼: {response.status_code}")
                continue
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 找所有商品連結（格式：detail.html?prod_id=0000000XXX）
            all_links = soup.find_all('a', href=re.compile(r'detail\.html\?prod_id=\d+'))
            
            print(f"[DEBUG] 找到 {len(all_links)} 個商品連結")
            
            new_count = 0
            for link in all_links:
                href = link.get('href', '')
                
                # 提取 prod_id
                prod_match = re.search(r'prod_id=(\d+)', href)
                if prod_match:
                    prod_id = prod_match.group(1)
                    
                    if prod_id in seen_prod_ids:
                        continue
                    seen_prod_ids.add(prod_id)
                    
                    # 構建完整 URL
                    full_url = urljoin(BASE_URL + "/food_products/onlineshop/", href)
                    
                    products.append({
                        'url': full_url,
                        'prod_id': prod_id
                    })
                    new_count += 1
            
            print(f"[進度] 新增 {new_count} 個商品，累計 {len(products)} 個")
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[錯誤] 爬取分類失敗: {e}")
            import traceback
            traceback.print_exc()
    
    print(f"\n[完成] 共找到 {len(products)} 個不重複商品")
    return products


def scrape_product_detail(url):
    """爬取單一商品詳細資訊"""
    try:
        response = session.get(url, timeout=30)
        
        if response.status_code != 200:
            print(f"[錯誤] 狀態碼: {response.status_code} - {url}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # 商品編號 - 從 URL 取得 prod_id
        prod_id = ""
        url_match = re.search(r'prod_id=(\d+)', url)
        if url_match:
            prod_id = url_match.group(1)
        
        # 商品代碼 - 從頁面取得（格式：商品コード／71111）
        sku = ""
        sku_match = re.search(r'商品コード[／/](\d+)', page_text)
        if sku_match:
            sku = sku_match.group(1)
        else:
            # 如果找不到商品代碼，使用 prod_id
            sku = f"SP{prod_id}"
        
        print(f"[DEBUG] prod_id: {prod_id}, SKU: {sku}")
        
        # 商品名稱 - 從 h2 標籤取得
        title = ""
        h2_elem = soup.select_one('h2')
        if h2_elem:
            title = h2_elem.get_text(strip=True)
        
        # 備用方案：從 title tag 取得
        if not title:
            title_tag = soup.select_one('title')
            if title_tag:
                title = title_tag.get_text(strip=True).split('│')[0].strip()
        
        print(f"[DEBUG] 標題: {title}")
        
        # 商品說明 - 從商品資訊區塊取得
        description = ""
        # 找 h2 後面的描述文字（在價格和購物車按鈕之前）
        content_area = soup.select_one('.productDetail, .product-detail, main')
        if content_area:
            # 找所有段落和文字
            for elem in content_area.find_all(['p', 'div']):
                text = elem.get_text(strip=True)
                # 排除價格、按鈕等
                if text and len(text) > 30 and '円' not in text[:20] and 'カート' not in text:
                    # 檢查是否是商品描述（通常包含關鍵字）
                    if any(kw in text for kw in ['銀座', 'チーズ', 'ケーキ', '焼き', 'クッキー', 'チョコ', '菓子', '詰め合わせ', '国産', '北海道', 'デンマーク']):
                        description = text
                        break
        
        # 備用方案：直接搜尋描述段落
        if not description:
            # 在頁面文本中找描述（通常在標題後、詳細資訊前）
            desc_patterns = [
                r'(銀座で生まれ.+?です。)',
                r'(国産の.+?です。)',
                r'(北海道.+?です。)',
            ]
            for pattern in desc_patterns:
                match = re.search(pattern, page_text, re.DOTALL)
                if match:
                    description = match.group(1).replace('\n', ' ').strip()
                    break
        
        print(f"[DEBUG] 說明: {description[:50]}..." if description else "[DEBUG] 說明: 無")
        
        # 價格 - 格式：¥1,080(税込)
        price = 0
        price_match = re.search(r'¥([\d,]+)\s*\(?税込\)?', page_text)
        if price_match:
            price = int(price_match.group(1).replace(',', ''))
        
        print(f"[DEBUG] 價格: {price}")
        
        # 過濾 1000 円以下商品
        if price < MIN_PRICE:
            print(f"[跳過] 價格 {price} 円 低於門檻 {MIN_PRICE} 円")
            return None
        
        # 庫存狀態
        in_stock = True
        if '在庫がありません' in page_text or '在庫切れ' in page_text or '完売' in page_text or 'SOLD OUT' in page_text.upper():
            in_stock = False
        
        print(f"[DEBUG] 有庫存: {in_stock}")
        
        # 解析商品尺寸和重量
        # 從 <dl class="mod-detail"> 結構中找 商品サイズ
        weight_info = {'dimension': None, 'actual_weight': None, 'final_weight': 0}
        
        # 方法1：從 HTML 結構中找
        for dl in soup.select('dl.mod-detail'):
            dt = dl.select_one('dt')
            dd = dl.select_one('dd')
            if dt and dd and '商品サイズ' in dt.get_text():
                size_text = dd.get_text(strip=True)
                print(f"[DEBUG] 找到商品サイズ: {size_text}")
                weight_info = parse_dimension_weight(size_text)
                break
        
        # 方法2：備用 - 從頁面文字中找
        if weight_info['final_weight'] == 0:
            size_match = re.search(r'商品サイズ[^\d]*(\d+(?:\.\d+)?(?:mm|㎜)[×xX]\d+(?:\.\d+)?(?:mm|㎜)[×xX]\d+(?:\.\d+)?(?:mm|㎜)\s*[*\s]?\s*\d+(?:\.\d+)?g)', page_text)
            if size_match:
                size_text = size_match.group(1)
                print(f"[DEBUG] 備用方法找到: {size_text}")
                weight_info = parse_dimension_weight(size_text)
        
        print(f"[DEBUG] 重量資訊: {weight_info}")
        
        # 圖片
        images = []
        seen_images = set()
        
        # 找商品圖片（格式：/files_cms/product/XXX.jpg）
        for img in soup.find_all('img'):
            src = img.get('src', '')
            if '/files_cms/product/' in src:
                full_src = urljoin(BASE_URL, src)
                if full_src not in seen_images:
                    seen_images.add(full_src)
                    images.append(full_src)
        
        print(f"[DEBUG] 找到 {len(images)} 張圖片")
        
        # 規格資訊
        specs = {}
        
        # 內容量
        content_match = re.search(r'内容量[^\d]*(\d+[個入枚本]+)', page_text)
        if content_match:
            specs['content'] = content_match.group(1)
        
        # 保存期限（賞味期限）
        expiry_match = re.search(r'賞味期限[：:]\s*製造日より(\d+日)', page_text)
        if expiry_match:
            specs['expiry'] = expiry_match.group(1)
        
        # 保存方法
        storage_match = re.search(r'保存方法[^\n]*', page_text)
        if storage_match:
            specs['storage'] = storage_match.group(0)[:100]
        
        return {
            'url': url,
            'prod_id': prod_id,
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
        import traceback
        traceback.print_exc()
        return None


def get_or_create_collection(collection_title="資生堂PARLOUR"):
    """取得或建立 Collection"""
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
        json={
            'custom_collection': {
                'title': collection_title,
                'published': True
            }
        }
    )
    
    if response.status_code == 201:
        return response.json()['custom_collection']['id']
    
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
    
    seen_names = set()
    unique_publications = []
    for pub in publications:
        name = pub['node']['name']
        if name not in seen_names:
            seen_names.add(name)
            unique_publications.append(pub['node'])
    
    print(f"[發布] 找到 {len(unique_publications)} 個唯一銷售渠道: {[p['name'] for p in unique_publications]}")
    
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
        print(f"[發布] 回應: {pub_response.text[:500]}")
        return False


def upload_to_shopify(product, collection_id=None):
    """上傳商品到 Shopify"""
    
    print(f"[翻譯] 正在翻譯: {product['title'][:30]}...")
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    
    if translated['success']:
        print(f"[翻譯成功] {translated['title'][:30]}...")
    else:
        print(f"[翻譯失敗] 使用原文")
    
    cost = product['price']
    weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    
    print(f"[價格計算] 進貨價: ¥{cost}, 重量: {weight}kg, 售價: ¥{selling_price}")
    print(f"[價格公式] ({cost} + {weight} * 1250) / 0.7 = {selling_price}")
    
    images = []
    for idx, img_url in enumerate(product.get('images', [])):
        images.append({
            'src': img_url,
            'position': idx + 1
        })
    
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': '資生堂PARLOUR',
            'product_type': '洋菓子',
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
            'tags': '資生堂パーラー, 資生堂PARLOUR, 銀座, 日本, 洋菓子, 伴手禮, 日本零食, 高級菓子',
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
    
    print(f"[DEBUG] 準備上傳: price={selling_price:.2f}")
    
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
        
        print(f"[DEBUG] 商品建立成功: ID={product_id}, Variant ID={variant_id}")
        
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
    """首頁"""
    token_loaded = load_shopify_token()
    token_status = '<span style="color: green;">✓ 已載入</span>' if token_loaded else '<span style="color: red;">✗ 未設定</span>'
    
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>資生堂PARLOUR 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #C41E3A; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #C41E3A; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #A01830; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-secondary:hover {{ background: #2980b9; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #C41E3A, #E85A6B); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 100px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #C41E3A; }}
        .stat-label {{ font-size: 12px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>🍰 資生堂PARLOUR 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 parlour.shiseido.co.jp 全站商品並上架到 Shopify</p>
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
                if (data.success) {{
                    log('✓ 連線成功！商店: ' + data.shop.name, 'success');
                }} else {{
                    log('✗ 連線失敗: ' + data.error, 'error');
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
                document.getElementById('deletedCount').textContent = data.deleted || 0;
                document.getElementById('errorCount').textContent = data.errors.length;
                
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval);
                    document.getElementById('startBtn').disabled = false;
                    log('========== 爬取完成 ==========', 'success');
                    log('上架: ' + data.uploaded + ' | 跳過: ' + data.skipped + ' | 價格過濾: ' + (data.filtered_by_price || 0) + ' | 草稿: ' + (data.deleted || 0) + ' | 錯誤: ' + data.errors.length);
                }}
            }} catch (e) {{
                console.error('Poll error:', e);
            }}
        }}
    </script>
</body>
</html>'''


@app.route('/api/status')
def get_status():
    """取得爬取狀態"""
    return jsonify(scrape_status)


@app.route('/api/start', methods=['POST'])
def start_scrape():
    """開始爬取"""
    global scrape_status
    
    if scrape_status['running']:
        return jsonify({'error': '爬取已在進行中'}), 400
    
    scrape_status = {
        "running": True,
        "progress": 0,
        "total": 0,
        "current_product": "正在取得商品列表...",
        "products": [],
        "errors": [],
        "uploaded": 0,
        "skipped": 0,
        "filtered_by_price": 0,
        "deleted": 0
    }
    
    if not load_shopify_token():
        scrape_status['running'] = False
        return jsonify({'error': '請先完成 Shopify OAuth 授權'}), 400
    
    import threading
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'message': '開始爬取'})


def run_scrape():
    """執行爬取流程"""
    global scrape_status
    
    try:
        # 1. 取得 Shopify 所有商品
        scrape_status['current_product'] = "正在檢查 Shopify 已有商品..."
        existing_products_map = get_existing_products_map()
        existing_skus = set(existing_products_map.keys())
        print(f"[INFO] Shopify 全站已有 {len(existing_skus)} 個商品")
        
        # 2. 取得或建立 Collection
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("資生堂PARLOUR")
        print(f"[INFO] Collection ID: {collection_id}")
        
        # 2.5 取得 Collection 內的商品
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        collection_skus = set(collection_products_map.keys())
        print(f"[INFO] 資生堂PARLOUR Collection 內有 {len(collection_skus)} 個商品")
        
        # 3. 爬取商品列表
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list(CATEGORY_URLS)
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        # 取得官網所有 SKU
        website_skus = set()
        
        # 4. 爬取每個商品詳情並上傳
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['prod_id']}"
            
            product = scrape_product_detail(item['url'])
            if not product:
                scrape_status['skipped'] += 1
                continue
            
            # 記錄官網 SKU
            website_skus.add(product['sku'])
            
            if product['sku'] in existing_skus:
                print(f"[跳過] SKU {product['sku']} 已存在")
                scrape_status['skipped'] += 1
                continue
            
            # 檢查價格門檻
            if product['price'] < MIN_PRICE:
                print(f"[跳過] SKU {product['sku']} 價格 ¥{product['price']} 低於門檻 ¥{MIN_PRICE}")
                scrape_status['filtered_by_price'] += 1
                continue
            
            if not product['in_stock']:
                print(f"[跳過] SKU {product['sku']} 無庫存")
                scrape_status['skipped'] += 1
                continue
            
            result = upload_to_shopify(product, collection_id)
            if result['success']:
                print(f"[成功] 上傳 SKU {product['sku']}")
                existing_skus.add(product['sku'])  # 防止同一批次重複上架
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'sku': product['sku'],
                    'title': result.get('translated', {}).get('title', product['title']),
                    'original_title': product['title'],
                    'price': product['price'],
                    'weight': product['weight'],
                    'page_title': result.get('translated', {}).get('page_title', ''),
                    'status': 'success'
                })
            else:
                print(f"[失敗] SKU {product['sku']}: {result['error']}")
                scrape_status['errors'].append(f"上傳失敗 {product['sku']}: {result['error']}")
                scrape_status['products'].append({
                    'sku': product['sku'],
                    'title': product['title'],
                    'status': 'failed',
                    'error': result['error']
                })
            
            time.sleep(1)
        
        # 5. 設為草稿：只針對 Collection 內、但官網已下架的商品
        print(f"[INFO] 官網 SKU 列表: {len(website_skus)} 個")
        scrape_status['current_product'] = "正在檢查已下架商品..."
        skus_to_draft = collection_skus - website_skus
        
        if skus_to_draft:
            print(f"[INFO] 發現 {len(skus_to_draft)} 個商品需要設為草稿: {skus_to_draft}")
            
            for sku in skus_to_draft:
                scrape_status['current_product'] = f"設為草稿: {sku}"
                product_id = collection_products_map.get(sku)
                
                if product_id:
                    if set_product_to_draft(product_id):
                        scrape_status['deleted'] += 1
                        scrape_status['products'].append({
                            'sku': sku,
                            'status': 'draft',
                            'title': f'已設為草稿 (SKU: {sku})'
                        })
                    else:
                        scrape_status['errors'].append(f"設為草稿失敗: {sku}")
                    
                    time.sleep(0.5)
        else:
            print("[INFO] 沒有需要設為草稿的商品")
        
    except Exception as e:
        print(f"[錯誤] {e}")
        scrape_status['errors'].append(str(e))
    
    finally:
        scrape_status['running'] = False
        scrape_status['current_product'] = "完成"


@app.route('/api/test-shopify')
def test_shopify():
    """測試 Shopify 連線"""
    if not load_shopify_token():
        return jsonify({'error': '未找到 Token'}), 400
    
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
    """測試爬取一個商品"""
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    test_url = "https://parlour.shiseido.co.jp/food_products/onlineshop/detail.html?prod_id=0000000291"
    product = scrape_product_detail(test_url)
    
    if not product:
        return jsonify({'error': '爬取失敗'}), 400
    
    return jsonify({
        'success': True,
        'product': product
    })


@app.route('/api/test-upload')
def test_upload():
    """測試爬取並上架一個商品"""
    if not load_shopify_token():
        return jsonify({'error': '請先完成 Shopify OAuth 授權'}), 400
    
    print(f"[DEBUG] Token: {SHOPIFY_ACCESS_TOKEN[:20]}...")
    
    test_response = requests.get(
        shopify_api_url('shop.json'),
        headers=get_shopify_headers()
    )
    print(f"[DEBUG] Shopify 連線測試: {test_response.status_code}")
    if test_response.status_code != 200:
        return jsonify({
            'error': f'Shopify 連線失敗: {test_response.status_code}',
            'detail': test_response.text
        }), 400
    
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    test_url = "https://parlour.shiseido.co.jp/food_products/onlineshop/detail.html?prod_id=0000000291"
    product = scrape_product_detail(test_url)
    
    if not product:
        return jsonify({'error': '爬取失敗'}), 400
    
    print(f"[DEBUG] 爬取成功: {product['title']}")
    
    if not product['in_stock']:
        return jsonify({'error': '商品無庫存', 'product': product}), 400
    
    collection_id = get_or_create_collection("資生堂PARLOUR")
    print(f"[DEBUG] Collection ID: {collection_id}")
    
    result = upload_to_shopify(product, collection_id)
    
    print(f"[DEBUG] 上傳結果: {result}")
    
    if result['success']:
        shopify_product = result['product']
        admin_url = f"https://admin.shopify.com/store/{SHOPIFY_SHOP}/products/{shopify_product['id']}"
        
        return jsonify({
            'success': True,
            'message': '上架成功！',
            'product': {
                'sku': product['sku'],
                'original_title': product['title'],
                'translated_title': result.get('translated', {}).get('title', ''),
                'cost': result.get('cost', product['price']),
                'selling_price': result.get('selling_price', 0),
                'weight': product['weight'],
                'page_title': result.get('translated', {}).get('page_title', ''),
                'meta_description': result.get('translated', {}).get('meta_description', ''),
                'shopify_id': shopify_product['id'],
                'shopify_handle': shopify_product.get('handle', ''),
                'shopify_url': admin_url,
                'images_count': len(product.get('images', []))
            }
        })
    else:
        return jsonify({
            'success': False,
            'error': result['error'],
            'product': product
        }), 400


@app.route('/api/test-translate')
def test_translate():
    """測試翻譯功能"""
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    test_url = "https://parlour.shiseido.co.jp/food_products/onlineshop/detail.html?prod_id=0000000291"
    product = scrape_product_detail(test_url)
    
    if not product:
        return jsonify({'error': '爬取失敗'}), 400
    
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    
    return jsonify({
        'original': {
            'title': product['title'],
            'description': product.get('description', '')
        },
        'translated': translated
    })


if __name__ == '__main__':
    print("=" * 50)
    print("資生堂パーラー（Shiseido Parlour）爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

"""
小倉山莊商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 ogurasansou.co.jp 所有商品
2. 過濾無庫存商品
3. 計算材積重量 vs 實際重量，取大值
4. 上架到 Shopify（不重複上架）
5. 原價寫入成本價（Cost）
6. ★ 自動同步庫存：已上架商品缺貨 → 設為草稿；恢復庫存 → 重新上架
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

# 取得目前檔案的目錄，確保 templates 路徑正確
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))

# ========== 設定 ==========
SHOPIFY_SHOP = ""  # 從 shopify_token.json 讀取
SHOPIFY_ACCESS_TOKEN = ""  # 從 shopify_token.json 讀取

BASE_URL = "https://www.ogurasansou.co.jp"
CATEGORY_URL = "https://www.ogurasansou.co.jp/shop/c/c10/"

# 模擬瀏覽器 Headers
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7,zh;q=0.6',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Referer': 'https://www.ogurasansou.co.jp/',
}

# 建立 Session 保持 cookies
session = requests.Session()
session.headers.update(BROWSER_HEADERS)

# OpenAI API 設定 (從環境變數讀取)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

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
    price = round(price)
    return price

def translate_with_chatgpt(title, description):
    """
    使用 ChatGPT 翻譯商品名稱和說明，並生成 SEO 內容
    回傳：translated_title, translated_description, page_title, meta_description
    """
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
1. 這是日本京都小倉山莊的傳統米菓（仙貝、米果）
2. 翻譯要自然流暢，不要生硬
3. SEO 內容要包含：小倉山莊、日本、京都、伴手禮等關鍵字
4. 只回傳 JSON，不要其他文字"""

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
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': title,
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
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
    "deleted": 0,
    "reactivated": 0
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

def get_existing_products_full():
    """
    取得 Shopify 所有商品的完整資訊
    回傳 {sku: {'product_id': id, 'status': 'active'|'draft'}} 字典
    包含 active 和 draft 狀態的商品
    """
    products_map = {}
    # 注意：加上 status=any 才能取得 draft 商品
    url = shopify_api_url("products.json?limit=250&status=any")
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            print(f"Error fetching products: {response.status_code}")
            break
        
        data = response.json()
        for product in data.get('products', []):
            product_id = product.get('id')
            status = product.get('status', 'active')
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    products_map[sku] = {
                        'product_id': product_id,
                        'status': status
                    }
        
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

def get_collection_products_full(collection_id):
    """
    取得特定 Collection 內所有商品完整資訊
    回傳 {sku: {'product_id': id, 'status': 'active'|'draft'}} 字典
    """
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
            status = product.get('status', 'active')
            for variant in product.get('variants', []):
                sku = variant.get('sku')
                if sku and product_id:
                    products_map[sku] = {
                        'product_id': product_id,
                        'status': status
                    }
        
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

def get_collection_products_map(collection_id):
    """向下相容：只回傳 {sku: product_id}"""
    full = get_collection_products_full(collection_id)
    return {sku: info['product_id'] for sku, info in full.items()}

def set_product_draft(product_id):
    """將 Shopify 商品設為草稿"""
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

def set_product_active(product_id):
    """將草稿商品重新上架"""
    url = shopify_api_url(f"products/{product_id}.json")
    
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {
            "id": product_id,
            "status": "active"
        }
    })
    
    if response.status_code == 200:
        print(f"[重新上架] Product ID: {product_id}")
        # 重新發布到所有渠道
        publish_to_all_channels(product_id)
        return True
    else:
        print(f"[重新上架失敗] Product ID: {product_id}, 錯誤: {response.status_code}")
        return False

# 保留舊名稱的向下相容
def delete_shopify_product(product_id):
    """將 Shopify 商品設為草稿（向下相容）"""
    return set_product_draft(product_id)

def parse_dimension_weight(html_content):
    """
    解析寸法和重量
    材積重量計算：長*寬*高/6000000
    取材積重量和實際重量的較大值
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    
    dimension = None
    weight = None
    
    text = soup.get_text()
    
    dim_match = re.search(r'【寸法】[タテ縦]*(\d+(?:\.\d+)?)[×xX][ヨコ横]*(\d+(?:\.\d+)?)[×xX][高さ]*(\d+(?:\.\d+)?)\s*mm', text)
    if dim_match:
        h, w, d = float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))
        dimension_weight = (h * w * d) / 6000000
        dimension_weight = round(dimension_weight, 2)
        dimension = {"h": h, "w": w, "d": d, "volume_weight": dimension_weight}
    
    weight_match = re.search(r'【重量】(\d+(?:\.\d+)?)\s*kg', text)
    if weight_match:
        weight = float(weight_match.group(1))
    
    final_weight = 0
    if dimension and weight:
        final_weight = max(dimension['volume_weight'], weight)
    elif dimension:
        final_weight = dimension['volume_weight']
    elif weight:
        final_weight = weight
    
    return {
        "dimension": dimension,
        "actual_weight": weight,
        "final_weight": round(final_weight, 2)
    }

def check_product_in_stock(soup, page_text):
    """
    ★ 增強版庫存偵測
    綜合多種方式判斷商品是否有庫存
    回傳 True = 有庫存, False = 無庫存
    """
    
    # ===== 1. 明確的無庫存文字 =====
    out_of_stock_keywords = [
        '在庫がありません',
        '在庫：×',
        '在庫切れ',
        '売り切れ',
        '品切れ',
        '完売',
        '販売終了',
        'SOLD OUT',
        'sold out',
        'ただ今お取扱いできない商品です',
        'お取扱いできない商品',
    ]
    
    for keyword in out_of_stock_keywords:
        if keyword in page_text:
            print(f"[庫存偵測] 發現無庫存關鍵字: '{keyword}'")
            return False
    
    # ===== 2. 檢查在庫狀態標記 =====
    # 有庫存：在庫：○  或  在庫：△（殘りわずか）
    # 無庫存：在庫：×
    stock_match = re.search(r'在庫[：:]\s*([○△×])', page_text)
    if stock_match:
        stock_symbol = stock_match.group(1)
        if stock_symbol == '×':
            print(f"[庫存偵測] 在庫標記為 ×")
            return False
        elif stock_symbol in ('○', '△'):
            print(f"[庫存偵測] 在庫標記為 {stock_symbol}")
            return True
    
    # ===== 3. 檢查購物車按鈕是否存在 =====
    cart_button = soup.select_one(
        'a[href*="cart.aspx?goods="], '
        'input[value*="買い物かご"], '
        'button:contains("買い物かご"), '
        '.block-cart-btn'
    )
    # 也檢查文字中是否有「買い物かごに入れる」
    has_cart_text = '買い物かごに入れる' in page_text
    
    if not cart_button and not has_cart_text:
        print(f"[庫存偵測] 找不到購物車按鈕")
        return False
    
    # ===== 4. 檢查是否為已下架商品頁面 =====
    # 官網下架商品會顯示特定訊息
    if 'ご指定の商品は販売終了か' in page_text:
        print(f"[庫存偵測] 商品已下架（販売終了）")
        return False
    
    # ===== 5. 檢查 CSS class =====
    # 有些網站用 class 來標記無庫存
    sold_out_elem = soup.select_one(
        '.sold-out, .out-of-stock, .stock-none, '
        '.is-soldout, .is-out-of-stock, '
        '[data-stock="0"], [data-soldout="true"]'
    )
    if sold_out_elem:
        print(f"[庫存偵測] 發現無庫存 CSS class")
        return False
    
    # ===== 6. 預設：如果以上都沒命中，視為有庫存 =====
    print(f"[庫存偵測] 未發現無庫存跡象，判定為有庫存")
    return True

def scrape_product_list(category_url):
    """爬取商品列表頁面，取得所有商品連結（包含所有分頁）"""
    products = []
    page = 1
    max_pages = 10
    
    # 先訪問首頁取得 cookies
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    while page <= max_pages:
        if page == 1:
            url = CATEGORY_URL
        else:
            url = f"https://www.ogurasansou.co.jp/shop/c/c10_p{page}/"
        
        print(f"[爬取] 第 {page} 頁: {url}")
        
        try:
            response = session.get(url, timeout=30)
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                print(f"[結束] 第 {page} 頁不存在，狀態碼: {response.status_code}")
                break
            
            if page > 1 and '_p' not in response.url:
                print(f"[結束] 第 {page} 頁被重定向回第一頁")
                break
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            all_links = soup.find_all('a', href=re.compile(r'/shop/g/g\d+/'))
            
            print(f"[DEBUG] 第 {page} 頁找到 {len(all_links)} 個商品連結")
            
            new_count = 0
            seen_skus_this_page = set()
            
            for link in all_links:
                href = link.get('href', '')
                sku_match = re.search(r'/g/g(\d+)/', href)
                if sku_match:
                    sku = sku_match.group(1)
                    if sku in seen_skus_this_page:
                        continue
                    seen_skus_this_page.add(sku)
                    
                    if sku not in [p['sku'] for p in products]:
                        full_url = urljoin(BASE_URL, href)
                        products.append({
                            'url': full_url,
                            'sku': sku
                        })
                        new_count += 1
            
            print(f"[進度] 第 {page} 頁新增 {new_count} 個商品，累計 {len(products)} 個")
            
            if new_count == 0:
                print(f"[結束] 第 {page} 頁沒有新商品")
                break
            
            page += 1
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[錯誤] 爬取第 {page} 頁失敗: {e}")
            import traceback
            traceback.print_exc()
            break
    
    print(f"[完成] 共找到 {len(products)} 個商品，爬取了 {page} 頁")
    return products

def scrape_product_detail(url):
    """爬取單一商品詳細資訊"""
    try:
        response = session.get(url, timeout=30)
        response.encoding = 'utf-8'
        
        if response.status_code != 200:
            print(f"[錯誤] 狀態碼: {response.status_code} - {url}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # 商品名稱
        title = ""
        title_elem = soup.select_one('h2.block-goods-name--text, .block-goods-name--text')
        if title_elem:
            title = title_elem.get_text(strip=True)
        
        if not title:
            title_tag = soup.select_one('title')
            if title_tag:
                title = title_tag.get_text(strip=True).split(':')[0].split('|')[0].strip()
        
        print(f"[DEBUG] 標題: {title}")
        
        # 商品說明
        description = ""
        desc_elem = soup.select_one('.block-goods-comment1')
        if desc_elem:
            description = desc_elem.get_text(strip=True)
        
        print(f"[DEBUG] 說明: {description[:50]}..." if description else "[DEBUG] 說明: 無")
        
        # 價格
        price = 0
        price_elem = soup.select_one('.block-thumbnail-t--price, .price')
        if price_elem:
            price_match = re.search(r'[¥￥]([\d,]+)', price_elem.get_text())
            if price_match:
                price = int(price_match.group(1).replace(',', ''))
        
        if not price:
            price_match = re.search(r'[¥￥]([\d,]+)', page_text)
            if price_match:
                price = int(price_match.group(1).replace(',', ''))
        
        # 商品編號 - 從 URL 取得
        sku = ""
        url_sku = re.search(r'/g/g(\d+)/', url)
        if url_sku:
            sku = url_sku.group(1)
        
        if not sku:
            sku_elem = soup.select_one('.block-thumbnail-t--goods-id')
            if sku_elem:
                sku_match = re.search(r'(\d+)', sku_elem.get_text())
                if sku_match:
                    sku = sku_match.group(1)
        
        print(f"[DEBUG] SKU: {sku}")
        
        # ★ 使用增強版庫存偵測
        in_stock = check_product_in_stock(soup, page_text)
        print(f"[DEBUG] 庫存狀態: {'有庫存' if in_stock else '無庫存'}")
        
        # 解析重量
        weight_info = parse_dimension_weight(response.text)
        
        # 圖片
        images = []
        seen_images = set()
        
        for slide in soup.select('.slick-slide:not(.slick-cloned) a.js-lightbox-gallery-info-ogura'):
            href = slide.get('href', '')
            if href and '/img/goods/' in href:
                full_src = urljoin(BASE_URL, href)
                if full_src not in seen_images:
                    seen_images.add(full_src)
                    images.append(full_src)
        
        if not images:
            for link in soup.select('a[href*="/img/goods/"]'):
                href = link.get('href', '')
                if href and '/img/goods/' in href:
                    full_src = urljoin(BASE_URL, href)
                    if full_src not in seen_images:
                        seen_images.add(full_src)
                        images.append(full_src)
        
        if not images:
            for img in soup.select('img.block-src-l--image, img[src*="/img/goods/"]'):
                src = img.get('src', '')
                if src and '/img/goods/' in src:
                    full_src = urljoin(BASE_URL, src)
                    if full_src not in seen_images:
                        seen_images.add(full_src)
                        images.append(full_src)
        
        print(f"[DEBUG] 找到 {len(images)} 張圖片")
        
        # 規格資訊
        specs = {}
        
        content_match = re.search(r'【内容量】([^\n【]+)', page_text)
        if content_match:
            specs['content'] = content_match.group(1).strip()
        
        expiry_match = re.search(r'賞味期限[：:]\s*([^\n]+)', page_text)
        if expiry_match:
            specs['expiry'] = expiry_match.group(1).strip()
        
        allergen_match = re.search(r'アレルギー[：:]\s*([^\n]+)', page_text)
        if allergen_match:
            specs['allergen'] = allergen_match.group(1).strip()
        
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
        import traceback
        traceback.print_exc()
        return None

def get_or_create_collection(collection_title="小倉山莊"):
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
            'vendor': '小倉山荘',
            'product_type': '米菓・詰め合わせ',
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
            'tags': '小倉山荘, 日本, 京都, 米菓, あられ, せんべい, 伴手禮, 日本零食',
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
    token_status = '<span class="token-status token-ok">✓ 已載入</span>' if token_loaded else '<span class="token-status token-missing">✗ 未設定</span>'
    
    return f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>小倉山莊 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
            max-width: 900px; 
            margin: 0 auto; 
            padding: 20px;
            background: #f5f5f5;
        }}
        h1 {{ color: #333; border-bottom: 2px solid #e74c3c; padding-bottom: 10px; }}
        .card {{ 
            background: white; 
            border-radius: 8px; 
            padding: 20px; 
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .btn {{ 
            background: #e74c3c; 
            color: white; 
            border: none; 
            padding: 12px 24px; 
            border-radius: 5px; 
            cursor: pointer;
            font-size: 16px;
            margin-right: 10px;
        }}
        .btn:hover {{ background: #c0392b; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-secondary:hover {{ background: #2980b9; }}
        .progress-bar {{ 
            width: 100%; 
            height: 20px; 
            background: #eee; 
            border-radius: 10px;
            overflow: hidden;
            margin: 10px 0;
        }}
        .progress-fill {{ 
            height: 100%; 
            background: linear-gradient(90deg, #e74c3c, #f39c12);
            transition: width 0.3s;
        }}
        .status {{ 
            padding: 10px; 
            background: #f8f9fa; 
            border-radius: 5px;
            margin-top: 10px;
        }}
        .log {{ 
            max-height: 300px; 
            overflow-y: auto; 
            font-family: monospace; 
            font-size: 13px;
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 15px;
            border-radius: 5px;
        }}
        .log-success {{ color: #4ec9b0; }}
        .log-error {{ color: #f14c4c; }}
        .log-skip {{ color: #ce9178; }}
        .log-reactivate {{ color: #dcdcaa; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ 
            flex: 1; 
            min-width: 100px;
            text-align: center; 
            padding: 15px;
            background: #f8f9fa;
            border-radius: 5px;
        }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #e74c3c; }}
        .stat-label {{ font-size: 12px; color: #666; margin-top: 5px; }}
        .token-status {{ 
            display: inline-block;
            padding: 5px 10px;
            border-radius: 3px;
            font-size: 14px;
        }}
        .token-ok {{ background: #d4edda; color: #155724; }}
        .token-missing {{ background: #f8d7da; color: #721c24; }}
    </style>
</head>
<body>
    <h1>🍘 小倉山莊 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
        <button class="btn btn-secondary" onclick="testScrape()">測試爬取 (單一商品)</button>
    </div>
    
    <div class="card">
        <h3>開始爬取 &amp; 同步</h3>
        <p>爬取官網全站商品，自動同步庫存狀態到 Shopify</p>
        <ul style="font-size: 14px; color: #666;">
            <li>新商品 → 自動上架</li>
            <li>已上架但官網缺貨 → 自動設為草稿</li>
            <li>草稿商品但官網恢復庫存 → 自動重新上架</li>
            <li>官網已下架的商品 → 設為草稿</li>
        </ul>
        <button class="btn" id="startBtn" onclick="startScrape()">🚀 開始爬取 &amp; 同步</button>
        
        <div id="progressSection" style="display: none;">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill" style="width: 0%"></div>
            </div>
            <div class="status" id="statusText">準備中...</div>
            
            <div class="stats">
                <div class="stat">
                    <div class="stat-number" id="uploadedCount">0</div>
                    <div class="stat-label">新上架</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="reactivatedCount" style="color: #27ae60;">0</div>
                    <div class="stat-label">恢復上架</div>
                </div>
                <div class="stat">
                    <div class="stat-number" id="skippedCount">0</div>
                    <div class="stat-label">已跳過</div>
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
            const className = type ? 'class="log-' + type + '"' : '';
            logArea.innerHTML += '<div ' + className + '>[' + time + '] ' + msg + '</div>';
            logArea.scrollTop = logArea.scrollHeight;
        }}
        
        function clearLog() {{
            document.getElementById('logArea').innerHTML = '';
        }}
        
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
        
        async function testScrape() {{
            log('測試爬取單一商品...');
            try {{
                const res = await fetch('/api/test-scrape');
                const data = await res.json();
                if (data.success) {{
                    log('✓ 測試成功！', 'success');
                    log('  商品: ' + (data.product.translated_title || data.product.original_title));
                    log('  SKU: ' + data.product.sku);
                    log('  成本: ¥' + data.product.cost + ' → 售價: ¥' + data.product.selling_price);
                }} else {{
                    log('✗ 測試失敗: ' + data.error, 'error');
                }}
            }} catch (e) {{
                log('✗ 請求失敗: ' + e.message, 'error');
            }}
        }}
        
        async function startScrape() {{
            clearLog();
            log('開始爬取 & 同步流程...');
            
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
                
                log('✓ 同步任務已啟動', 'success');
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
                document.getElementById('statusText').textContent = 
                    data.current_product + ' (' + data.progress + '/' + data.total + ')';
                
                document.getElementById('uploadedCount').textContent = data.uploaded;
                document.getElementById('reactivatedCount').textContent = data.reactivated || 0;
                document.getElementById('skippedCount').textContent = data.skipped;
                document.getElementById('deletedCount').textContent = data.deleted || 0;
                document.getElementById('errorCount').textContent = data.errors.length;
                
                if (!data.running && data.progress > 0) {{
                    clearInterval(pollInterval);
                    document.getElementById('startBtn').disabled = false;
                    log('========== 同步完成 ==========', 'success');
                    log('新上架: ' + data.uploaded + 
                        ' | 恢復: ' + (data.reactivated || 0) + 
                        ' | 跳過: ' + data.skipped + 
                        ' | 草稿: ' + (data.deleted || 0) + 
                        ' | 錯誤: ' + data.errors.length);
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
    
    # 重置狀態
    scrape_status = {
        "running": True,
        "progress": 0,
        "total": 0,
        "current_product": "正在取得商品列表...",
        "products": [],
        "errors": [],
        "uploaded": 0,
        "skipped": 0,
        "deleted": 0,
        "reactivated": 0
    }
    
    if not load_shopify_token():
        scrape_status['running'] = False
        return jsonify({'error': '請先完成 Shopify OAuth 授權'}), 400
    
    import threading
    thread = threading.Thread(target=run_scrape)
    thread.start()
    
    return jsonify({'message': '開始爬取'})

def run_scrape():
    """
    ★ 改進後的爬取流程
    
    核心邏輯：
    1. 取得 Shopify 上「小倉山莊」Collection 內所有商品（含 draft）
    2. 爬取官網所有商品列表
    3. 對每個官網商品：
       a. Shopify 不存在 → 有庫存就新上架
       b. Shopify 存在且 active → 重新檢查庫存，缺貨就設草稿
       c. Shopify 存在且 draft → 重新檢查庫存，恢復就重新上架
    4. Collection 內有、但官網已不存在的 → 設為草稿
    """
    global scrape_status
    
    try:
        # 1. 取得或建立 Collection
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("小倉山莊")
        print(f"[INFO] Collection ID: {collection_id}")
        
        # 2. 取得 Shopify 所有商品（含 draft），用於新商品查重
        scrape_status['current_product'] = "正在取得 Shopify 商品列表..."
        all_products_full = get_existing_products_full()
        print(f"[INFO] Shopify 全站共 {len(all_products_full)} 個商品 (含草稿)")
        
        # 3. 取得「小倉山莊」Collection 內的商品（含 draft）
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_full = get_collection_products_full(collection_id)
        print(f"[INFO] 小倉山莊 Collection 內有 {len(collection_products_full)} 個商品")
        
        # 統計 Collection 內的狀態
        active_count = sum(1 for v in collection_products_full.values() if v['status'] == 'active')
        draft_count = sum(1 for v in collection_products_full.values() if v['status'] == 'draft')
        print(f"[INFO] Collection 狀態: active={active_count}, draft={draft_count}")
        
        # 4. 爬取官網商品列表
        scrape_status['current_product'] = "正在爬取官網商品列表..."
        product_list = scrape_product_list(CATEGORY_URL)
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 官網找到 {len(product_list)} 個商品")
        
        website_skus = set(item['sku'] for item in product_list)
        print(f"[INFO] 官網 SKU 列表: {len(website_skus)} 個")
        
        # 5. 逐一處理每個官網商品
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            sku = item['sku']
            scrape_status['current_product'] = f"處理: {sku} ({idx+1}/{len(product_list)})"
            
            # 檢查這個 SKU 在 Shopify 上的狀態
            existing_info = all_products_full.get(sku)
            
            if existing_info:
                product_id = existing_info['product_id']
                current_status = existing_info['status']
                
                # ★ 已存在的商品：重新檢查庫存
                print(f"[檢查庫存] SKU {sku} 已存在 (狀態: {current_status})，重新檢查庫存...")
                
                product = scrape_product_detail(item['url'])
                if not product:
                    print(f"[跳過] SKU {sku} 無法爬取詳情")
                    scrape_status['skipped'] += 1
                    time.sleep(0.5)
                    continue
                
                if product['in_stock'] and product['price'] >= 1000:
                    # 官網有庫存
                    if current_status == 'draft':
                        # ★ 草稿 → 恢復上架
                        print(f"[恢復上架] SKU {sku} 官網恢復庫存，重新上架")
                        if set_product_active(product_id):
                            scrape_status['reactivated'] += 1
                            scrape_status['products'].append({
                                'sku': sku,
                                'title': product['title'],
                                'status': 'reactivated'
                            })
                        else:
                            scrape_status['errors'].append(f"恢復上架失敗: {sku}")
                    else:
                        # active → 保持不動
                        print(f"[跳過] SKU {sku} 有庫存且已上架")
                        scrape_status['skipped'] += 1
                else:
                    # 官網無庫存或價格過低
                    if current_status == 'active':
                        # ★ 上架中 → 設為草稿
                        reason = '無庫存' if not product['in_stock'] else f'價格過低 (¥{product["price"]})'
                        print(f"[設為草稿] SKU {sku} {reason}")
                        if set_product_draft(product_id):
                            scrape_status['deleted'] += 1
                            scrape_status['products'].append({
                                'sku': sku,
                                'title': product['title'],
                                'status': 'draft',
                                'reason': reason
                            })
                        else:
                            scrape_status['errors'].append(f"設為草稿失敗: {sku}")
                    else:
                        # 本來就是草稿 → 保持不動
                        print(f"[跳過] SKU {sku} 無庫存且已是草稿")
                        scrape_status['skipped'] += 1
                
                time.sleep(0.5)
                continue
            
            # ===== 新商品：尚未在 Shopify 上 =====
            product = scrape_product_detail(item['url'])
            if not product:
                scrape_status['errors'].append(f"無法爬取: {item['url']}")
                time.sleep(0.5)
                continue
            
            # 檢查庫存
            if not product['in_stock']:
                print(f"[跳過] SKU {product['sku']} 無庫存（新商品不上架）")
                scrape_status['skipped'] += 1
                time.sleep(0.5)
                continue
            
            # 檢查價格
            if product['price'] < 1000:
                print(f"[跳過] SKU {product['sku']} 價格過低 (¥{product['price']})")
                scrape_status['skipped'] += 1
                time.sleep(0.5)
                continue
            
            # 上傳到 Shopify
            result = upload_to_shopify(product, collection_id)
            if result['success']:
                print(f"[成功] 新上架 SKU {product['sku']}")
                # 更新本地快取，防止同批次重複
                all_products_full[product['sku']] = {
                    'product_id': result['product']['id'],
                    'status': 'active'
                }
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'sku': product['sku'],
                    'title': result.get('translated', {}).get('title', product['title']),
                    'original_title': product['title'],
                    'price': product['price'],
                    'weight': product['weight'],
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
        
        # 6. ★ 處理官網已下架的商品（Collection 內有，但官網列表沒有）
        scrape_status['current_product'] = "正在處理官網已下架的商品..."
        collection_skus = set(collection_products_full.keys())
        skus_to_check = collection_skus - website_skus
        
        if skus_to_check:
            print(f"[INFO] 發現 {len(skus_to_check)} 個商品在官網列表中不存在，需要確認")
            
            for sku in skus_to_check:
                info = collection_products_full.get(sku, {})
                product_id = info.get('product_id')
                current_status = info.get('status', 'active')
                
                # 如果已經是草稿就跳過
                if current_status == 'draft':
                    print(f"[跳過] SKU {sku} 已是草稿")
                    continue
                
                if product_id:
                    scrape_status['current_product'] = f"設為草稿 (官網已下架): {sku}"
                    print(f"[設為草稿] SKU {sku} 官網已下架")
                    if set_product_draft(product_id):
                        scrape_status['deleted'] += 1
                        scrape_status['products'].append({
                            'sku': sku,
                            'status': 'draft',
                            'reason': '官網已下架',
                            'title': f'已設為草稿 (SKU: {sku})'
                        })
                    else:
                        scrape_status['errors'].append(f"設為草稿失敗: {sku}")
                    
                    time.sleep(0.5)
        else:
            print("[INFO] 沒有官網已下架的商品需要處理")
        
    except Exception as e:
        print(f"[錯誤] {e}")
        import traceback
        traceback.print_exc()
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
    
    test_url = "https://www.ogurasansou.co.jp/shop/g/g00167/"
    product = scrape_product_detail(test_url)
    
    if not product:
        return jsonify({'error': '爬取失敗'}), 400
    
    print(f"[DEBUG] 爬取成功: {product['title']}")
    
    if not product['in_stock']:
        return jsonify({'error': '商品無庫存', 'product': product}), 400
    
    collection_id = get_or_create_collection("小倉山莊")
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
    
    test_url = "https://www.ogurasansou.co.jp/shop/g/g00167/"
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
    os.makedirs('templates', exist_ok=True)
    
    print("=" * 50)
    print("小倉山莊爬蟲工具（含庫存同步）")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

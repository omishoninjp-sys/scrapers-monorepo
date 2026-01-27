"""
神戶風月堂商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 shop.fugetsudo-kobe.jp 所有分頁商品
2. 過濾成本價低於 1000 円的商品
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
import sys
import time
from urllib.parse import urljoin, urlencode
import math

# 處理 PyInstaller 打包後的路徑
if getattr(sys, 'frozen', False):
    # 執行的是 exe
    BASE_DIR = os.path.dirname(sys.executable)
    TEMPLATE_DIR = os.path.join(sys._MEIPASS, 'templates')
else:
    # 執行的是 py
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)

# ========== 設定 ==========
SHOPIFY_SHOP = ""  # 從 shopify_token.json 讀取
SHOPIFY_ACCESS_TOKEN = ""  # 從 shopify_token.json 讀取

BASE_URL = "https://shop.fugetsudo-kobe.jp"
# 商品列表頁面（分頁 URL）- 需要完整參數
LIST_URL_TEMPLATE = "https://shop.fugetsudo-kobe.jp/shop/shopbrand.html?page={page}&search=&sort=&money1=&money2=&prize1=&company1=&content1=&originalcode1=&category=&subcategory="

# 最低成本價門檻
MIN_COST_THRESHOLD = 1000

# 模擬瀏覽器 Headers
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7,zh;q=0.6',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Referer': 'https://shop.fugetsudo-kobe.jp/',
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
    token_file = os.path.join(BASE_DIR, "shopify_token.json")
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
    print(f"[錯誤] 找不到設定")
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
    
    # 四捨五入到整數
    price = round(price)
    
    return price

def translate_with_chatgpt(title, description):
    """
    使用 ChatGPT 翻譯商品名稱和說明，並生成 SEO 內容
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
1. 這是日本神戶風月堂的高級法蘭酥、餅乾禮盒
2. 【重要】商品名稱的開頭必須是「神戶風月堂」四個字，例如：「神戶風月堂 法蘭酥禮盒 12入」
3. ゴーフル 翻譯為「法蘭酥」
4. プティーゴーフル 翻譯為「迷你法蘭酥」
5. ミニゴーフル 翻譯為「小法蘭酥」
6. 神戸ぶっせ 翻譯為「神戶布雪」
7. レスポワール 翻譯為「雷斯波瓦」或保留「Lespoir」
8. 翻譯要自然流暢，不要生硬
9. SEO 內容要包含：神戶風月堂、日本、法蘭酥、伴手禮等關鍵字
10. 只回傳 JSON，不要其他文字"""

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
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家，專門處理日本傳統甜點的中文翻譯。商品名稱開頭一定要加上品牌名「神戶風月堂」。"},
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
            
            translated = json.loads(content)
            
            # 確保標題開頭有「神戶風月堂」
            translated_title = translated.get('title', title)
            if not translated_title.startswith('神戶風月堂'):
                translated_title = f"神戶風月堂 {translated_title}"
            
            return {
                'success': True,
                'title': translated_title,
                'description': translated.get('description', description),
                'page_title': translated.get('page_title', ''),
                'meta_description': translated.get('meta_description', '')
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': f"神戶風月堂 {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"神戶風月堂 {title}",
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
        
        # 處理分頁
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
        
        # 處理分頁
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

def parse_dimension_weight(soup, page_text):
    """
    解析寸法和重量
    從 .detailTxt 的表格中找「サイズ」欄位
    格式：6.0×14.4×9.2cm 或 14.4×15.7×5.5cm
    
    材積重量計算：長*寬*高/6000 (cm為單位)
    """
    dimension = None
    weight = None
    
    # 方法1：從 .detailTxt 的表格結構中找サイズ
    detail_txt = soup.select_one('.detailTxt')
    if detail_txt:
        # 找所有 row
        rows = detail_txt.select('.row')
        for row in rows:
            cells = row.select('.cell')
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True)
                value = cells[1].get_text(strip=True)
                
                if 'サイズ' in label:
                    # 解析尺寸：6.0×14.4×9.2cm
                    # 可能用 × 或 x 或 X
                    size_match = re.search(r'([\d.]+)\s*[×xX]\s*([\d.]+)\s*[×xX]\s*([\d.]+)\s*cm', value)
                    if size_match:
                        d1 = float(size_match.group(1))
                        d2 = float(size_match.group(2))
                        d3 = float(size_match.group(3))
                        # 材積重量 = 長*寬*高/6000 (cm為單位)
                        volume_weight = (d1 * d2 * d3) / 6000
                        volume_weight = round(volume_weight, 2)
                        dimension = {
                            "d1": d1, 
                            "d2": d2, 
                            "d3": d3, 
                            "size_str": value,
                            "volume_weight": volume_weight
                        }
                        print(f"[DEBUG] 尺寸: {d1} × {d2} × {d3} cm, 材積重量: {volume_weight} kg")
                    break
    
    # 方法2：如果方法1沒找到，從整個頁面文字找
    if not dimension:
        # 嘗試找 サイズ 後面的尺寸
        size_patterns = [
            r'サイズ[^\d]*([\d.]+)\s*[×xX]\s*([\d.]+)\s*[×xX]\s*([\d.]+)\s*cm',
            r'([\d.]+)\s*[×xX]\s*([\d.]+)\s*[×xX]\s*([\d.]+)\s*cm',
        ]
        
        for pattern in size_patterns:
            size_match = re.search(pattern, page_text)
            if size_match:
                d1 = float(size_match.group(1))
                d2 = float(size_match.group(2))
                d3 = float(size_match.group(3))
                volume_weight = (d1 * d2 * d3) / 6000
                volume_weight = round(volume_weight, 2)
                dimension = {
                    "d1": d1, 
                    "d2": d2, 
                    "d3": d3, 
                    "volume_weight": volume_weight
                }
                print(f"[DEBUG] 尺寸(備用): {d1} × {d2} × {d3} cm, 材積重量: {volume_weight} kg")
                break
    
    # 計算最終重量
    final_weight = 0
    if dimension:
        final_weight = dimension['volume_weight']
    else:
        # 如果完全找不到尺寸，設為 0，不要亂猜
        print(f"[WARNING] 找不到尺寸資訊，重量設為 0")
        final_weight = 0
    
    return {
        "dimension": dimension,
        "actual_weight": weight,
        "final_weight": round(final_weight, 2)
    }

def scrape_product_list():
    """
    爬取所有分頁的商品列表
    URL 格式：shopbrand.html?page=1, page=2, page=3...
    商品連結格式：/shop/shopdetail.html?brandcode=000000000539&amp;search=&amp;sort=
    """
    products = []
    seen_skus = set()
    
    # 先訪問首頁取得 cookies
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    page = 1
    max_pages = 20  # 最多爬 20 頁
    
    while page <= max_pages:
        url = LIST_URL_TEMPLATE.format(page=page)
        print(f"[爬取] {url}")
        
        try:
            response = session.get(url, timeout=30)
            # 風月堂使用 EUC-JP 編碼
            response.encoding = 'euc-jp'
            
            if response.status_code != 200:
                print(f"[結束] 頁面不存在，狀態碼: {response.status_code}")
                break
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # DEBUG: 印出所有 <a> 標籤的 href
            all_links = soup.find_all('a')
            print(f"[DEBUG] 頁面共有 {len(all_links)} 個連結")
            
            # 找含有 shopdetail 的連結
            shopdetail_links = [a for a in all_links if a.get('href') and 'shopdetail' in a.get('href', '')]
            print(f"[DEBUG] 含有 shopdetail 的連結: {len(shopdetail_links)} 個")
            
            if shopdetail_links:
                print(f"[DEBUG] 範例連結: {shopdetail_links[0].get('href')[:100]}")
            
            # 找所有商品連結 - 使用更寬鬆的匹配
            product_links = []
            for link in all_links:
                href = link.get('href', '')
                if 'shopdetail' in href and 'brandcode=' in href:
                    product_links.append(link)
            
            print(f"[DEBUG] 找到 {len(product_links)} 個商品連結")
            
            new_count = 0
            seen_brandcodes = set()
            
            for link in product_links:
                href = link.get('href', '')
                if not href:
                    continue
                
                # 解析商品編號 (brandcode)
                sku_match = re.search(r'brandcode=(\d+)', href)
                
                if sku_match:
                    brandcode = sku_match.group(1)
                    
                    # 跳過已處理的 brandcode（同一商品可能有多個連結）
                    if brandcode in seen_brandcodes:
                        continue
                    seen_brandcodes.add(brandcode)
                    
                    sku = f"FGT-{brandcode}"
                    
                    # 使用詳情頁 URL 格式
                    full_url = f"{BASE_URL}/shopdetail/{brandcode}/"
                    
                    if sku not in seen_skus:
                        products.append({
                            'url': full_url,
                            'sku': sku,
                            'brandcode': brandcode
                        })
                        seen_skus.add(sku)
                        new_count += 1
            
            print(f"[進度] 新增 {new_count} 個商品，累計 {len(products)} 個")
            
            # 如果這頁沒有新商品，可能到底了
            if new_count == 0:
                print(f"[結束] 沒有新商品")
                break
            
            # 檢查是否有下一頁
            next_page = soup.find('a', href=re.compile(rf'page={page + 1}'))
            if not next_page:
                # 嘗試找「次へ」或「Next」連結
                next_link = soup.find('a', string=re.compile(r'次|next', re.IGNORECASE))
                if not next_link:
                    print(f"[結束] 沒有下一頁")
                    break
            
            page += 1
            time.sleep(0.5)
            
        except Exception as e:
            print(f"[錯誤] 爬取失敗: {e}")
            import traceback
            traceback.print_exc()
            break
    
    print(f"[完成] 共找到 {len(products)} 個商品")
    return products

def scrape_product_detail(url):
    """爬取單一商品詳細資訊 - 神戶風月堂"""
    try:
        response = session.get(url, timeout=30)
        # 風月堂使用 EUC-JP 編碼
        response.encoding = 'euc-jp'
        
        if response.status_code != 200:
            print(f"[錯誤] 狀態碼: {response.status_code} - {url}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # === 商品名稱 ===
        # 從 #itemInfo h2 取得
        title = ""
        title_elem = soup.select_one('#itemInfo h2')
        if title_elem:
            title = title_elem.get_text(strip=True)
        
        if not title:
            # 備用：從 og:title meta 取得
            og_title = soup.find('meta', property='og:title')
            if og_title:
                title = og_title.get('content', '').split('－')[0].strip()
        
        print(f"[DEBUG] 標題: {title}")
        
        # === 商品說明 ===
        # 從 .detailTxt 取得
        description = ""
        desc_elem = soup.select_one('.detailTxt')
        if desc_elem:
            # 只取第一段文字，不要整個規格表
            first_p = desc_elem.find('p')
            if first_p:
                description = first_p.get_text(strip=True)
            else:
                description = desc_elem.get_text(strip=True)[:500]
        
        # 備用：從 og:description
        if not description:
            og_desc = soup.find('meta', property='og:description')
            if og_desc:
                description = og_desc.get('content', '')[:500]
        
        print(f"[DEBUG] 說明: {description[:50]}..." if description else "[DEBUG] 說明: 無")
        
        # === 價格 ===
        # 優先從 meta product:price:amount 取得（最準確）
        price = 0
        price_meta = soup.find('meta', property='product:price:amount')
        if price_meta:
            try:
                price = int(price_meta.get('content', '0'))
            except:
                pass
        
        # 備用：從頁面文字解析 税込XXX円
        if not price:
            price_match = re.search(r'税込\s*([\d,]+)\s*円', page_text)
            if price_match:
                price = int(price_match.group(1).replace(',', ''))
        
        print(f"[DEBUG] 價格: {price}")
        
        # === 商品編號（SKU）===
        # 從 URL 取得 brandcode
        sku = ""
        brandcode_match = re.search(r'/shopdetail/(\d+)/', url)
        if brandcode_match:
            sku = f"FGT-{brandcode_match.group(1)}"
        else:
            # 從頁面取得商品コード
            code_match = re.search(r'商品コード\s*[：:]\s*(\d+)', page_text)
            if code_match:
                sku = f"FGT-{code_match.group(1)}"
        
        print(f"[DEBUG] SKU: {sku}")
        
        # === 庫存狀態 ===
        in_stock = True
        # 檢查「残りあと」或無庫存關鍵字
        if '在庫がありません' in page_text or '在庫切れ' in page_text or '品切れ' in page_text or 'SOLD OUT' in page_text:
            in_stock = False
        
        # 檢查有沒有庫存數量顯示
        stock_match = re.search(r'残りあと(\d+)個', page_text)
        if stock_match:
            stock_count = int(stock_match.group(1))
            in_stock = stock_count > 0
        
        print(f"[DEBUG] 庫存: {'有' if in_stock else '無'}")
        
        # === 重量 ===
        weight_info = parse_dimension_weight(soup, page_text)
        
        # === 圖片 ===
        images = []
        seen_images = set()
        
        # 從 .M_imageMain 取得主圖
        main_images = soup.select('.M_imageMain img')
        for img in main_images:
            src = img.get('src', '')
            if src and 'noimage' not in src.lower():
                # 把縮圖 URL 改成大圖
                full_src = src.replace('/s1_', '/1_').replace('/s2_', '/2_').replace('/s3_', '/3_').replace('/s4_', '/4_').replace('/s5_', '/5_').replace('/s6_', '/6_')
                if full_src not in seen_images:
                    seen_images.add(full_src)
                    images.append(full_src)
        
        # 從 .M_imageCatalog 取得縮圖（轉大圖）
        thumb_images = soup.select('.M_imageCatalog img')
        for img in thumb_images:
            src = img.get('src', '')
            if src and 'noimage' not in src.lower():
                # 縮圖格式: s1_000000000020.jpg → 1_000000000020.jpg
                full_src = re.sub(r'/s(\d)_', r'/\1_', src)
                if full_src not in seen_images:
                    seen_images.add(full_src)
                    images.append(full_src)
        
        # 從 og:image 取得
        if not images:
            og_image = soup.find('meta', property='og:image')
            if og_image:
                img_url = og_image.get('content', '')
                if img_url:
                    images.append(img_url)
        
        print(f"[DEBUG] 找到 {len(images)} 張圖片")
        
        # === 規格資訊 ===
        specs = {}
        
        # 內容量
        content_match = re.search(r'内容量[^\d]*?([\w\d]+(?:個|枚|入|g|kg|本|缶))', page_text)
        if content_match:
            specs['content'] = content_match.group(1).strip()
        
        # 賞味期限
        expiry_match = re.search(r'賞味期[間限][^\d]*?(?:出荷日より)?約?(\d+日?)', page_text)
        if expiry_match:
            specs['expiry'] = expiry_match.group(1).strip()
        
        # 特定原材料（過敏原）
        allergen_match = re.search(r'特定原材料等\d*品目[^\w]*([\w・]+)', page_text)
        if allergen_match:
            specs['allergen'] = allergen_match.group(1).strip()
        
        # 尺寸
        size_match = re.search(r'サイズ[^\d]*([\d.]+[×xX][\d.]+(?:[×xX][\d.]+)?)\s*cm', page_text)
        if size_match:
            specs['size'] = size_match.group(1) + 'cm'
        
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

def get_or_create_collection(collection_title="神戶風月堂"):
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
    
    print(f"[發布] 找到 {len(unique_publications)} 個銷售渠道")
    
    publication_inputs = [{"publicationId": pub['id']} for pub in unique_publications]
    
    mutation = """
    mutation publishablePublish($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount {
            count
          }
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
        
        if errors:
            real_errors = [e for e in errors if 'does not exist' not in e.get('message', '')]
            if real_errors:
                print(f"[發布] 錯誤: {real_errors}")
        
        return True
    else:
        print(f"[發布] GraphQL 請求失敗: {pub_response.status_code}")
        return False

def upload_to_shopify(product, collection_id=None):
    """上傳商品到 Shopify"""
    
    # 翻譯商品名稱和說明
    print(f"[翻譯] 正在翻譯: {product['title'][:30]}...")
    translated = translate_with_chatgpt(product['title'], product.get('description', ''))
    
    if translated['success']:
        print(f"[翻譯成功] {translated['title'][:30]}...")
    else:
        print(f"[翻譯失敗] 使用原文")
    
    # 計算售價
    cost = product['price']
    weight = product.get('weight', 0)
    selling_price = calculate_selling_price(cost, weight)
    
    print(f"[價格計算] 進貨價: ¥{cost}, 重量: {weight}kg, 售價: ¥{selling_price}")
    
    # 準備圖片資料
    images = []
    for idx, img_url in enumerate(product.get('images', [])):
        images.append({
            'src': img_url,
            'position': idx + 1
        })
    
    # 建立商品資料
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': translated['description'],
            'vendor': '神戶風月堂',
            'product_type': '法蘭酥',
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
            'tags': '神戶風月堂, 日本, 法蘭酥, ゴーフル, 伴手禮, 日本零食, 神戶',
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
        
        print(f"[DEBUG] 商品建立成功: ID={product_id}")
        
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
    <title>神戶風月堂 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #8B4513; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #8B4513; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #6B3510; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .btn-secondary:hover {{ background: #2980b9; }}
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
    <h1>🍪 神戶風月堂 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
        <button class="btn btn-secondary" onclick="testScrape()">測試爬取</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 shop.fugetsudo-kobe.jp 全站商品並上架到 Shopify</p>
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
        
        async function testScrape() {{
            log('測試爬取...');
            try {{
                const res = await fetch('/api/test-scrape');
                const data = await res.json();
                log('結果: ' + JSON.stringify(data).substring(0, 200) + '...');
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
        # 1. 取得 Shopify 所有商品 (用於檢查是否已存在，避免重複上架)
        scrape_status['current_product'] = "正在檢查 Shopify 已有商品..."
        existing_products_map = get_existing_products_map()
        existing_skus = set(existing_products_map.keys())
        print(f"[INFO] Shopify 全站已有 {len(existing_skus)} 個商品")
        
        # 2. 取得或建立 Collection
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("神戶風月堂")
        print(f"[INFO] Collection ID: {collection_id}")
        
        # 2.5 取得 Collection 內的商品（只有這些才會被設為草稿）
        scrape_status['current_product'] = "正在取得 Collection 內商品..."
        collection_products_map = get_collection_products_map(collection_id)
        collection_skus = set(collection_products_map.keys())
        print(f"[INFO] 神戶風月堂 Collection 內有 {len(collection_skus)} 個商品")
        
        # 3. 爬取商品列表
        scrape_status['current_product'] = "正在爬取商品列表..."
        product_list = scrape_product_list()
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        # 取得官網所有 SKU（用於草稿比對）
        website_skus = set(item['sku'] for item in product_list)
        print(f"[INFO] 官網 SKU 列表: {len(website_skus)} 個")
        
        # 4. 爬取每個商品詳情並上傳
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item['sku']}"
            
            if item['sku'] in existing_skus:
                print(f"[跳過] SKU {item['sku']} 已存在")
                scrape_status['skipped'] += 1
                continue
            
            product = scrape_product_detail(item['url'])
            if not product:
                scrape_status['errors'].append(f"無法爬取: {item['url']}")
                continue
            
            # 檢查成本價門檻
            if product['price'] < MIN_COST_THRESHOLD:
                print(f"[跳過] SKU {product['sku']} 成本價 ¥{product['price']} 低於門檻 ¥{MIN_COST_THRESHOLD}")
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
                    'selling_price': result.get('selling_price', 0),
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
        
        # 5. 設為草稿：只針對 Collection 內、但官網已下架的商品
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
    """測試爬取（不上架）"""
    if not load_shopify_token():
        return jsonify({'error': '請先完成 Shopify OAuth 授權'}), 400
    
    # 先訪問首頁取得 cookies
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    # 測試抓取列表頁第一頁
    products = []
    url = LIST_URL_TEMPLATE.format(page=1)
    
    try:
        response = session.get(url, timeout=30)
        response.encoding = 'euc-jp'
        
        html_content = response.text
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 找所有連結
        all_links = soup.find_all('a')
        
        # 找含有 shopdetail 的連結
        shopdetail_links = []
        for link in all_links:
            href = link.get('href', '')
            if 'shopdetail' in href and 'brandcode=' in href:
                shopdetail_links.append(href)
        
        # 去重（用 brandcode）
        seen = set()
        unique_items = []
        for href in shopdetail_links:
            brandcode_match = re.search(r'brandcode=(\d+)', href)
            if brandcode_match:
                brandcode = brandcode_match.group(1)
                if brandcode not in seen:
                    seen.add(brandcode)
                    unique_items.append({'href': href, 'brandcode': brandcode})
        
        # 取前 3 個測試
        for item in unique_items[:3]:
            brandcode = item['brandcode']
            full_url = f"{BASE_URL}/shopdetail/{brandcode}/"
            
            product = scrape_product_detail(full_url)
            if product:
                products.append(product)
        
        return jsonify({
            'success': True,
            'message': f'找到 {len(unique_items)} 個商品連結',
            'total_links': len(all_links),
            'shopdetail_links': len(shopdetail_links),
            'products': products,
            'sample_links': [item['href'] for item in unique_items[:5]],
            'html_sample': html_content[:3000]  # 回傳部分 HTML 供檢視
        })
        
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 400

@app.route('/api/test-upload')
def test_upload():
    """測試爬取並上架一個商品"""
    if not load_shopify_token():
        return jsonify({'error': '請先完成 Shopify OAuth 授權'}), 400
    
    # 先訪問首頁取得 cookies
    session.get(BASE_URL, timeout=30)
    time.sleep(0.5)
    
    # 爬取列表頁第一頁，找第一個符合條件的商品
    url = LIST_URL_TEMPLATE.format(page=1)
    
    try:
        response = session.get(url, timeout=30)
        response.encoding = 'euc-jp'
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 找商品連結 - 格式: shopdetail.html?brandcode=000000000539
        product_links = soup.find_all('a', href=re.compile(r'shopdetail\.html\?brandcode='))
        
        # 去重（用 brandcode）
        seen = set()
        unique_items = []
        for link in product_links:
            href = link.get('href', '')
            brandcode_match = re.search(r'brandcode=(\d+)', href)
            if brandcode_match:
                brandcode = brandcode_match.group(1)
                if brandcode not in seen:
                    seen.add(brandcode)
                    unique_items.append({'href': href, 'brandcode': brandcode})
        
        if not unique_items:
            return jsonify({'error': '找不到商品連結'}), 400
        
        # 找第一個價格 >= 1000 的商品
        product = None
        for item in unique_items:
            brandcode = item['brandcode']
            full_url = f"{BASE_URL}/shopdetail/{brandcode}/"
            
            p = scrape_product_detail(full_url)
            if p and p['price'] >= MIN_COST_THRESHOLD and p['in_stock']:
                product = p
                break
        
        if not product:
            return jsonify({'error': f'找不到價格 >= ¥{MIN_COST_THRESHOLD} 且有庫存的商品'}), 400
        
        print(f"[DEBUG] 爬取成功: {product['title']}")
        
        # 取得或建立 Collection
        collection_id = get_or_create_collection("神戶風月堂")
        
        # 上傳到 Shopify
        result = upload_to_shopify(product, collection_id)
        
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
                    'shopify_id': shopify_product['id'],
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
            
    except Exception as e:
        import traceback
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 400

if __name__ == '__main__':
    print("=" * 50)
    print("神戶風月堂爬蟲工具")
    print(f"最低成本價門檻：¥{MIN_COST_THRESHOLD}")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

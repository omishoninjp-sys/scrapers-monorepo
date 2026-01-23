"""
虎屋羊羹商品爬蟲 + Shopify 上架工具
功能：
1. 爬取 toraya-group.co.jp 的所有商品
2. 過濾 1000円以下的商品（不上架）
3. 計算材積重量 vs 實際重量，取大值
4. 上架到 Shopify（不重複上架）
5. 原價寫入成本價（Cost）
6. 商品名稱開頭加上「虎屋羊羹」
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
SHOPIFY_SHOP = ""  # 從 shopify_token.json 讀取
SHOPIFY_ACCESS_TOKEN = ""  # 從 shopify_token.json 讀取

BASE_URL = "https://www.toraya-group.co.jp"
CHECKOUT_URL = "https://checkout.toraya-group.co.jp"
PRODUCT_LIST_URL = "https://www.toraya-group.co.jp/onlineshop/all"

# 最低價格門檻（1000円以下不上架）
MIN_PRICE = 1000

# 商品名稱前綴
PRODUCT_PREFIX = "虎屋羊羹｜"

# 模擬瀏覽器 Headers
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7,zh;q=0.6',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Referer': 'https://www.toraya-group.co.jp/',
}

# 建立 Session 保持 cookies
session = requests.Session()
session.headers.update(BROWSER_HEADERS)

# OpenAI API 設定 (從環境變數讀取)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# 預設重量（當無法取得時使用）
DEFAULT_WEIGHT = 0.5

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

【重要翻譯規則】：
1. 商品名稱開頭必須是「虎屋羊羹｜」（注意｜是全形分隔符）
2. 所有日文必須完全翻譯成繁體中文，不可保留任何日文字符（包括平假名、片假名、漢字讀音）
3. 常見翻譯對照：
   - 羊羹・煉菓子詰合せ → 羊羹・煉菓子禮盒
   - 羊羹・あんやき詰合せ → 羊羹・紅豆燒禮盒
   - 蜜芋ごよみ → 蜜芋時光
   - ラムレーズン → 蘭姆葡萄
   - 黒糖ココア → 黑糖可可
   - 小形羊羹 → 小型羊羹
   - 夜の梅 → 夜之梅
   - おもかげ → 憶影
   - 新緑 → 新綠
   - はちみつ → 蜂蜜
   - 和紅茶 → 和紅茶
   - 詰合せ/詰め合わせ → 禮盒
   - 号/號 → 號
4. 數字「3号」翻譯為「3號」
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
                    {"role": "system", "content": "你是專業的日本商品翻譯和 SEO 專家，專門處理日本傳統和菓子的中文翻譯。你必須將所有日文完全翻譯成繁體中文，絕對不可保留任何日文字符。"},
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
            
            # 確保標題開頭有「虎屋羊羹｜」
            title_result = translated.get('title', title)
            # 移除可能已存在的舊格式前綴
            if title_result.startswith('虎屋羊羹｜'):
                pass  # 已經有正確格式
            elif title_result.startswith('虎屋羊羹'):
                # 移除舊前綴，加上新前綴
                title_result = title_result[4:].lstrip()  # 移除「虎屋羊羹」
                title_result = f"{PRODUCT_PREFIX}{title_result}"
            else:
                title_result = f"{PRODUCT_PREFIX}{title_result}"
            
            return {
                'success': True,
                'title': title_result,
                'description': translated.get('description', description),
                'page_title': translated.get('page_title', ''),
                'meta_description': translated.get('meta_description', '')
            }
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return {
                'success': False,
                'title': f"{PRODUCT_PREFIX} {title}",
                'description': description,
                'page_title': '',
                'meta_description': ''
            }
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return {
            'success': False,
            'title': f"{PRODUCT_PREFIX} {title}",
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
            if match:
                url = match.group(1)
            else:
                url = None
        else:
            url = None
    
    print(f"[INFO] Collection 內有 {len(products_map)} 個商品")
    return products_map

def set_product_to_draft(product_id):
    """將 Shopify 商品設為草稿"""
    url = shopify_api_url(f"products/{product_id}.json")
    response = requests.put(url, headers=get_shopify_headers(), json={
        "product": {"id": product_id, "status": "draft"}
    })
    if response.status_code == 200:
        print(f"[設為草稿] Product ID: {product_id}")
        return True
    return False

def parse_dimension_weight_from_soup(soup):
    """
    從 BeautifulSoup 解析寸法和重量
    虎屋格式：
    - <dt>大きさ</dt><dd>22.9×22.4×6.0cm</dd>
    - <dt>重さ</dt><dd>1.054kg</dd>
    
    材積重量計算：長*寬*高/6000 (cm為單位)
    取材積重量和實際重量的較大值
    """
    dimension = None
    weight = None
    
    # 找 DefinitionBlock 區塊
    definition_blocks = soup.select('.DefinitionBlock, dl')
    
    for block in definition_blocks:
        # 找所有 dt/dd 配對
        dts = block.find_all('dt')
        for dt in dts:
            dt_text = dt.get_text(strip=True)
            dd = dt.find_next_sibling('dd')
            if not dd:
                continue
            dd_text = dd.get_text(strip=True)
            
            # 解析大きさ (尺寸)
            if '大きさ' in dt_text:
                # 格式: 22.9×22.4×6.0cm
                dim_match = re.search(r'(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*cm', dd_text)
                if dim_match:
                    l, w, h = float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))
                    # 材積重量 = 長*寬*高/6000 (cm為單位)
                    volume_weight = (l * w * h) / 6000
                    volume_weight = round(volume_weight, 2)
                    dimension = {"l": l, "w": w, "h": h, "volume_weight": volume_weight}
                    print(f"[DEBUG] 寸法: {l} x {w} x {h} cm, 材積重量: {volume_weight} kg")
            
            # 解析重さ (重量)
            if '重さ' in dt_text:
                # 格式: 1.054kg 或 500g
                weight_match = re.search(r'(\d+(?:\.\d+)?)\s*(kg|g)', dd_text, re.IGNORECASE)
                if weight_match:
                    weight_val = float(weight_match.group(1))
                    unit = weight_match.group(2).lower()
                    if unit == 'g':
                        weight = weight_val / 1000
                    else:
                        weight = weight_val
                    print(f"[DEBUG] 實際重量: {weight} kg")
    
    # 如果沒找到，嘗試從全文解析
    if not dimension or not weight:
        page_text = soup.get_text()
        
        if not dimension:
            dim_match = re.search(r'(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*cm', page_text)
            if dim_match:
                l, w, h = float(dim_match.group(1)), float(dim_match.group(2)), float(dim_match.group(3))
                volume_weight = (l * w * h) / 6000
                volume_weight = round(volume_weight, 2)
                dimension = {"l": l, "w": w, "h": h, "volume_weight": volume_weight}
                print(f"[DEBUG] 從全文找到寸法: {l} x {w} x {h} cm, 材積重量: {volume_weight} kg")
        
        if not weight:
            weight_match = re.search(r'(\d+(?:\.\d+)?)\s*kg', page_text, re.IGNORECASE)
            if weight_match:
                weight = float(weight_match.group(1))
                print(f"[DEBUG] 從全文找到重量: {weight} kg")
    
    # 計算最終重量（取較大值）
    final_weight = 0
    if dimension and weight:
        final_weight = max(dimension['volume_weight'], weight)
        print(f"[DEBUG] 取較大值: 材積重量 {dimension['volume_weight']} kg vs 實際重量 {weight} kg = {final_weight} kg")
    elif dimension:
        final_weight = dimension['volume_weight']
    elif weight:
        final_weight = weight
    else:
        # 根據商品類型估算重量
        final_weight = 0.3
        print(f"[DEBUG] 無法取得重量，預設: {final_weight} kg")
    
    return {
        "dimension": dimension,
        "actual_weight": weight,
        "final_weight": round(final_weight, 2)
    }


def extract_landing_page_html(soup):
    """
    提取 Landing Page 的 AssortItems 區塊（詰め合わせ内容）
    只抓取這個區塊，其他不要
    回傳原始資料供後續翻譯
    """
    # 只抓取 AssortItems 區塊
    assort_items = soup.select_one('.AssortItems')
    if not assort_items:
        print("[DEBUG] 找不到 AssortItems 區塊")
        return None
    
    print("[DEBUG] 找到 AssortItems 區塊")
    
    # 提取結構化資料供翻譯
    items_data = []
    
    # 只用 .AssortItemList li 來找商品，避免重複
    items = assort_items.select('.AssortItemList > li')
    
    # 如果找不到，嘗試其他選擇器
    if not items:
        items = assort_items.select('ul > li')
    
    print(f"[DEBUG] 找到 {len(items)} 個 li 項目")
    
    for item in items:
        # 圖片
        img = item.select_one('img')
        img_src = img.get('src', '') if img else ''
        
        # 商品名 - 從 h4 取得
        name_elem = item.select_one('h4')
        name = name_elem.get_text(strip=True) if name_elem else ''
        
        # 特定原材料等
        allergen = ''
        for dl in item.select('dl'):
            dt = dl.select_one('dt')
            if dt and '特定原材料' in dt.get_text():
                dd = dl.select_one('dd')
                if dd:
                    allergen = dd.get_text(strip=True)
                break
        
        # 賞味期限
        expiry = ''
        for dl in item.select('dl'):
            dt = dl.select_one('dt')
            if dt and '賞味' in dt.get_text():
                dd = dl.select_one('dd')
                if dd:
                    expiry = dd.get_text(strip=True)
                break
        
        # 數量
        count = ''
        count_elem = item.select_one('.AssortItem__Count')
        if count_elem:
            count = count_elem.get_text(strip=True)
        
        if name:  # 只有有名稱的才加入
            items_data.append({
                'img_src': img_src,
                'name': name,
                'allergen': allergen,
                'expiry': expiry,
                'count': count
            })
            print(f"[DEBUG] 加入項目: {name}")
    
    if not items_data:
        return None
    
    print(f"[DEBUG] 總共提取 {len(items_data)} 個項目")
    return items_data


def translate_landing_html_with_chatgpt(items_data):
    """
    使用 ChatGPT 翻譯 Landing Page 的 AssortItems 內容
    """
    if not items_data:
        return ''
    
    # 準備要翻譯的文字
    items_text = json.dumps(items_data, ensure_ascii=False, indent=2)
    
    prompt = f"""你是專業的日本商品翻譯專家。請將以下日本和菓子商品資訊翻譯成繁體中文。

商品資料（JSON 格式）：
{items_text}

請將每個商品的以下欄位翻譯成繁體中文：
- name: 商品名稱
- allergen: 特定原材料（過敏原）
- expiry: 賞味期限

請回傳 JSON 格式（不要加 markdown 標記），保持原有結構，只翻譯文字內容：
[
  {{
    "img_src": "原封不動",
    "name": "翻譯後的商品名稱",
    "allergen": "翻譯後的過敏原（なし翻譯為「無」）",
    "expiry": "翻譯後的賞味期限",
    "count": "原封不動（數量）"
  }},
  ...
]

注意：
1. 這是日本虎屋的傳統羊羹（和菓子）
2. 商品名稱保留日文特色，但要讓台灣人能理解
3. 「なし」翻譯為「無」
4. 賞味期限格式如「製造から1年、到着日から8ヶ月前後」翻譯為「製造日起1年，預計到貨後約8個月」
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
                    {"role": "system", "content": "你是專業的日本商品翻譯專家，專門處理日本傳統和菓子的中文翻譯。"},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0,
                "max_tokens": 2000
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
            
            translated_items = json.loads(content)
            print(f"[翻譯] Landing HTML 翻譯成功，共 {len(translated_items)} 個項目")
            return translated_items
        else:
            print(f"[OpenAI 錯誤] {response.status_code}: {response.text}")
            return items_data  # 翻譯失敗，返回原始資料
            
    except Exception as e:
        print(f"[翻譯錯誤] {e}")
        return items_data  # 翻譯失敗，返回原始資料


def build_landing_html(translated_items):
    """
    根據翻譯後的資料建立 HTML
    """
    if not translated_items:
        return ''
    
    html = '<div class="product-assort-items" style="margin: 20px 0;">'
    html += '<h3 style="font-size: 18px; margin-bottom: 15px; border-bottom: 2px solid #8B4513; padding-bottom: 10px;">📦 詰合內容</h3>'
    
    html += '<table style="width:100%; border-collapse:collapse; margin:15px 0;">'
    html += '<thead><tr style="background:#f5f5f5;">'
    html += '<th style="padding:10px; border:1px solid #ddd; text-align:left;">商品</th>'
    html += '<th style="padding:10px; border:1px solid #ddd; text-align:center; width:100px;">過敏原</th>'
    html += '<th style="padding:10px; border:1px solid #ddd; text-align:left;">賞味期限</th>'
    html += '<th style="padding:10px; border:1px solid #ddd; text-align:center; width:60px;">數量</th>'
    html += '</tr></thead>'
    html += '<tbody>'
    
    for item in translated_items:
        html += '<tr>'
        html += '<td style="padding:10px; border:1px solid #ddd;">'
        if item.get('img_src'):
            html += f'<img src="{item["img_src"]}" style="width:50px; height:50px; object-fit:cover; margin-right:10px; vertical-align:middle; border-radius:4px;">'
        html += f'<span style="vertical-align:middle;">{item.get("name", "")}</span></td>'
        html += f'<td style="padding:10px; border:1px solid #ddd; text-align:center;">{item.get("allergen", "")}</td>'
        html += f'<td style="padding:10px; border:1px solid #ddd; font-size:13px;">{item.get("expiry", "")}</td>'
        html += f'<td style="padding:10px; border:1px solid #ddd; text-align:center; font-weight:bold;">{item.get("count", "")}</td>'
        html += '</tr>'
    
    html += '</tbody></table>'
    html += '</div>'
    
    return html

def scrape_product_list_selenium():
    """使用 requests 爬取商品列表（替代 Selenium 版本）"""
    products = []
    
    try:
        print("[INFO] 使用 requests 爬取商品列表...")
        
        # 嘗試從官網列表頁爬取
        response = session.get(PRODUCT_LIST_URL, timeout=30)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 查找商品連結 - 虎屋的格式：/onlineshop/xxx
            product_links = soup.find_all('a', href=re.compile(r'^/onlineshop/[^/]+$'))
            
            print(f"[DEBUG] 找到 {len(product_links)} 個連結")
            
            seen_urls = set()
            for link in product_links:
                href = link.get('href', '')
                if href and '/onlineshop/' in href:
                    handle = href.replace('/onlineshop/', '')
                    # 排除非商品頁
                    if handle in ['all', 'product', 'products', ''] or '/' in handle:
                        continue
                    
                    full_url = urljoin(BASE_URL, href)
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        products.append({
                            'url': full_url,
                            'sku': f"toraya-{handle}",
                            'need_detail_scrape': True
                        })
            
            print(f"[INFO] 從官網找到 {len(products)} 個商品")
        else:
            print(f"[WARN] 官網列表頁狀態碼: {response.status_code}")
        
    except Exception as e:
        print(f"[錯誤] requests 爬取失敗: {e}")
    
    return products

def scrape_product_detail_selenium(url):
    """使用 requests 爬取單一商品詳細資訊（替代 Selenium 版本）"""
    
    try:
        print(f"[INFO] 爬取商品: {url}")
        response = session.get(url, timeout=30)
        
        if response.status_code != 200:
            print(f"[WARN] 無法取得頁面，狀態碼: {response.status_code}")
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # 商品名稱
        title = ""
        title_elem = soup.select_one('h1')
        if title_elem:
            title = title_elem.get_text(strip=True)
        if not title:
            title_tag = soup.select_one('title')
            if title_tag:
                title = title_tag.get_text(strip=True).split('|')[0].strip()
        
        print(f"[DEBUG] 標題: {title}")
        
        # 提取 AssortItems 區塊資料（詰め合わせ内容）
        assort_items_data = extract_landing_page_html(soup)
        if assort_items_data:
            print(f"[DEBUG] AssortItems 找到 {len(assort_items_data)} 個項目")
        else:
            print("[DEBUG] 沒有找到 AssortItems")
        
        # 商品說明（簡短版，用於翻譯）
        description = ""
        for selector in ['.ProductDescription', '.product-description', '[class*="description"]', '[class*="detail"]']:
            desc_elem = soup.select_one(selector)
            if desc_elem:
                description = desc_elem.get_text(strip=True)[:500]
                break
        
        if not description:
            # 從 AssortItems 取得簡要說明
            assort = soup.select_one('.AssortItems')
            if assort:
                items = assort.select('.AssortItem h4')
                item_names = [item.get_text(strip=True) for item in items[:5]]
                if item_names:
                    description = f"詰め合わせ内容：{', '.join(item_names)}"
        
        print(f"[DEBUG] 說明: {description[:50]}..." if description else "[DEBUG] 說明: 無")
        
        # 價格 - 虎屋格式：¥1,000 或 1,000円
        price = 0
        price_patterns = [
            r'¥([\d,]+)',
            r'([\d,]+)円',
            r'税込[：:]\s*([\d,]+)',
        ]
        for pattern in price_patterns:
            price_match = re.search(pattern, page_text)
            if price_match:
                price = int(price_match.group(1).replace(',', ''))
                break
        
        print(f"[DEBUG] 價格: {price}")
        
        # 商品編號 - 從 URL 取得
        sku = ""
        # 匹配 /onlineshop/xxx 格式 (虎屋官網)
        url_sku = re.search(r'/onlineshop/([^/?]+)$', url)
        if url_sku:
            handle = url_sku.group(1)
            # 排除 "all" 這種列表頁
            if handle not in ['all', 'product', 'products']:
                sku = f"toraya-{handle}"
        
        # 備用：匹配 /products/xxx 格式 (Shopify)
        if not sku:
            url_sku = re.search(r'/products/([^/?]+)', url)
            if url_sku:
                sku = f"toraya-{url_sku.group(1)}"
        
        print(f"[DEBUG] SKU: {sku}")
        
        # 庫存狀態
        in_stock = True
        if '在庫がありません' in page_text or '在庫切れ' in page_text or '品切れ' in page_text or 'SOLD OUT' in page_text or '売り切れ' in page_text:
            in_stock = False
        
        print(f"[DEBUG] 庫存: {'有' if in_stock else '無'}")
        
        # 解析重量（使用新的函數）
        weight_info = parse_dimension_weight_from_soup(soup)
        
        # 如果無法取得重量，使用預設值
        if weight_info['final_weight'] == 0:
            weight_info['final_weight'] = DEFAULT_WEIGHT
            print(f"[DEBUG] 使用預設重量: {DEFAULT_WEIGHT}kg")
        
        # 圖片
        images = []
        seen_images = set()
        
        # 優先從主要商品圖片區域抓取
        for img in soup.select('.ProductImage img, .product-image img, [class*="ProductGallery"] img, [class*="Gallery"] img'):
            src = img.get('src', '') or img.get('data-src', '')
            if src and 'cdn.shopify' in src:
                # 清理圖片 URL，取得高解析度版本
                if '?' in src:
                    base_src = src.split('?')[0]
                else:
                    base_src = src
                if src.startswith('//'):
                    src = 'https:' + src
                    base_src = 'https:' + base_src
                
                if base_src not in seen_images:
                    seen_images.add(base_src)
                    images.append(src)
        
        # 備用：從所有 img 標籤找
        if len(images) < 3:
            for img in soup.select('img[src*="cdn.shopify"]'):
                src = img.get('src', '')
                if src and 'logo' not in src.lower() and 'icon' not in src.lower():
                    if '?' in src:
                        base_src = src.split('?')[0]
                    else:
                        base_src = src
                    if src.startswith('//'):
                        src = 'https:' + src
                        base_src = 'https:' + base_src
                    
                    if base_src not in seen_images:
                        seen_images.add(base_src)
                        images.append(src)
        
        print(f"[DEBUG] 找到 {len(images)} 張圖片")
        
        return {
            'url': url,
            'sku': sku,
            'title': title,
            'price': price,
            'in_stock': in_stock,
            'description': description,
            'assort_items_data': assort_items_data,  # 原始資料供後續翻譯
            'weight': weight_info['final_weight'],
            'weight_info': weight_info,
            'images': images[:10],
        }
        
    except Exception as e:
        print(f"[錯誤] 爬取商品失敗 {url}: {e}")
        import traceback
        traceback.print_exc()
        return None

def scrape_shopify_products():
    """從 Shopify API 爬取虎屋商品"""
    products = []
    
    try:
        # 嘗試直接從 Shopify 端點獲取商品
        url = f"{CHECKOUT_URL}/products.json"
        print(f"[INFO] 嘗試獲取 Shopify 產品: {url}")
        
        response = session.get(url, timeout=30)
        print(f"[DEBUG] 狀態碼: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            shopify_products = data.get('products', [])
            
            for p in shopify_products:
                handle = p.get('handle', '')
                title = p.get('title', '')
                
                # 取得價格
                variants = p.get('variants', [])
                price = 0
                if variants:
                    price_str = variants[0].get('price', '0')
                    price = int(float(price_str))
                
                # 取得圖片
                images = [img.get('src', '') for img in p.get('images', [])]
                
                # 商品頁 URL - 格式為 https://www.toraya-group.co.jp/onlineshop/{handle}
                product_url = f"{BASE_URL}/onlineshop/{handle}"
                
                products.append({
                    'url': product_url,
                    'sku': f"toraya-{handle}",
                    'title': title,
                    'price': price,
                    'description': '',  # 需要從詳情頁爬取
                    'landing_html': '',  # 需要從詳情頁爬取
                    'images': images,
                    'in_stock': True,
                    'weight': 0,  # 需要從詳情頁爬取
                    'weight_info': {'final_weight': 0},
                    'need_detail_scrape': True  # 標記需要爬取詳情
                })
            
            print(f"[INFO] 從 Shopify 找到 {len(products)} 個商品")
        else:
            print(f"[WARN] Shopify API 無法訪問，狀態碼: {response.status_code}")
            
    except Exception as e:
        print(f"[錯誤] Shopify 爬取失敗: {e}")
    
    return products

def get_or_create_collection(collection_title="虎屋羊羹"):
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
        print(f"[翻譯失敗] 使用原文（加上前綴）")
    
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
    
    # 準備商品說明 HTML
    description_html = ""
    
    # 翻譯後的說明
    if translated.get('description'):
        description_html += f"<div class='product-intro' style='margin-bottom:20px;'><p>{translated['description']}</p></div>"
    
    # 翻譯並建立 AssortItems HTML（詰合內容）
    assort_items_data = product.get('assort_items_data')
    if assort_items_data:
        print(f"[翻譯] 正在翻譯 AssortItems ({len(assort_items_data)} 個項目)...")
        translated_items = translate_landing_html_with_chatgpt(assort_items_data)
        landing_html = build_landing_html(translated_items)
        if landing_html:
            description_html += landing_html
    
    # 如果都沒有，使用原始說明
    if not description_html:
        desc = product.get('description', '')
        if desc:
            description_html = f"<p>{desc}</p>"
    
    # 建立商品資料
    shopify_product = {
        'product': {
            'title': translated['title'],
            'body_html': description_html,
            'vendor': '虎屋',
            'product_type': '羊羹',
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
            'tags': '虎屋, 羊羹, 日本, 和菓子, 伴手禮, 日本零食, toraya',
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
    <title>虎屋羊羹 爬蟲工具</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }}
        h1 {{ color: #333; border-bottom: 2px solid #2F4F4F; padding-bottom: 10px; }}
        .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .btn {{ background: #2F4F4F; color: white; border: none; padding: 12px 24px; border-radius: 5px; cursor: pointer; font-size: 16px; margin-right: 10px; }}
        .btn:hover {{ background: #1F3F3F; }}
        .btn:disabled {{ background: #ccc; cursor: not-allowed; }}
        .btn-secondary {{ background: #3498db; }}
        .progress-bar {{ width: 100%; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #2F4F4F, #5F8F8F); transition: width 0.3s; }}
        .status {{ padding: 10px; background: #f8f9fa; border-radius: 5px; margin-top: 10px; }}
        .log {{ max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 5px; }}
        .stats {{ display: flex; gap: 15px; margin-top: 15px; flex-wrap: wrap; }}
        .stat {{ flex: 1; min-width: 100px; text-align: center; padding: 15px; background: #f8f9fa; border-radius: 5px; }}
        .stat-number {{ font-size: 24px; font-weight: bold; color: #2F4F4F; }}
        .stat-label {{ font-size: 12px; color: #666; margin-top: 5px; }}
    </style>
</head>
<body>
    <h1>🍡 虎屋羊羹 爬蟲工具</h1>
    
    <div class="card">
        <h3>Shopify 連線狀態</h3>
        <p>Token: {token_status}</p>
        <button class="btn btn-secondary" onclick="testShopify()">測試連線</button>
    </div>
    
    <div class="card">
        <h3>開始爬取</h3>
        <p>爬取 toraya-group.co.jp 全站商品並上架到 Shopify</p>
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
                if (data.success) log('✓ 連線成功！', 'success');
                else log('✗ 連線失敗: ' + data.error, 'error');
            }} catch (e) {{ log('✗ 請求失敗: ' + e.message, 'error'); }}
        }}
        async function startScrape() {{
            clearLog(); log('開始爬取流程...');
            document.getElementById('startBtn').disabled = true;
            document.getElementById('progressSection').style.display = 'block';
            try {{
                const res = await fetch('/api/start', {{ method: 'POST' }});
                const data = await res.json();
                if (data.error) {{ log('✗ ' + data.error, 'error'); document.getElementById('startBtn').disabled = false; return; }}
                log('✓ 爬取任務已啟動', 'success');
                pollInterval = setInterval(pollStatus, 1000);
            }} catch (e) {{ log('✗ ' + e.message, 'error'); document.getElementById('startBtn').disabled = false; }}
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
                }}
            }} catch (e) {{ console.error(e); }}
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
        # 1. 取得已存在的 SKU
        scrape_status['current_product'] = "正在檢查 Shopify 已有商品..."
        existing_skus = get_existing_skus()
        print(f"[INFO] Shopify 已有 {len(existing_skus)} 個商品")
        
        # 2. 取得或建立 Collection
        scrape_status['current_product'] = "正在設定 Collection..."
        collection_id = get_or_create_collection("虎屋羊羹")
        print(f"[INFO] Collection ID: {collection_id}")
        
        # 3. 爬取商品列表
        scrape_status['current_product'] = "正在爬取商品列表..."
        
        # 先嘗試 Shopify API
        product_list = scrape_shopify_products()
        
        # 如果 Shopify API 失敗，使用 Selenium
        if not product_list:
            print("[INFO] 使用 Selenium 爬取...")
            product_list = scrape_product_list_selenium()
        
        scrape_status['total'] = len(product_list)
        print(f"[INFO] 找到 {len(product_list)} 個商品")
        
        # 4. 處理每個商品
        for idx, item in enumerate(product_list):
            scrape_status['progress'] = idx + 1
            scrape_status['current_product'] = f"處理: {item.get('title', item['sku'])}"
            
            # 檢查 SKU 是否已存在
            if item['sku'] in existing_skus:
                print(f"[跳過] SKU {item['sku']} 已存在")
                scrape_status['skipped'] += 1
                continue
            
            # 檢查價格門檻（如果已知價格）
            if item.get('price', 0) > 0 and item.get('price', 0) < MIN_PRICE:
                print(f"[跳過] SKU {item['sku']} 價格 {item['price']} 低於 {MIN_PRICE}円")
                scrape_status['skipped'] += 1
                continue
            
            # 需要爬取詳情頁來取得完整資訊（重量、Landing HTML 等）
            if item.get('need_detail_scrape') or item.get('weight', 0) == 0 or not item.get('landing_html'):
                print(f"[INFO] 爬取詳情頁: {item['url']}")
                detail = scrape_product_detail_selenium(item['url'])
                
                if detail:
                    # 合併資料（詳情頁的資料優先）
                    item['assort_items_data'] = detail.get('assort_items_data')
                    item['weight'] = detail.get('weight', item.get('weight', 0.3))
                    item['weight_info'] = detail.get('weight_info', item.get('weight_info', {}))
                    item['description'] = detail.get('description', item.get('description', ''))
                    
                    # 如果詳情頁有更多圖片，補充進來
                    if detail.get('images'):
                        existing_images = set(item.get('images', []))
                        for img in detail['images']:
                            if img not in existing_images:
                                item.setdefault('images', []).append(img)
                    
                    # 如果沒有價格，從詳情頁取
                    if item.get('price', 0) == 0 and detail.get('price', 0) > 0:
                        item['price'] = detail['price']
                    
                    # 檢查庫存
                    if not detail.get('in_stock', True):
                        item['in_stock'] = False
                else:
                    print(f"[WARN] 無法爬取詳情頁，使用現有資料")
                
                time.sleep(1)  # 避免請求過快
            
            product = item
            
            # 再次檢查價格門檻
            if product.get('price', 0) < MIN_PRICE:
                print(f"[跳過] SKU {product['sku']} 價格 {product['price']} 低於 {MIN_PRICE}円")
                scrape_status['skipped'] += 1
                continue
            
            # 檢查庫存
            if not product.get('in_stock', True):
                print(f"[跳過] SKU {product['sku']} 無庫存")
                scrape_status['skipped'] += 1
                continue
            
            # 確保有重量
            if product.get('weight', 0) == 0:
                product['weight'] = 0.3  # 預設重量
                product['weight_info'] = {'final_weight': 0.3}
            
            # 上傳到 Shopify
            result = upload_to_shopify(product, collection_id)
            if result['success']:
                print(f"[成功] 上傳 SKU {product['sku']}")
                scrape_status['uploaded'] += 1
                scrape_status['products'].append({
                    'sku': product['sku'],
                    'title': result.get('translated', {}).get('title', product['title']),
                    'original_title': product['title'],
                    'price': product['price'],
                    'selling_price': result.get('selling_price', 0),
                    'weight': product.get('weight', 0),
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
        collection_products_map = get_collection_products_map(collection_id)
        collection_skus = set(collection_products_map.keys())
        website_skus = set(item['sku'] for item in product_list)
        
        skus_to_draft = collection_skus - website_skus
        if skus_to_draft:
            print(f"[INFO] 發現 {len(skus_to_draft)} 個商品需要設為草稿")
            for sku in skus_to_draft:
                scrape_status['current_product'] = f"設為草稿: {sku}"
                product_id = collection_products_map.get(sku)
                if product_id and set_product_to_draft(product_id):
                    scrape_status['deleted'] += 1
                time.sleep(0.5)
        
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
    """測試爬取虎屋商品"""
    
    # 先嘗試 Shopify API
    products = scrape_shopify_products()
    
    if products:
        return jsonify({
            'success': True,
            'source': 'shopify_api',
            'count': len(products),
            'sample': products[:3] if products else []
        })
    
    # 使用 Selenium
    products = scrape_product_list_selenium()
    
    return jsonify({
        'success': True,
        'source': 'selenium',
        'count': len(products),
        'sample': products[:3] if products else []
    })

@app.route('/api/test-detail')
def test_detail():
    """測試爬取單一商品詳情"""
    test_url = request.args.get('url', '')
    
    if not test_url:
        return jsonify({'error': '請提供 url 參數'}), 400
    
    product = scrape_product_detail_selenium(test_url)
    
    if product:
        return jsonify({
            'success': True,
            'product': product
        })
    else:
        return jsonify({
            'success': False,
            'error': '爬取失敗'
        }), 400

@app.route('/api/test-upload')
def test_upload():
    """測試上傳一個商品到 Shopify"""
    if not load_shopify_token():
        return jsonify({'error': '請先完成 Shopify OAuth 授權'}), 400
    
    # 先爬取商品
    products = scrape_shopify_products()
    
    if not products:
        return jsonify({'error': '無法取得商品列表'}), 400
    
    # 找一個價格 >= 1000 的商品
    test_product = None
    for p in products:
        if p.get('price', 0) >= MIN_PRICE:
            test_product = p
            break
    
    if not test_product:
        return jsonify({'error': '找不到符合價格條件的商品'}), 400
    
    # 取得或建立 Collection
    collection_id = get_or_create_collection("虎屋羊羹")
    
    # 上傳到 Shopify
    result = upload_to_shopify(test_product, collection_id)
    
    if result['success']:
        shopify_product = result['product']
        admin_url = f"https://admin.shopify.com/store/{SHOPIFY_SHOP}/products/{shopify_product['id']}"
        
        return jsonify({
            'success': True,
            'message': '上架成功！',
            'product': {
                'sku': test_product['sku'],
                'original_title': test_product['title'],
                'translated_title': result.get('translated', {}).get('title', ''),
                'cost': result.get('cost', test_product['price']),
                'selling_price': result.get('selling_price', 0),
                'weight': test_product.get('weight', 0),
                'shopify_id': shopify_product['id'],
                'shopify_url': admin_url,
                'images_count': len(test_product.get('images', []))
            }
        })
    else:
        return jsonify({
            'success': False,
            'error': result['error'],
            'product': test_product
        }), 400

@app.route('/api/test-translate')
def test_translate():
    """測試翻譯功能"""
    test_title = "小形羊羹 10本入"
    test_desc = "とらやを代表する小形羊羹の詰合せです。夜の梅、おもかげ、新緑、はちみつ、和紅茶の5種類をお楽しみいただけます。"
    
    translated = translate_with_chatgpt(test_title, test_desc)
    
    return jsonify({
        'original': {
            'title': test_title,
            'description': test_desc
        },
        'translated': translated
    })

if __name__ == '__main__':
    print("=" * 50)
    print("虎屋羊羹爬蟲工具")
    print("=" * 50)
    
    port = int(os.environ.get('PORT', 8080))
    print(f"開啟瀏覽器訪問: http://localhost:{port}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=False)

"""
診斷 Shopify 重複商品問題
分析重複商品的 SKU、Handle、標題關係
"""

import requests
import json
import os
from collections import defaultdict

# Shopify 設定 - 請填入你的資訊
SHOPIFY_SHOP = os.environ.get('SHOPIFY_SHOP', '')
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN', '')

def get_shopify_headers():
    return {
        'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN,
        'Content-Type': 'application/json',
    }

def shopify_api_url(endpoint):
    return f"https://{SHOPIFY_SHOP}.myshopify.com/admin/api/2024-01/{endpoint}"

def get_all_products():
    """取得所有商品"""
    products = []
    url = shopify_api_url("products.json?limit=250")
    
    while url:
        response = requests.get(url, headers=get_shopify_headers())
        if response.status_code != 200:
            print(f"Error: {response.status_code}")
            break
        
        data = response.json()
        products.extend(data.get('products', []))
        
        # 處理分頁
        link_header = response.headers.get('Link', '')
        if 'rel="next"' in link_header:
            import re
            match = re.search(r'<([^>]+)>; rel="next"', link_header)
            url = match.group(1) if match else None
        else:
            url = None
    
    return products

def analyze_duplicates(products):
    """分析重複商品"""
    
    # 按標題分組
    by_title = defaultdict(list)
    # 按 SKU 分組
    by_sku = defaultdict(list)
    # 按 Handle 基礎名稱分組（去除 -1, -2 後綴）
    by_base_handle = defaultdict(list)
    
    for p in products:
        title = p.get('title', '')
        handle = p.get('handle', '')
        vendor = p.get('vendor', '')
        status = p.get('status', '')
        product_id = p.get('id')
        created_at = p.get('created_at', '')
        
        # 取得 SKU 和 metafield link
        sku = ''
        cost = ''
        for v in p.get('variants', []):
            sku = v.get('sku', '')
            cost = v.get('cost', '')
            break
        
        # 取得原始連結 (metafield)
        original_link = ''
        for m in p.get('metafields', []):
            if m.get('key') == 'link':
                original_link = m.get('value', '')
                break
        
        product_info = {
            'id': product_id,
            'title': title,
            'handle': handle,
            'sku': sku,
            'vendor': vendor,
            'status': status,
            'created_at': created_at,
            'cost': cost,
            'original_link': original_link
        }
        
        by_title[title].append(product_info)
        if sku:
            by_sku[sku].append(product_info)
        
        # 計算 base handle（去除數字後綴）
        import re
        base_handle = re.sub(r'-\d+$', '', handle)
        by_base_handle[base_handle].append(product_info)
    
    return by_title, by_sku, by_base_handle

def print_report(by_title, by_sku, by_base_handle):
    """印出分析報告"""
    
    print("=" * 80)
    print("📊 重複商品診斷報告")
    print("=" * 80)
    
    # 1. 標題重複分析
    print("\n\n📌 【1】按標題分組 - 相同標題的商品")
    print("-" * 60)
    
    title_duplicates = {k: v for k, v in by_title.items() if len(v) > 1}
    
    if title_duplicates:
        for title, items in sorted(title_duplicates.items(), key=lambda x: -len(x[1])):
            print(f"\n🔴 標題: {title}")
            print(f"   數量: {len(items)} 個重複")
            for item in items:
                print(f"   ├─ Handle: {item['handle']}")
                print(f"   │  SKU: {item['sku']}")
                print(f"   │  ID: {item['id']}")
                print(f"   │  建立時間: {item['created_at']}")
                print(f"   │  狀態: {item['status']}")
    else:
        print("✅ 沒有標題重複的商品")
    
    # 2. SKU 重複分析
    print("\n\n📌 【2】按 SKU 分組 - 相同 SKU 的商品")
    print("-" * 60)
    
    sku_duplicates = {k: v for k, v in by_sku.items() if len(v) > 1 and k}
    
    if sku_duplicates:
        for sku, items in sorted(sku_duplicates.items()):
            print(f"\n🔴 SKU: {sku}")
            print(f"   數量: {len(items)} 個重複")
            for item in items:
                print(f"   ├─ 標題: {item['title']}")
                print(f"   │  Handle: {item['handle']}")
                print(f"   │  ID: {item['id']}")
    else:
        print("✅ 沒有 SKU 重複的商品")
    
    # 3. 標題重複但 SKU 不同的情況（這是問題所在）
    print("\n\n📌 【3】標題相同但 SKU 不同 - 可能是官網多個 brandcode 對應同一商品")
    print("-" * 60)
    
    for title, items in sorted(title_duplicates.items(), key=lambda x: -len(x[1])):
        skus = set(item['sku'] for item in items)
        if len(skus) > 1:
            print(f"\n⚠️  標題: {title}")
            print(f"   不同的 SKU: {skus}")
            for item in items:
                print(f"   ├─ SKU: {item['sku']} | Handle: {item['handle']}")
    
    # 4. 總結
    print("\n\n📌 【4】總結")
    print("-" * 60)
    print(f"總商品數: {sum(len(v) for v in by_title.values())}")
    print(f"標題重複的群組數: {len(title_duplicates)}")
    print(f"涉及的重複商品數: {sum(len(v) for v in title_duplicates.values())}")
    
    # 5. 建議刪除的商品 ID（保留最早建立的）
    print("\n\n📌 【5】建議刪除的商品（保留最早建立的）")
    print("-" * 60)
    
    to_delete = []
    for title, items in title_duplicates.items():
        # 按建立時間排序，保留最早的
        sorted_items = sorted(items, key=lambda x: x['created_at'])
        for item in sorted_items[1:]:  # 跳過第一個（最早的）
            to_delete.append(item)
            print(f"刪除: ID={item['id']} | {item['title']} | Handle={item['handle']}")
    
    print(f"\n共建議刪除 {len(to_delete)} 個商品")
    
    # 輸出可直接使用的 ID 列表
    if to_delete:
        print("\n\n📌 【6】刪除用的 Product ID 列表（複製使用）")
        print("-" * 60)
        ids = [str(item['id']) for item in to_delete]
        print(",".join(ids))
    
    return to_delete

def main():
    if not SHOPIFY_SHOP or not SHOPIFY_ACCESS_TOKEN:
        print("❌ 請設定環境變數 SHOPIFY_SHOP 和 SHOPIFY_ACCESS_TOKEN")
        print("範例：")
        print("  export SHOPIFY_SHOP='your-shop-name'")
        print("  export SHOPIFY_ACCESS_TOKEN='shpat_xxxxx'")
        return
    
    print(f"正在連接 Shopify: {SHOPIFY_SHOP}...")
    
    # 取得所有商品（包含 metafields）
    products = get_all_products()
    print(f"取得 {len(products)} 個商品")
    
    # 分析重複
    by_title, by_sku, by_base_handle = analyze_duplicates(products)
    
    # 印出報告
    to_delete = print_report(by_title, by_sku, by_base_handle)

if __name__ == '__main__':
    main()

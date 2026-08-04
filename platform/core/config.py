"""全域設定。所有環境變數集中在這裡讀取，方便盤點。"""
import os

# ---- Shopify ----
SHOPIFY_SHOP = (os.environ.get("SHOPIFY_SHOP", "")
                .replace("https://", "").replace("http://", "")
                .replace(".myshopify.com", "").strip("/"))
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")

# 注意：這個版本號是「load-bearing」的，不要隨手改。
# REST products.json 帶 variants 建立商品的行為在較新版本已變更，
# 升版必須同時把建立商品改走 GraphQL productSet。詳見 README。
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-01")

# ---- 排程 ----
# 關閉後只能手動觸發。本機測試時建議關閉，避免不小心跑到正式資料。
SCHEDULER_ENABLED = os.environ.get(
    "SCHEDULER_ENABLED", "1").lower() in ("1", "true", "yes")

# ---- 存取控制 ----
# 設了就必須帶 token 才能觸發同步。舊的 12 支服務沒有這層保護，
# 但它們的清理階段本來就壞著（抓不到 collection SKU），誤觸也刪不掉東西。
# 新版刪除是真的會執行的，公開網址等於任何人都能刪光一個 collection。
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")

# ---- OpenAI ----
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TRANSLATE_MODEL = os.environ.get("TRANSLATE_MODEL", "gpt-4o-mini")

# ---- 同步行為 ----
# 連續幾件爬取失敗就中止（網站掛了或被封鎖時及早停手，避免把整個 collection
# 判定成已下架）。單件失敗只跳過，不影響整輪。
MAX_CONSECUTIVE_ERRORS = int(os.environ.get("MAX_CONSECUTIVE_ERRORS", "8"))
MAX_CONSECUTIVE_TRANSLATION_FAILURES = int(
    os.environ.get("MAX_CONSECUTIVE_TRANSLATION_FAILURES", "3"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

# 刪除保險絲。刪除量超過門檻就中止清理階段，避免判定邏輯出錯時一次砍掉大量商品。
# 誤刪是這套系統歷史上最貴的一次故障（庫存誤判導致既有商品被清空），所以預設保守。
MAX_DELETE_ABS = int(os.environ.get("MAX_DELETE_ABS", "25"))
MAX_DELETE_RATIO = float(os.environ.get("MAX_DELETE_RATIO", "0.2"))
ALLOW_BULK_DELETE = os.environ.get("ALLOW_BULK_DELETE", "").lower() in ("1", "true", "yes")

# 改價保險絲。改價直接影響營收，與刪除同級保護。
PRICE_ALERT_RATIO = float(os.environ.get("PRICE_ALERT_RATIO", "0.1"))
MAX_PRICE_CHANGES = int(os.environ.get("MAX_PRICE_CHANGES", "15"))
ALLOW_BULK_REPRICE = os.environ.get("ALLOW_BULK_REPRICE", "").lower() in ("1", "true", "yes")

# collection 商品數與 SKU 數對不上時，刪除判定的依據就不完整，預設停用清理階段。
# 官網列表相對 collection 的最低覆蓋率。低於此值就停用清理階段：
# 列表抓取不完整時，「官網已下架」的判定會把還在賣的商品全部誤判。
MIN_LIST_COVERAGE = float(os.environ.get("MIN_LIST_COVERAGE", "0.5"))
# 官網列表相對 collection 的最低覆蓋率。低於此值就停用清理階段：
# 列表抓取不完整時，「官網已下架」的判定會把還在賣的商品全部誤判。
MIN_LIST_COVERAGE = float(os.environ.get("MIN_LIST_COVERAGE", "0.5"))
MAX_DEDUP_DELETES = int(os.environ.get("MAX_DEDUP_DELETES", "20"))
ALLOW_INCOMPLETE_CLEANUP = os.environ.get(
    "ALLOW_INCOMPLETE_CLEANUP", "").lower() in ("1", "true", "yes")

# ---- 定價（GOYOUTATI 服務費 v2）----
# 逐件套用，非合併訂單總額；每件最低 ¥300
FEE_TIERS = [
    (5000, 1.25),
    (10000, 1.22),
    (20000, 1.20),
    (30000, 1.18),
    (None, 1.15),
]
MIN_FEE_PER_ITEM = 300

# ---- 翻譯輸出格式（不可放進 f-string，會被當成替換欄位）----
JSON_FORMAT = ('{"title":"翻譯後的商品名稱","description":"翻譯後的商品說明（HTML格式）",'
               '"page_title":"SEO標題50字以內","meta_description":"SEO描述100字以內"}')

# 部署版號。每次改完 core / platforms 就改這裡，同步 log 的第一行會印出來。
# 「檔案換了但跑的還是舊的」這種問題，靠猜是查不出來的，必須有東西可以對。
PLATFORM_VERSION = "2026-08-04a variant"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8,zh-TW;q=0.7",
}

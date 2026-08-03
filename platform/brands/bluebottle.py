from core.shipping import SHIPPING_HTML
from core.brand import Route
from platforms.shopify_source import ShopifySourceBrand

GLOSSARY = {
    # 選項名稱。長鍵優先，所以整句放在單詞前面 ——
    # 「お好みのブレンドコーヒー」若被拆成 ブレンド + コーヒー 會變成「綜合咖啡咖啡」。
    "お好みのインスタントコーヒー": "即溶咖啡口味",
    "選べるインスタントコーヒー": "即溶咖啡口味",
    "お好みの定番ブレンド豆": "經典綜合咖啡豆",
    "お好みのブレンドコーヒー": "綜合咖啡選擇",
    "選べるブレンドコーヒー": "綜合咖啡選擇",
    "コーヒー豆の組み合わせ": "咖啡豆組合",
    "ブレンドセレクト": "綜合咖啡選擇",
    "ブレンドコーヒー": "綜合咖啡",
    "豆の種類": "咖啡豆種類",
    "ボトルカラー": "瓶身顏色",
    "フレーバー": "口味",
    "サイズ": "規格",
    "Color": "顏色",
    # 選項值。這些直接顯示在商品頁的下拉選單上，漏一個客人就看到日文。
    "ルワンダ・ニャマシェケ・ニャブメラ・ナチュラル": "Rwanda Nyabumera 日曬",
    "ナイトライト・ディカフェ": "Night Light 低咖啡因",
    "みかん＆ドライフルーツ": "蜜柑＆綜合果乾",
    "テリーヌショコラ": "巧克力凍糕",
    "抹茶テリーヌ": "抹茶凍糕",
    "オリジナル": "原味",
    "シルバー": "銀色",
    "グレー": "灰色",
    "ブルー": "藍色",
    # 咖啡品項
    "シングルオリジン": "單品咖啡",
    "インスタントコーヒー": "即溶咖啡",
    "ドリップバッグ": "濾掛咖啡",
    "エスプレッソ": "濃縮咖啡",
    "カフェインレス": "低咖啡因",
    "コーヒー豆": "咖啡豆",
    "ブレンド": "綜合咖啡",
    "デカフェ": "低咖啡因",
    "コーヒー": "咖啡",
    "深煎り": "深焙",
    "中煎り": "中焙",
    "浅煎り": "淺焙",
    "焙煎": "烘焙",
    "豆のまま": "原豆",
    "挽き": "研磨",
    # 固定商品線名稱。模型每次翻得不一樣（Bella Donovan 會變「貝拉多諾萬」
    # 又變「美麗的多諾萬」），一律確定性替換。
    "ヘイズ・バレー・エスプレッソ": "Hayes Valley Espresso",
    "ジャイアント・ステップス": "Giant Steps",
    "スリー・アフリカズ": "Three Africas",
    "ベラ・ドノヴァン": "Bella Donovan",
    "ヒューマンメイド": "Human Made",
    "ブライト": "Bright",
    "ノラ": "Nola",
    # 器具與雜貨
    "ステンレスボトル": "保溫瓶",
    "キャニスター": "保存罐",
    "トートバッグ": "托特包",
    "タンブラー": "隨行杯",
    "ドリッパー": "濾杯",
    "フィルター": "濾紙",
    "グラノーラ": "穀麥",
    "サーバー": "下壺",
    "ケトル": "手沖壺",
    "マグ": "馬克杯",
    # 規格與選項用語。這些會出現在 option 名稱與選項值上，
    # 且必須與 options[].values 逐字一致，所以只能靠詞彙表，不能送翻譯。
    "詰め合わせ": "綜合組合",
    "セット": "組合",
    # 限定標示
    "オンライン限定": "線上限定",
    "数量限定": "數量限定",
    "期間限定": "期間限定",
    "季節限定": "季節限定",
}


class BlueBottle(ShopifySourceBrand):
    """
    Blue Bottle Coffee Japan。原本是 fashion-scraper 底下一支獨立的 Node.js
    服務，收進來的理由：來源是標準 Shopify products.json，與 toraya、yokumoku
    同型，沒有任何需要自成一支服務的地方，而獨立那份缺了刪除保險絲與改價保險絲。

    來源整份目錄的 grams 都是 0（沒有重量資料），所以重量一律 0。二段式收費下
    運費本來就到倉秤重後另計，不影響定價，只是商品頁無法預估運費。
    """
    slug = "bluebottle"
    name = "Blue Bottle Coffee"
    base_url = "https://store.bluebottlecoffee.jp"
    collection = "Blue bottle 藍瓶咖啡"
    vendor = "Blue Bottle Coffee"
    product_type = "咖啡・咖啡器具"
    tags = "Blue Bottle Coffee, 藍瓶咖啡, 日本, 咖啡, 咖啡豆, 手沖, 日本代購"
    schedule = "07:00"                  # JST，等於台北 06:00

    # 27% 的商品有多規格（100g/200g、口味選擇），塌成單一規格等於把選項
    # 從商品頁上拿掉，客人只能買到其中一種。
    multi_variant = True
    # 定期便是訂閱制，代購不了。
    skip_handle_prefixes = ("su",)
    # 酒類不是「要不要賣」的問題：寄台灣涉及菸酒稅與私人進口限制。
    exclude_types = ("お酒",)

    max_pages = 3                       # 目錄約 183 件，一頁 250 件就吃得下
    glossary = GLOSSARY
    shipping_html = SHIPPING_HTML

    routes = [
        Route(
            name="Human Made 聯名",
            match=r"ヒューマンメイド|HUMAN ?MADE|Human Made",
            collection="Blue bottle 藍瓶咖啡",
            tags=("Blue Bottle Coffee, 藍瓶咖啡, Human Made, 聯名商品, "
                  "日本, 咖啡, 日本代購"),
            prompt_rules=[
                "此商品為 Blue Bottle Coffee 與 Human Made 的聯名商品",
                "標題須在商品名中保留「× Human Made」，兩個品牌名都不可翻成中文",
            ],
        ),
    ]

    prompt_rules = [
        "品牌背景：美國奧克蘭創立的精品咖啡品牌 Blue Bottle Coffee，日本分店的線上商店",
        "標題開頭必須是「Blue bottle 藍瓶咖啡」，後接繁體中文商品名，不得省略",
        "品牌名一律保留為 Blue Bottle Coffee 或藍瓶咖啡，不可音譯成其他寫法",
        "咖啡豆的產地名、莊園名與處理法（水洗、日曬、蜜處理）要正確翻譯，不可音譯",
        "風味描述（果香、堅果、可可尾韻）如實翻譯，不可自行補上來源沒有的風味",
        "不得在說明中寫出賞味期限的具體日期，食品到貨日期會因批次而異",
        "SEO 關鍵字須包含：Blue Bottle、藍瓶咖啡、日本、咖啡豆、手沖、代購",
    ]

    def _variant_sku(self, raw, variant):
        """
        沿用舊 Node 版的 SKU 格式 BBC-{品號或 handle}-{規格 id}。

        格式相同就不需要跑 SKU 遷移：店上既有的藍瓶商品會直接被認出來，
        評論、SEO 排名與既有連結都保得住，也不會整批重新上架。
        """
        base = (variant.get("sku") or "").strip() or raw.get("handle") or ""
        return f"BBC-{base}-{variant.get('id')}"

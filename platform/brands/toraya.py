from core.shipping import SHIPPING_HTML
from core.brand import Route
from platforms.shopify_source import ShopifySourceBrand

GLOSSARY = {
    "小形羊羹": "小形羊羹",
    "夜の梅": "夜之梅",
    "おもかげ": "面影",
    "新緑": "新綠",
    "はちみつ": "蜂蜜",
    "紅茶": "紅茶",
    "水羊羹": "水羊羹",
    "羊羹": "羊羹",
    "最中": "最中餅",
    "干菓子": "乾菓子",
    "生菓子": "生菓子",
    "季節限定": "季節限定",
    "詰合せ": "綜合禮盒",
    "詰め合わせ": "綜合禮盒",
    "詰合わせ": "綜合禮盒",
    "本入り": "本入",
    "本入": "本入",
    "個入り": "個入",
    "個入": "個入",
    "竹皮包": "竹皮包裝",
    "化粧箱": "精裝禮盒",
    "敬老の日": "敬老之日",
    "シール付": "附貼紙",
    "の日": "之日",
    "の梅": "之梅",
    "あんペースト": "紅豆抹醬",
    "こしあん": "細餡紅豆",
    "つぶあん": "顆粒紅豆",
    "あんこ": "紅豆餡",
    "とメープルシロップ": "與楓糖漿",
    "メープルシロップ": "楓糖漿",
    "シロップ": "糖漿",
    "あんやき": "紅豆燒",
    "パッケージ": "包裝",
    "ゆるるか": "Yururuka",
    "ミラベル之めぐみ": "Mirabelle 之恩惠",
    "ミラベル": "Mirabelle 黃李",
    "めぐみ": "恩惠",
    "いちじく": "無花果",
    "あんず": "杏桃",
    "れもん": "檸檬",
    "もも": "水蜜桃",
    "ぶどう": "葡萄",
    "オリーブレモン": "橄欖檸檬",
    "オリーブ": "橄欖",
    "レモン": "檸檬",
    "お汁粉": "紅豆湯",
    "汁粉": "紅豆湯",
    "ごよみ": "曆",
    "しらべ": "調",
    "かおり": "香",
    "ささ栗": "小栗",
    "こばこ": "小盒",
    "ほとり": "畔",
    "みち": "道",
    "が袖": "之袖",
    "ヶ盛": "之盛",
    "け初め": "初綻",
    "ぼり": "",
    "なすび餅": "茄子餅",
    "ノワール": "Noir",
    "調べ": "調",
    "こし餡": "細餡",
    "ティーバッグ": "茶包",
    "かぶせ茶": "冠茶",
    "つつじ": "杜鵑",
    "ばら": "薔薇",
    "おはぎ": "萩餅",
    "しるし": "印記",
    "こ道": "小徑",
    "餡": "餡",
    "の": "之",
}


NOTICE_CHOJI = ('<div style="margin:0 0 18px;padding:14px 16px;border:1px solid #d8c9a8;background:#fbf7ee;border-radius:8px;font-size:13px;line-height:1.8;color:#5a4a2f;"><strong style="display:block;margin-bottom:6px;font-size:14px;">關於此商品的用途</strong>本商品為日本「弔事用」商品，是日本在喪禮、法事、追思場合致贈的答禮或供品，包裝與配色（白茶等）皆依此用途設計。<br>若您要購買的是一般送禮或伴手禮，建議選擇本店其他系列商品。</div>')


class Toraya(ShopifySourceBrand):
    slug = "toraya"
    name = "虎屋"
    base_url = "https://www.toraya-group.co.jp"
    # Hydrogen 前台沒有 products.json，要打後端 myshopify 網域
    json_base = "https://toraya-group.myshopify.com"
    product_path = "/onlineshop/products/"
    collection = "虎屋羊羹"
    vendor = "とらや"
    product_type = "和菓子・羊羹"
    tags = "虎屋, とらや, 日本, 京都, 東京, 羊羹, 和菓子, 伴手禮"
    schedule = None
    # 虎屋只有帶「販売.EC販売可」標籤的商品能網購。
    # 其餘 299 件是店頭現賣的生菓子（柏餅、桜餅、紫陽花等），
    # 賞味期限一兩天，無法空運到台灣，也無從代購。
    require_tags = ("販売.EC販売可",)
    def legacy_sku_keys(self, product):
        """舊 scraper 的 SKU 是 f"toraya-{handle}"，新的是虎屋自家品號。
        靠 handle 把兩者接起來，避免整批換血洗掉評論與 SEO。"""
        handle = (product.raw or {}).get("handle")
        return [f"toraya-{handle}"] if handle else []

    routes = [
        Route(
            name="弔事用",
            match=r"弔事用|白茶",
            collection="虎屋 弔事供品",
            tags="虎屋, とらや, 日本, 羊羹, 和菓子, 弔事用, 喪禮答禮, 法事, 追思",
            prompt_rules=[
                "此商品為日本弔事用（喪禮、法事、追思場合的答禮或供品）",
                "標題必須在「虎屋」之後、商品名之前加上「【弔事用】」標記",
                "商品說明開頭須說明此為日本喪禮場合致贈用途，不可描述為一般伴手禮",
                "不得使用「送禮首選」「伴手禮推薦」這類一般送禮用語",
            ],
            notice_html=NOTICE_CHOJI,
        ),
    ]
    shipping_html = SHIPPING_HTML
    glossary = GLOSSARY
    prompt_rules = [
        "品牌背景：日本室町時代創業的和菓子老舖「とらや」，以羊羹聞名",
        "標題開頭必須是「虎屋」，後接繁體中文商品名，不得省略",
        "「夜の梅」「おもかげ」「新緑」是羊羹的商品名，保留其意境不要直譯成花草",
        "SEO 關鍵字須包含：虎屋、とらや、日本、羊羹、和菓子、伴手禮",
    ]

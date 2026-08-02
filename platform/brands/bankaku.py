import re

from core.shipping import SHIPPING_HTML
from platforms.ecbeing import EcbeingClassic

GLOSSARY = {
    "ゆかり": "Yukari 蝦餅",
    "えびせんべい": "蝦餅",
    "えびせん": "蝦餅",
    "えび": "蝦",
    "せんべい": "仙貝",
    "煎餅": "仙貝",
    "あられ": "米菓",
    "おかき": "米菓",
    "詰合せ": "綜合禮盒",
    "詰め合わせ": "綜合禮盒",
    "詰合わせ": "綜合禮盒",
    "化粧箱": "精裝禮盒",
    "枚入り": "枚入",
    "枚入": "枚入",
    "個入り": "個入",
    "袋入り": "袋入",
    "缶入り": "罐裝",
    "工場できたて便": "工廠現做直送",
    "できたて": "現做",
    "手提げ袋": "提袋",
    "小丸": "小丸",
    "海老": "蝦",
    "お中元短冊付き": "附中元贈禮籤",
    "お歳暮短冊付き": "附歲暮贈禮籤",
    "短冊付き": "附贈禮籤",
    "短冊": "贈禮籤",
    "お中元": "中元贈禮",
    "お歳暮": "歲暮贈禮",
    "徳用": "實惠裝",
    "の": "之",
}


class Bankaku(EcbeingClassic):
    slug = "bankaku"
    name = "坂角總本舖"
    base_url = "https://www.bankaku.co.jp"
    category_path = "/shop/c/c10/"
    collection = "坂角總本舖"
    vendor = "坂角総本舗"
    product_type = "えびせんべい・詰め合わせ"
    tags = "坂角總本舖, 坂角総本舗, 日本, 愛知, 名古屋, 蝦餅, 伴手禮, 日本零食"
    schedule = None
    shipping_html = SHIPPING_HTML
    glossary = GLOSSARY
    prompt_rules = [
        "品牌背景：日本愛知縣創業於明治時代的蝦餅老舖「坂角総本舗」，代表商品是ゆかり",
        "標題開頭必須是「坂角總本舖」，後接繁體中文商品名，不得省略",
        "SEO 關鍵字須包含：坂角總本舖、日本、名古屋、蝦餅、伴手禮",
    ]

    def extract_weights(self, soup):
        text = self.scope(soup).get_text()
        actual = volume = 0.0
        m = re.search(r"(?:重量|内容量)[^\d]{0,6}(\d+(?:\.\d+)?)\s*(kg|g)", text)
        if m:
            actual = float(m.group(1)) / (1 if m.group(2) == "kg" else 1000)
        m = re.search(r"(?:寸法|サイズ|箱)[^\d]{0,10}(\d+(?:\.\d+)?)\s*[×xX]\s*"
                      r"(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*(cm|mm)", text)
        if m:
            w, d, hgt = (float(m.group(i)) for i in (1, 2, 3))
            volume = (w * d * hgt) / (6000 if m.group(4) == "cm" else 6_000_000)
        return actual, volume

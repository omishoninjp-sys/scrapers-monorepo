import re

from core.shipping import SHIPPING_HTML
from platforms.ecbeing import EcbeingClassic

GLOSSARY = {
    "グーテ・デ・ロワ": "Gouter de Roi 法國吐司脆餅",
    "グーテ": "Gouter",
    "ラスク": "脆餅",
    "化粧缶": "精裝鐵罐",
    "化粧箱": "精裝禮盒",
    "詰合せ": "綜合禮盒",
    "詰め合わせ": "綜合禮盒",
    "枚入り": "枚入",
    "枚入": "枚入",
    "個入り": "個入",
    "袋入り": "袋入",
    "ホワイトチョコレート": "白巧克力",
    "チョコレート": "巧克力",
    "ショコラ": "巧克力",
    "ミルク": "牛奶",
    "キャラメル": "焦糖",
    "メープル": "楓糖",
    "フロマージュ": "起司",
    "チーズ": "起司",
    "アーモンド": "杏仁",
    "プレミアム": "頂級",
    "ソレイユ": "Soleil",
    "レーヌ": "Reine",
    "ロワ・カカオ": "Roi 可可",
    "デ・ロワ": "de Roi",
    "ロワ": "Roi",
    "テイスティングボックス": "品飲禮盒",
    "テイスティング": "品飲",
    "ボックス": "禮盒",
    "マハラジャ": "Maharaja",
    "イタリアン": "義式",
    "カカオ": "可可",
    "レジェ": "Leger",
    "ソムリエ": "侍酒師精選",
    "セット": "組合",
    "トロピカルマンゴー": "熱帶芒果",
    "マンゴー": "芒果",
    "トロピカル": "熱帶",
    "お徳用": "實惠裝",
    "徳用": "實惠裝",
    "それお": "",
    "詰替え": "補充裝",
    "詰替": "補充裝",
    "グーテ・デ・レーヌ": "Gouter de Reine 巧克力脆餅",
    "デ・レーヌ": "de Reine",
    "グーテ・デ・ミエル": "Gouter de Miel",
    "グーテ・デ・": "Gouter de ",
    "割れ": "碎片",
    "ワレ": "碎片",
    "の": "之",
}


class GateauFestaHarada(EcbeingClassic):
    slug = "gateaufesta-harada"
    name = "Gateau Festa Harada"
    base_url = "https://shop.gateaufesta-harada.com"
    category_paths = (
        "/shop/c/croi/", "/shop/c/creine/", "/shop/c/ccacao/", "/shop/c/cleger/",
        "/shop/c/cwhite/", "/shop/c/cpremium/", "/shop/c/cex-pr/", "/shop/c/csoleil/",
        "/shop/c/cpr-ve/", "/shop/c/cpr-wz/", "/shop/c/crtb/", "/shop/c/crhw/",
        "/shop/c/csommelie/", "/shop/c/cmh/", "/shop/c/cgrt/", "/shop/c/cfromage/",
        "/shop/c/citalien/",
    )
    pages_per_category = 2
    max_pages = 40                 # 17 個分類 × 2 頁 + 餘裕
    empty_pages_before_stop = 6    # 空分類很常見，不能一遇到就收工
    collection = "Gateau Festa Harada"
    vendor = "ガトーフェスタ ハラダ"
    product_type = "ラスク・詰め合わせ"
    tags = "Gateau Festa Harada, ハラダ, 日本, 群馬, 法國吐司脆餅, 伴手禮, 日本零食"
    schedule = None
    shipping_html = SHIPPING_HTML
    glossary = GLOSSARY
    prompt_rules = [
        "品牌背景：日本群馬縣的西點品牌 Gateau Festa Harada，"
        "代表商品是法國吐司脆餅「グーテ・デ・ロワ」",
        "標題開頭必須是「Gateau Festa Harada」，後接繁體中文商品名，不得省略",
        "SEO 關鍵字須包含：Gateau Festa Harada、日本、群馬、法國吐司脆餅、伴手禮",
    ]

    def extract_description(self, soup):
        desc = super().extract_description(soup)
        if desc:
            return desc
        for sel in (".block-goods-description", ".block-goods-spec",
                    ".block-goods-detail--description", ".block-goods-freearea"):
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 20:
                return el.get_text(" ", strip=True)
        return ""

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

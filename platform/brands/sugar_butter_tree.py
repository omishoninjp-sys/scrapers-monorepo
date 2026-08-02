import re

from core.shipping import SHIPPING_HTML
from platforms.ecbeing import EcbeingSummary


class SugarButterTree(EcbeingSummary):
    slug = "sugar-butter-tree"
    name = "砂糖奶油樹"
    base_url = "https://www.paqtomog.com"
    category_path = "/shop/c/csbt/"
    collection = "砂糖奶油樹"
    vendor = "シュガーバターの木"
    product_type = "洋菓子・詰め合わせ"
    tags = "砂糖奶油樹, シュガーバターの木, 日本, 東京, 伴手禮, 日本零食"
    min_price = 0        # 全部同步，不再依價格門檻過濾
    schedule = "10:00"          # JST，原本 hontaka / sugar-butter-tree 各自有排程器
    shipping_html = SHIPPING_HTML
    japanese_ignore = ("シュガーバターの木",)
    prompt_rules = [
        "品牌背景：日本東京的人氣洋菓子品牌「シュガーバターの木」",
        "標題開頭必須是「砂糖奶油樹」，後接繁體中文商品名，不得省略",
        "詞彙對照：サンド→夾心餅；詰合せ→綜合禮盒；限定→限定版",
        "SEO 關鍵字須包含：砂糖奶油樹、日本、東京、伴手禮、餅乾",
    ]

    def extract_description(self, soup):
        desc = super().extract_description(soup)
        if desc:
            return desc
        for p in self.scope(soup).find_all("p"):
            text = p.get_text(strip=True)
            if len(text) > 50 and any(k in text for k in ("シュガーバター", "特製", "サンド")):
                return text
        return ""

    def extract_weights(self, soup):
        """砂糖奶油樹標示格式：箱サイズ・重さ ○×○×○ cm ○ g"""
        text = self.scope(soup).get_text()
        m = re.search(r"箱サイズ・重さ[^\d]*(\d+(?:\.\d+)?)[×xX](\d+(?:\.\d+)?)"
                      r"[×xX](\d+(?:\.\d+)?)\s*cm\s*(\d+(?:\.\d+)?)\s*g", text)
        if not m:
            return 0.0, 0.0
        w, d, h = (float(m.group(i)) for i in (1, 2, 3))
        actual = float(m.group(4)) / 1000
        volume = (w * d * h) / 6000
        return actual, volume

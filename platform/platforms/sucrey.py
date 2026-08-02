"""
シュクレイ（sucrey）平台。cocoris、francais、The maple mania 打同一個站
sucreyshopping.jp，靠 ?brand= 參數區分。

同站三品牌，所以列表與明細邏輯完全共用，各品牌只差 brand 參數與文案設定。
（舊 repo 是三支獨立服務、三份程式：cocoris 與 francais 相似度 75~84%，
maple-mania 只有 19% —— 同一個站被寫了兩套不同解法。）

平台特徵：
  - 商品頁 /shop/g/g{SKU}/，SKU 是英數混合（ec996524、101700013），統一轉小寫
  - 分類頁 /shop/c/c10/?brand=xxx，第 2 頁起 /shop/c/c10_p2/?brand=xxx
  - ecbeing Classic 佈景：.block-goods-name--text、.block-goods-price--price

庫存判定（2026-08 實測 75 件）：
  這個站**沒有**小倉那種「在庫：○△×」文字，也沒有任何缺貨關鍵字。
  有貨：.block-goods-cart-area 內同時有 .block-goods-stock（在庫○）
        與 .block-add-cart--btn（カートに入れる）
  無法線上購買：兩者都沒有，頁面只剩價格與「取扱店舗」
        —— 這是**實體店限定**商品，不是暫時缺貨，75 件中有 31 件屬於此類。
  因此判定採雙重訊號，缺任一即視為不可販售。
"""
import re
from urllib.parse import urljoin

from core.brand import BaseBrand
from core.models import Product, billable_weight

SKU_RE = re.compile(r"/shop/g/g([^/?#]+)")

STOCK_MARK_SELECTOR = ".block-goods-stock"
CART_BTN_SELECTOR = ".block-add-cart--btn"
CART_AREA_SELECTOR = ".block-goods-cart-area"

OUT_OF_STOCK_KEYWORDS = ["在庫がありません", "在庫切れ", "売り切れ", "品切れ", "完売",
                         "販売終了", "SOLD OUT", "sold out", "入荷日未定",
                         "ただ今お取扱いできない商品です"]


class SucreyBrand(BaseBrand):
    base_url = "https://sucreyshopping.jp"
    brand_param = ""              # cocoris / francais / themaplemania
    category_code = "c10"
    main_selector = "div.block-goods-detail"
    max_pages = 10
    image_exclude = ("haisou", "noimage", "common/")
    max_images = 10

    # ---- 列表 ----
    def list_page_url(self, page):
        code = self.category_code if page == 1 else f"{self.category_code}_p{page}"
        return f"{self.base_url}/shop/c/{code}/?brand={self.brand_param}"

    def parse_list(self, soup, page_url):
        items = []
        for a in soup.select('a[href*="/shop/g/g"]'):
            m = SKU_RE.search(a.get("href", ""))
            if not m:
                continue
            raw = m.group(1)
            items.append({"sku": raw.strip().lower(),
                          "url": f"{self.base_url}/shop/g/g{raw}/"})
        return items

    # ---- 明細 ----
    def scope(self, soup):
        return soup.select_one(self.main_selector) or soup

    def check_in_stock(self, soup):
        """
        雙重訊號：庫存標記 + 加入購物車按鈕，缺一即視為不可販售。
        單看按鈕不夠 —— 實體店限定商品同樣沒有按鈕，但它不是暫時缺貨。
        """
        if any(kw in self.scope(soup).get_text() for kw in OUT_OF_STOCK_KEYWORDS):
            return False
        area = soup.select_one(CART_AREA_SELECTOR) or soup
        has_stock_mark = bool(area.select_one(STOCK_MARK_SELECTOR))
        has_cart_btn = bool(area.select_one(CART_BTN_SELECTOR))
        if has_stock_mark:
            mark = area.select_one(STOCK_MARK_SELECTOR).get_text(strip=True)
            if "×" in mark:
                return False
        return has_stock_mark and has_cart_btn

    def is_store_only(self, soup):
        """實體店限定：沒有購物車區塊的庫存與按鈕，但頁面標示取扱店舗"""
        area = soup.select_one(CART_AREA_SELECTOR)
        if area and (area.select_one(CART_BTN_SELECTOR)
                     or area.select_one(STOCK_MARK_SELECTOR)):
            return False
        return "取扱店舗" in soup.get_text()

    def extract_title(self, soup):
        for sel in (".block-goods-name--text", "h1"):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        el = soup.select_one("title")
        return el.get_text(strip=True).split("|")[0].strip() if el else ""

    def extract_description(self, soup):
        parts = []
        for sel in (".block-goods-comment1", ".block-goods-comment2",
                    ".block-goods-comment3", ".block-goods-introduction-item--text"):
            for el in soup.select(sel):
                text = el.get_text(" ", strip=True)
                if len(text) > 20 and text not in parts:
                    parts.append(text)
            if parts:
                break
        return " ".join(parts)[:1500]

    def extract_price(self, soup):
        for sel in (".block-goods-price--price", ".price"):
            el = soup.select_one(sel)
            if el:
                m = re.search(r"([\d,]+)", el.get_text())
                if m:
                    return int(m.group(1).replace(",", ""))
        m = re.search(r"([\d,]+)\s*円", self.scope(soup).get_text())
        return int(m.group(1).replace(",", "")) if m else 0

    def extract_images(self, soup):
        gallery = soup.select_one(".block-goods-gallery") or self.scope(soup)
        urls, seen, out = [], set(), []
        for el in gallery.select('img[src*="/img/goods/"], a[href*="/img/goods/"]'):
            urls.append(el.get("href") or el.get("src") or "")
        for u in urls:
            if not u:
                continue
            full = urljoin(self.base_url, u)
            if full in seen or any(bad in full.lower() for bad in self.image_exclude):
                continue
            seen.add(full)
            out.append(full)
        return out[:self.max_images]

    def extract_weights(self, soup):
        """
        シュクレイ的規格區沒有統一的重量欄位，格式散落在說明文字裡。
        先以「○g」「○kg」抓實重，抓不到就回 0（二段式收費下，
        運費是到倉秤重後另計，這裡的重量只作為預估參考）。
        """
        text = self.scope(soup).get_text()
        actual = volume = 0.0
        m = re.search(r"(?:総重量|重量|内容量)[^\d]{0,6}(\d+(?:\.\d+)?)\s*(kg|g)", text)
        if m:
            actual = float(m.group(1)) / (1 if m.group(2) == "kg" else 1000)
        m = re.search(r"(?:サイズ|寸法|箱)[^\d]{0,10}(\d+(?:\.\d+)?)\s*[×xX]\s*"
                      r"(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*(cm|mm)", text)
        if m:
            w, d, h = (float(m.group(i)) for i in (1, 2, 3))
            divisor = 6000 if m.group(4) == "cm" else 6_000_000
            volume = (w * d * h) / divisor
        return actual, volume

    def parse_detail(self, soup, url, sku) -> Product:
        actual, volume = self.extract_weights(soup)
        return Product(
            sku=sku,
            url=url,
            title=self.extract_title(soup),
            price=self.extract_price(soup),
            in_stock=self.check_in_stock(soup),
            description=self.extract_description(soup),
            images=self.extract_images(soup),
            actual_weight=round(actual, 3),
            volume_weight=round(volume, 3),
            weight=billable_weight(actual, volume),
            raw={"store_only": self.is_store_only(soup)},
        )

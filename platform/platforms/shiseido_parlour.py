"""
資生堂 PARLOUR（parlour.shiseido.co.jp）。

自建站，不是任何電商平台，所以這個 adapter 只服務這一個品牌。

結構：
  - 分類頁 /food_products/onlineshop/category.html?cat_id=00X
    另有 recommend.html（推薦），兩者商品有重疊，去重後約 42 件
  - 商品頁 /food_products/onlineshop/detail.html?prod_id={10 位數字}
  - 主要區塊 .area-detail，欄位 .product-name / .product-price /
    .product-description，加入購物車 .btn-add-cart

注意 `.product-price` 在頁面上會出現多次（下方有推薦商品），
所以一律限定在 .area-detail 內取第一個。
"""
import re
from urllib.parse import urljoin

from core.brand import BaseBrand
from core.models import Product, billable_weight

PROD_RE = re.compile(r"prod_id=(\d+)")

OUT_OF_STOCK_KEYWORDS = ["売り切れ", "完売", "品切れ", "在庫切れ", "SOLD OUT",
                         "販売終了", "お取扱いを終了", "入荷未定"]


class ShiseidoParlour(BaseBrand):
    slug = "shiseido"
    name = "資生堂 PARLOUR"
    base_url = "https://parlour.shiseido.co.jp"
    detail_path = "/food_products/onlineshop/detail.html?prod_id="
    category_urls = (
        "/food_products/onlineshop/recommend.html",
        "/food_products/onlineshop/category.html?cat_id=002",
        "/food_products/onlineshop/category.html?cat_id=003",
        "/food_products/onlineshop/category.html?cat_id=004",
        "/food_products/onlineshop/category.html?cat_id=005",
        "/food_products/onlineshop/category.html?cat_id=007",
        "/food_products/onlineshop/category.html?cat_id=008",
    )
    # .area-detail 不含商品資訊（價格與購物車都在它外面）。
    # .area-product 才是主商品區塊：只有一個 .product-price，且含加入購物車按鈕。
    # 不能用 .section-onlineshop-detail —— 那裡有 7 個價格（含下方推薦商品）。
    main_selector = ".area-product"
    max_images = 10

    # ---- 列表 ----
    def list_page_url(self, page):
        paths = self.category_urls
        if page > len(paths):
            return None
        return urljoin(self.base_url, paths[page - 1])

    def parse_list(self, soup, page_url):
        items = []
        for a in soup.select('a[href*="prod_id="]'):
            m = PROD_RE.search(a.get("href", ""))
            if m:
                items.append({"sku": m.group(1),
                              "url": f"{self.base_url}{self.detail_path}{m.group(1)}"})
        return items

    # 分類彼此有重疊，空分類不代表走完了
    empty_pages_before_stop = 99
    max_pages = 12

    # ---- 明細 ----
    def scope(self, soup):
        return soup.select_one(self.main_selector) or soup

    def extract_title(self, soup):
        """
        主商品名在 <h2>。**不要用 .product-name** —— 那個 class 只出現在
        下方的推薦商品區塊，抓到的會是別的商品
        （prod_id=0000000291 是「3個入」，.product-name 第一個卻是「6個入」）。
        """
        el = soup.select_one("h2")
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
        el = soup.select_one("title")
        return el.get_text(strip=True).split("│")[0].strip() if el else ""

    def extract_price(self, soup):
        el = self.scope(soup).select_one(".product-price")
        if el:
            m = re.search(r"([\d,]+)", el.get_text())
            if m:
                return int(m.group(1).replace(",", ""))
        return 0

    def extract_description(self, soup):
        el = (self.scope(soup).select_one(".product-description")
              or soup.select_one(".inner-product-info .product-description"))
        return el.get_text(" ", strip=True)[:1500] if el else ""

    def check_in_stock(self, soup):
        main = self.scope(soup)
        if any(kw in main.get_text() for kw in OUT_OF_STOCK_KEYWORDS):
            return False
        if main.select_one(".btn-add-cart, .link-cart"):
            return True
        return self.extract_price(soup) > 0

    def extract_images(self, soup):
        gallery = (soup.select_one(".product-photo-gallery")
                   or soup.select_one(".product-photo") or self.scope(soup))
        urls, seen, out = [], set(), []
        for img in gallery.select("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or "/img/common/" in src:
                continue
            urls.append(src)
        for u in urls:
            full = urljoin(self.base_url, u)
            if full in seen:
                continue
            seen.add(full)
            out.append(full)
        return out[:self.max_images]

    def extract_weights(self, soup):
        text = self.scope(soup).get_text()
        actual = volume = 0.0
        m = re.search(r"(?:重量|内容量|総重量)[^\d]{0,6}(\d+(?:\.\d+)?)\s*(kg|g)", text)
        if m:
            actual = float(m.group(1)) / (1 if m.group(2) == "kg" else 1000)
        m = re.search(r"(?:寸法|サイズ|箱)[^\d]{0,10}(\d+(?:\.\d+)?)\s*[×xX]\s*"
                      r"(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)\s*(cm|mm)", text)
        if m:
            w, d, hgt = (float(m.group(i)) for i in (1, 2, 3))
            volume = (w * d * hgt) / (6000 if m.group(4) == "cm" else 6_000_000)
        return actual, volume

    def extract_product_code(self, soup):
        """
        商品コード（.product-code「商品コード／71111」）才是穩定識別，
        也是 Shopify 現有商品的 SKU。網址的 prod_id 只是頁面參數，
        和商品編號不是一對一（prod_id=0000000291 的商品コード是 71111）。
        """
        el = soup.select_one(".product-code")
        if el:
            m = re.search(r"([A-Za-z0-9_-]{3,})\s*$", el.get_text(strip=True))
            if m:
                return m.group(1)
        return ""

    def get_product(self, url, sku):
        soup, _ = self.fetch(url)
        if soup is None:
            return None
        code = self.extract_product_code(soup)
        return self.parse_detail(soup, url, code or sku)

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
        )

"""
MakeShop 平台（本高砂屋、神戶風月堂）。

平台特徵：
  - 頁面編碼是 **EUC-JP**，不是 UTF-8。requests 猜錯就整頁變亂碼。
  - 商品頁 /shopdetail/{12 位數字}/
  - 分類頁 /shopbrand/{ct編號}/，第 2 頁起 /shopbrand/{ct編號}/page2/
  - 主要區塊 #itemInfo、#itemImg，庫存 .M_stock-display

兩個坑（2026-08 實測）：

1. **`/shopbrand/all_items/` 不是全部商品**，只是首頁精選。
   kobe-fugetsudo 的 all_items 只有 4 件，逐分類走完是 105 件；
   hontaka all_items 48 件，逐分類是 86 件。
   分類清單從首頁自動抓（/shopbrand/ct\\d+/），不寫死。
   舊 scraper 用的 `shopbrand.html?page=` 入口現在只回 4 件，已失效。

2. **價格在 `<input class="m_price" value="5,400">` 的 value 屬性裡**，
   不是文字節點。頁面上看得到的「486円（税込）」是推薦商品區塊，
   直接抓文字會拿到別人的價格。
"""
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.brand import BaseBrand
from core.models import Product, billable_weight

SKU_RE = re.compile(r"/shopdetail/(\d+)")
CATEGORY_RE = re.compile(r"/shopbrand/(ct\d+)/")

OUT_OF_STOCK_KEYWORDS = ["売り切れ", "完売", "品切れ", "在庫切れ", "SOLD OUT",
                         "販売終了", "入荷未定", "取扱いを終了"]


class MakeShopBrand(BaseBrand):
    encoding = "euc-jp"
    pages_per_category = 5
    max_images = 10
    category_paths = ()        # 留空 = 從首頁自動抓分類

    # ---- 抓頁面（覆寫編碼處理）----
    def fetch(self, url):
        r = self.session.get(url, timeout=30)
        if r.status_code != 200:
            return None, r.status_code
        html = r.content.decode(self.encoding, "ignore")
        return BeautifulSoup(html, "html.parser"), r.status_code

    # ---- 列表 ----
    def discover_categories(self):
        if self.category_paths:
            return list(self.category_paths)
        soup, _ = self.fetch(self.base_url)
        if soup is None:
            return []
        found = set()
        for a in soup.select('a[href*="/shopbrand/"]'):
            m = CATEGORY_RE.search(a.get("href", ""))
            if m:
                found.add(m.group(1))
        return sorted(found)

    def list_page_url(self, page):
        return f"{self.base_url}/shopbrand/all_items/" if page == 1 else None

    def parse_list(self, soup, page_url):
        items = []
        for a in soup.select('a[href*="/shopdetail/"]'):
            m = SKU_RE.search(a.get("href", ""))
            if m:
                items.append({"sku": m.group(1),
                              "url": f"{self.base_url}/shopdetail/{m.group(1)}/"})
        return items

    def list_products(self):
        """逐分類走到底。all_items 只是首頁精選，不能當作全部商品。"""
        self.warm_up()
        found, seen = [], set()
        for cat in self.discover_categories():
            for page in range(1, self.pages_per_category + 1):
                url = (f"{self.base_url}/shopbrand/{cat}/" if page == 1
                       else f"{self.base_url}/shopbrand/{cat}/page{page}/")
                soup, _ = self.fetch(url)
                if soup is None:
                    break
                new = 0
                for item in self.parse_list(soup, url):
                    if item["sku"] in seen:
                        continue
                    seen.add(item["sku"])
                    found.append(item)
                    new += 1
                time.sleep(self.request_delay)
                if new == 0:
                    break
        return found

    # ---- 明細 ----
    def info_block(self, soup):
        return soup.select_one("#itemInfo") or soup

    def extract_title(self, soup):
        """
        優先用 <title>：MakeShop 的 #itemInfo 會把商品說明整段黏在商品名後面
        （kobe-fugetsudo「パピヨット 4FN −薄焼きせんべいを巻いた…」），
        當成標題送去翻譯會得到一整段文案。<title> 只有商品名。
        """
        el = soup.select_one("title")
        if el:
            raw = el.get_text(strip=True)
            # 「商品名 - 本高砂屋オンラインショップ」「商品名｜神戸風月堂 公式通販」
            name = re.split(r"[｜|]|\s-\s", raw)[0].strip()
            # 商品名後面接的「−說明」也切掉
            name = re.split(r"[−–—]", name)[0].strip()
            if name and name not in ("トップ", ""):
                return name
        el = soup.select_one("#itemInfo .name, #itemInfo h2, #itemInfo h1")
        if el and el.get_text(strip=True):
            return re.split(r"[−–—]", el.get_text(strip=True))[0].strip()
        return ""

    def extract_price(self, soup):
        """
        兩種佈景各有各的放法，都不是單純的文字節點：
          hontaka        <input class="m_price" value="5,400">   ← 在 value 屬性
          kobe-fugetsudo 「税込648円（本体価格600円）」          ← 「税込」在數字前

        頁面上其他看得到的「486円（税込）」是**推薦商品區塊**的價格，
        直接抓文字會拿到別人的價錢，所以一律限定在 #itemInfo 內、且用上面兩種樣式。
        """
        for sel in ('input.m_price', 'input[name="price2"]', "#M_price2"):
            el = soup.select_one(sel)
            if el:
                m = re.search(r"([\d,]+)", el.get("value") or "")
                if m:
                    return int(m.group(1).replace(",", ""))

        text = self.info_block(soup).get_text(" ", strip=True)
        m = re.search(r"税込\s*([\d,]+)\s*円", text)
        if m:
            return int(m.group(1).replace(",", ""))
        m = re.search(r"([\d,]+)\s*円\s*[（(]\s*税込", text)
        if m:
            return int(m.group(1).replace(",", ""))
        return 0

    def check_in_stock(self, soup):
        info = self.info_block(soup)
        text = info.get_text()
        if any(kw in text for kw in OUT_OF_STOCK_KEYWORDS):
            return False
        stock = soup.select_one(".M_stock-display, .M_item-stock-smallstock, "
                                ".M_quantity-stock")
        if stock:
            st = stock.get_text(strip=True)
            if any(kw in st for kw in OUT_OF_STOCK_KEYWORDS):
                return False
            m = re.search(r"残りあと\s*(\d+)", st)
            if m:
                return int(m.group(1)) > 0
        return self.extract_price(soup) > 0

    def extract_product_code(self, soup):
        """本高砂屋把商品代碼放在標題括號裡：「エコルセ　E50　〔33050〕」→ 33050"""
        title = soup.select_one("#itemInfo")
        text = title.get_text() if title else soup.get_text()
        m = re.search(r"〔(\d+)〕", text)
        return m.group(1) if m else ""

    def extract_description(self, soup):
        parts = []
        for sel in (".detailTxt", ".detailExtTxt"):
            for el in soup.select(sel):
                t = el.get_text(" ", strip=True)
                if len(t) > 20 and t not in parts:
                    parts.append(t)
        return " ".join(parts)[:1500]

    def extract_images(self, soup):
        block = soup.select_one("#itemImg") or soup
        urls, seen, out = [], set(), []
        for img in block.select("img"):
            src = img.get("src") or ""
            if "shopimages" not in src:
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
        text = self.info_block(soup).get_text()
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
            raw={"product_code": self.extract_product_code(soup)},
        )

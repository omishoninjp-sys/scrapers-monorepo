"""
ecbeing 平台。

共用的是**列表與 URL 結構**（實測 ogura vs sugar-butter-tree 相似度 90%）：
  - 商品頁 /shop/g/g{SKU}/
  - 分類頁 /shop/c/{code}/，第 2 頁起 /shop/c/{code}_p2/
  - 圖片路徑 /img/goods/

明細頁則分兩種佈景（實測相似度僅 37%，因此拆成兩個子類別）：
  - EcbeingClassic：舊佈景，.block-goods-name--text / 在庫：○△×（小倉山莊）
  - EcbeingSummary：新佈景，.block-goods-detail-summary--*（砂糖奶油樹 paqtomog.com）

庫存判定的坑（2026-07 修）：
  舊佈景的商品頁下方推薦商品縮圖 (ul.block-thumbnail-t) 會渲染
  <div class="block-no-stock">在庫がありません</div>。
  對整頁文字掃關鍵字的話，只要任何一個推薦商品缺貨，主商品就被誤判缺貨，
  進而在同步收尾階段被從 Shopify 刪掉。判定範圍必須限制在主商品區塊。
"""
import re
import time
from urllib.parse import urljoin

from core.brand import BaseBrand
from core.models import Product, billable_weight

# ecbeing 的商品代號不限數字：ogura 是純數字，但 bankaku 有 G225、
# gateaufesta-harada 全部是 R1/R2 這種字母開頭。限定 \d+ 會整批漏抓。
SKU_RE = re.compile(r"/shop/g/g([A-Za-z0-9._-]+)/?")

RELATED_BLOCK_PREFIXES = ("block-thumbnail", "block-recommend", "block-history",
                          "block-cart-in", "block-together", "block-ranking",
                          "block-goods-history")

# 各站的加入購物車措辭略有差異：ogura「買い物かごに入れる」、
# harada「お買い物かごへ入れる」、sucrey/harada 用 .block-add-cart--btn。
CART_SELECTORS = ('a[href*="cart.aspx?goods="]', '.block-cart-btn',
                  '.block-add-cart--btn', '.block-add-cart button')
CART_PHRASES = ("買い物かごに入れる", "お買い物かごへ入れる", "カートに入れる",
                "買い物かごへ入れる", "カートへ入れる")

OUT_OF_STOCK_KEYWORDS = ["在庫がありません", "在庫切れ", "入荷日未定", "売り切れ",
                         "品切れ", "完売", "販売終了", "SOLD OUT", "sold out",
                         "ただ今お取扱いできない商品です"]


def in_related_block(el):
    for parent in el.parents:
        for cls in (parent.get("class") or []):
            if cls.startswith(RELATED_BLOCK_PREFIXES):
                return True
    return False


class EcbeingBrand(BaseBrand):
    """共用列表與 URL 結構；明細頁交給子類別。"""
    category_path = ""            # 單一分類；多分類請改用 category_paths
    category_paths = ()           # 多個分類路徑，會依序走完再去重
    main_selector = ""
    image_selector = 'a[href*="/img/goods/"], img[src*="/img/goods/"]'
    image_exclude = ("haisou",)
    max_images = 10
    pages_per_category = 6        # 多分類時，每個分類最多走幾頁

    # ---- 列表 ----
    @property
    def _paths(self):
        return list(self.category_paths) or ([self.category_path]
                                             if self.category_path else [])

    def list_page_url(self, page):
        """單一分類時給 BaseBrand 的迴圈用；多分類走 list_products() 的覆寫。"""
        paths = self._paths
        if not paths:
            return None
        base = urljoin(self.base_url, paths[0])
        return base if page == 1 else base.rstrip("/") + f"_p{page}/"

    def list_products(self):
        """
        多分類來源：每個分類**各自**分頁走到底，不共用「連續空白」計數器。

        先前把分類 × 分頁攤平成一維序號共用一個計數器，harada 中段有 8 個
        連續空分類，一路累加就提早收工 —— 41 件只抓到 22 件，
        清理階段會把沒抓到的 19 件判定成「官網已下架」。
        （幸好覆蓋率保護擋下了，但根因在這裡。）
        """
        paths = self._paths
        if len(paths) <= 1:
            return super().list_products()

        self.warm_up()
        found, seen = [], set()
        for path in paths:
            base = urljoin(self.base_url, path)
            for sub in range(1, self.pages_per_category + 1):
                url = base if sub == 1 else base.rstrip("/") + f"_p{sub}/"
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
                if new == 0:          # 這個分類走完了，換下一個分類
                    break
        return found

    def parse_list(self, soup, page_url):
        items = []
        for a in soup.select('a[href*="/shop/g/g"]'):
            m = SKU_RE.search(a.get("href", ""))
            if m:
                # 正規化 URL，去掉 #review 之類的 fragment
                items.append({"sku": m.group(1),
                              "url": f"{self.base_url}/shop/g/g{m.group(1)}/"})
        return items

    # ---- 共用工具 ----
    def main_block(self, soup):
        return soup.select_one(self.main_selector) if self.main_selector else None

    def scope(self, soup):
        return self.main_block(soup) or soup

    def scoped_text_without_related(self, soup):
        main = self.scope(soup)
        text = main.get_text()
        for block in main.select("ul.block-thumbnail-t, .block-thumbnail-t, "
                                 ".block-recommend, .block-history, .block-goods-history"):
            text = text.replace(block.get_text(), "")
        return text

    def extract_images(self, soup):
        main = self.scope(soup)
        urls, seen, out = [], set(), []
        for el in main.select(self.image_selector):
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
        """回傳 (實重kg, 材積重kg)。標示格式各品牌不同，由品牌類別覆寫。"""
        return 0.0, 0.0

    def parse_detail(self, soup, url, sku) -> Product:
        return Product(
            sku=sku,
            url=url,
            title=self.extract_title(soup),
            price=self.extract_price(soup),
            in_stock=self.check_in_stock(soup),
            description=self.extract_description(soup),
            images=self.extract_images(soup),
            **self._weight_fields(soup),
        )

    def _weight_fields(self, soup):
        actual, volume = self.extract_weights(soup)
        return {"actual_weight": round(actual, 3), "volume_weight": round(volume, 3),
                "weight": billable_weight(actual, volume)}


class EcbeingClassic(EcbeingBrand):
    """舊佈景：小倉山莊"""
    main_selector = "div.block-goods-detail"

    def check_in_stock(self, soup):
        main = self.scope(soup)
        text = main.get_text()

        # 1) 主商品「在庫：○／△／×」最準
        m = re.search(r"在庫[：:]\s*([○△×])", text)
        if m:
            return m.group(1) != "×"

        # 2) 主區塊內的缺貨標記，排除推薦商品縮圖
        for el in main.select(".block-no-stock, .sold-out, .out-of-stock"):
            if not in_related_block(el):
                return False

        # 3) 關鍵字（已挖掉推薦商品文字）
        scoped = self.scoped_text_without_related(soup)
        if any(kw in scoped for kw in OUT_OF_STOCK_KEYWORDS):
            return False

        cart = main.select_one(", ".join(CART_SELECTORS))
        return bool(cart) or any(p in scoped for p in CART_PHRASES)

    def extract_title(self, soup):
        main = self.scope(soup)
        for sel in ("h2.block-goods-name--text", ".block-goods-name--text", "h1"):
            el = main.select_one(sel) or soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        el = soup.select_one("title")
        return el.get_text(strip=True).split("|")[0].split(":")[0].strip() if el else ""

    def extract_description(self, soup):
        main = self.scope(soup)
        for sel in (".block-goods-comment1", ".block-goods-comment",
                    ".block-goods-info p", "h2 + p"):
            el = main.select_one(sel)
            if el and len(el.get_text(strip=True)) > 20:
                return el.get_text(strip=True)
        return ""

    def extract_price(self, soup):
        main = self.scope(soup)
        el = main.select_one(".block-goods-price--price, .price")
        if el:
            m = re.search(r"([\d,]+)", el.get_text())
            if m:
                return int(m.group(1).replace(",", ""))
        for pattern in (r"商品価格[^\d]*([\d,]+)\s*円",
                        r"([\d,]+)\s*円\s*（税込）", r"[¥￥]([\d,]+)"):
            m = re.search(pattern, main.get_text())
            if m:
                return int(m.group(1).replace(",", ""))
        return 0


class EcbeingSummary(EcbeingBrand):
    """新佈景：砂糖奶油樹（paqtomog.com）"""
    main_selector = ".block-goods-detail-summary"
    cart_selector = ".block-goods-detail-summary--function-cart-btn"

    def check_in_stock(self, soup):
        scoped = self.scoped_text_without_related(soup)
        if any(kw in scoped for kw in OUT_OF_STOCK_KEYWORDS):
            return False
        # 這個佈景沒有「在庫：○」記號，改以加入購物車按鈕是否存在判定。
        # 注意：測試當下站上 18 件全部有貨，缺貨狀態的實際 HTML 尚未觀察到，
        # 首次遇到缺貨商品時要回來驗證這條規則。
        return bool(soup.select_one(self.cart_selector))

    def extract_title(self, soup):
        # h1 會把品牌名與商品名黏在一起，優先取 --header-title-name
        for sel in (".block-goods-detail-summary--header-title-name", "h1"):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        el = soup.select_one("title")
        return el.get_text(strip=True).split(":")[0].strip() if el else ""

    def extract_description(self, soup):
        for sel in (".block-goods-detail-summary--description-comment1",
                    ".block-goods-detail-summary--description-comment-text",
                    ".block-goods-detail-summary--description"):
            el = soup.select_one(sel)
            if el and len(el.get_text(strip=True)) > 20:
                return el.get_text(" ", strip=True)
        return ""

    def extract_price(self, soup):
        for sel in (".block-goods-detail-summary--price-value",
                    ".block-goods-detail-fixed--price-value"):
            el = soup.select_one(sel)
            if el:
                m = re.search(r"([\d,]+)", el.get_text())
                if m:
                    return int(m.group(1).replace(",", ""))
        return 0

    def extract_images(self, soup):
        gallery = soup.select_one(".block-goods-detail-gallery")
        urls, seen, out = [], set(), []
        for el in (gallery or soup).select('img[src*="/img/goods/"], a[href*="/img/goods/"]'):
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

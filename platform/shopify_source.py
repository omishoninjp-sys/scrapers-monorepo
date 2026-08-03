"""
以 Shopify 公開 products.json 為來源的 adapter（toraya、yokumoku）。

這是所有來源裡最穩固的一種：
  - 不需要解析 HTML，前台改版不會壞
  - `available` 直接給庫存狀態，不必猜測購物車按鈕或缺貨字樣
  - `grams` 給實重，不必從說明文字硬湊
  - yokumoku 舊版用 Playwright 開瀏覽器爬列表，這裡完全不需要

注意 toraya 是 Hydrogen（Shopify headless）：前台網域沒有 products.json，
要打後端的 *.myshopify.com。yokumoku 是一般 Shopify 前台，直接打自家網域即可。

分頁：products.json 用 ?page=N，回傳少於 limit 就是最後一頁。
"""
import math
import re
import time

from bs4 import BeautifulSoup

from core.brand import BaseBrand
from core.models import Product, Variant, billable_weight


class ShopifySourceBrand(BaseBrand):
    json_base = ""            # 提供 products.json 的網域（可能與 base_url 不同）
    product_path = "/products/"   # 前台商品網址前綴，用來組回給客人看的連結
    page_limit = 250
    max_pages = 8
    only_types = ()           # 只收這些 product_type（空 = 全收）
    # 必須命中其中一個標籤才「可販售」。不合格的商品**仍會留在列表裡**，
    # 只是標記為缺貨 → 轉草稿，等標籤回來自動復活。
    # 若改成直接跳過，這些商品會被清理階段判定「官網已下架」而刪除；
    # 但季節輪替商品（四季の富士 冬、照紅葉）下一季就會回來，
    # 刪了等於每季重新上架、重跑翻譯、評論歸零。
    require_tags = ()
    exclude_tags = ()         # 命中任一標籤就跳過（贈品、非賣品等）
    max_images = 10
    # 預設 False：一件商品一個規格，伴手禮既有品牌的行為完全不變。
    # 咖啡（100g/200g）、服飾（尺寸顏色）這類才打開。
    multi_variant = False
    exclude_types = ()        # 命中就整件跳過的 product_type（酒類等不可寄送品項）
    skip_handle_prefixes = () # handle 前綴命中就跳過（定期便等）

    # ---- 列表 ----
    def list_page_url(self, page):
        base = self.json_base or self.base_url
        return f"{base}/products.json?limit={self.page_limit}&page={page}"

    def _fetch_json(self, url):
        r = self.session.get(url, timeout=45)
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            # Hydrogen 前台會回 HTML，代表 json_base 設錯了
            return None

    def list_products(self):
        """一次把整份 JSON 撈回來，明細不需要再打第二次請求。"""
        self.warm_up()
        self._raw = {}
        items = []
        for page in range(1, self.max_pages + 1):
            data = self._fetch_json(self.list_page_url(page))
            if data is None:
                break
            products = data.get("products", [])
            if not products:
                break
            for p in products:
                sku = self._pick_sku(p)
                if not sku:
                    continue
                if self.only_types and p.get("product_type") not in self.only_types:
                    continue
                if self.exclude_types and p.get("product_type") in self.exclude_types:
                    continue
                if self.skip_handle_prefixes and (p.get("handle") or "").startswith(
                        tuple(self.skip_handle_prefixes)):
                    continue
                tags = p.get("tags") or []
                if self.exclude_tags and any(
                        any(bad in t for bad in self.exclude_tags) for t in tags):
                    continue
                if sku in self._raw:
                    continue
                self._raw[sku] = p
                items.append({"sku": sku,
                              "url": f"{self.base_url}{self.product_path}{p['handle']}"})
            if len(products) < self.page_limit:
                break
            time.sleep(self.request_delay)
        return items

    def _pick_sku(self, product):
        """
        商品層 SKU。多規格模式下必須取「第一個有貨規格的規格 SKU」，
        因為 Shopify 那側只認得逐規格展開的 SKU，取共用的商品編號會對不上。
        """
        if self.multi_variant:
            for v in product.get("variants", []):
                if v.get("available"):
                    return self._variant_sku(product, v)
            return ""
        for v in product.get("variants", []):
            sku = (v.get("sku") or "").strip()
            if sku:
                return sku
        return ""

    def _variant_sku(self, raw, variant):
        """
        規格層級 SKU。來源常常整件商品共用一個品號，必須帶上規格 id 才唯一。
        品牌可以覆寫成與舊系統相同的格式，就不必跑 SKU 遷移。
        """
        base = (variant.get("sku") or "").strip() or raw.get("handle") or ""
        return f"{base}-{variant.get('id')}"

    def build_variants(self, raw):
        """回傳 (variants, options, 代表成本)。沒有可販售規格就回傳 (None, None, 0)。"""
        image_index = {}
        for i, img in enumerate(raw.get("images", [])[:self.max_images]):
            for vid in (img.get("variant_ids") or []):
                image_index.setdefault(vid, i)
        variants = []
        for v in raw.get("variants", []):
            if not v.get("available"):
                continue          # 缺貨規格不上架，補貨時下一輪同步自動加回來
            price = int(round(float(v.get("price") or 0)))
            if price <= 0:
                continue
            variants.append(Variant(
                sku=self._variant_sku(raw, v),
                price=price,
                title=v.get("title") or "",
                in_stock=True,
                option_values=[x for x in (v.get("option1"), v.get("option2"),
                                           v.get("option3")) if x],
                image_index=image_index.get(v.get("id")),
            ))
        if not variants:
            return None, None, 0
        options = [{"name": o.get("name") or "選項", "values": o.get("values") or []}
                   for o in (raw.get("options") or [])][:3]
        # 代表成本取第一個有貨規格，與商品層 SKU 取同一個規格，兩者必須一致，
        # 否則改價比對會拿 A 規格的價去對 B 規格的 variant。
        return variants, options, variants[0].price

    def listing_skus(self, items):
        base = {i["sku"] for i in items}
        if not self.multi_variant:
            return base
        for raw in getattr(self, "_raw", {}).values():
            for v in raw.get("variants", []):
                if v.get("available"):
                    base.add(self._variant_sku(raw, v))
        return base

    @staticmethod
    def _pick_variant(product):
        """優先取有貨的變體，否則取第一個"""
        variants = product.get("variants", [])
        for v in variants:
            if v.get("available") and (v.get("sku") or "").strip():
                return v
        return variants[0] if variants else {}

    # ---- 明細（不再發請求，直接用列表撈回的資料）----
    def get_product(self, url, sku):
        raw = getattr(self, "_raw", {}).get(sku)
        if raw is None:
            return None
        return self.build_product(raw, url, sku)

    def tag_sellable(self, raw):
        if not self.require_tags:
            return True
        tags = raw.get("tags") or []
        return any(any(need in t for need in self.require_tags) for t in tags)

    def build_product(self, raw, url, sku) -> Product:
        variant = self._pick_variant(raw)
        price = int(round(float(variant.get("price") or 0)))
        grams = variant.get("grams") or 0
        actual = grams / 1000 if grams else 0.0
        images = [im.get("src") for im in raw.get("images", []) if im.get("src")]
        variants, options = [], []
        if self.multi_variant:
            variants, options, rep_price = self.build_variants(raw)
            if variants:
                price = rep_price
            else:
                variants, options = [], []
        return Product(
            sku=sku,
            url=url,
            title=(raw.get("title") or "").strip(),
            price=price,
            in_stock=bool(variant.get("available")) and self.tag_sellable(raw),
            description=self.html_to_text(raw.get("body_html") or ""),
            images=images[:self.max_images],
            actual_weight=round(actual, 3),
            volume_weight=0.0,
            weight=billable_weight(actual, 0.0),
            variants=variants,
            options=options,
            raw={"handle": raw.get("handle"), "tags": raw.get("tags"),
                 "product_type": raw.get("product_type"),
                 "tag_sellable": self.tag_sellable(raw)},
        )

    @staticmethod
    def html_to_text(html):
        if not html:
            return ""
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        return re.sub(r"\s{2,}", " ", text)[:1500]

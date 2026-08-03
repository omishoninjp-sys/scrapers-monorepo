"""品牌基底類別。新增品牌只需繼承並填設定，最多覆寫兩個 parse 方法。"""
import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from . import config
from .models import Product
from .text import (apply_glossary, clean_source_description, clean_source_title,
                    dedupe_repeats, strip_kana_readings)


@dataclass
class Route:
    """
    商品分流。同一個品牌底下若有性質不同的商品（例如佛事供品），
    用 Route 導到不同的 collection、標籤與翻譯規則。
    """
    name: str
    match: str                      # 對「來源標題 + SKU」做的 regex
    collection: str
    tags: str = ""
    prompt_rules: list = field(default_factory=list)
    notice_html: str = ""           # 插在商品說明最前面的用途說明
    enabled: bool = True

    def matches(self, product: Product) -> bool:
        return bool(re.search(self.match, f"{product.title} {product.sku}"))


class BaseBrand:
    # ---- 必填 ----
    slug = ""
    name = ""
    base_url = ""
    collection = ""               # 預設 collection

    # ---- 選填 ----
    vendor = ""
    product_type = ""
    tags = ""
    # 0 = 不設價格門檻（現行政策：全部同步）。
    # 二段式收費下運費另計，低價商品同樣有販售價值，不該在來源端就過濾掉。
    min_price = 0
    # 官網缺貨但商品頁還在時怎麼處理：
    #   "draft"  → 轉為 Shopify 草稿，補貨時自動改回上架（保留評論、SEO、既有連結）
    #   "delete" → 直接刪除，補貨時重新上架（會重跑翻譯，成本較高）
    # 「官網頁面已下架」一律刪除，不受這個設定影響。
    out_of_stock_action = "draft"
    schedule = None               # "10:00" (JST) 或 None
    prompt_rules = []
    shipping_html = ""
    routes = []                   # [Route, ...]，第一個命中的生效
    # 重複 SKU 時保留哪一筆：
    #   "oldest"（預設）保住評論與 SEO
    #   "newest"        來源站重用商品編號時
    #   "auto"          逐組看價格：同價視為同商品留最舊，異價視為換商品留最新
    dedup_keep = "oldest"
    clean_patterns = ()           # 額外要從來源文字移除的 regex
    glossary = {}                 # 日文 → 中文，翻譯前做確定性替換
    request_delay = 0.5
    max_pages = 20
    empty_pages_before_stop = 1   # 連續幾頁沒有新商品就停止（多分類來源需調高）

    def __init__(self):
        self.session = requests.Session()
        headers = dict(config.BROWSER_HEADERS)
        if self.base_url:
            headers["Referer"] = self.base_url.rstrip("/") + "/"
        self.session.headers.update(headers)
        self._warmed = False

    # ---- 子類別要實作 ----
    def parse_list(self, soup, page_url):
        raise NotImplementedError

    def parse_detail(self, soup, url, sku) -> Product:
        raise NotImplementedError

    def list_page_url(self, page):
        raise NotImplementedError

    # ---- 共用流程 ----
    def warm_up(self):
        if self._warmed or not self.base_url:
            return
        try:
            self.session.get(self.base_url, timeout=30)
            time.sleep(self.request_delay)
        except Exception:
            pass
        self._warmed = True

    def fetch(self, url):
        r = self.session.get(url, timeout=30)
        if r.status_code != 200:
            return None, r.status_code
        r.encoding = r.encoding or "utf-8"
        return BeautifulSoup(r.text, "html.parser"), r.status_code

    def list_products(self):
        """
        走完分頁回傳商品清單。第一頁抓不到東西時會重試 ——
        暫時性的空列表若被當成「官網全部下架」，清理階段會準備刪光整個 collection。
        """
        self.warm_up()
        found, seen = [], set()
        empty_streak = 0
        for page in range(1, self.max_pages + 1):
            url = self.list_page_url(page)
            if not url:
                break
            soup, _ = self.fetch(url)
            if soup is None and page == 1:
                for _ in range(2):
                    time.sleep(3)
                    soup, _ = self.fetch(url)
                    if soup is not None:
                        break
            if soup is None:
                break
            if page == 1 and not self.parse_list(soup, url):
                for _ in range(2):
                    time.sleep(3)
                    soup, _ = self.fetch(url)
                    if soup is not None and self.parse_list(soup, url):
                        break
            new = 0
            for item in self.parse_list(soup, url):
                if item["sku"] in seen:
                    continue
                seen.add(item["sku"])
                found.append(item)
                new += 1
            if new == 0:
                empty_streak += 1
                # 多分類來源常有空分類，連續數頁沒有新商品才收工
                if empty_streak >= self.empty_pages_before_stop:
                    break
            else:
                empty_streak = 0
            time.sleep(self.request_delay)
        return found

    def get_product(self, url, sku):
        soup, _ = self.fetch(url)
        if soup is None:
            return None
        return self.parse_detail(soup, url, sku)

    def listing_skus(self, items):
        """
        官網列表這一輪佔用的完整 SKU 集合，清理階段用它判斷誰已下架。

        單規格品牌就是商品 SKU 本身。多規格品牌必須連規格 SKU 一起回報 ——
        Shopify 那側的 collection SKU 是逐規格展開的，只回報商品層 SKU 的話，
        其餘規格會全部被判定「官網已下架」而排進刪除清單。
        """
        return {i["sku"] for i in items}

    def legacy_sku_keys(self, product: Product):
        """
        回傳這個商品在**舊 SKU 體系**下可能長什麼樣，用於遷移比對。
        預設沒有舊體系；來源站換過 SKU 規則的品牌才覆寫。
        """
        return []

    # ---- 分流 ----
    def resolve_route(self, product: Product):
        for route in self.routes:
            if route.enabled and route.matches(product):
                return route
        return None

    def collections_in_use(self):
        names = [self.collection]
        names += [r.collection for r in self.routes if r.enabled]
        return list(dict.fromkeys(n for n in names if n))

    # ---- 翻譯前清理 ----
    def prepare_for_translation(self, product: Product):
        title = strip_kana_readings(clean_source_title(product.title, self.clean_patterns))
        desc = clean_source_description(product.description, self.clean_patterns)
        # 去讀音與套詞彙表後重複單元才會縮到偵測長度內，所以再去重一次
        return (dedupe_repeats(apply_glossary(title, self.glossary)),
                apply_glossary(desc, self.glossary))

    def rules_for(self, route: Route = None):
        rules = list(self.prompt_rules)
        if route:
            rules += list(route.prompt_rules)
        return rules

    # ---- Shopify payload ----
    def build_payload(self, product: Product, translated: dict, route: Route = None):
        body = translated["description"]
        if route and route.notice_html:
            body = route.notice_html + body
        body += (self.shipping_html or "")
        return self._payload_shell(product, translated, route, body)

    def _variant_rows(self, product: Product):
        """
        多規格商品展開成 Shopify variants + options。

        規格名稱與選項值只做詞彙表替換，不送翻譯：variants[].option1 必須與
        options[].values 逐字相同，交給模型翻兩次一定對不上；而「100g」「S」
        這類值本來也不需要翻。
        """
        sellable = product.sellable_variants
        if not sellable:
            return None, None
        rows = []
        for v in sellable:
            row = {
                "sku": v.sku,
                "price": f"{v.selling_price:.2f}",
                "inventory_management": None,
                "inventory_policy": "continue",
                "requires_shipping": True,
            }
            for i, value in enumerate(v.option_values[:3]):
                row[f"option{i + 1}"] = apply_glossary(value.strip(), self.glossary)
            rows.append(row)
        # options 的 values 只留還有貨的規格，否則 Shopify 會建出選不到東西的選項
        options = []
        for i, opt in enumerate(product.options[:3]):
            values = []
            for v in sellable:
                if i < len(v.option_values):
                    value = apply_glossary(v.option_values[i].strip(), self.glossary)
                    if value not in values:
                        values.append(value)
            if values:
                options.append({"name": apply_glossary(
                    (opt.get("name") or "選項").strip(), self.glossary),
                                "values": values})
        return rows, (options or None)

    def _payload_shell(self, product: Product, translated: dict, route, body):
        variants, options = (self._variant_rows(product)
                             if product.variants else (None, None))
        if not variants:
            variants = [{
                "sku": product.sku,
                "price": f"{product.selling_price:.2f}",
                "inventory_management": None,
                "inventory_policy": "continue",
                "requires_shipping": True,
            }]
            options = None
        payload = {
            "title": translated["title"],
            "body_html": body,
            "vendor": self.vendor or self.name,
            "product_type": self.product_type,
            "status": "active",
            "published": True,
            "variants": variants,
            "images": [{"src": u, "position": i + 1}
                       for i, u in enumerate(product.images)],
            "tags": (route.tags if route and route.tags else self.tags),
            "metafields_global_title_tag": translated["page_title"],
            "metafields_global_description_tag": translated["meta_description"],
            "metafields": [{"namespace": "custom", "key": "link",
                            "value": product.url, "type": "url"}],
        }
        if options:
            payload["options"] = options
        return payload

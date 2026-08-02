"""Shopify Admin API 封裝。取代 12 支各自漂移的 Shopify 函式。"""
import re
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config


class ShopifyError(Exception):
    pass


def _prefer(a, b, keep="oldest"):
    """
    重複 SKU 時該留哪一筆。先看是否上架中（active 優先於 draft/archived），
    同狀態再依 keep 決定：

      keep="oldest"（預設）—— 保留最舊。評論、SEO 排名與既有連結都累積在它身上。
      keep="newest"       —— 保留最新。**來源站會重用商品編號時必須用這個**：
                             資生堂 PARLOUR 的 SKU 38171 舊資料是 CRN36、
                             新資料是 CRN39，是兩件不同商品（價格也不同）。
                             留最舊等於留下已停售的商品、刪掉現行商品。
    """
    a_active = a.get("status") == "active"
    b_active = b.get("status") == "active"
    if a_active != b_active:
        return a_active
    a_time = a.get("created_at", "")
    b_time = b.get("created_at", "")
    if keep == "newest":
        return a_time > b_time
    return (a_time or "\uffff") < (b_time or "\uffff")


def _make_session():
    """
    帶重試的 session。Shopify 偶爾會 read timeout 或直接斷線，
    單次失敗不該讓整輪同步陣亡（2026-08 實際發生過兩次）。

    **POST 刻意不自動重試**：建立商品若在回應遺失時重送，會產生重複商品。
    urllib3 的 allowed_methods 預設只含冪等方法（GET/PUT/DELETE 等），
    正好符合需求，不要為了「更保險」把 POST 加進去。
    """
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,          # 1.5s, 3s, 6s, 12s
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class ShopifyClient:
    def __init__(self, shop=None, token=None, api_version=None):
        self.shop = shop or config.SHOPIFY_SHOP
        self.token = token or config.SHOPIFY_ACCESS_TOKEN
        self.api_version = api_version or config.SHOPIFY_API_VERSION
        self.session = _make_session()

    # ---- 基礎 ----
    @property
    def configured(self):
        return bool(self.shop and self.token)

    def url(self, endpoint):
        return f"https://{self.shop}.myshopify.com/admin/api/{self.api_version}/{endpoint}"

    @property
    def headers(self):
        return {"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"}

    def _paged(self, endpoint):
        """跟著 Link header 走完所有分頁，yield 每一頁的 products"""
        url = self.url(endpoint)
        while url:
            r = self.session.get(url, headers=self.headers, timeout=60)
            if r.status_code != 200:
                raise ShopifyError(f"HTTP {r.status_code} on {url}: {r.text[:200]}")
            yield r.json().get("products", [])
            link = r.headers.get("Link", "")
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None

    @staticmethod
    def _to_sku_map(pages, stats=None):
        """
        SKU → 商品資訊。stats 會回填 fetched（抓到的商品數）與 no_sku
        （沒有任何 variant 帶 SKU 的商品），用來分辨
        「分頁漏抓」與「商品本身沒填 SKU」——兩者的商品數與 SKU 數都會對不上，
        但成因和處理方式完全不同。
        """
        pm = {}
        fetched = 0
        no_sku = []
        owners = {}          # sku -> [商品...]，用來偵測重複商品
        for products in pages:
            for p in products:
                fetched += 1
                pid = p.get("id")
                hit = False
                for v in p.get("variants", []):
                    sku = (v.get("sku") or "").strip()
                    if sku and pid:
                        hit = True
                        entry = {
                            "product_id": pid,
                            "variant_id": v.get("id"),
                            "price": float(v.get("price") or 0),
                            "status": p.get("status"),
                        }
                        owners.setdefault(sku, []).append(
                            {**entry, "title": p.get("title", "")[:60],
                             "created_at": p.get("created_at", "")})
                        entry = {**entry, "created_at": p.get("created_at", "")}
                        prev = pm.get(sku)
                        if prev is None or _prefer(entry, prev):
                            pm[sku] = entry
                if not hit:
                    no_sku.append({"product_id": pid, "title": p.get("title", "")[:60],
                                   "status": p.get("status")})
        if stats is not None:
            stats["fetched"] = fetched
            stats["no_sku"] = no_sku
            stats["duplicates"] = {k: v for k, v in owners.items() if len(v) > 1}
        return pm

    # ---- 查詢 ----
    def shop_info(self):
        r = self.session.get(self.url("shop.json"), headers=self.headers, timeout=30)
        if r.status_code != 200:
            raise ShopifyError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json().get("shop", {})

    def all_products_map(self, stats=None):
        """全店 SKU → {product_id, variant_id, price}，用於去重"""
        return self._to_sku_map(
            self._paged("products.json?status=active,draft,archived&limit=250"), stats)

    def collection_products_map(self, collection_id, stats=None):
        """
        collection 內 SKU map，用於刪除比對。

        刻意**不用** collections/{id}/products.json：那支 legacy 端點回傳的商品
        representation 不含 variants（也就沒有 SKU），而且社群長期回報分頁會漏資料
        （有案例 869 件只拿到 408 件）。改用 products.json?collection_id= 過濾，
        與 all_products_map() 走同一支端點，variants 正常帶回。
        """
        if not collection_id:
            return {}
        # status=active,draft,archived：預設不帶 status 時 Shopify 只回傳
        # active 商品，轉成草稿的商品會整批消失，導致補貨復活永遠不會觸發。
        return self._to_sku_map(
            self._paged(f"products.json?collection_id={collection_id}"
                        f"&status=active,draft,archived&limit=250"), stats)

    # ---- Collection ----
    def find_collections(self, title):
        """
        依標題找 collection。兩件事很重要：
          1. Shopify 有 custom_collections（手動）與 smart_collections（自動）兩種，
             只查前者會漏掉自動集合，然後誤建一個同名的空集合。
          2. 用 ?title= 過濾而不是抓 250 筆自己比對，店裡集合數超過一頁時才不會漏。
        回傳所有同名集合（Shopify 允許同名），呼叫端自己決定怎麼處理。
        """
        found = []
        for kind, endpoint in (("custom", "custom_collections.json"),
                               ("smart", "smart_collections.json")):
            url = self.url(f"{endpoint}?limit=250&title={quote(title)}")
            while url:
                r = self.session.get(url, headers=self.headers, timeout=30)
                if r.status_code != 200:
                    break
                key = f"{kind}_collections"
                for c in r.json().get(key, []):
                    if c.get("title") == title:
                        found.append({"id": c["id"], "kind": kind, "title": c["title"]})
                link = r.headers.get("Link", "")
                m = re.search(r'<([^>]+)>;\s*rel="next"', link)
                url = m.group(1) if m else None
        return found

    def get_or_create_collection(self, title, create=True):
        """create=False 時只查不建（dry-run 用），找不到回傳 None"""
        found = self.find_collections(title)
        if found:
            return found[0]["id"]
        if not create:
            return None
        r = self.session.post(self.url("custom_collections.json"), headers=self.headers,
                          json={"custom_collection": {"title": title, "published": True}},
                          timeout=30)
        if r.status_code == 201:
            return r.json()["custom_collection"]["id"]
        raise ShopifyError(f"建立 collection 失敗 HTTP {r.status_code}: {r.text[:200]}")

    def collection_product_count(self, collection_id):
        r = self.session.get(self.url(f"products/count.json?collection_id={collection_id}"),
                         headers=self.headers, timeout=30)
        return r.json().get("count") if r.status_code == 200 else None

    def add_to_collection(self, product_id, collection_id):
        r = self.session.post(self.url("collects.json"), headers=self.headers,
                          json={"collect": {"product_id": product_id,
                                            "collection_id": collection_id}}, timeout=30)
        return r.status_code == 201

    # ---- 商品 ----
    def create_product(self, payload):
        r = self.session.post(self.url("products.json"), headers=self.headers,
                          json={"product": payload}, timeout=90)
        if r.status_code != 201:
            raise ShopifyError(f"HTTP {r.status_code}: {r.text[:400]}")
        return r.json()["product"]

    def update_variant(self, variant_id, **fields):
        r = self.session.put(self.url(f"variants/{variant_id}.json"), headers=self.headers,
                         json={"variant": {"id": variant_id, **fields}}, timeout=30)
        return r.status_code == 200

    @staticmethod
    def _resolve_keep(items, keep):
        """
        keep="auto"：同組價格全部相同 → 視為同一商品重複上架，保留最舊
        （保住評論與 SEO）；價格有差異 → 視為來源站重用了商品編號，
        是不同商品，保留最新（否則會留下已停售的舊商品）。

        資生堂 PARLOUR 同一份清單裡兩種都有：
          71111 兩筆都是 ¥1,736  → 同一商品，只是翻譯不同 → 留最舊
          38171 ¥6,820 vs ¥7,283 → CRN36 與 CRN39 是不同商品 → 留最新
        """
        if keep != "auto":
            return keep
        prices = {round(float(x.get("price") or 0), 2) for x in items}
        return "oldest" if len(prices) <= 1 else "newest"

    def dedup_plan(self, duplicates, keep="oldest"):
        """
        產生重複商品的清理計畫。不執行任何寫入。
        回傳 [{sku, keep, remove:[...]}]
        """
        plan = []
        for sku, items in sorted(duplicates.items()):
            mode = self._resolve_keep(items, keep)
            keeper = items[0]
            for it in items[1:]:
                if _prefer(it, keeper, mode):
                    keeper = it
            remove = [x for x in items if x["product_id"] != keeper["product_id"]]
            if remove:
                plan.append({"sku": sku, "keep": keeper, "remove": remove,
                             "mode": mode})
        return plan

    def update_variant_sku(self, variant_id, sku):
        r = self.session.put(self.url(f"variants/{variant_id}.json"), headers=self.headers,
                         json={"variant": {"id": variant_id, "sku": sku}}, timeout=30)
        return r.status_code == 200

    def set_product_status(self, product_id, status):
        """status: 'active' 或 'draft'。缺貨轉 draft 可保留評論、SEO 與既有連結。"""
        r = self.session.put(self.url(f"products/{product_id}.json"), headers=self.headers,
                         json={"product": {"id": product_id, "status": status}}, timeout=30)
        return r.status_code == 200

    def delete_product(self, product_id):
        r = self.session.delete(self.url(f"products/{product_id}.json"),
                            headers=self.headers, timeout=30)
        return r.status_code == 200

    # ---- 上架到所有銷售管道 ----
    def publish_to_all_channels(self, product_id):
        gql = f"https://{self.shop}.myshopify.com/admin/api/{self.api_version}/graphql.json"
        r = self.session.post(gql, headers=self.headers,
                          json={"query": "{ publications(first:20){ edges{ node{ id name }}}}"},
                          timeout=30)
        if r.status_code != 200:
            return False
        edges = r.json().get("data", {}).get("publications", {}).get("edges", [])
        seen, pubs = set(), []
        for e in edges:
            node = e["node"]
            if node["name"] not in seen:
                seen.add(node["name"])
                pubs.append(node)
        if not pubs:
            return False
        mutation = ("mutation publishablePublish($id:ID!,$input:[PublicationInput!]!)"
                    "{publishablePublish(id:$id,input:$input){userErrors{field message}}}")
        self.session.post(gql, headers=self.headers, json={
            "query": mutation,
            "variables": {"id": f"gid://shopify/Product/{product_id}",
                          "input": [{"publicationId": p["id"]} for p in pubs]},
        }, timeout=30)
        return True

"""
同步引擎。取代 12 份各自漂移的 run_scrape()。

流程與原版一致：
  1. 取得/建立 collection
  2. 抓全店 SKU map（去重用）+ collection SKU map（刪除比對用）
  3. 爬列表 → 逐件爬明細
  4. 已存在 → 只更新售價；新商品 → 翻譯後上架
  5. 收尾刪除「官網已下架」+「已確認缺貨」的商品

與原版的差異：
  - 取 SKU map 失敗會 raise 並中止，不再靜默回傳空 dict
    （原版 break 後回空 map，會導致全部商品被視為新商品而重複上架）
  - 連續翻譯失敗達上限時中止，且跳過刪除階段（沿用原版保護）
  - DRY_RUN=1 時不對 Shopify 做任何寫入
"""
import threading
import time
import traceback

from . import config
from .models import Product
from .shopify import ShopifyClient, ShopifyError
from .text import japanese_kanji_found, kana_words, normalize_kanji
from .translate import translate


def new_status():
    return {"running": False, "brand": "", "phase": "待機", "progress": 0, "total": 0,
            "current": "", "uploaded": 0, "skipped": 0, "out_of_stock": 0,
            "price_updated": 0, "deleted": 0, "translation_failed": 0,
            "translation_stopped": False, "kana_retried": 0, "kana_rejected": 0,
            "drafted": 0, "reactivated": 0, "no_price": 0, "price_changes": [],
            "data_integrity": "", "no_sku_products": [], "duplicate_skus": {},
            "glossary_gaps": {},
            "errors": [], "log": [], "done": False}


class SyncRunner:
    """一個品牌一個 runner。狀態存在自己身上，前端輪詢 as_dict()。"""

    def __init__(self, brand, client=None, dry_run=None):
        self.brand = brand
        self.client = client or ShopifyClient()
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run
        self.status = new_status()
        self._price_changes = []
        self._glossary_gaps = {}
        self.status["brand"] = brand.slug
        self._lock = threading.Lock()

    # ---- 狀態 ----
    def log(self, message):
        with self._lock:
            self.status["log"].append(message)
            self.status["log"] = self.status["log"][-300:]
        print(f"[{self.brand.slug}] {message}", flush=True)

    def error(self, message):
        with self._lock:
            self.status["errors"].append(message)
        self.log("✗ " + message)

    def as_dict(self):
        with self._lock:
            return dict(self.status)

    # ---- 主流程 ----
    def run(self):
        st = self.status
        st.update(new_status())
        st.update({"running": True, "brand": self.brand.slug})
        self._price_changes = []
        self._glossary_gaps = {}
        try:
            if not self.client.configured and not self.dry_run:
                raise ShopifyError("SHOPIFY_SHOP / SHOPIFY_ACCESS_TOKEN 未設定")

            # 讀取一律執行（唯讀操作），dry-run 只跳過寫入。
            # 否則去重不會發生，dry-run 的「會上架」數字會失真到無法跟舊服務比對。
            st["phase"] = "解析 Collection"
            collection_ids = {}
            for name in self.brand.collections_in_use():
                # dry-run 只查不建，建立 collection 是寫入操作
                cid = self.client.get_or_create_collection(name, create=not self.dry_run)
                collection_ids[name] = cid
                if cid is None:
                    self.log(f"⚠ collection「{name}」不存在（dry-run 不建立）")
                else:
                    dupes = self.client.find_collections(name)
                    if len(dupes) > 1:
                        self.error(f"collection「{name}」有 {len(dupes)} 個同名集合："
                                   f"{[(d['id'], d['kind']) for d in dupes]}，請先在 Shopify 清理")

            st["phase"] = "取得全店商品"
            existing = self.client.all_products_map()

            st["phase"] = "取得 Collection 商品"
            collection_map = {}
            for name, cid in collection_ids.items():
                if not cid:
                    continue
                stats = {}
                part = self.client.collection_products_map(cid, stats)
                reported = self.client.collection_product_count(cid)
                fetched = stats.get("fetched", 0)
                no_sku = stats.get("no_sku", [])
                self.log(f"collection「{name}」(id {cid}) 商品 {fetched} 件 / SKU {len(part)} 個")

                if reported is not None and reported != fetched:
                    self.status["data_integrity"] = "pagination"
                    self.error(f"「{name}」Shopify 回報 {reported} 件但只抓到 {fetched} 件，"
                               f"分頁可能漏資料，本輪清理階段已停用")
                dups = stats.get("duplicates", {})
                if dups:
                    self.status["duplicate_skus"] = {
                        k: [x["product_id"] for x in v] for k, v in dups.items()}
                    self.status.setdefault("data_integrity", "duplicate_sku")
                    self.error(f"「{name}」有 {len(dups)} 組重複 SKU"
                               f"（同一 SKU 掛在多件商品上），去重與清理無法正確判定。"
                               f"請用「資料健檢」查看完整清單")
                if no_sku:
                    self.status["no_sku_products"] = no_sku
                    self.status.setdefault("data_integrity", "no_sku")
                    self.error(f"「{name}」有 {len(no_sku)} 件商品沒有填 SKU，"
                               f"無法比對去重與清理。前 5 件："
                               + "、".join(x["title"] for x in no_sku[:5]))
                collection_map.update(part)

            existing_skus = set(existing)
            collection_skus = set(collection_map)
            self.log(f"全店 {len(existing_skus)} 個 SKU，"
                     f"納管 collection（{', '.join(collection_ids)}）內 {len(collection_skus)} 個")

            st["phase"] = "爬取官網列表"
            items = self.brand.list_products()
            st["total"] = len(items)
            website_skus = self.brand.listing_skus(items)
            self.log(f"官網列表 {len(items)} 件")

            # 空列表幾乎一定是抓取失敗，不是官網真的把商品全下架了。
            # 若放行，清理階段會把整個 collection 判定為「已下架」並刪光。
            if not items:
                raise RuntimeError(
                    "官網列表 0 件（重試後仍為空）。這通常是網站暫時性錯誤或被限流，"
                    "不是商品真的全部下架，已中止本輪同步。")

            out_of_stock = set()
            consecutive_failures = 0
            consecutive_errors = 0

            st["phase"] = "處理商品"
            for idx, item in enumerate(items, 1):
                st["progress"] = idx
                sku = item["sku"]
                st["current"] = sku

                try:
                    product = self.brand.get_product(item["url"], sku)
                except Exception as e:
                    # 單一商品的網路錯誤不該讓整輪陣亡
                    product = None
                    self.error(f"爬取例外 {sku}: {type(e).__name__}")
                if product is None:
                    consecutive_errors += 1
                    if consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                        raise RuntimeError(
                            f"連續 {consecutive_errors} 件爬取失敗，可能是網站或網路異常，"
                            f"已中止本輪同步")
                    self.error(f"爬取失敗: {sku}")
                    time.sleep(1)
                    continue
                consecutive_errors = 0

                reason = product.is_sellable(self.brand.min_price)
                if reason:
                    if reason == "no_price":
                        st["no_price"] += 1
                    if reason == "out_of_stock":
                        out_of_stock.add(sku)
                        st["out_of_stock"] += 1
                        self._handle_out_of_stock(sku, collection_skus,
                                                  collection_map.get(sku),
                                                  product.raw or {})
                    st["skipped"] += 1
                    time.sleep(0.5)
                    continue

                known = next((s for s in product.all_skus if s in existing_skus), None)
                if known:
                    self._maybe_reactivate(known, existing.get(known, {}))
                    self._collect_price_change(product, collection_map)
                    st["skipped"] += 1
                    time.sleep(0.5)
                    continue

                try:
                    ok = self._upload(product, collection_ids)
                except Exception as e:
                    self.error(f"上架例外 {sku}: {type(e).__name__}: {e}")
                    ok = False
                if ok is True:
                    existing_skus.update(product.all_skus)
                    st["uploaded"] += 1
                    consecutive_failures = 0
                elif ok == "kana_rejected":
                    st["skipped"] += 1
                    consecutive_failures = 0
                elif ok == "translation_failed":
                    st["translation_failed"] += 1
                    consecutive_failures += 1
                    if consecutive_failures >= config.MAX_CONSECUTIVE_TRANSLATION_FAILURES:
                        st["translation_stopped"] = True
                        self.error(f"翻譯連續失敗 {consecutive_failures} 次，自動停止")
                        break
                else:
                    consecutive_failures = 0
                time.sleep(1)

            if self._glossary_gaps:
                top = sorted(self._glossary_gaps.items(), key=lambda x: -x[1])
                st["glossary_gaps"] = dict(top)
                self.log("詞彙表缺口（把這些加進 brand.glossary 可減少退回）："
                         + "、".join(f"{w}×{c}" for w, c in top[:15]))

            self._apply_price_changes()

            if st["translation_stopped"]:
                st["phase"] = "翻譯異常停止（跳過刪除）"
            else:
                self._cleanup(collection_skus, website_skus, out_of_stock, collection_map)
                st["phase"] = "完成"
        except Exception as e:
            traceback.print_exc()
            self.error(f"{type(e).__name__}: {e}")
            st["phase"] = "中止"
        finally:
            st["running"] = False
            st["done"] = True

    # ---- 子步驟 ----
    def _handle_out_of_stock(self, sku, collection_skus, info, raw=None):
        """
        官網買不到（但商品頁還在）→ 依品牌設定轉 draft 或留給清理階段刪除。

        兩種原因要分清楚，log 才看得懂：
          缺貨      —— 商品有在賣，只是現在沒庫存
          非網購品項 —— 商品在，但目前不開放網路販售（虎屋的季節輪替羊羹）
        """
        reason = ("非網購品項" if raw and raw.get("tag_sellable") is False
                  else "官網缺貨")
        if self.brand.out_of_stock_action != "draft":
            return
        if sku not in collection_skus or not info:
            return
        if info.get("status") == "draft":
            return
        if self.dry_run:
            self.log(f"[dry-run] 會轉為草稿 {sku}（{reason}）")
            self.status["drafted"] += 1
            return
        if self.client.set_product_status(info["product_id"], "draft"):
            self.status["drafted"] += 1
            self.log(f"已轉草稿 {sku}（{reason}）")
        else:
            self.error(f"轉草稿失敗 {sku}")

    def _maybe_reactivate(self, sku, info):
        """補貨了但 Shopify 還是草稿 → 改回上架"""
        if info.get("status") != "draft":
            return
        if self.dry_run:
            self.log(f"[dry-run] 會重新上架 {sku}（已補貨）")
            self.status["reactivated"] += 1
            return
        if self.client.set_product_status(info["product_id"], "active"):
            self.status["reactivated"] += 1
            self.log(f"已重新上架 {sku}（已補貨）")
        else:
            self.error(f"重新上架失敗 {sku}")

    def _collect_price_change(self, product: Product, collection_map):
        """
        只蒐集待改價清單，不立即寫入。改價會直接影響營收，
        必須先過保險絲、且在 dry-run 也要看得到，才輪到寫入。

        多規格商品逐規格比對：100g 與 200g 是兩個 variant_id、兩個價格，
        只看商品層代表價會讓其他規格永遠停在舊價。
        """
        if product.variants:
            for v in product.sellable_variants:
                self._queue_price_change(v.sku, collection_map.get(v.sku),
                                         v.price, v.selling_price)
            return
        self._queue_price_change(product.sku, collection_map.get(product.sku),
                                 product.price, product.selling_price)

    def _queue_price_change(self, sku, info, cost, new_price):
        if not info:
            return                      # 不在納管 collection 內，不動它
        variant_id = info.get("variant_id")
        if not variant_id:
            return
        old_price = info.get("price", 0)
        if abs(new_price - old_price) < 1:
            return
        self._price_changes.append({
            "sku": sku, "variant_id": variant_id,
            "old": old_price, "new": new_price, "cost": cost,
            "delta_ratio": (new_price - old_price) / old_price if old_price else 0,
        })

    def _apply_price_changes(self):
        changes = self._price_changes
        self.status["price_changes"] = changes
        if not changes:
            return
        drops = [c for c in changes if c["delta_ratio"] <= -config.PRICE_ALERT_RATIO]
        rises = [c for c in changes if c["delta_ratio"] >= config.PRICE_ALERT_RATIO]
        self.log(f"待改價 {len(changes)} 件"
                 f"（降幅超過 {config.PRICE_ALERT_RATIO:.0%} 的 {len(drops)} 件、"
                 f"漲幅超過 {config.PRICE_ALERT_RATIO:.0%} 的 {len(rises)} 件）")
        for c in changes[:40]:
            self.log(f"   {c['sku']} {c['old']:.0f} → {c['new']:.0f} "
                     f"({c['delta_ratio']:+.0%}，成本¥{c['cost']})")
        if len(changes) > 40:
            self.log(f"   ...另外 {len(changes) - 40} 件，完整清單見 /api/<品牌>/status")

        if (len(drops) + len(rises)) > config.MAX_PRICE_CHANGES and not config.ALLOW_BULK_REPRICE:
            self.status["phase"] = "改價量超過門檻，已中止改價"
            self.error(
                f"大幅改價 {len(drops) + len(rises)} 件，超過門檻 "
                f"{config.MAX_PRICE_CHANGES} 件，已中止改價。請先確認定價政策；"
                f"確認無誤再設 ALLOW_BULK_REPRICE=1 重跑。")
            return
        if self.dry_run:
            self.log(f"[dry-run] 會改價 {len(changes)} 件")
            self.status["price_updated"] = len(changes)
            return
        for c in changes:
            if self.client.update_variant(c["variant_id"], price=f"{c['new']:.2f}",
                                          cost=f"{c['cost']:.2f}"):
                self.status["price_updated"] += 1
            else:
                self.error(f"改價失敗 {c['sku']}")

    def _upload(self, product: Product, collection_ids):
        route = self.brand.resolve_route(product)
        collection_name = route.collection if route else self.brand.collection
        title, description = self.brand.prepare_for_translation(product)
        rules = self.brand.rules_for(route)

        translated, failure_kind = self._translate_checked(
            product.sku, title, description, rules)
        if translated is None:
            return failure_kind

        tag = f"[{route.name}] " if route else ""
        if self.dry_run:
            self.log(f"[dry-run] {tag}會上架 {product.sku} → {collection_name}｜"
                     f"{translated['title']}｜成本¥{product.price} 售價¥{product.selling_price}")
            return True

        payload = self.brand.build_payload(product, translated, route)
        try:
            created = self.client.create_product(payload)
        except ShopifyError as e:
            self.error(f"上傳失敗 {product.sku}: {e}")
            return False

        pid = created["id"]
        variants = created.get("variants") or []
        if variants:
            # 逐規格寫回成本；找不到對應就退回商品層代表成本。
            cost_by_sku = {v.sku: v.price for v in product.sellable_variants}
            for cv in variants:
                cost = cost_by_sku.get((cv.get("sku") or "").strip(), product.price)
                self.client.update_variant(cv["id"], cost=f"{cost:.2f}")
            self.client.assign_variant_images(created, product)
        else:
            self.error(f"警告 {product.sku}: Shopify 沒有回傳 variant，"
                       f"請檢查 API 版本 {self.client.api_version}")
        cid = collection_ids.get(collection_name)
        if cid:
            self.client.add_to_collection(pid, cid)
        self.client.publish_to_all_channels(pid)
        self.log(f"{tag}已上架 {product.sku} → {collection_name}｜{translated['title']}"
                 f"｜¥{product.selling_price}")
        return True

    def _translate_checked(self, sku, title, description, rules):
        """翻譯並檢查殘留日文；偵測到任何假名就重試一次，仍失敗則放棄該商品。"""
        for attempt in (1, 2):
            extra = []
            if attempt == 2:
                extra = ["上一次的輸出殘留了平假名或片假名，這次必須完全消除，"
                         "包含商品名、規格名與包裝名，全部改為繁體中文"]
            result = translate(title, description, brand_rules=rules + extra)
            if not result.get("success"):
                self.error(f"翻譯失敗 {sku}: {result.get('error')}")
                return None, "translation_failed"
            for field in ("title", "description", "page_title", "meta_description"):
                if result.get(field):
                    jp = japanese_kanji_found(result[field])
                    if jp and field == "title":
                        self.log(f"   {sku} 日本字體已轉繁體: {''.join(jp)}")
                    result[field] = normalize_kanji(result[field])

            leftovers = kana_words(result["title"])
            if not leftovers:
                return result, None
            for w in leftovers:
                self._glossary_gaps[w] = self._glossary_gaps.get(w, 0) + 1
            self.status["kana_retried"] += 1
            self.log(f"⚠ {sku} 標題殘留日文 {leftovers[:4]}"
                     f"{'，重試一次' if attempt == 1 else '，重試後仍殘留，跳過'}")
        self.status["kana_rejected"] += 1
        self.error(f"殘留日文無法消除，跳過 {sku}")
        return None, "kana_rejected"

    def _cleanup(self, collection_skus, website_skus, out_of_stock, collection_map):
        self.status["phase"] = "清理下架/缺貨商品"
        delisted = collection_skus - website_skus
        oos = collection_skus & out_of_stock
        if self.brand.out_of_stock_action == "draft":
            targets = delisted          # 缺貨已在迴圈中轉為草稿，不刪除
        else:
            targets = delisted | oos
        if not targets:
            self.log("沒有需要刪除的商品")
            return
        # ---- 覆蓋率硬性保護（ALLOW_BULK_DELETE 無法繞過）----
        # 官網列表相對 collection 太少時，代表列表抓取不完整，
        # 這時的「已下架」判定不可信。這道關卡刻意不提供環境變數覆寫。
        if collection_skus and not website_skus:
            self.status["phase"] = "官網列表為空，已停用清理"
            self.error("官網列表為空但 collection 有商品，判定不可信，已跳過清理階段")
            return
        coverage = len(website_skus) / len(collection_skus) if collection_skus else 1
        if coverage < config.MIN_LIST_COVERAGE:
            self.status["phase"] = "官網列表覆蓋率過低，已停用清理"
            self.error(
                f"官網列表僅 {len(website_skus)} 件，collection 有 {len(collection_skus)} 件"
                f"（覆蓋率 {coverage:.0%}，低於 {config.MIN_LIST_COVERAGE:.0%}），"
                f"列表可能抓取不完整，已跳過清理階段。"
                f"若官網確實大量下架，請調整 MIN_LIST_COVERAGE 後重跑。")
            return

        if self.status.get("data_integrity") and not config.ALLOW_INCOMPLETE_CLEANUP:
            self.status["phase"] = "collection 資料不完整，已停用清理"
            self.error("collection 商品數與 SKU 數對不上，刪除判定的依據不完整，"
                       "已跳過清理階段。請先處理上面的資料問題；"
                       "確認無誤再設 ALLOW_INCOMPLETE_CLEANUP=1 重跑。")
            return

        mode = ("缺貨轉草稿" if self.brand.out_of_stock_action == "draft" else "缺貨一併刪除")
        self.log(f"準備刪除 {len(targets)} 件（官網頁面已下架 {len(delisted)}、"
                 f"官網缺貨 {len(oos)} 件 → {mode}）")

        # ---- 刪除保險絲 ----
        limit = max(config.MAX_DELETE_ABS,
                    int(len(collection_skus) * config.MAX_DELETE_RATIO))
        if len(targets) > limit and not config.ALLOW_BULK_DELETE:
            self.status["phase"] = "刪除量超過門檻，已中止清理"
            self.error(
                f"要刪 {len(targets)} 件，超過門檻 {limit} 件"
                f"（collection 內共 {len(collection_skus)} 件），已中止清理階段。"
                f"請先確認這是預期結果；確認無誤再設 ALLOW_BULK_DELETE=1 重跑。")
            self.log("待刪清單前 30 筆：" + ", ".join(sorted(targets)[:30]))
            return
        for sku in sorted(targets):
            self.status["current"] = f"刪除 {sku}"
            if self.dry_run:
                self.log(f"[dry-run] 會刪除 {sku}")
                continue
            pid = (collection_map.get(sku) or {}).get("product_id")
            if not pid:
                continue
            if self.client.delete_product(pid):
                self.status["deleted"] += 1
                self.log(f"已刪除 {sku}")
            else:
                self.error(f"刪除失敗 {sku}")
            time.sleep(0.3)

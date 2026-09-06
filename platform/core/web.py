"""Flask 介面。單一入口，品牌用下拉選單切換，取代 12 份各自的 index()。"""
import threading
import traceback

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from . import config, registry
from .shopify import ShopifyClient
from .scheduler import enabled as scheduler_enabled, get_scheduler
from .sync import SyncRunner
from .translate import translate

app = Flask(__name__)
_runners = {}
_lock = threading.Lock()


def runner_for(slug):
    with _lock:
        if slug not in _runners:
            _runners[slug] = SyncRunner(registry.get(slug))
        return _runners[slug]


scheduler = get_scheduler(runner_for)


def start_scheduler():
    """啟動排程器。由 app.py 在模組層級呼叫。

    刻意不在 import 時自動啟動 —— 只要 import core.web 就會叫起排程器的話，
    check_ui.py 這類「只想檢查前端 JS 語法」的工具也會觸發它。本機若帶著
    正式 Shopify token，行程跨過 JST 排程時間就會對正式店真的跑同步。

    放在 app.py 的模組層級（而不是 if __name__ == "__main__"）是因為
    Procfile 用 `gunicorn app:app`，gunicorn 是 import app 模組而不是執行它。
    """
    if scheduler_enabled():
        scheduler.start()


@app.route("/api/schedule")
def api_schedule():
    return jsonify({"enabled": scheduler_enabled(), "jobs": scheduler.status()})


@app.errorhandler(Exception)
def handle_error(e):
    """/api/* 一律回 JSON，避免前端 r.json() 收到 HTML 錯誤頁"""
    code = e.code if isinstance(e, HTTPException) else 500
    if request.path.startswith("/api/"):
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}", "status": code}), code
    if isinstance(e, HTTPException):
        return e
    raise e


# ---------- API ----------
@app.route("/api/brands")
def api_brands():
    return jsonify({"brands": registry.summary(),
                    "dry_run": config.DRY_RUN,
                    "token_required": bool(config.SYNC_TOKEN),
                    "version": config.PLATFORM_VERSION,
                    "api_version": config.SHOPIFY_API_VERSION})


@app.route("/api/<slug>/test-shopify")
def api_test_shopify(slug):
    registry.get(slug)
    client = ShopifyClient()
    if not client.configured:
        return jsonify({"error": "SHOPIFY_SHOP / SHOPIFY_ACCESS_TOKEN 未設定"})
    shop = client.shop_info()
    return jsonify({"success": True, "shop": shop.get("name"),
                    "api_version": client.api_version})


@app.route("/api/<slug>/test-translate")
def api_test_translate(slug):
    brand = registry.get(slug)
    if not config.OPENAI_API_KEY:
        return jsonify({"error": "OPENAI_API_KEY 未設定"})
    result = translate("テスト商品 化粧箱", "日本の伝統的なお菓子の詰め合わせです。",
                       brand_rules=brand.prompt_rules)
    key = config.OPENAI_API_KEY
    result["key_preview"] = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "太短"
    return jsonify(result)


@app.route("/api/<slug>/collections")
def api_collections(slug):
    """診斷用：列出這個品牌會用到的 collection 目前的實際狀況"""
    brand = registry.get(slug)
    client = ShopifyClient()
    if not client.configured:
        return jsonify({"error": "SHOPIFY_SHOP / SHOPIFY_ACCESS_TOKEN 未設定"})
    out = []
    for name in brand.collections_in_use():
        for c in client.find_collections(name):
            out.append({**c, "product_count": client.collection_product_count(c["id"])})
        if not client.find_collections(name):
            out.append({"title": name, "id": None, "kind": None, "product_count": None})
    return jsonify({"success": True, "collections": out})


@app.route("/api/<slug>/audit")
def api_audit(slug):
    """診斷 collection 資料完整性：商品數 / SKU 數 / 沒填 SKU 的商品清單"""
    brand = registry.get(slug)
    client = ShopifyClient()
    if not client.configured:
        return jsonify({"error": "SHOPIFY_SHOP / SHOPIFY_ACCESS_TOKEN 未設定"})
    out = []
    for name in brand.collections_in_use():
        for c in client.find_collections(name):
            stats = {}
            skus = client.collection_products_map(c["id"], stats)
            out.append({
                "title": name, "id": c["id"], "kind": c["kind"],
                "reported": client.collection_product_count(c["id"]),
                "fetched": stats.get("fetched", 0),
                "sku_count": len(skus),
                "no_sku": stats.get("no_sku", []),
                "duplicates": stats.get("duplicates", {}),
            })
    return jsonify({"success": True, "collections": out})


@app.route("/api/<slug>/dedup", methods=["GET", "POST"])
def api_dedup(slug):
    """
    重複 SKU 清理。GET 只回傳計畫（不寫入），POST 才執行刪除。
    POST 需要 SYNC_TOKEN（若有設定）。
    """
    brand = registry.get(slug)
    client = ShopifyClient()
    if not client.configured:
        return jsonify({"error": "SHOPIFY_SHOP / SHOPIFY_ACCESS_TOKEN 未設定"})

    execute = request.method == "POST"
    if execute and config.SYNC_TOKEN:
        supplied = request.args.get("token") or request.headers.get("X-Sync-Token", "")
        if supplied != config.SYNC_TOKEN:
            return jsonify({"error": "token 不正確或未提供"}), 403

    plan, results = [], []
    for name in brand.collections_in_use():
        for c in client.find_collections(name):
            stats = {}
            client.collection_products_map(c["id"], stats)
            plan.extend(client.dedup_plan(stats.get("duplicates", {}),
                                          keep=brand.dedup_keep))

    to_remove = sum(len(r["remove"]) for r in plan)
    if execute:
        if to_remove > config.MAX_DEDUP_DELETES and not config.ALLOW_BULK_DELETE:
            return jsonify({"error": f"要刪除 {to_remove} 件重複商品，超過門檻 "
                                     f"{config.MAX_DEDUP_DELETES} 件。確認計畫無誤後"
                                     f"設 ALLOW_BULK_DELETE=1 重跑",
                            "plan": plan})
        for row in plan:
            for r in row["remove"]:
                ok = client.delete_product(r["product_id"])
                results.append({"sku": row["sku"], "product_id": r["product_id"],
                                "deleted": ok})
    return jsonify({"success": True, "executed": execute,
                    "keep": brand.dedup_keep,
                    "groups": len(plan), "to_remove": to_remove,
                    "plan": plan, "results": results})


@app.route("/api/<slug>/migrate-sku", methods=["GET", "POST"])
def api_migrate_sku(slug):
    """
    舊 SKU 體系 → 新 SKU 的遷移。GET 只出計畫，POST 才寫入。

    來源站換過 SKU 規則時（toraya 從 handle-based 換成自家品號），
    若不遷移，現有商品會全被判定「已下架」而刪除、新資料全被當成新商品重上，
    評論、SEO 排名與既有連結全部歸零。
    """
    brand = registry.get(slug)
    client = ShopifyClient()
    if not client.configured:
        return jsonify({"error": "SHOPIFY_SHOP / SHOPIFY_ACCESS_TOKEN 未設定"})

    execute = request.method == "POST"
    if execute and config.SYNC_TOKEN:
        supplied = request.args.get("token") or request.headers.get("X-Sync-Token", "")
        if supplied != config.SYNC_TOKEN:
            return jsonify({"error": "token 不正確或未提供"}), 403

    # 官網現況：舊 SKU 樣式 → 新 SKU
    legacy_to_new = {}
    for item in brand.list_products():
        p = brand.get_product(item["url"], item["sku"])
        if not p:
            continue
        for key in brand.legacy_sku_keys(p):
            legacy_to_new[key] = {"new_sku": p.sku, "title": p.title[:50]}
    if not legacy_to_new:
        # 沒有舊體系代表 SKU 從一開始就一致，不需要遷移，這不是錯誤
        return jsonify({"success": True, "executed": False, "matched": 0,
                        "unmatched": 0, "plan": [], "unmatched_list": [],
                        "results": [], "note": f"{slug} 的 SKU 與來源站一致，不需要遷移"})

    existing = client.all_products_map()
    plan, unmatched, already = [], [], 0
    claimed = set()          # 本輪已經要改成這個新 SKU 的，避免兩件撞成同一個
    for name in brand.collections_in_use():
        for c in client.find_collections(name):
            stats = {}
            coll = client.collection_products_map(c["id"], stats)
            for old_sku, info in coll.items():
                hit = legacy_to_new.get(old_sku)
                if not hit:
                    unmatched.append({"old_sku": old_sku,
                                      "product_id": info["product_id"],
                                      "reason": "官網已無對應商品"})
                    continue
                new_sku = hit["new_sku"]
                if old_sku == new_sku:
                    # 已經是新體系了，不需要動
                    already += 1
                    continue
                if new_sku in claimed:
                    # 舊資料有兩種 SKU 形式指向同一商品（FGT-543 / FGT-000000000543），
                    # 只能遷移其中一件，另一件是重複資料，留給清理階段刪除
                    unmatched.append({"old_sku": old_sku, "new_sku": new_sku,
                                      "reason": "同一商品的重複資料，改由清理階段刪除"})
                    continue
                if new_sku in existing:
                    unmatched.append({"old_sku": old_sku, "new_sku": new_sku,
                                      "reason": "新 SKU 已被其他商品占用"})
                    continue
                claimed.add(new_sku)
                plan.append({"old_sku": old_sku, "new_sku": new_sku,
                             "variant_id": info["variant_id"],
                             "product_id": info["product_id"], "title": hit["title"]})

    results = []
    if execute:
        for row in plan:
            ok = client.update_variant_sku(row["variant_id"], row["new_sku"])
            results.append({**row, "updated": ok})
    return jsonify({"success": True, "executed": execute,
                    "matched": len(plan), "unmatched": len(unmatched),
                    "already": already,
                    "plan": plan, "unmatched_list": unmatched[:50],
                    "results": results})


@app.route("/api/<slug>/test-scrape")
def api_test_scrape(slug):
    """只爬不寫，用來驗證選擇器。?limit=5"""
    brand = registry.get(slug)
    limit = min(int(request.args.get("limit", 5)), 30)
    items = brand.list_products()
    sample = []
    for item in items[:limit]:
        p = brand.get_product(item["url"], item["sku"])
        if p:
            clean_title, _ = brand.prepare_for_translation(p)
            route = brand.resolve_route(p)
            sample.append({"sku": p.sku, "title": p.title, "clean_title": clean_title,
                           "route": route.name if route else None, "price": p.price,
                           "selling_price": p.selling_price, "in_stock": p.in_stock,
                           "images": len(p.images), "weight": p.weight,
                           "desc_len": len(p.description)})
    return jsonify({"success": True, "version": config.PLATFORM_VERSION,
                    "list_count": len(items),
                    "dropped": getattr(brand, "last_dropped", None),
                    "sample": sample})


@app.route("/api/<slug>/start", methods=["POST"])
def api_start(slug):
    if config.SYNC_TOKEN:
        supplied = (request.args.get("token")
                    or request.headers.get("X-Sync-Token", ""))
        if supplied != config.SYNC_TOKEN:
            return jsonify({"error": "token 不正確或未提供，無法觸發同步"}), 403
    runner = runner_for(slug)
    if runner.status["running"]:
        return jsonify({"error": "此品牌已在執行中"})
    if request.args.get("dry_run") == "1":
        runner.dry_run = True
    threading.Thread(target=runner.run, daemon=True).start()
    return jsonify({"success": True, "brand": slug, "dry_run": runner.dry_run})


@app.route("/api/<slug>/status")
def api_status(slug):
    return jsonify(runner_for(slug).as_dict())


# ---------- UI ----------
PAGE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GOYOUTATI 品牌同步</title><style>
*{box-sizing:border-box}body{font-family:-apple-system,"Noto Sans TC",sans-serif;
margin:0;background:#f5f6fa;color:#1a1a2e}
.wrap{max-width:940px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 20px}
.card{background:#fff;border-radius:10px;padding:20px;margin-bottom:16px;
box-shadow:0 1px 3px rgba(0,0,0,.06)}
select,button{font:inherit;padding:9px 14px;border-radius:6px;border:1px solid #dde3f0}
button{background:#3b4cca;color:#fff;border:0;cursor:pointer;margin-right:8px}
button.ghost{background:#eef0f8;color:#3b4cca}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-top:14px}
.stat{background:#f7f8fc;border-radius:8px;padding:10px 12px}
.stat b{display:block;font-size:20px}.stat span{font-size:12px;color:#777}
pre{background:#1a1a2e;color:#e8eaf0;padding:14px;border-radius:8px;
max-height:340px;overflow:auto;font-size:12px;line-height:1.6;margin:0;white-space:pre-wrap}
.err{color:#ff8a8a}.ok{color:#7ee787}.warn{color:#ffd479}
.bar{height:6px;background:#eef0f8;border-radius:99px;overflow:hidden;margin-top:12px}
.bar i{display:block;height:100%;background:#3b4cca;width:0;transition:width .3s}
label.chk{font-size:13px;color:#555}
</style></head><body><div class="wrap">
<h1>GOYOUTATI 品牌同步</h1>
<div class="card"><div class="row">
<select id="brand"></select>
<button class="ghost" onclick="testShopify()">測試連線</button>
<button class="ghost" onclick="testTranslate()">測試翻譯</button>
<button class="ghost" onclick="testScrape()">測試爬取</button>
<button class="ghost" onclick="checkCollections()">檢查 Collection</button>
<button class="ghost" onclick="audit()">資料健檢</button>
<button class="ghost" onclick="showSchedule()">排程狀態</button>
<button class="ghost" onclick="migrateSku(false)">SKU 遷移計畫</button>
<button class="ghost" onclick="migrateSku(true)">執行 SKU 遷移</button>
<button class="ghost" onclick="dedup(false)">重複清理計畫</button>
<button class="ghost" onclick="dedup(true)">執行重複清理</button>
<button id="go" onclick="start()">開始同步</button>
<label class="chk"><input type="checkbox" id="dry"> dry-run（不寫入 Shopify）</label>
</div>
<div class="bar"><i id="bar"></i></div>
<div class="stats" id="stats"></div></div>
<div class="card"><pre id="log">等待開始...</pre></div>
</div><script>
let timer=null;
const el=i=>document.getElementById(i);
function log(m,c){const p=el('log');p.innerHTML+='\\n'+(c?'<span class="'+c+'">'+m+'</span>':m);p.scrollTop=p.scrollHeight}
async function call(path,opt){
  const r=await fetch(path,opt);const t=await r.text();
  try{return {ok:true,status:r.status,data:JSON.parse(t)}}
  catch(e){return {ok:false,status:r.status,text:t.slice(0,300)}}
}
async function boot(){
  const r=await call('/api/brands');
  if(!r.ok){log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');return}
  el('brand').innerHTML=r.data.brands.map(b=>'<option value="'+b.slug+'">'+b.name+
    (b.schedule?' ⏰'+b.schedule:'')+'</option>').join('');
  log('已載入 '+r.data.brands.length+' 個品牌 / Shopify API '+r.data.api_version
      +' / 版本 '+(r.data.version||'未標示'),'ok');
  if(r.data.token_required&&!new URLSearchParams(location.search).get('token'))
    log('⚠ 此服務需要 token，請用 ?token=... 開啟本頁，否則無法觸發同步','warn');
  if(r.data.dry_run){el('dry').checked=true;log('環境變數 DRY_RUN 已啟用','warn')}
}
const slug=()=>el('brand').value;
async function testShopify(){log('測試連線...');const r=await call('/api/'+slug()+'/test-shopify');
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  r.data.success?log('✓ '+r.data.shop+'（API '+r.data.api_version+'）','ok')
                :log('✗ '+(r.data.error||JSON.stringify(r.data)),'err')}
async function testTranslate(){log('測試翻譯...');const r=await call('/api/'+slug()+'/test-translate');
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  r.data.success?log('✓ '+r.data.title+'（model: '+r.data.model+'）','ok')
                :log('✗ '+(r.data.error||JSON.stringify(r.data)),'err')}
async function testScrape(){log('測試爬取（約 30 秒）...');
  const r=await call('/api/'+slug()+'/test-scrape?limit=5');
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  if(!r.data.success)return log('✗ '+(r.data.error||''),'err');
  log('✓ 列表 '+r.data.list_count+' 件，抽樣：','ok');
  r.data.sample.forEach(s=>{
    log('   '+s.sku+' ¥'+s.price+'→¥'+s.selling_price+(s.in_stock?' 有貨':' 缺貨')+
        ' 圖'+s.images+' 說明'+s.desc_len+'字'+(s.route?' ['+s.route+']':''));
    log('      原: '+s.title.slice(0,44));
    log('      清: '+s.clean_title.slice(0,44))})}
async function checkCollections(){log('檢查 Collection...');
  const r=await call('/api/'+slug()+'/collections');
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  if(r.data.error)return log('✗ '+r.data.error,'err');
  r.data.collections.forEach(c=>log(c.id?('   '+c.title+' → id '+c.id+' ('+c.kind+') 商品 '+c.product_count+' 件')
    :('   '+c.title+' → 不存在'), c.id?'ok':'warn'))}
async function showSchedule(){
  const r=await call('/api/schedule');
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  if(!r.data.enabled)return log('排程已停用（SCHEDULER_ENABLED=0）','warn');
  if(!r.data.jobs.length)return log('沒有品牌設定排程','warn');
  log('排程（日本時間）：','ok');
  r.data.jobs.forEach(j=>log('   '+j.at+' '+j.name+
    (j.last_run?('｜上次 '+j.last_run+'：'+(j.last_result||'')):'｜尚未執行')))}
async function audit(){log('資料健檢（約 30 秒）...');
  const r=await call('/api/'+slug()+'/audit');
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  if(r.data.error)return log('✗ '+r.data.error,'err');
  r.data.collections.forEach(c=>{
    const okc=(c.reported===c.fetched&&c.no_sku.length===0);
    log('   '+c.title+' → 回報 '+c.reported+' / 抓到 '+c.fetched+' / 有SKU '+c.sku_count,okc?'ok':'warn');
    if(c.reported!==c.fetched)log('      ⚠ 分頁可能漏抓 '+(c.reported-c.fetched)+' 件','err');
    c.no_sku.slice(0,20).forEach(x=>log('      無SKU: ['+x.status+'] '+x.title+' (id '+x.product_id+')','warn'));
    if(c.no_sku.length>20)log('      ...另外 '+(c.no_sku.length-20)+' 件','warn');
    const dups=Object.entries(c.duplicates||{});
    if(dups.length){log('      ⚠ 重複 SKU '+dups.length+' 組：','err');
      dups.slice(0,15).forEach(([sku,arr])=>{
        log('        SKU '+sku+' 有 '+arr.length+' 件商品：','warn');
        arr.forEach(x=>log('           id '+x.product_id+' ['+x.status+'] ¥'+x.price+
          ' 建立於 '+(x.created_at||'?').slice(0,10)+' '+x.title))});
      if(dups.length>15)log('        ...另外 '+(dups.length-15)+' 組','warn')}})}
async function migrateSku(exec){
  if(exec&&!confirm('將改寫現有商品的 SKU，確定執行？請先看過計畫。'))return;
  log(exec?'執行 SKU 遷移...':'產生 SKU 遷移計畫（約 30 秒）...');
  const tok=new URLSearchParams(location.search).get('token');
  const r=await call('/api/'+slug()+'/migrate-sku'+(tok?('?token='+tok):''),
                     exec?{method:'POST'}:undefined);
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  if(r.data.error)return log('✗ '+r.data.error,'err');
  if(r.data.note)return log('✓ '+r.data.note,'ok');
  if(r.data.already)log('已是新 SKU、不需遷移：'+r.data.already+' 件','ok');
  log('可對應 '+r.data.matched+' 件，無法對應 '+r.data.unmatched+' 件',
      r.data.matched?'ok':'warn');
  r.data.plan.forEach(x=>log('   '+x.old_sku+' → '+x.new_sku+'  '+x.title));
  (r.data.unmatched_list||[]).forEach(x=>log('   ✗ '+x.old_sku+
      ' ('+(x.reason||'?')+')','warn'));
  if(r.data.executed)log('已更新 '+r.data.results.filter(x=>x.updated).length+' 件','ok')}
async function dedup(exec){
  if(exec&&!confirm('將永久刪除重複商品，確定執行？請先看過清理計畫。'))return;
  log(exec?'執行重複清理...':'產生重複清理計畫...');
  const tok=new URLSearchParams(location.search).get('token');
  const q=tok?('?token='+tok):'';
  const r=await call('/api/'+slug()+'/dedup'+q,exec?{method:'POST'}:undefined);
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  if(r.data.error){log('✗ '+r.data.error,'err');
    (r.data.plan||[]).slice(0,10).forEach(p=>log('   '+p.sku+' 保留 '+p.keep.product_id+
      '，刪除 '+p.remove.map(x=>x.product_id).join(', ')));return}
  log('共 '+r.data.groups+' 組重複，預計刪除 '+r.data.to_remove+' 件'+
      '（保留規則：'+({newest:'最新',oldest:'最舊',auto:'自動判斷'}[r.data.keep]||r.data.keep)+'）',
      r.data.to_remove?'warn':'ok');
  r.data.plan.forEach(p=>{
    log('   SKU '+p.sku+(p.mode?'（留'+(p.mode==='newest'?'新':'舊')+'）':''),'ok');
    log('      保留 '+p.keep.product_id+' ['+p.keep.status+'] '+
        (p.keep.created_at||'').slice(0,10)+' ¥'+p.keep.price+' '+p.keep.title);
    p.remove.forEach(x=>log('      刪除 '+x.product_id+' ['+x.status+'] '+
        (x.created_at||'').slice(0,10)+' ¥'+x.price+' '+x.title,'warn'))});
  if(r.data.executed)log('已刪除 '+r.data.results.filter(x=>x.deleted).length+' 件','ok')}
async function start(){
  const params=new URLSearchParams();
  if(el('dry').checked)params.set('dry_run','1');
  const tok=new URLSearchParams(location.search).get('token');
  if(tok)params.set('token',tok);
  const q=params.toString()?('?'+params.toString()):'';
  const r=await call('/api/'+slug()+'/start'+q,{method:'POST'});
  if(!r.ok)return log('✗ HTTP '+r.status+' 非 JSON：'+r.text,'err');
  if(r.data.error)return log('✗ '+r.data.error,'err');
  log('開始同步 '+slug()+(r.data.dry_run?'（dry-run）':''),'ok');
  el('go').disabled=true;let seen=0;
  timer=setInterval(async()=>{
    const s=await call('/api/'+slug()+'/status');if(!s.ok)return;
    const d=s.data;
    el('bar').style.width=(d.total?d.progress/d.total*100:0)+'%';
    el('stats').innerHTML=[['已上架',d.uploaded],['已跳過',d.skipped],['缺貨',d.out_of_stock],
      ['售價更新',d.price_updated],['已刪除',d.deleted],['翻譯失敗',d.translation_failed],
      ['轉草稿',d.drafted],['重新上架',d.reactivated],
      ['日文重試',d.kana_retried],['日文退回',d.kana_rejected]]
      .map(([k,v])=>'<div class="stat"><b>'+v+'</b><span>'+k+'</span></div>').join('')+
      '<div class="stat"><b>'+d.progress+'/'+d.total+'</b><span>'+d.phase+'</span></div>';
    (d.log||[]).slice(seen).forEach(m=>log(m));seen=(d.log||[]).length;
    if(d.done){clearInterval(timer);el('go').disabled=false;
      log('=== '+d.phase+' ===',d.errors.length?'warn':'ok')}
  },1500)}
boot();
</script></body></html>"""


@app.route("/")
def index():
    return PAGE

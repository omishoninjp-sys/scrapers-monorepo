"""
排程器。取代舊服務裡各自實作的 BackgroundScheduler。

品牌用 `schedule = "10:00"`（JST）宣告每日同步時間，None 表示只手動。

設計重點：

1. **序列執行，不併行。** 多個品牌撞在同一個時間點時排隊跑完 ——
   併行會同時打 OpenAI 與 Shopify，很容易觸發限流，而限流的重試又會拖長時間。

2. **同一天只跑一次。** 用「已執行日期」比對，不是靠 sleep 精準對時。
   容器重啟、時鐘漂移、執行超過一分鐘都不會造成漏跑或重複跑。

3. **跳過補跑。** 若服務在排程時間之後才啟動（例如中午重新部署），
   當天不會立刻補跑一次 —— 那通常不是預期行為，而且可能撞上手動操作。
   要補跑就手動按。

4. **只在單一 worker 執行。** Procfile 用 `-w 1`；若改成多 worker，
   每個 worker 都會各自排程，變成重複執行。要擴充 worker 數時
   必須改用外部排程（Zeabur Cron 打 /api/<slug>/start）。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config, registry

JST = timezone(timedelta(hours=9))


class Scheduler:
    def __init__(self, runner_factory, interval=30):
        self.runner_factory = runner_factory
        self.interval = interval
        self.last_run = {}        # slug -> 'YYYY-MM-DD'（JST）
        self.last_result = {}     # slug -> 摘要字串
        self._thread = None
        self._stop = threading.Event()

    # ---- 排程表 ----
    @staticmethod
    def entries():
        out = []
        for slug in registry.all_slugs():
            brand = registry.get(slug)
            if brand.schedule:
                out.append({"slug": slug, "name": brand.name, "at": brand.schedule})
        return sorted(out, key=lambda x: (x["at"], x["slug"]))

    def status(self):
        return [{**e,
                 "last_run": self.last_run.get(e["slug"]),
                 "last_result": self.last_result.get(e["slug"])}
                for e in self.entries()]

    # ---- 主迴圈 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        # 啟動當下已經過的時間點視為今天跑過了，避免重新部署就補跑一次
        now = datetime.now(JST)
        today = now.strftime("%Y-%m-%d")
        for entry in self.entries():
            if now.strftime("%H:%M") >= entry["at"]:
                self.last_run.setdefault(entry["slug"], today)
                self.last_result.setdefault(entry["slug"], "啟動前已過排程時間，今日跳過")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[scheduler] 已啟動，排程 {len(self.entries())} 個品牌", flush=True)

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[scheduler] 迴圈例外: {type(e).__name__}: {e}", flush=True)
            self._stop.wait(self.interval)

    def _tick(self):
        now = datetime.now(JST)
        today = now.strftime("%Y-%m-%d")
        hhmm = now.strftime("%H:%M")
        for entry in self.entries():
            slug = entry["slug"]
            if self.last_run.get(slug) == today:
                continue
            if hhmm < entry["at"]:
                continue
            self.last_run[slug] = today       # 先記錄，失敗也不當天重試
            self._run(slug)

    def _run(self, slug):
        runner = self.runner_factory(slug)
        if runner.status.get("running"):
            self.last_result[slug] = "上一輪仍在執行，本次跳過"
            print(f"[scheduler] {slug} 仍在執行，跳過", flush=True)
            return
        print(f"[scheduler] 開始同步 {slug}", flush=True)
        started = time.time()
        try:
            runner.run()          # 同步執行，確保品牌之間不會併行
            st = runner.status
            self.last_result[slug] = (
                f"上架 {st['uploaded']}／改價 {st['price_updated']}／"
                f"草稿 {st['drafted']}／刪除 {st['deleted']}／"
                f"錯誤 {len(st['errors'])}（{int(time.time() - started)}s）")
        except Exception as e:
            self.last_result[slug] = f"例外中止：{type(e).__name__}: {e}"
        print(f"[scheduler] {slug} 完成：{self.last_result[slug]}", flush=True)


_scheduler = None


def get_scheduler(runner_factory):
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(runner_factory)
    return _scheduler


def enabled():
    return config.SCHEDULER_ENABLED

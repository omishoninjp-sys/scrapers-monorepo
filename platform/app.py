"""單一入口。取代 12 個 Zeabur 服務。"""
import os

from core.web import app, start_scheduler

# 模組層級呼叫：Procfile 用 `gunicorn app:app`，gunicorn 是 import 這個模組
# 而不是執行它，所以不能放進 if __name__ == "__main__"。
# 反過來，只 import core.web 的工具（check_ui.py）不會走到這裡，排程器就不會被誤觸。
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

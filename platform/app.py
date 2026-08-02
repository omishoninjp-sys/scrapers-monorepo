"""單一入口。取代 12 個 Zeabur 服務。"""
import os

from core.web import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

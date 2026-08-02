"""
前端 JS 語法檢查。core/web.py 的 PAGE 是一大段內嵌 JS，
Python 語法檢查看不到它 —— 一個括號錯位就會讓整段腳本掛掉，
畫面上只剩空的品牌下拉和「等待開始...」，看起來像伺服器沒起來。

用法：python check_ui.py
需要 node；沒有 node 時退回括號平衡檢查。
"""
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, ".")
from core.web import PAGE  # noqa: E402

js = PAGE[PAGE.index("<script>") + 8:PAGE.index("</script>")]

if shutil.which("node"):
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                     delete=False) as f:
        f.write(js)
        path = f.name
    result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    if result.returncode == 0:
        print("✅ JS 語法通過")
    else:
        print("✗ JS 語法錯誤：")
        print(result.stderr)
        sys.exit(1)
else:
    depth = {"(": 0, "[": 0, "{": 0}
    pairs = {")": "(", "]": "[", "}": "{"}
    in_str = None
    esc = False
    for i, ch in enumerate(js):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in "'\"`":
            in_str = ch
            continue
        if ch in depth:
            depth[ch] += 1
        elif ch in pairs:
            depth[pairs[ch]] -= 1
            if depth[pairs[ch]] < 0:
                print(f"✗ 第 {js[:i].count(chr(10)) + 1} 行多出一個 {ch}")
                sys.exit(1)
    if any(v for v in depth.values()):
        print("✗ 括號不平衡:", depth)
        sys.exit(1)
    print("✅ 括號平衡（未安裝 node，僅做基本檢查）")

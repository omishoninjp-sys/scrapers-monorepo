"""
修正 scrapers-monorepo 各品牌 app.py 的翻譯 prompt f-string bug。

問題：prompt 是 f-string，裡面的 JSON 範例
    {"title":"翻譯後的商品名稱", ...}
會被 Python 當成替換欄位，"title" 後面的冒號被解讀成格式規格，
執行時丟出 ValueError: Invalid format specifier，翻譯功能完全無法使用。

修法：把該行的大括號改成 {{ }} 轉義，輸出結果不變。

用法（在 repo 根目錄執行）：
    python fix_translate_prompt.py           # 檢查並修正
    python fix_translate_prompt.py --dry-run # 只檢查不寫入
"""
import glob
import re
import sys

DRY_RUN = "--dry-run" in sys.argv
PATTERN = re.compile(r'^\{"title":.*?"meta_description":.*?\}$', re.M)

changed, skipped, missing = [], [], []

for path in sorted(glob.glob("*/app.py")):
    src = open(path, encoding="utf-8").read()

    if re.search(r'^\{\{"title":', src, re.M):
        skipped.append(path)
        continue

    hits = PATTERN.findall(src)
    if not hits:
        missing.append(path)
        continue

    new_src = src
    for h in hits:
        new_src = new_src.replace(h, "{" + h + "}")

    # 語法檢查後才寫入
    compile(new_src, path, "exec")

    if not DRY_RUN:
        open(path, "w", encoding="utf-8").write(new_src)
    changed.append(path)

print(f"{'[dry-run] ' if DRY_RUN else ''}已修正 {len(changed)} 個檔案")
for p in changed:
    print("  ✔", p)
if skipped:
    print(f"已是修正版，略過 {len(skipped)} 個：{', '.join(skipped)}")
if missing:
    print(f"⚠ 找不到 prompt 範本，需手動檢查 {len(missing)} 個：{', '.join(missing)}")

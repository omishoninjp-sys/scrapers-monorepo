"""
詞彙表地雷檢查：短鍵會咬進長詞。

apply_glossary 是長鍵優先，所以只要長詞本身也在表裡就安全；
問題出在「短鍵在表裡、長詞不在」的組合 ——
yokumoku 曾把「モン→Mont」用在「フルーツレモン」上，
翻出「フルーツレMont」這種東西。

用法：python check_glossary.py
"""
import sys

sys.path.insert(0, ".")
from core import registry  # noqa: E402

# 常見的日文長詞，短鍵咬進去就會壞
COMMON = [
    "レモン", "モンブラン", "サーモン", "アーモンド", "シナモン", "マカロン", "メロン",
    "プレーン", "クリーム", "チーズ", "バター", "ミルク", "セット", "ケーキ", "クッキー",
    "チョコレート", "ストロベリー", "キャラメル", "ヘーゼルナッツ", "マンゴー",
    "パイナップル", "ラングドシャ", "フィナンシェ", "ミルフィユ", "シガール",
]

issues = 0
for slug in registry.all_slugs():
    brand = registry.get(slug)
    bad = []
    # 單字元片假名鍵幾乎一定會咬進別的詞（「ラ」→ ラング 變成 Laング）
    solo = [k for k in brand.glossary
            if len(k) == 1 and "\u30a1" <= k <= "\u30fa"]
    if solo:
        issues += len(solo)
        print(f"⚠ {slug}：單字元片假名鍵 {solo} —— 幾乎一定會咬進別的詞，請改用長鍵")
    for key in brand.glossary:
        if len(key) > 3:
            continue
        for word in COMMON:
            if key in word and key != word and word not in brand.glossary:
                bad.append((key, word))
    if bad:
        issues += len(bad)
        print(f"⚠ {slug}")
        for key, word in bad:
            print(f"    「{key}」會咬進「{word}」—— 請把「{word}」也加進詞彙表，"
                  f"或改用更長的鍵")

print("✅ 沒有發現詞彙表地雷" if not issues else f"\n共 {issues} 處需要處理")
sys.exit(1 if issues else 0)

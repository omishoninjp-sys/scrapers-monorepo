"""來源文字清理。在送去翻譯之前處理掉官網標題的結構性雜訊。"""
import re

# 官網標題常把「商品名」和「規格名」用這些符號黏在一起
# 刻意不含「・」：它在規格括號內是正常的列舉分隔，切開會破壞內容
SEPARATORS = "●○□■◆"

# 日本國內限定的字樣，對台灣客人是錯誤資訊，必須移除
JP_DOMESTIC_PATTERNS = [
    r"【?国内送料無料】?", r"【?國內免運】?", r"【?送料無料】?",
    r"【?通販限定】?", r"【?お一人様\d+回?\d*個?限り】?",
    r"※?クール便[^。]*", r"【?日本国内[^】]*】?",
]

# 刻意排除 U+30FB「・」KATAKANA MIDDLE DOT —— 它落在片假名區塊內，
# 但實際是標點符號，中文規格列舉也常用（沙拉風味・和三盆風味）。
# 把它算成假名會誤退大量已經翻譯乾淨的標題。
KANA_CHARS = r"\u3041-\u3096\u3099-\u309F\u30A1-\u30FA\u30FC-\u30FF"
KANA_RE = re.compile(f"[{KANA_CHARS}]")


def has_kana(text):
    """只要出現任何平假名或片假名就回 True。比佔比門檻嚴格，用於上架前把關。"""
    return bool(text) and bool(KANA_RE.search(text))


def kana_words(text):
    """回傳殘留的假名詞，方便 log 指出問題在哪"""
    return re.findall(f"[{KANA_CHARS}][{KANA_CHARS}\u4e00-\u9fff]*", text or "")


def _common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return a[:n]


def dedupe_repeats(text):
    """對外版本。去讀音、套詞彙表之後要再跑一次 —— 重複的單元在那些步驟後
    才會縮到偵測長度內（古今凉の音（ここんすずのね） 化粧箱（大）×2 就是這種）。"""
    return _dedupe_adjacent(text or "")


def _dedupe_adjacent(text):
    """
    去掉相鄰重複片段。官網標題例：
      「定家の月 大缶□大缶（サラダ仕立て14枚...）」→ 大缶 重複
      「詰め替え袋詰め替え袋（39枚）」
    只處理「緊鄰重複」，避免誤刪正常疊字。
    """
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"(?<![\u3040-\u309F])(.{2,24}?)\s*\1", r"\1", text)
    return text


def clean_source_title(title, extra_patterns=()):
    """標題送翻譯前的清理：拆分隔符 → 去重複 → 去日本國內字樣 → 收斂空白"""
    if not title:
        return ""
    text = title

    for pattern in list(JP_DOMESTIC_PATTERNS) + list(extra_patterns):
        text = re.sub(pattern, "", text)

    # 分隔符後面通常是規格名的重複，切開後只保留較長的一段與不重複的部分
    if any(sep in text for sep in SEPARATORS):
        parts = [p.strip() for p in re.split(f"[{SEPARATORS}]", text) if p.strip()]
        # 先留最長的，再丟掉被它包含的較短片段（分隔符後面通常是規格名的殘缺重複）
        keep, tails = [], []
        for p in sorted(parts, key=len, reverse=True):
            if any(p in k for k in keep):
                continue
            # 兩段共用 4 字以上的開頭 → 同一商品名的不同殘缺形式
            # （京・七味あられ 袋詰め ／ 京・七味あられ（100g））。
            # 留較長的那段，但把短段獨有的尾巴接回去，否則會丟失規格
            # （袋詰め = 袋裝，罐裝／袋裝版本會塌成同一個標題）。
            twin = next((k for k in keep if p[:4] and k.startswith(p[:4])), None)
            if twin:
                tail = p[len(_common_prefix(p, twin)):].strip(" 　（(")
                if tail and tail not in twin:
                    tails.append(tail)
                continue
            keep.append(p)
        ordered = [p for p in parts if p in keep]
        text = " ".join(ordered + tails)

    text = _dedupe_adjacent(text)
    text = re.sub(r"[（(]\s*[)）]", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" 　-–—")
    return text


def clean_source_description(desc, extra_patterns=()):
    if not desc:
        return ""
    text = desc
    for pattern in list(JP_DOMESTIC_PATTERNS) + list(extra_patterns):
        text = re.sub(pattern, "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


_READING_RE = None


def strip_kana_readings(text):
    """
    移除純假名的讀音註記。日本商品名常附振假名或別記：
      古今凉の音（ここんすずのね）  → 古今凉の音
      寄石恋-いしによするこい-      → 寄石恋
    這是給日本人看的讀音，中文商品頁不需要，留著只會變成翻不掉的殘留。
    只在括號／夾號內**完全是假名**時才移除，避免誤刪規格說明。
    """
    global _READING_RE
    if _READING_RE is None:
        k = KANA_CHARS + r"\u30FB\u30FC・\s"
        # 夾號規則只認 - 與 －：長音符 ー 是片假名的一部分
        # （メープル、クッキー），拿它當夾號會把商品名本身吃掉。
        _READING_RE = [
            re.compile(f"[（(]\\s*[{k}]{{2,}}\\s*[)）]"),
            re.compile(f"(?<![{KANA_CHARS}])[-－]\\s*[{KANA_CHARS}]{{2,}}\\s*[-－]"),
            re.compile(f"[〈《]\\s*[{k}]{{2,}}\\s*[〉》]"),
        ]
    if not text:
        return text
    for pattern in _READING_RE:
        text = pattern.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def apply_glossary(text, glossary):
    """
    用品牌詞彙表把來源文字裡的固定用語先換成中文，再交給翻譯模型。

    這比在 prompt 裡寫規則可靠得多：商品線名稱（ご愛食用袋）、和歌典故名
    （いづみ流るゝ）、規格用語（仕立て、ヶ入り）這類詞，模型每次翻的結果都不同，
    甚至同一次請求內前後不一致。改成確定性替換後模型只需處理剩下的描述性文字。

    長鍵優先，避免短鍵先吃掉長詞的一部分。
    """
    if not text or not glossary:
        return text
    for jp in sorted(glossary, key=len, reverse=True):
        text = text.replace(jp, glossary[jp])
    return text


# 日本新字體 → 繁體。模型偶爾會把來源的日本漢字原封不動帶進輸出，
# 這些字在繁中語境是錯字（単品 / 塩味 / 包装）。只收在商品文案實際出現過、
# 且對應關係明確的字，避免過度轉換。
JP_TO_TRAD = {
    "単": "單", "塩": "鹽", "内": "內", "装": "裝", "価": "價", "数": "數",
    "実": "實", "発": "發", "図": "圖", "沢": "澤", "気": "氣", "楽": "樂",
    "続": "續", "経": "經", "済": "濟", "鉄": "鐵", "応": "應", "変": "變",
    "帯": "帶", "団": "團", "圧": "壓", "覚": "覺", "観": "觀", "関": "關",
    "験": "驗", "駅": "驛", "齢": "齡", "万": "萬", "竜": "龍", "医": "醫",
    "号": "號", "対": "對", "専": "專", "帰": "歸", "検": "檢", "縁": "緣",
    "択": "擇", "沖": "沖", "殻": "殼", "麦": "麥", "乗": "乘", "権": "權",
}
_JP_TABLE = str.maketrans(JP_TO_TRAD)


def normalize_kanji(text):
    """把殘留的日本新字體轉成繁體。用在翻譯**輸出**上（來源端由 glossary 處理）。"""
    return text.translate(_JP_TABLE) if text else text


def japanese_kanji_found(text):
    """診斷用：回傳文字中出現的日本新字體"""
    return sorted({c for c in (text or "") if c in JP_TO_TRAD})

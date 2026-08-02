"""翻譯 + SEO。prompt 用字串串接組裝，JSON 範本永遠不進 f-string。"""
import json
import re
import requests

from . import config

_ENDPOINT = "https://api.openai.com/v1/chat/completions"

_BASE_RULES = [
    "【強制禁止日文】所有輸出必須是繁體中文或英文，不可出現任何平假名或片假名",
    "SEO 關鍵字必須自然融入",
    "只回傳 JSON，不得有任何其他文字",
]


def build_prompt(title, description, brand_rules=None):
    """組 prompt。刻意不用 f-string 包 JSON_FORMAT，避免大括號被當成替換欄位。"""
    rules = list(brand_rules or []) + _BASE_RULES
    numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))
    return "\n".join([
        "你是專業的日本商品翻譯和 SEO 專家。將以下日本商品資訊翻譯成繁體中文並優化 SEO。",
        "",
        "商品名稱：" + (title or ""),
        "商品說明：" + (description or "")[:1500],
        "",
        "只回傳此 JSON 格式，不加 markdown、不加任何其他文字：",
        config.JSON_FORMAT,
        "",
        "規則：",
        numbered,
    ])


def translate(title, description, brand_rules=None, model=None, api_key=None):
    """回傳 dict：success / title / description / page_title / meta_description / error"""
    key = api_key or config.OPENAI_API_KEY
    if not key:
        return {"success": False, "error": "OPENAI_API_KEY 未設定"}

    prompt = build_prompt(title, description, brand_rules)
    used_model = model or config.TRANSLATE_MODEL
    try:
        r = requests.post(_ENDPOINT, timeout=60,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": used_model,
                                "messages": [
                                    {"role": "system",
                                     "content": "你是專業的日本商品翻譯和 SEO 專家，只回傳 JSON。"},
                                    {"role": "user", "content": prompt}],
                                "temperature": 0.3})
        if r.status_code != 200:
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}",
                    "model": used_model}
        content = r.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
        data = json.loads(content)
        missing = [k for k in ("title", "description", "page_title", "meta_description")
                   if not data.get(k)]
        if missing:
            return {"success": False, "error": f"回應缺少欄位: {', '.join(missing)}",
                    "model": used_model}
        data["success"] = True
        data["model"] = used_model
        return data
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON 解析失敗: {e}", "model": used_model}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}", "model": used_model}


def is_japanese_text(text, ignore=()):
    """偵測殘留日文。ignore 放品牌漢字名（如「小倉山荘」）避免誤判。"""
    if not text:
        return False
    check = text
    for word in ignore:
        check = check.replace(word, "")
    check = check.strip()
    if not check:
        return False
    kana = len(re.findall(r"[\u3040-\u309F\u30A0-\u30FF]", check))
    han = len(re.findall(r"[\u4e00-\u9fff]", check))
    total = len(re.sub(r"[\s\d\W]", "", check))
    if total == 0:
        return False
    return kana > 0 and (kana / total > 0.3 or han == 0)

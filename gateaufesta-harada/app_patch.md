# app.py 修改說明

在 `@app.route('/api/test-shopify')` **之前**，加入以下程式碼：

```python
@app.route('/api/test-translate')
def test_translate():
    """測試翻譯功能"""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({
            'error': 'OPENAI_API_KEY 環境變數未設定',
            'key_exists': False
        })
    
    # 顯示 key 的前後幾個字元方便確認
    key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "太短"
    
    result = translate_with_chatgpt("ゴーフル10S", "神戸の銘菓ゴーフルの詰め合わせです")
    result['key_preview'] = key_preview
    result['key_length'] = len(api_key)
    
    return jsonify(result)
```

部署後訪問 `/api/test-translate` 即可看到：
- `key_exists`: API Key 是否存在
- `key_preview`: Key 的前8碼...後4碼（確認是否正確）
- `success`: 翻譯是否成功
- `title`: 翻譯結果（應該是繁體中文）

如果 `success: false`，就是 OpenAI API 的問題（key 無效或餘額不足）。

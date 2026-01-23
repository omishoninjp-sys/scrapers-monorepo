# Japanese Confectionery Scrapers

日本菓子代購爬蟲 Monorepo

## 爬蟲列表

| 目錄 | 品牌 | Collection 名稱 |
|------|------|-----------------|
| `ogura` | 小倉山莊 | 小倉山莊 |
| `kobe-fugetsudo` | 神戶風月堂 | 神戶風月堂 |
| `bankaku` | 坂角總本舖 | 坂角總本舖 |
| `shiseido` | 資生堂PARLOUR | 資生堂PARLOUR |
| `sugar-butter-tree` | 砂糖奶油樹 | 砂糖奶油樹 |
| `toraya` | 虎屋羊羹 | 虎屋羊羹 |

## Zeabur 部署方式

1. 將此 repo 連結到 Zeabur
2. 建立多個 Service，每個 Service 設定不同的 **Root Directory**
3. 每個 Service 設定環境變數：

```
OPENAI_API_KEY=你的key
SHOPIFY_ACCESS_TOKEN=你的token
SHOPIFY_SHOP=你的商店名稱
```

## 共同規則

- 成本價 < ¥1000 不上架
- 無庫存跳過
- 官網下架 → Shopify 設為草稿
- 重量取材積重量與實際重量較大值
- 售價公式：(進貨價 + 重量×1250) / 0.7

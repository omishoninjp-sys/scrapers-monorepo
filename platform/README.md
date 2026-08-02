# GOYOUTATI 品牌同步平台

把 12 支各自獨立的 scraper 收斂成單一服務。每個品牌只剩設定 + 最多兩個 parse 方法。

## 目前狀態

| 品牌 | 狀態 | 佈景 |
|---|---|---|
| ogura 小倉山莊 | ✅ 已遷移，實測與舊版逐件一致 | EcbeingClassic |
| sugar-butter-tree 砂糖奶油樹 | ✅ 已遷移 | EcbeingSummary |
| cocoris | ✅ 已遷移 | Sucrey |
| francais | ✅ 已遷移 | Sucrey |
| maple-mania 楓糖男孩 | ✅ 已遷移 | Sucrey |
| bankaku 坂角總本舖 | ✅ 已遷移 | EcbeingClassic |
| gateaufesta-harada | ✅ 已遷移 | EcbeingClassic（多分類）|
| toraya 虎屋 | ✅ 已遷移 | ShopifySource |
| yokumoku | ✅ 已遷移 | ShopifySource |
| hontaka 本高砂屋 | ✅ 已遷移 | MakeShop |
| kobe-fugetsudo 神戶風月堂 | ✅ 已遷移 | MakeShop |
| shiseido 資生堂 PARLOUR | ✅ 已遷移 | ShiseidoParlour |

舊的 12 支服務**都沒有動**，可以並行運行直到驗證完畢。

## 結構

```
app.py                 單一入口
core/
  config.py            所有環境變數集中在此
  models.py            Product dataclass + 定價
  shopify.py           ShopifyClient
  translate.py         翻譯 + 日文殘留偵測
  brand.py             BaseBrand（品牌契約）
  registry.py          自動掃描 brands/ 載入
  sync.py              SyncRunner（取代 12 份 run_scrape）
  shipping.py          運費表 HTML（原本 12 份完全相同的副本）
  web.py               Flask UI + API
platforms/
  makeshop.py          MakeShopBrand（hontaka / kobe-fugetsudo）
  shiseido_parlour.py  ShiseidoParlour（自建站，單一品牌）
  shopify_source.py    ShopifySourceBrand（toraya / yokumoku）
  sucrey.py            SucreyBrand（cocoris / francais / maple-mania）
  ecbeing.py           EcbeingBrand / EcbeingClassic / EcbeingSummary
  sucrey.py            SucreyBrand（cocoris / francais / maple-mania 共用）
brands/
  ogura.py
  sugar_butter_tree.py
```

## 新增品牌

1. 判斷目標站屬於哪個平台。同平台 → 繼承現成 adapter，只填設定。
2. 新平台 → 在 `platforms/` 新增一個 adapter，實作 `list_page_url` /
   `parse_list` / `parse_detail`。
3. 在 `brands/` 放一個檔案，`registry` 會自動撿到，不需要註冊。

## 環境變數

| 變數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| `SHOPIFY_SHOP` | ✔ | — | 只填 shop name，不含 `.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | ✔ | — | |
| `SHOPIFY_API_VERSION` | | `2024-01` | **見下方警告** |
| `OPENAI_API_KEY` | ✔ | — | |
| `SYNC_TOKEN` | 建議 | 空 | 設了才需帶 token 觸發同步 |
| `TRANSLATE_MODEL` | | `gpt-4o-mini` | |
| `MAX_CONSECUTIVE_TRANSLATION_FAILURES` | | `3` | |
| `DRY_RUN` | | 關 | `1` 則完全不寫入 Shopify（含不建立 collection）|
| `MAX_DELETE_ABS` | | `25` | 單次刪除上限（絕對值）|
| `MAX_DELETE_RATIO` | | `0.2` | 單次刪除上限（占 collection 比例）|
| `ALLOW_BULK_DELETE` | | 關 | `1` 才允許超過門檻的刪除（**用完就拿掉**）|

首次同步會累積大量歷史下架商品，必然超過門檻，這時用一次性覆寫而不是調高預設值：
穩定運行後每次刪個位數才正常，`25` 這個門檻在穩定期是對的。

## ⚠️ Shopify API 版本

`2024-01` 這個版本號是 load-bearing 的，**不要隨手升版**。

這版早已退役，目前是靠 Shopify 的 fall-forward 機制自動由最舊的支援版本服務，
等於實際行為版本是浮動的。而 REST `products.json` 帶 `variants` 建立商品的行為
在較新版本已經變更（變體會被靜默丟棄）。

要升版必須同時把 `ShopifyClient.create_product` 改走 GraphQL `productSet` mutation。
`sync.py` 已經在 Shopify 沒回傳 variant 時記錄警告，就是為了讓這個問題不再無聲發生。
這是獨立的工作項，不要和這次遷移混在一起做。

## 存取控制

`SYNC_TOKEN` 設定後，`/api/<slug>/start` 必須帶 `?token=` 或 `X-Sync-Token` 標頭，
否則回 403。讀取端點（status、collections、test-*）不受影響。

開頁面時用 `https://你的網址/?token=xxx`，前端會自動把 token 帶進同步請求。

**為什麼現在才需要**：舊的 12 支服務沒有這層保護，但它們的清理階段本來就壞著
（`collections/{id}/products.json` 抓不到 SKU），誤觸也刪不掉東西。
新版刪除是真的會執行的 —— 公開網址等於任何人都能刪光一個 collection。

## 佈署順序

**先佈署新服務、驗證通過，再關舊服務。** 中間並存幾天，出事還有退路。

sugar-butter-tree 的舊服務**先別關** —— 它有每日 JST 10:00 的排程，
而新平台的排程器尚未實作（`schedule` 目前只是設定值）。

## 驗證步驟

介面上三顆測試鈕，建議照順序按：

1. **測試連線** — 確認 Shopify token
2. **測試翻譯** — 確認 OpenAI，會顯示實際使用的 model
3. **測試爬取** — 只爬不寫，列出抽樣 5 件的 SKU／價格／庫存／圖片數／說明字數

三顆都綠了再勾 dry-run 跑一次完整同步，對照舊服務的數字。數字吻合才關掉舊服務。

## 商品分流（Route）

同一品牌底下性質不同的商品可以導到不同 collection、標籤與翻譯規則。
小倉山莊已設定一條：

```python
routes = [
    Route(name="佛事用", match=r"仏事用|白菊",
          collection="小倉山莊 佛事供品",
          tags="..., 仏事用, 佛事供品, 法事, 追思, 答禮",
          prompt_rules=[...],   # 要求標題加【佛事用】、禁止一般送禮用語
          notice_html=NOTICE_BUTSUJI)   # 商品說明最前面插入用途說明
]
```

小倉山莊實測命中 10 件（97972/97973/97974/97977、99162/99163/99164/99166/99167/99170），
其餘 51 件走預設 collection，無誤判。
虎屋比照辦理：`match=r"弔事用|白茶"` → `虎屋 弔事供品`，實測命中 15 件。

## 詞彙表地雷：短鍵咬進長詞

`apply_glossary` 長鍵優先，所以只要長詞也在表裡就安全；
問題出在「短鍵在表、長詞不在」。實際踩過的：

| 錯誤條目 | 後果 |
|---|---|
| `モン → Mont` | `フルーツレモン` → `フルーツレMont` |
| `ラ → La` | `ラング` → `Laング`、`グラス` → `グLaス` |
| `ナッツ → 堅果` | `ヘーゼルナッツ` → `ヘーゼル堅果` |

**單字元片假名鍵幾乎一定出事，不要用。**

`python check_glossary.py` 會掃出這兩類問題（短鍵咬長詞、單字元鍵）。
每次改詞彙表後跑一次。

## 前端 JS 語法檢查

`core/web.py` 的 `PAGE` 是一大段內嵌 JS，Python 的 `ast.parse` 看不到它。
一個括號錯位就讓整段腳本掛掉，畫面只剩空的品牌下拉和「等待開始...」，
**症狀看起來像伺服器沒起來**，容易誤判方向。

`python check_ui.py` 用 node 真正解析那段 JS。改完 web.py 後跑一次。

## 詞彙對照表（glossary）

品牌設定 `glossary`（日文 → 中文），在**送翻譯之前**做確定性替換。
2026-07 首輪同步 18 件退回全部源自固定用語：商品線名（ご愛食用袋）、
和歌典故名（いづみ流るゝ）、規格用語（仕立て、ヶ入り）。這些詞模型每次翻的結果
都不同，甚至同一次請求內前後不一致，靠 prompt 規則無法穩定。

長鍵優先替換，所以通用的 `"の": "之"` 要放最後，讓 `"の目安"`、`"定家の月"`
這類長詞先命中。

實測效果（ogura 全站 140 件，來源標題殘留假名比例）：

| 階段 | 殘留 |
|---|---|
| 只有清理 | 58% |
| 加詞彙表第一版 | 35% |
| 加讀音移除規則 + 補詞 | 4% |
| 補完最後 5 件 | 0% |

`strip_kana_readings()` 移除純假名的讀音註記 —— 日本商品名常附振假名或別記
（`古今凉の音（ここんすずのね）`、`寄石恋-いしによするこい-`），那是給日本人看的，
中文商品頁不需要。只在括號／夾號內**完全是假名**時才移除，避免誤刪規格說明。

同步結束時會在 log 印出「詞彙表缺口」（本輪被退回的殘留詞及次數），
完整資料在 `/api/<品牌>/status` 的 `glossary_gaps`。擴充詞彙表因此是機械性的工作。

## 翻譯品質把關

上架前會檢查翻譯結果的標題是否殘留假名（`core/text.has_kana`，
**只要出現任何一個假名就算不合格**，不是佔比門檻）：

偵測範圍刻意排除 **U+30FB「・」KATAKANA MIDDLE DOT** —— 它落在片假名區塊內，
但實際是標點符號，中文規格列舉也常用（`沙拉風味・和三盆風味`）。
把它算成假名會誤退大量已經翻譯乾淨的標題（2026-07 有實際誤退案例）。


1. 殘留 → 追加一條「上次殘留假名，這次必須完全消除」的規則重試一次
2. 重試後仍殘留 → 記錄殘留詞並跳過該商品，計入 `kana_rejected`

`kana_rejected` **不計入**連續翻譯失敗的安全停機計數，否則一批難翻的商品
會誤觸停機。

來源標題在送翻譯前會先清理（`core/text.clean_source_title`）：

| 問題 | 處理 |
|---|---|
| `定家の月 大缶□大缶（...）` | 去掉分隔符後的重複規格名 |
| `詰め替え袋詰め替え袋（39枚）` | 去掉緊鄰重複片段 |
| `【国内送料無料】`、`【國內免運】` | 移除（日本國內限定，對台灣客人是錯誤資訊）|
| `【お一人様1回1個限り】` | 移除 |
| `・` | **保留**，它在規格括號內是正常列舉分隔 |

## 定價與運費：二段式收費

Shopify 上的標價**只含商品費用**，運費在商品到倉、確認實際重量後另行請款。
`calculate_selling_price()` 只做「成本 × 費率」，不加任何運費。

舊資料的價格是含運費的（比率散落在成本的 1.79～2.18 倍之間，沒有單一公式，
應是不同時期人工設定後凍結）。2026-07 首次同步時 47 件一次對齊到現行費率，
降幅約 35～42%，這是預期的修正。

| 日幣原價 | 費率 |
|---|---|
| ¥0 ～ ¥5,000 | × 1.25 |
| ¥5,001 ～ ¥10,000 | × 1.22 |
| ¥10,001 ～ ¥20,000 | × 1.20 |
| ¥20,001 ～ ¥30,000 | × 1.18 |
| ¥30,001 以上 | × 1.15 |

最低手續費 ¥300／件，逐件套用（不是合併訂單總額）。

`shipping_fee(kg)` 是運費表，供後續請款流程使用，不參與商品標價。
**計費重採三倍材積規則**：依實重計費，材積重在實重三倍以內不加收材積費，
超過三倍才改用材積重（`billable_weight()`）。`Product` 同時保留
`actual_weight`、`volume_weight`、`weight`（計費重）三個欄位。

## 改價保險絲

改價直接影響營收，保護等級與刪除相同：

| 變數 | 預設 | 說明 |
|---|---|---|
| `PRICE_ALERT_RATIO` | `0.1` | 超過此漲跌幅算「大幅改價」|
| `MAX_PRICE_CHANGES` | `15` | 大幅改價件數上限 |
| `ALLOW_BULK_REPRICE` | 關 | `1` 才允許超過門檻 |

改價**不再在商品迴圈中即時寫入**，改為先蒐集清單、印出 `SKU 原價 → 新價（漲跌幅）`，
過了保險絲才一次套用。dry-run 一樣會印出完整清單，只是不寫入 ——
2026-07 有一次 47 件改價因為 dry-run 靜默而未經預覽就執行，這是為了避免重演。

完整清單可從 `/api/<品牌>/status` 的 `price_changes` 取得。

## 缺貨處理：draft 而非刪除

品牌設定 `out_of_stock_action`：

| 值 | 行為 |
|---|---|
| `"draft"`（預設）| 官網缺貨 → Shopify 轉草稿；補貨後偵測到有貨自動改回 `active` |
| `"delete"` | 官網缺貨 → 直接刪除，補貨後重新上架（會重跑翻譯）|

**「官網商品頁已下架」一律刪除，不受這個設定影響。** 兩者的判別方式：
下架的頁面沒有 `div.block-goods-detail` 主商品區塊，頁面文字是
「ご指定の商品は販売終了かお取扱いできない商品です」，解析出來 price=0；
缺貨的頁面主區塊完整、有價格、只是 `在庫：×`。

轉草稿的好處是保留商品評論、SEO 排名與既有連結，補貨時不必重跑翻譯。
`all_products_map()` 會一併帶回每個商品的 `status`，所以復活判斷不需要額外請求。

## 列表覆蓋率硬性保護（無法覆寫）

清理階段開始前先檢查官網列表相對 collection 的覆蓋率：

| 情況 | 行為 |
|---|---|
| 官網列表為空、collection 有商品 | **無條件停用清理** |
| 覆蓋率低於 `MIN_LIST_COVERAGE`（預設 50%）| 停用清理，調整該變數才能放行 |

`ALLOW_BULK_DELETE` **無法繞過這兩道關卡**。

2026-08 實際發生過：maple-mania 的列表抓取暫時失敗回傳 0 件，
系統據此推導出「27 件全部下架」，準備刪光整個 collection。
當時 `ALLOW_BULK_DELETE=1` 還開著，件數保險絲（門檻 25）被繞過，
只因為那輪是 dry-run 才沒有真的執行。

`list_products()` 也加了重試：第一頁抓不到商品時會間隔 3 秒重試兩次。

**`ALLOW_BULK_DELETE` 與 `ALLOW_BULK_REPRICE` 是一次性覆寫，用完立刻移除。**
留著等於整套保險絲長期失效。

## 刪除保險絲

清理階段若要刪的數量超過 `max(MAX_DELETE_ABS, collection件數 × MAX_DELETE_RATIO)`，
會**中止清理**、印出待刪清單前 30 筆，並要求人工確認後設 `ALLOW_BULK_DELETE=1` 重跑。

誤刪是這套系統歷史上最貴的故障（庫存誤判把既有商品清空），所以預設保守。

## Collection 商品清單

`collection_products_map()` 刻意**不用** `collections/{id}/products.json`：

- 該 legacy 端點回傳的商品 representation 不含 `variants`，也就沒有 SKU
- 社群長期回報它分頁會漏資料（有案例 869 件只拿到 408 件）

改用 `products.json?collection_id=` 過濾，與 `all_products_map()` 同一支端點。
每次抓完會拿 `products/count.json?collection_id=` 交叉核對，數量不符就發警告。

## Collection 解析

`find_collections()` 有兩個容易踩的點：

1. Shopify 有 **custom_collections（手動）** 和 **smart_collections（自動）** 兩種端點。
   只查前者的話，若目標集合是自動集合就會查不到，然後誤建一個同名的空集合。
2. 用 `?title=` 過濾，不要抓 250 筆回來自己比對 —— 店裡集合數超過一頁就會漏。

標題比對是**完全相符**，所以 `小倉山莊`（繁體莊）和 `小倉山荘`（日文荘）是兩個不同集合。
brand 設定裡 `collection` 和 `vendor` 剛好一個用繁體一個用日文，改動時要留意。

dry-run 時 `get_or_create_collection(create=False)` 只查不建。同名集合超過一個會發警告，
因為那代表歷史上誤建過，去重與刪除比對都會失準。

介面上「檢查 Collection」按鈕會列出每個集合的 id、型別與實際商品數。

## 日本新字體正規化

模型偶爾會把來源的日本漢字原封不動帶進輸出（`単品`、`塩味`、`包装`），
在繁中語境是錯字。`normalize_kanji()` 套在**翻譯輸出**上（來源端由 glossary 處理），
目前收 41 個對應關係明確、且在商品文案實際出現過的字。

轉換發生時會在 log 記一行 `SKU 日本字體已轉繁體: 単`，方便發現新的漏網字。

## 標題去重的三個層次

官網標題會把商品名與規格名黏在一起，有三種形態：

| 形態 | 例 | 處理 |
|---|---|---|
| 分隔符兩側完全重複 | `化粧箱(大)●化粧箱(大)（8個入23袋）` | 留較長的那段 |
| 分隔符兩側共用開頭但各有資訊 | `京・七味あられ 袋詰め●京・七味あられ（100g）` | 留長段，把短段獨有的尾巴接回去 —— 否則會丟失 `袋詰め`（袋裝），罐裝與袋裝版本會塌成同一個標題 |
| 無分隔符的相鄰重複 | `詰め替え袋詰め替え袋（39枚）` | 正則去重，容許中間有空白 |

去重要跑**兩次**：`clean_source_title()` 一次，套完詞彙表與移除讀音後再一次 ——
重複單元在那些步驟之後才會縮到偵測長度內
（`古今凉の音（ここんすずのね） 化粧箱（大）` ×2 就是這種）。

## MakeShop 的兩個坑

**`/shopbrand/all_items/` 不是全部商品**，只是首頁精選。
kobe-fugetsudo 的 all_items 只有 4 件，逐分類走完是 105 件；
hontaka all_items 48 件，逐分類 86 件。分類清單從首頁自動抓
（`/shopbrand/ct\d+/`），不寫死。舊 scraper 用的
`shopbrand.html?page=` 入口現在只回 4 件，已失效。

**價格不在文字節點裡。** 兩種佈景各有各的放法：

| 站 | 位置 |
|---|---|
| hontaka | `<input class="m_price" value="5,400">` 的 value 屬性 |
| kobe-fugetsudo | 「税込648円（本体価格600円）」，「税込」在數字前 |

頁面上看得到的「486円（税込）」是**推薦商品區塊**的價格，直接抓文字會拿到別人的價錢。

另外標題要用 `<title>` —— `#itemInfo` 會把整段商品說明黏在商品名後面。
頁面編碼是 **EUC-JP**，requests 猜錯會整頁亂碼。

## 資生堂 PARLOUR（自建站）

**SKU 用商品コード，不是網址的 prod_id。** 頁面上 `.product-code`
顯示「商品コード／71111」，那才是穩定識別，也是 Shopify 現有商品的 SKU。
`prod_id` 只是頁面參數，和商品編號不是一對一
（`prod_id=0000000291` 的商品コード是 `71111`）。用對之後 42 件全部
一一對應，不需要 SKU 遷移。


主商品區塊是 `.area-product`，不是 `.area-detail`（後者不含商品資訊），
也不能用 `.section-onlineshop-detail`（含 7 個價格，下方全是推薦商品）。

**商品名必須用 `<h2>`，不能用 `.product-name`** —— 那個 class 只出現在推薦商品區塊，
抓到的會是別的商品（`prod_id=0000000291` 是「3個入」，
`.product-name` 第一個卻是「6個入」）。

## Shopify 來源（最穩固的一種）

toraya 與 yokumoku 的來源站本身就是 Shopify，直接讀公開的 `products.json`：

- 不解析 HTML，前台改版不會壞
- `available` 直接給庫存狀態，不必猜購物車按鈕或缺貨字樣
- `grams` 給實重，不必從說明文字硬湊
- **yokumoku 舊版用 Playwright 開瀏覽器爬列表，這裡完全不需要**

`list_products()` 一次把整份 JSON 撈回快取，`get_product()` 不再發第二次請求 ——
417 件的 toraya 只需要 2 次 HTTP 請求。

**toraya 是 Hydrogen（Shopify headless）**：前台 `www.toraya-group.co.jp`
沒有 products.json，要打後端 `toraya-group.myshopify.com`。
`json_base` 就是為此而設，yokumoku 則前後台同一個網域。

## 重複清理：保留最舊 vs 保留最新

`dedup_keep` 決定同一 SKU 有多件商品時留哪一筆：

| 值 | 適用 |
|---|---|
| `"oldest"`（預設）| 同一件商品被重複上架。留最舊保住評論、SEO 與既有連結 |
| `"newest"` | 來源站重用商品編號。留最舊等於留下已停售商品、刪掉現行商品 |
| `"auto"` | **逐組用價格判斷**，同一份清單裡兩種情況混雜時用這個 |

`"auto"` 的判準：同組價格全部相同 → 同一商品重複上架 → 留最舊；
價格有差異 → 來源站換了商品 → 留最新。

資生堂 PARLOUR 兩種都有，所以設 `"auto"`：

| SKU | 舊 | 新 | 判定 |
|---|---|---|---|
| `38171` | CRN36 ¥6,820 | CRN39 ¥7,283 | 異價 → 不同商品 → 留新 |
| `75122` | E40 ¥7,654 | E45 ¥8,425 | 異價 → 留新 |
| `71111` | 起司蛋糕 3入 ¥1,736 | 銀座三個入濃厚起司蛋糕 ¥1,736 | 同價 → 同商品 → 留舊 |

若整組都用 `"newest"`，`71111` 這種會丟掉評論與 SEO，
而且留下的翻譯還比較差（`ラング ド シャ`、`菓子・プリン詰め合わせ` 都沒翻）。

## SKU 體系遷移

來源站換過 SKU 規則時（toraya 從 `toraya-{handle}` 換成虎屋自家品號），
若不處理，現有商品會全被判定「已下架」而刪除、官網資料全被當成新商品重上，
**評論、SEO 排名與既有連結全部歸零**。

品牌覆寫 `legacy_sku_keys(product)` 回傳「這個商品在舊體系下長什麼樣」，
`/api/<slug>/migrate-sku` 就能把現有商品的 SKU 就地改寫成新的：

- `GET` 只出計畫（可對應／無法對應各幾件、逐筆列出）
- `POST` 才寫入，需 `SYNC_TOKEN`
- 新 SKU 已被別的商品占用時會列入「無法對應」，不會覆蓋造成撞號

遷移完再跑同步，那些商品就會被正確辨識為既有商品，只更新價格而不是重上。

## 標籤過濾與 ¥0 商品

`require_tags`（Shopify 來源）：不合格的商品**仍留在列表裡**，只是標記為不可販售
→ 轉草稿，等標籤回來自動復活。

**不要改成直接跳過。** 那樣這些商品會從系統視角消失，被清理階段判定
「官網已下架」而刪除，SKU 遷移也會誤判成「官網已無此商品」。
但虎屋的季節輪替商品（四季の富士 冬、照紅葉、雲の峰）下一季就會回來，
刪了等於每季重新上架、重跑翻譯、評論歸零。
2026-08 實測：改成跳過時 SKU 遷移只能對應 15/66，保留後 16/16 抽樣全中。

虎屋設 `("販売.EC販売可",)` —— 417 件裡只有 118 件能網購，
其餘 299 件是店頭現賣的生菓子（柏餅、桜餅、紫陽花），賞味期限一兩天，
無法空運也無從代購。虎屋全部商品的 `product_type` 都是「食品・飲料」，
分不出來，真正的區分在標籤。

`price <= 0` 獨立擋掉（`is_sellable` 回 `no_price`）。
價格門檻取消後 `price < min_price` 永遠不成立，¥0 商品會漏進來 ——
那是資料缺失或季節限定未開賣，不是便宜商品。

## ecbeing 的兩個通用修正（2026-08）

**SKU 不限數字。** 原本正則寫死 `/g/g(\d+)/`，因為 ogura 的商品代號全是數字。
實際上 bankaku 有 `G225`、gateaufesta-harada 全部是 `R1`/`R2` 這種字母開頭 ——
限定數字會讓 bankaku 漏抓 7 件、harada 整個回傳 0 件。已改為 `[A-Za-z0-9._-]+`。

**加入購物車的措辭各站不同。** ogura 是「買い物かごに入れる」，
harada 是「お買い物かごへ入れる」（に vs へ），sucrey 與 harada 另有
`.block-add-cart--btn`。集中在 `CART_SELECTORS` / `CART_PHRASES` 兩份清單，
新增 ecbeing 站台時先確認這兩項。

## 多分類來源

`category_paths` 可填多個分類路徑（harada 有 17 個），
adapter 會把「分類 × 分頁」攤平成一維序號給 `BaseBrand` 的迴圈。
**每個分類各自分頁走到底，不共用「連續空白」計數器。**
先前把分類 × 分頁攤平成一維序號共用一個計數器，harada 中段有 8 個連續空分類
（cwhite / cpremium / cex-pr / csoleil / cpr-ve / cpr-wz / crtb / crhw 全是 0 件），
一路累加就提早收工 —— 41 件只抓到 22 件。
清理階段會把沒抓到的 19 件判定成「官網已下架」，所幸覆蓋率保護擋下了。

## sucrey 平台的兩個特點

**三品牌同站**：cocoris、francais、maple-mania 都在 sucreyshopping.jp，
靠 `?brand=` 參數區分，所以列表與明細邏輯完全共用，品牌檔只差 `brand_param` 與文案。

**實體店限定商品**：這個站沒有「在庫：○△×」文字，也沒有任何缺貨關鍵字。
判定採雙重訊號 —— `.block-goods-stock`（在庫○）與 `.block-add-cart--btn`
必須同時存在。只看購物車按鈕不夠：**實體店限定商品同樣沒有按鈕，但它不是暫時缺貨**。
2026-08 實測 75 件中有 30 件屬於此類（cocoris 全部 8 件都是），
頁面只剩價格與「取扱店舗」字樣。`Product.raw["store_only"]` 會標記這類商品。

這也意味著 **cocoris 目前線上可購買的商品是 0 件** —— 它的商品全在實體店販售。
遷移後同步不會上架任何 cocoris 商品，這是正確行為，不是故障。

## 已知待辦

- `EcbeingSummary.check_in_stock` 以購物車按鈕存在與否判定。測試當下
  paqtomog 站上 18 件全部有貨，**缺貨狀態的 HTML 尚未觀察到**，第一次遇到
  缺貨商品時要回來驗證這條規則。
- 砂糖奶油樹的商品說明只取 `--description-comment1`（約 36 字）。
  comment2/4/6 是日本國內的配送與受付條款，不適合給台灣客人看，所以刻意排除。
  若要更豐富的翻譯素材，可考慮把 `.block-goods-detail-body--contents` 清掉
  麵包屑與評論後餵給翻譯，但不要直接寫進商品說明。
- 標題去重無法處理「全角半角括號不一致」的近似重複，例如
  `泉流 精裝禮盒 6個入精裝禮盒（6個入）`（SKU 25194/25196）。全站 140 件中僅 2 件，
  模型仍能產出合理標題，暫不處理。
- 排程器（`schedule` 欄位）尚未實作。目前只是設定值，沿用舊服務的排程直到遷移完成。
- 重複商品清理（原 `/api/dedup`）尚未移植。

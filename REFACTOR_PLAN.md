# 整理計畫（凍結功能、只搬家）

目標：降低維護成本，**不改任何現有功能、畫面、分數、管線或資料契約**。
本文件只規劃，不在同一輪實作。每一階段都要能獨立還原。

原則：**只搬家、不改行為。** 發現舊缺口（例如本機 GUI 沒有 `/api/etf`、`/api/dividends`、`/api/daytrade` handler）也**不要順便修**，那是行為變更，不屬於本計畫。

---

## 不做的事

- 不改分數公式、排行名單、門檻、權重
- 不改 GitHub Actions 步驟順序、cron、cache key、site 發布流程
- 不改 `/api/*` 路徑、query、POST body、JSON 欄位名
- 不改 HTML `id` / `data-tab` / `rankingCols` / `columnGroups` / `RANKING_COLUMN_GROUP`
- 不改 `docs/data/` 檔名與靜態對應（`staticGetJSON`）
- 不改 `window.STOCK_CHIP_STATIC` 切換邏輯
- 不改 SQLite schema
- 不「順便」補功能、改文案、改 CSS、改預設自選股
- 不刪 `reports/ranking_*.csv`、`scan_all_*.csv`、`ranking_snapshots.csv`、`watchlist_*.csv`

---

## 凍結契約（動到任一項就算出界）

### A. 本機 GUI 路由

`python3 -m stock_chip.gui` 的 GET / POST 路徑與參數維持現狀。  
前端 JS 與 `GUIHandler` 必須繼續對得上。

GET：`/`、`/api/meta`、`/api/ranking`、`/api/watchlist`、`/api/scan-all`、`/api/industries`、`/api/watchlist-items`、`/api/stock/resolve`、`/api/stock`、`/api/market`、`/api/market-sentiment`、`/api/news`、`/api/us-news`、`/api/ci-us-news`、`/api/ci-us-news-live`、`/api/mops-events`、`/api/obsidian/status`、`/api/coverage`、`/api/backtest`、`/api/digest`、`/api/suggest`、`/api/trend`、`/api/weekly`、`/api/health`、`/api/update-tasks`、`/api/static/status`

POST：`/api/watchlist`、`/api/update`、`/api/update/cancel`、`/api/news/refresh`、`/api/us-news/refresh`、`/api/mops-events/refresh`、`/api/tdcc/refresh`、`/api/financials/refresh`、`/api/obsidian/export`、`/api/stock/refresh-section`、`/api/stock/ensure`、`/api/static/export`、`/api/static/publish`

已知現況（凍結、不修）：本機 handler **沒有** `/api/etf`、`/api/dividends`、`/api/daytrade`。靜態版由 `staticGetJSON` 讀 JSON。不要為了「對稱」去加 handler。

### B. 靜態頁 JSON

`export_static` 仍從 `gui.INDEX_HTML` 寫出 `docs/index.html`，並把 `STOCK_CHIP_STATIC` 改成 `true`。路徑不變：

| 頁面用途 | 檔案 |
|---|---|
| 排行 / 搜尋 / 族群 | `data/rankings/{5,20}d/{ranking}.json`、`search_index.json` |
| 個股完整 / 精簡 | `data/stocks/{id}/{5,20}d.json`、`chart_lite.json` |
| 週報 / 當沖 / 趨勢 | `data/weekly.json`、`data/daytrade.json`、`data/trend_{days}d.json` |
| 大盤 / 情緒 / 回測 | `data/market*.json`、`data/market_sentiment.json`、`data/backtest.json` |
| 新聞 / MOPS / 健康 | `data/us_news.json`、`data/ci_us_news.json`、`data/mops_events.json`、`data/health.json` |
| 其他 | `data/meta.json`、`data/suggest.json`、`data/digest_{days}d.json`、`data/coverage_{days}d.json`、`data/dividends.json`、`data/etf.json` |

### C. 外部仍從 `stock_chip.gui` 取用的名稱

`export_static.py`、`tests/test_ranking_columns.py`、`tests/test_scan_row_contract.py`、`tests/test_mispriced.py` 繼續用這些名字（可用 re-export，不可改呼叫端語意）：

`INDEX_HTML`、`DB_PATH`、`REPORTS_DIR`、`as_float`、`connect_db`、`current_watchlist_ids`、`db_meta`、`market_payload`、`market_sentiment_payload`、`normalize_scan_row`、`now_text`、`ranking_path`、`read_csv`、`stock_detail`

`test_ranking_columns.py` 用正則掃 `INDEX_HTML` 字串裡的 `rankingCols` / `columnGroups` / `RANKING_COLUMN_GROUP`。抽出 HTML 後，`INDEX_HTML` 內容必須與抽出前逐字相同。

### D. 頁面分頁 `data-tab`

`ranking`、`weekly`、`daytrade`、`watchlist`、`sector`（以及現有其餘 tab）不可改名。

### E. 每日管線仍依賴的 reports（不可刪）

- `ranking_*.csv`（含 1d / 5d / 20d）：排行頁、新聞 `--from-rankings`、分點 workflow
- `scan_all_*.csv`：週報、當沖、趨勢、digest、搜尋建議、產業列表
- `ranking_snapshots.csv`：回測備援
- `watchlist_*.csv`、`branch_report_{5,20}d.md`、`branch_topn_{5,20}d.csv`：現行輸出，保留

可刪（無模組讀取）：約 600 個 `branch_report_20d_all_offset*_limit5.md`、`branch_topn_20d_all_offset*_limit5.csv`

---

## 階段（每階段一個 PR，做完再開下一階段）

### 第 0 階段：契約測試（先加安全網，再搬家）

新增測試，**不改產品碼**：

1. 從 `gui.py` 的 `do_GET` / `do_POST` 抽出路徑集合，對照上面 A 節清單。少一條失敗。
2. `INDEX_HTML` 必須含 `window.STOCK_CHIP_STATIC`、各 `data-tab`、以及 `staticGetJSON` 用到的 `data/*.json` 路徑。
3. 斷言 `export_static` 仍從 `stock_chip.gui` import 第 C 節名稱。

既有測試維持必過：

```bash
python -m pytest tests/ -q
# 有 docs/data（site 分支）時再跑：
npm run e2e
```

e2e 覆蓋：記帳（`portfolio`）、同步（`sync`）、競態（`race`）、週報、當沖、當沖降級。這些測的是靜態頁行為，搬家時不得改斷言。

**完成定義：** 新契約測試綠、產品碼 diff 為空。

### 第 1 階段：抽出 HTML（唯一碰得到頁面的一步）

- 把 `INDEX_HTML` 字串原封不動寫到例如 `stock_chip/static/index.html`
- `gui.py` 改為 `INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")`
- `export_static`、CI e2e 產生靜態頁、本機 `GET /` 仍讀 `INDEX_HTML`

禁止：格式化 HTML、改縮排、改引號、改註解、Prettier、把 JS 拆檔。  
`test_ranking_columns` 對排版敏感，任何「美化」都會被當成契約破壞。

驗證：

```bash
# 抽出後與抽出前必須相同（實作時用 git show 或暫存檔比對）
python -m pytest tests/test_ranking_columns.py tests/test_scan_row_contract.py tests/test_mispriced.py -q
```

**完成定義：** `INDEX_HTML` 位元組級相同；本機與靜態頁看起來與現在無差別。

### 第 2 階段：Python 後端拆檔（可選，頁面無感）

只在第 1 階段穩定後做。新模組（名稱可再定）只搬函式，**不改函式簽名與回傳**：

- `stock_chip/gui_http.py`：`GUIHandler`、`json_response`、`main`
- `stock_chip/gui_data.py`：`normalize_scan_row`、`stock_detail`、`ranking_path`、`db_meta` 等讀取函式
- `stock_chip/gui_jobs.py`：更新佇列、`UPDATE_TASKS`、publish

`stock_chip/gui.py` 保留 re-export，既有 `from stock_chip.gui import …` 一行都不必改。

**完成定義：** `git grep 'from stock_chip.gui'` 的呼叫端不必改；pytest 全綠。

### 第 3 階段：舊根目錄腳本

這些檔**沒有任何現行程式 import**：

`taiwan_stock_analysis.py`、`taiwan_stock_analysis_upgrade.py`、`get_news.py`、`stock_gui.py`、`stock_reports.py`

做法：移到 `legacy/`，不刪內容、不改邏輯。頁面與 workflow 零依賴。  
若擔心本機捷徑，可在原路徑留 5 行轉址 stub；預設不留，避免以為它們還是正式入口。

**完成定義：** `stock_chip/`、`.github/`、`tests/`、`docs/index.html` 的 diff 為空。

### 第 4 階段：清 reports offset 碎片

- 刪 `reports/*_all_offset*`（約 600 檔）
- `.gitignore` 加上 `reports/*_all_offset*`
- **不改** `branch.py` 的檔名規則（本機分批抓仍可寫出 offset 檔，只是不再進 git）

保留第 E 節全部檔案。  
`update-taiwan-data.yml` 的 `git add reports` 之後只會提交有效報表，不會再把過程檔推進 main。

**完成定義：** 頁面與掃描仍讀得到 `ranking_*`、`scan_all_*`；`git ls-files reports | grep offset` 為空。

---

## 建議順序與風險

| 階段 | 碰頁面？ | 風險 | 建議 |
|---|---|---|---|
| 0 契約測試 | 否 | 低 | 必做 |
| 1 抽出 HTML | 是（同一份字串） | 中（只能因複製出錯） | 必做；用位元組比對 |
| 2 拆 Python | 否（re-export） | 中 | 可延後 |
| 3 legacy/ | 否 | 極低 | 可與 4 一起 |
| 4 清 offset | 否 | 低（刪錯 keep 檔才會出事） | 用 glob 白名單刪 |

第 2 階段不做也不影響 1、3、4。若只想減 repo 體積，做 0 + 4 即可。

---

## 每階段檢查清單

1. `python -m pytest tests/ -q`
2. 有 `docs/data` 時 `npm run e2e`
3. 人工：靜態頁切「排行 / 週報 / 當沖 / 族群 / 自選」、搜尋一檔有明細、一檔精簡
4. 本機 GUI（若有 SQLite）：`/` 能開、排行與個股 API 200
5. `git diff` 不含 `scan.py`、workflow、分數、JSON schema

任一項失敗就停，不進入下一階段。

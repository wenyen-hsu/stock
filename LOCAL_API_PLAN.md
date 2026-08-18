# 本機補齊 ETF / 除權息 / 當沖 API（只接線）

目標：本機 GUI 打開「當沖候選」、持股除權息、ETF 資金流時，不再 404。  
做法比照已經補過的 `/api/weekly`：**只在 `do_GET` 接上現成函式**，不改計算、不改前端、不改管線。

---

## 現況（為什麼本機缺、靜態站有）

前端三種模式走同一條 `getJSON("/api/…")`：

| 路徑 | 靜態站 | 本機 GUI |
|---|---|---|
| `/api/weekly` | `data/weekly.json` | 已有 handler（`build_weekly_report`） |
| `/api/etf` | `data/etf.json` | **沒有 handler → 404** |
| `/api/dividends` | `data/dividends.json` | **沒有 handler → 404** |
| `/api/daytrade` | `data/daytrade.json` | **沒有 handler → 404** |

靜態 JSON 本來就是這些函式產的（`export_static.py`）：

- `load_etf_flows(conn)` → `etf.json`
- `load_dividend_events(conn, days=400)` → `dividends.json`
- `build_daytrade_report(DB_PATH, reports/scan_all_{N}d.csv)` → `daytrade.json`

本機缺的只是 HTTP 那一層。頁面、欄位、空資料文案都已經寫好。

---

## 不做的事（動到算出界）

- 不改 `load_etf_flows` / `load_dividend_events` / `build_daytrade_report` 的簽名、欄位、門檻
- 不改 `stock_chip/static/index.html`（`getJSON`、`staticGetJSON`、表格、警語都不動）
- 不改分數、排行、scan、branch、workflow、SQLite schema
- 不新增抓取步驟、不新增「更新中心」任務、不加 POST `/api/etf/refresh` 之類
- 不包一層新 JSON（例如 `{data: …}`）；回傳必須與匯出檔同一形狀
- 不順便修其他本機／靜態差異

缺資料時沿用現成 payload：ETF `state: empty/accumulating`、當沖 `state: insufficient`、除權息空 `events`。前端已會顯示「累積中／尚無資料」。

---

## 要做的事（對齊 `/api/weekly`）

只改 `stock_chip/gui_http.py` 的 `do_GET`，插在 `/api/weekly` 附近：

```python
if parsed.path == "/api/etf":
    from stock_chip.etf import load_etf_flows
    with connect_db(DB_PATH) as conn:
        json_response(self, load_etf_flows(conn))
    return

if parsed.path == "/api/dividends":
    from stock_chip.dividends import load_dividend_events
    with connect_db(DB_PATH) as conn:
        json_response(self, load_dividend_events(conn, days=400))
    return

if parsed.path == "/api/daytrade":
    from stock_chip.daytrade import build_daytrade_report
    json_response(self, build_daytrade_report(DB_PATH, REPORTS_DIR / "scan_all_20d.csv"))
    return
```

參數對齊現況，不發明新的：

| 路徑 | 資料來源 | 與誰一致 |
|---|---|---|
| `/api/etf` | SQLite `etf_universe` / `etf_holdings` | `export_static` 的 `etf.json` |
| `/api/dividends` | `days=400` | `export_static` 寫 `dividends.json` 時的參數 |
| `/api/daytrade` | `reports/scan_all_20d.csv` | 本機 `/api/weekly`（同樣寫死 20 日掃描） |

不讀 query string 換天數：靜態版這三條也不吃 `?days=`。

`connect_db`、`DB_PATH`、`REPORTS_DIR` 已在 handler 使用，不新增架構層。

---

## 契約測試怎麼改

`tests/test_gui_contract.py` 現在把這三條列在 `ABSENT_LOCAL_GET`（整理時凍結「不要順便加」）。

這次是**有意補上**，所以：

1. 把 `/api/etf`、`/api/dividends`、`/api/daytrade` 移進 `FROZEN_GET`
2. 刪掉 `ABSENT_LOCAL_GET`（或改成空集合並斷言本機 GET **包含**這三條）
3. 加一個小測試：`do_GET` 原始碼裡這三條必須呼叫 `load_etf_flows` / `load_dividend_events` / `build_daytrade_report`，避免日後改寫成另一套計算

不加會打網路、不需要 SQLite 的測試。e2e 測的是靜態站，本來就綠，不必改斷言。

---

## 驗證

```bash
python3 -m pytest tests/ -q
```

本機若有 `data/stock_chip.sqlite`：

1. `python3 -m stock_chip.gui --host 127.0.0.1 --port 8502`
2. 開當沖分頁：不再「讀取失敗」，有資料或「尚無候選」都可以
3. 持股頁：除權息能進成本（`payload.events`）
4. 排行頁 ETF 區塊：有列或「累積中」，不是 fetch 錯誤

靜態站：用 `site` 的 `docs/data` 跑 `npm run e2e`（或等 CI），確認 HTML 沒被改所以行為不變。

`git diff` 應只見 `gui_http.py`、`tests/test_gui_contract.py`，以及這份計畫／`usage.md`。

---

## 風險

低。函式與頁面都已上線，只補本機路由。  
若本機從沒跑過 ETF／除權息／當沖更新，畫面會空，那是資料未入庫，不是接線錯誤。

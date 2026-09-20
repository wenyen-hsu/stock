# 使用說明

本機 GUI、CLI、GitHub Actions 與 GitHub Pages 的操作方式見 [README.md](README.md)。  
指標與資料源沿革見 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 本機 API 接線（已做）

[LOCAL_API_PLAN.md](LOCAL_API_PLAN.md)：本機 GUI 的 `/api/etf`、`/api/dividends`、`/api/daytrade` 已接上 `export_static` 同一組函式。前端、分數、管線未改。

## 整理計畫（已合併）

[REFACTOR_PLAN.md](REFACTOR_PLAN.md) 的約束不變：只搬家、不改行為。

- **第 0 階段（已做）：** `tests/test_gui_contract.py` 凍結本機路由、`data-tab`、靜態 JSON 路徑，以及 `export_static` 從 `stock_chip.gui` 取用的名稱。
- **第 1 階段（已做）：** `INDEX_HTML` 改從 `stock_chip/static/index.html` 讀取，內容與抽出前位元組級相同。
- **第 2 階段（已做）：** Python 後端拆成 `gui_data.py` / `gui_jobs.py` / `gui_http.py`；`gui.py` 只 re-export，呼叫端 import 不變。
- **第 3 階段（已做）：** 舊腳本移到 `legacy/`，內容未改。
- **第 4 階段（已做）：** 刪除 `reports/*_all_offset*`；`.gitignore` 忽略之後再產生的過程檔。`ranking_*.csv`、`scan_all_*.csv` 等管線報表保留。

## 當沖候選（2026-08-19 修正）

硬門檻沒改：上市當沖比率 ≥ 25%、日均額 3 億；上櫃日均額 5 億；收盤 10–500；處置股排除。

排序補了兩件事：

- **追強只收昨日上漲**（`chg_1d_pct > 0`），與接刀只收昨日下跌對稱。昨平、昨跌不再進追強榜。
- **昨漲 ≥ 9%** 標 `yesterday_overheated`（頁面「昨過熱」）。只做標記，不改分數。

頁面補上三欄對應的動作：追強等今開回檔、接刀等弱開再接、賣壓在開盤附近賣或避。賣壓榜來源是 `day_traders.json`，檔數少，頁面標「僅供參考」。

## 每日更新監測（台北 03:00，固定執行）

這則 Cloud Agent 對話每天 **03:00 Asia/Taipei**（UTC `0 19 * * *`）檢查前一晚管線；沒過就修到過。每次醒來結束前會**先退訂再重訂**同一支 timer，把 7 天到期往後推，不必再下指令。

步驟見 [`.cursor/skills/monitor-daily-update/SKILL.md`](.cursor/skills/monitor-daily-update/SKILL.md)。

成功條件摘要：當日（或上週五）的 `Update Taiwan Stock Data` **schedule** 成功、`site` 的 `meta.json` `latest_date` 對、`health.json` 無 fail、Pages `built`。分點另查 `Fetch Branch Data`。

若這則對話被封存，監測會停。要完全獨立於對話，到 [cursor.com/automations](https://cursor.com/automations) 建 cron `0 3 * * *`、時區台北，指示寫「照 monitor-daily-update skill」。

## Cloud Agent 開發環境（已設定）

Cursor Cloud Agent 的環境已備妥，讓代理能直接跑應用與測試：

- **base image**：Cursor 預設映像（已含 Python 3.12、Node 22、npm、passwordless sudo）。
- **install**（開機建置一次，idempotent）：
  1. `python3 -m pip install --break-system-packages -r requirements.txt -r requirements-dev.txt`（系統 Python 為 externally-managed，需 `--break-system-packages`）
  2. `npm install --no-save playwright` 並 `npx playwright install --with-deps chromium`（e2e 用）
  3. 由 `stock_chip.gui.INDEX_HTML` 重新產生 `docs/index.html`（`STOCK_CHIP_STATIC=true`）。e2e 測的是**當前原始碼**產生的頁面，committed 的 `docs/index.html` 是較舊的發佈版，故每次都重生；這會讓 `docs/index.html` 在工作區呈現 modified，提交前可 `git checkout -- docs/index.html` 還原。
- **terminals**（常駐服務）：
  - `static-site`：`python3 -m http.server 9000 -d docs`（發佈用靜態站，含真實資料）
  - `local-gui`：`python3 -m stock_chip.gui --host 127.0.0.1 --port 8502`（本機 GUI；無 SQLite 時回傳 `state: insufficient` 空狀態）
- **驗證**：`python3 -m pytest tests/ -q`（173 passed，無網路／無 DB）與 `npm run e2e`（Playwright chromium 對 `docs/` 靜態站，6 支測試全綠）。

本機 GUI 若要有真實資料，需先跑管線 `python3 -m stock_chip.daily` 灌 `data/stock_chip.sqlite`（會連台股資料源，需網路）。

## 日常指令（現況，整理後不變）

```bash
# 本機介面
python3 -m stock_chip.gui --host 127.0.0.1 --port 8502

# 更新行情並掃描
python3 -m stock_chip.daily --days 20 --limit 50

# 匯出靜態站
python3 -m stock_chip.export_static --out docs
python3 -m http.server 9000 -d docs

# 測試
python -m pytest tests/ -q
npm run e2e
```

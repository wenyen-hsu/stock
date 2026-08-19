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

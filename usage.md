# 使用說明

本機 GUI、CLI、GitHub Actions 與 GitHub Pages 的操作方式見 [README.md](README.md)。  
指標與資料源沿革見 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 整理計畫（執行中）

[REFACTOR_PLAN.md](REFACTOR_PLAN.md) 的約束不變：只搬家、不改行為。

- **第 0 階段（已做）：** `tests/test_gui_contract.py` 凍結本機路由、`data-tab`、靜態 JSON 路徑，以及 `export_static` 從 `stock_chip.gui` 取用的名稱。
- **第 1 階段：** 抽出 `INDEX_HTML`（位元組級相同）。
- **第 2 階段：** 延後（Python 拆檔）。
- **第 3、4 階段：** 舊腳本移到 `legacy/`；只刪 `reports/*_all_offset*`。

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

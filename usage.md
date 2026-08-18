# 使用說明

本機 GUI、CLI、GitHub Actions 與 GitHub Pages 的操作方式見 [README.md](README.md)。  
指標與資料源沿革見 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 整理計畫（尚未實作）

[REFACTOR_PLAN.md](REFACTOR_PLAN.md) 規劃三件事：抽出 `gui.py` 內嵌 HTML、根目錄舊腳本移到 `legacy/`、刪除 `reports/` 裡無人讀取的 `*_all_offset*` 過程檔。

約束：**不改現有功能。** 分數、API、頁面 `id`、靜態 JSON 路徑、Actions 管線都不動。只搬家，不改行為。舊缺口也不順便修。

執行順序：先加契約測試（第 0 階段），再抽出 HTML（第 1 階段）。其餘可延後。每一階段單獨 PR，測試紅就停。

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

# 台股籌碼觀察

目前支援 TWSE 上市股票、TPEx 上櫃股票，以及興櫃當日行情的官方資料：

- 日成交、成交金額、收盤價、成交均價
- 外資買賣超
- 投信買賣超
- 自營商買賣超
- 上市、上櫃籌碼排行與個股明細
- 自選股摘要與每日明細
- 個股 24 個月月營收、去年同期、月增率、年增率
- 區間買超前十分點與已抓取分點的最近 10 日買賣超

興櫃目前只接 TPEx OpenAPI 的「興櫃股票當日行情表」，可從每天執行開始累積價格資料；免費公開端點尚未接到興櫃歷史日行情與三大法人買賣超。分點逐價均價尚未接入。TWSE 買賣日報表公開頁面目前要求驗證碼，不適合作為穩定自動化來源；FinMind 分點資料已保留測試接口，但需要 token 且可能需要 sponsor 權限。

## 每日更新與掃描

```bash
python3 -m stock_chip.daily --days 20 --limit 50
```

預設會使用本機已完整入庫的交易日資料，避免每天重抓整個 20 日窗口。若要強制重抓，例如要補寫當日興櫃行情：

```bash
python3 -m stock_chip.daily --days 20 --force-refresh
```

指定日期：

```bash
python3 -m stock_chip.daily --days 20 --end-date 2026-04-17 --limit 50
```

指定自選股：

```bash
python3 -m stock_chip.daily --days 20 --watchlist 2376,2382,2324,6196
```

連同自選股分點 Top N 一起抓：

```bash
python3 -m stock_chip.daily --days 20 --watchlist 2376,2382,2324,6196 --with-branch --branch-top 10
```

## 分開執行

只更新官方資料：

```bash
python3 -m stock_chip.official --days 20
```

只掃描既有資料庫：

```bash
python3 -m stock_chip.scan --days 20 --limit 50
```

只更新自選股分點 Top N：

```bash
python3 -m stock_chip.branch --days 20 --top 10 --watchlist 2376,2382,2324,6196
```

更新自選股最近 10 個交易日分點明細：

```bash
python3 -m stock_chip.branch --days 10 --top 80 --daily --watchlist 2376,2382,2324,6196 --sleep 1.0
```

更新自選股 24 個月月營收：

```bash
python3 -m stock_chip.revenue --months 24 --watchlist 2376,2382,2324,6196
```

分批更新上市/上櫃全市場分點 Top N：

```bash
python3 -m stock_chip.branch --days 20 --top 5 --all --markets TWSE,TPEX --limit 100 --offset 0 --skip-existing --sleep 1.0
python3 -m stock_chip.branch --days 20 --top 5 --all --markets TWSE,TPEX --limit 100 --offset 100 --skip-existing --sleep 1.0
```

全市場分點來源是第三方公開頁，請分批、限速執行；不要一次高頻抓完整市場。

查看目前分點覆蓋率：

```bash
python3 -m stock_chip.branch --days 20 --coverage
```

## GUI

啟動本機網頁介面：

```bash
python3 -m stock_chip.gui --host 127.0.0.1 --port 8501
```

然後打開：

```text
http://127.0.0.1:8501
```

GUI 目前包含：

- 總分 / 法人分 / 分點分排行
- 外資買超、投信買超、外資 + 投信排行
- 市場篩選：上市、上櫃
- 代號 / 名稱搜尋
- 自選股摘要
- 點選排行或自選股進入個股頁
- 個股 20 日法人每日進出
- 個股區間合計買超前十分點
- 點選分點查看最近 10 日買賣超
- 個股 24 個月營收與去年同期比較
- 分點覆蓋率與批次抓取指令

## GitHub Pages 靜態版

GitHub Pages 版不啟動 Python 後端，也不會即時抓資料；它只讀本機匯出的 `docs/data/*.json`，適合公開給別人看排行、排序、搜尋已匯出的股票，以及點進個股看快取資料。

先在本機更新資料庫與報表，再匯出靜態檔：

```bash
python3 -m stock_chip.export_static --out docs
```

本機預覽 GitHub Pages 版：

```bash
python3 -m http.server 9000 -d docs
```

然後打開：

```text
http://127.0.0.1:9000
```

預設會匯出 5 日、20 日排行中出現的股票與自選股個股資料。若要把 `scan_all` 內所有股票個股資料都匯出，使用：

```bash
python3 -m stock_chip.export_static --out docs --include-all-details
```

靜態版自選股只存於使用者自己的瀏覽器 `localStorage`。更新每日、營收、分點、新聞等按鈕會隱藏；需要重新抓資料時，請回本機 GUI 或 CLI 更新後重新匯出並 push。

## 主要輸出

- `data/stock_chip.sqlite`：本機 SQLite 資料庫
- `reports/scan_report_20d.md`：Markdown 摘要報告
- `reports/scan_all_20d.csv`：上市、上櫃、已累積興櫃掃描結果
- `reports/watchlist_summary_20d.csv`：自選股 N 日摘要
- `reports/watchlist_daily_20d.csv`：自選股每日明細
- `reports/ranking_chip_score_20d.csv`：籌碼分數排行
- `reports/ranking_total_score_20d.csv`：法人籌碼分數 + 分點分數排行
- `reports/ranking_branch_score_20d.csv`：自選股分點分數排行
- `reports/ranking_foreign_buy_20d.csv`：外資買超排行
- `reports/ranking_trust_buy_20d.csv`：投信買超排行
- `reports/ranking_inst_buy_20d.csv`：外資 + 投信買超排行
- `reports/ranking_foreign_trust_same_buy_20d.csv`：外資、投信同步買超排行
- `reports/ranking_near_avg_with_inst_buy_20d.csv`：接近均價且法人買超排行
- `reports/branch_report_20d.md`：自選股分點 Top N 摘要
- `reports/branch_topn_20d.csv`：自選股分點 Top N 明細

## 資料源驗證

```bash
python3 -m stock_chip.probe --days 5 --top 5
```

若要測 FinMind token：

```bash
FINMIND_TOKEN=你的token python3 -m stock_chip.probe --days 5 --top 5
```

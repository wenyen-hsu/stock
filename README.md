# 台股籌碼觀察

這個專案分成三個層次：

1. **本機 GUI / SQLite**：主要操作環境。台股行情、法人買賣超、融資融券、營收、MOPS 事件、個股分點與同步後的新聞都寫入本機 `data/stock_chip.sqlite`。
2. **GitHub Actions 自動快取**：GitHub 上定時抓美股 RSS 新聞，並於台股交易日晚間自動抓官方台股資料、分點買賣超、重算排行。整條流程都在 GitHub 的雲端主機執行（不需要任何自家電腦開機），網站資料發布到 **`site` 分支**（單一 commit 歷史，避免 repo 隨每日資料膨脹）；main 分支只保留程式碼與 reports。它不會更新你的本機 SQLite。
3. **GitHub Pages 靜態頁**：只讀 `docs/data/*.json` 的靜態快照，給別人看已匯出的排行、個股快取、新聞快取與頁面功能；沒有 Python 後端，也不能直接替使用者更新資料庫。

因此，clone 這個 repo 的人可以看到程式碼與目前提交的靜態快照，但完整可操作資料仍需要在自己的電腦執行更新流程產生。`data/stock_chip.sqlite` 是本機資料庫，不作為共用資料來源。

目前支援 TWSE 上市股票、TPEx 上櫃股票，以及興櫃當日行情的官方資料：

- 日成交、成交金額、收盤價、成交均價
- 外資買賣超
- 投信買賣超
- 自營商買賣超
- 上市、上櫃籌碼排行與個股明細
- 自選股摘要與每日明細
- 個股 24 個月月營收、去年同期、月增率、年增率
- 區間買超前十分點與已抓取分點的最近 10 日買賣超
- MOPS 公開資訊觀測站重大事件月曆、事件列表與內文明細
- 動能與風險指標：區間報酬率、RSI14、年化波動率、距區間高低點、日均成交額（億）
- 估值分（本益比 / 股價淨值比 / 殖利率）與多因子綜合分（基礎分 + 動能 + 估值 + 量能×0.5）
- 新排行：多因子綜合分、動能 + 法人買超、低估值 + 高殖利率（皆含日均成交額 ≥ 0.3 億的流動性門檻）

指標設計與後續路線圖見 [OPTIMIZATION.md](OPTIMIZATION.md)。

興櫃目前只接 TPEx OpenAPI 的「興櫃股票當日行情表」，可從每天執行開始累積價格資料；免費公開端點尚未接到興櫃歷史日行情與三大法人買賣超。分點資料來源為 MoneyDJ 券商鏡像（免登入、無均價欄位）；TWSE 買賣日報表公開頁面目前要求驗證碼，不適合作為穩定自動化來源。

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

更新自選股新聞標題與連結：

```bash
python3 -m stock_chip.news --watchlist 2376,2382,2324,6196 --limit 5
```

預設只抓 Yahoo 股市 RSS 標題、連結與摘要。若要同時抓文章內文摘錄，可加上 `--content`；批次更新中心預設不抓內文。

大量抓取（雲端管線用法）改以排行決定名單，並帶上護欄：

```bash
python3 -m stock_chip.news --days 20 --from-rankings 50 --include-db-watchlist \
  --limit 8 --sleep 0.4 --max-requests 450 --empty-streak 40 --fail-tolerance 0.2
```

- `--from-rankings N`：各排行榜前 N 名的交錯聯集（各榜第 1 名 → 各榜第 2 名 → …），被 `--max-requests` 截斷時仍覆蓋每個榜的前段；自選股永遠排在最前面。
- `--empty-streak N`：Yahoo 被限速時回的是**空 feed 而非錯誤**，連續 N 檔空回即中止並以 1 退出，避免名單後段靜默沒新聞而步驟仍顯示成功。
- `--fail-tolerance R`：允許的失敗比例，數百檔規模下零星逾時是常態；預設 0 維持單機「一檔失敗就失敗」的行為。

網站上的個股新聞就是這個步驟預抓的：靜態站沒有後端可寫入，不在名單內的個股新聞區會顯示引導而非留白。

抓取 MOPS 重大事件：

```bash
python3 -m stock_chip.mops --days 14
```

MOPS 目前用來補「重大事件」頁：抓公開資訊觀測站近期重大訊息與公告，存入 SQLite 後可用月曆、股票代號、關鍵字查詢，並點事件查看內文明細。每次抓取 MOPS 時會自動刪除距離目前日期半年以前的事件，避免資料庫長期膨脹。MOPS 公開頁可能受 DNS 或憑證鏈影響；程式會先走 `mopsov.twse.com.tw`，失敗時改用已驗證過的同站 IP fallback。

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
python3 -m stock_chip.gui --host 127.0.0.1 --port 8502
```

然後打開：

```text
http://127.0.0.1:8502
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
- MOPS 重大事件熱度月曆、單日事件列表與事件明細
- 週報：上週表現、資金主要流向與下週事件行事曆（見下）
- 資料來源速查與未接入來源備忘

### 週報

分頁「週報」以**最近一個已收完的交易週**（用 `trading_days` 依 ISO 週切，連假短週照算，
但會標明該週只有幾個交易日）為區間，一週更新一次。全部由已入庫資料計算，
不新增任何抓取步驟。

- **上週主流強勢股 / 投機飆股分開列**。純漲幅榜會被小型投機股佔滿——實測某週漲幅前 10
  有 8 檔是低量小型股。日均成交額 ≥ 0.5 億且法人週買超為正才算「主流」，其餘歸投機側。
- **資金主要流向**四層：大盤（TAIEX 週漲跌、外資台指未平倉週變、全市場融資餘額週變）、
  族群（法人週買賣超，細分類優先）、個股（外資／投信週買超前 15）、
  ETF（單位數週變＝初級市場申購贖回）。
- **下週重點行事曆**：`mops_events` 的 `event_date` 是**發言日不是事件日**，未來事件藏在公告
  內文的具名欄位裡（`1.召開法人說明會之日期:115/08/07`）。週報只抽具名欄位而非全文抓日期——
  後者實測會把「盈利警告」「停工函」誤判成事件。再併入 TPEx 除權息預告表補上櫃個股。
  行事曆分層顯示：法說會、除權息交易日、現增認股基準日與自選股事件預設展開，
  財報董事會（實測佔某週 1017 件中的 739 件）等低訊號類型收在「展開其餘 N 件」後面。

### 本機資料更新原則

- 一鍵更新適合日常更新官方行情、法人買賣超、融資融券、營收增量、新聞同步、MOPS 與排行重算。
- 全市場分點不放在一鍵更新；雲端每日已自動抓排行前 300 檔＋自選股的分點，本機只在個股頁需要時單獨更新，用來輔助判斷，不參與全市場基礎排名。
- 全市場營收第一次需要補 24 個月歷史；之後只補最新已公告月份附近資料，避免每次重抓完整歷史。
- 大盤指數、期貨多空與融資融券採「先補歷史、之後只補缺漏交易日」的方式，避免重複下載已入庫資料。
- 若 clone 專案到新電腦，請先跑本機 GUI 或 CLI 的更新項目建立自己的 SQLite；GitHub Pages 上的快照不是本機資料庫。

## GitHub Pages 靜態版

GitHub Pages 版不啟動 Python 後端，也不會即時抓資料；它只讀匯出的 `docs/data/*.json`，適合公開給別人看排行、族群熱力圖、全市場搜尋，以及點進個股看快取資料。

**部署來源**：GitHub Actions 每日把 `docs/` 發布到 `site` 分支（強制覆蓋、單一 commit），Pages 設定需指向 **branch `site`、folder `/docs`**（Settings → Pages → Deploy from a branch）。main 上的 `docs/` 僅作為程式碼的一部分（介面 HTML），資料不再提交到 main。

搜尋索引（search_index）涵蓋**全市場**已入庫股票；個股完整明細（K 線、營收、每日進出）僅匯出排行與自選股票，其餘股票在個股頁顯示精簡版指標。

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

### 台股資料自動更新（含分點，全雲端）

Repo 內有 GitHub Actions workflow：`.github/workflows/update-taiwan-data.yml`。整條流程在 GitHub 的雲端主機執行，**不需要任何自家電腦開機或參與**。

- 定時：台股交易日（週一至週五）22:30 台北時間自動執行；也可在 Actions 頁手動觸發 `Update Taiwan Stock Data` 並指定天數。
- 每次執行的步驟依序為：
  1. 還原共用 SQLite 快取（actions cache，增量更新避免全量重抓）
  2. 抓官方行情、三大法人、融資融券（20 個交易日）＋公司基本資料與產業分類
  3. 初步掃描產生排行，據此更新月營收（排行優先補缺漏＋全市場分批補齊）
  4. 更新大盤指數、期貨多空與 MOPS 重大事件
  5. **抓分點買賣超**：各排行前 80 名聯集＋總分前 300＋自選股，每檔前 10 大買賣超分點（MoneyDJ 券商鏡像，最近 20 個交易日區間）
  6. 重算 5 日與 20 日排行（含動能、風險、估值、多因子與共振分數）
  7. **抓個股新聞**：各排行前 50 名聯集＋自選股（約 412 檔）的 Yahoo 股市 RSS 標題；排在重算排行之後，讀到的是最終排行
  8. `export_static` 匯出網站資料，發布到 `site` 分支（GitHub Pages 自動重新部署），reports 提交回 main
- 營收、大盤、MOPS、分點與新聞等步驟設為 `continue-on-error`，個別來源暫時失效不會中斷整體更新。
- 不依賴也不會修改你本機的 `data/stock_chip.sqlite`。

### 分點資料來源與手動補抓

- 來源：MoneyDJ 系統的券商網站鏡像（富邦 `fubon-ebrokerdj.fbs.com.tw` 為主、元大 `jdata.yuanta.com.tw` 備援），免登入、GitHub 雲端 IP 可直連。區間主力進出提供每檔前 15 大買賣超分點（買進/賣出/買賣超張數；**無均價欄位**，均價相關欄位顯示空白、均價相關加減分自動略過）。
- 手動補抓：Actions 頁觸發 `Fetch Branch Data`（`.github/workflows/fetch-branch.yml`），可指定「前 N 大分點」與「排行前幾檔」，抓完自動觸發重新匯出。中斷後重跑會自動續傳（`--skip-existing`）。
- 歷史沿革：原始來源 HiStock 於 2026-06 起將分點日報改為登入後才顯示，曾短暫改用自家電腦當 self-hosted runner（住宅 IP）繞過雲端 IP 封鎖，換到 MoneyDJ 後已不需要——舊的 self-hosted workflow 已移除，Mac runner 可自行解除註冊。細節見 [OPTIMIZATION.md](OPTIMIZATION.md)。

### 美股新聞自動更新

Repo 內有 GitHub Actions workflow：`.github/workflows/fetch-us-news.yml`。

- 定時：每小時約第 17 分鐘自動抓 Yahoo Finance、CNBC、MarketWatch RSS。
- 手動：GitHub Actions 頁面可執行 `Fetch US News`。
- 輸出：只更新 `docs/data/us_news.json`，不依賴本機 `data/stock_chip.sqlite`。
- 分類：CI 使用本機 rules 分類，token 為 0；Ollama / AI 細分類仍建議在本機同步後執行。
- 去重：依正規化 URL 產生 `news_id`，同新聞重複抓取時會合併，不會重複顯示；預設每個 RSS 抓 15 則，最多保留 1000 則。

本機若要把 CI 已抓的新聞同步到 SQLite 與 Obsidian，可在 GUI 更新中心執行「同步 GitHub 新聞到本機」，或使用 CLI：

```bash
python3 -m stock_chip.import_us_news_static --json docs/data/us_news.json --db data/stock_chip.sqlite
```

同步後新聞會進入本機 SQLite，再由本機規則重新分類並輸出 Obsidian vault。這一步是本機行為，不會由 GitHub Actions 自動寫回你的電腦。

## 新環境啟動建議

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m stock_chip.gui --host 127.0.0.1 --port 8502
```

第一次使用建議在 GUI 的「資料狀態」依序完成：

1. 更新官方行情與排行。
2. 補齊全市場營收。
3. 更新融資融券。
4. 更新大盤指數與期貨多空。
5. 抓取或同步美股新聞。
6. 需要分點時，到個股頁單獨按「更新分點資訊」。

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

## 資料來源備忘

目前已接入：

- TWSE / TPEx：行情、成交量、三大法人買賣超、融資融券、上市上櫃清單。
- TAIFEX：大台、小台、微台三大法人期貨多空與未平倉。
- FinMind：目前用於月營收；分點資料實測免費等級無權限（需 sponsor），不採用。
- MoneyDJ 券商鏡像（富邦/元大）：分點排行與單一分點日明細，免登入、雲端可直連；無分點均價欄位。
- HiStock：已停用——2026-06 起分點日報改為登入後才顯示。
- MOPS 公開資訊觀測站：重大訊息與公告，已接入事件月曆；之後可再擴到法說會、財報公告與官方月營收追溯。
- Yahoo 股市、Yahoo Finance、CNBC、MarketWatch：台股與美股新聞標題、連結、摘要。

已確認、之後可評估：

- Stooq：適合美股、全球指數、匯率、商品等 OHLCV 歷史資料備援；不適合補台股法人、分點、融資融券。
- Goodinfo / CMoney / Wantgoo：資料豐富但偏網頁或商業服務，需先確認授權與穩定性，不建議直接作為核心免費來源。
- TWSE 買賣日報表：公開頁面目前有驗證碼，不適合穩定自動化；若未來有官方 API 或下載檔再重新評估。

## 資料源驗證

```bash
python3 -m stock_chip.probe --days 5 --top 5
```

若要測 FinMind token：

```bash
FINMIND_TOKEN=你的token python3 -m stock_chip.probe --days 5 --top 5
```

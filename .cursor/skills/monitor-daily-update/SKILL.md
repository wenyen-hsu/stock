---
name: monitor-daily-update
description: >-
  每天台北 03:00 檢查 wenyen-hsu/stock 的雲端更新是否完成；失敗就修到行情與網站上線。
  用在 /subscribe 醒來、Cursor Automation 排程、或使用者問「今天更新了沒」。
---

# 每日更新監測

對象：`wenyen-hsu/stock`。時區一律 **Asia/Taipei**。

排程管線：週一至週五 22:30 跑 `Update Taiwan Stock Data`（UTC `30 14 * * 1-5`）。
分點接著跑 `Fetch Branch Data`，再 dispatch 同一條管線重新匯出。
03:00 檢查時，前一晚整條（含分點重匯）應該已經結束。

## 醒來後先做

1. `TZ=Asia/Taipei date`，算出「該有資料的最後交易日」`D`：
   - 週二至週六 03:00：`D` = 昨天（週一至週五）。
   - 週日、週一 03:00：`D` = 上週五。不因週六日沒跑排程而重跑。
   - 國定假日：排程仍會觸發，但官方可能沒有新交易日。`site` 的 `latest_date` 停在前一交易日且 `health.json` 的 `data_freshness` 不是 fail → 視為正常，不要重跑。
2. 讀本檔，不要改分數、硬門檻、或另造資料源。

## 成功條件（全部要過）

用 `gh`，repo 是 `wenyen-hsu/stock`。

1. **排程那一輪**（只在 `D` 是平日時檢查）  
   `gh run list --workflow="Update Taiwan Stock Data" --limit 10`  
   必須有一筆 `event=schedule`、約 `D` 當天 14:30 UTC 之後開始、`conclusion=success`。  
   只有 `workflow_dispatch`、沒有當日 `schedule` → 算漏跑。
2. **網站資料日**  
   `git fetch origin site --depth 1`  
   `origin/site:docs/data/meta.json` 的 `latest_date` ≥ `D`（假日則 ≥ 前一交易日）。  
   `exported_at` 必須落在該輪更新之後，不能停在更早的一天。
3. **健康**  
   `origin/site:docs/data/health.json`：`ok == true`，`fail_count == 0`。  
   `warn` 可記，不必為 warn 開修程式 PR。
4. **Pages**  
   `gh api repos/wenyen-hsu/stock/pages/builds/latest` 的 `status` 為 `built`。
5. **分點（平日 `D`）**  
   `Fetch Branch Data` 在當日 schedule 成功之後應有一筆 `workflow_run` + `success`。  
   被 skip 是因為上游不是 schedule（迴圈防護），不要把 dispatch 重匯誤當成漏抓。  
   分點失敗只影響分點；行情已上線時先保證行情，再補跑分點。

## 沒過就修到過

依序，做完一步就重跑上面的成功條件。

1. **當日 schedule 沒跑或失敗**  
   讀失敗 job log（路徑錯、JSON 404、限速、push 衝突）。  
   - 暫時性：`gh workflow run "Update Taiwan Stock Data" --ref main`，等到結束。  
   - 程式 bug：開 `cursor/…-2b73` 分支修、測、PR、CI 綠後合併，再 `workflow run`。  
   必跑步驟失敗才算管線失敗；`continue-on-error` 步驟失敗看 `health.json` 對應來源。
2. **schedule 綠但 `latest_date` 舊**  
   再 dispatch 一次 `Update Taiwan Stock Data`（會重新匯出並推 `site`）。
3. **分點沒接上**  
   `gh workflow run "Fetch Branch Data" --ref main`，等它自己觸發重匯。
4. **Pages 不是 built**  
   `gh api -X POST repos/wenyen-hsu/stock/pages/builds`，再查 latest。
5. **`health.json` fail**  
   對照 `stock_chip/health.py` 的來源。能重抓就重跑對應模組所在的 workflow；來源改版就修 parser。  
   管線已有 `python -m stock_chip.health --github-issue`；有 open 的 `data-health` issue 一併處理。

重試上限：同一晚同一故障最多 3 次 dispatch。第 3 次仍失敗 → 把 log、已試過的修法、剩餘阻礙寫進這則對話（或 PR／issue），不要無限重跑。

## 不要做

- 不要改當沖硬門檻、分數權重、JSON 既有欄名（除非那就是今晚的 bug）。
- 不要為了監測改 cron 或把分點併回每日管線。
- 週末／假日資料日正確時不要「補跑昨天」。
- 不要把本機 `data/stock_chip.sqlite` 當成網站資料來源；網站只看 `site` 分支。

## 過了怎麼回

三行就夠：`D`、schedule run id、`meta.latest_date` / `exported_at`、health ok。然後等下一次 03:00。

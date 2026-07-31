"""normalize_scan_row 是 CSV → 發布 JSON 的白名單，漏欄不會報錯只會靜默消失。

這個坑踩過兩次：
1. 錯殺價值排行上線後分數全是 null（mispriced_* 欄沒進白名單）
2. 持股警示 4 條規則有 3 條從未觸發——suggest.json 的 f/g/t 在線上恆為 None
   （latest_foreign_net_lot / latest_trust_net_lot / close_vs_avg_pct 沒進白名單）

兩次都不是程式壞掉，是「新增 scan 欄位時忘記登記」。這裡把真實 CSV 的欄名
拿來對照，任何未登記也未明確排除的欄位都會讓測試失敗。
"""
import csv
from pathlib import Path

import pytest

from stock_chip.gui import normalize_scan_row

REPORT = Path("reports/scan_all_20d.csv")

# 明確不上網站的欄位：僅供 CLI 報表或中間計算，加進來只會膨脹 6MB 的
# search_index.json。新增排除項時請寫清楚理由。
DELIBERATELY_DROPPED = {
    "days",              # 由呼叫端以參數傳入，不重複存
    "latest_date",       # normalize 會另外處理
}


def _csv_columns() -> list[str]:
    with REPORT.open(encoding="utf-8-sig") as f:
        return next(csv.reader(f))


@pytest.mark.skipif(not REPORT.exists(), reason="需要 reports/scan_all_20d.csv")
def test_every_scan_column_is_registered_or_explicitly_dropped():
    columns = _csv_columns()
    with REPORT.open(encoding="utf-8-sig") as f:
        sample = next(csv.DictReader(f))
    exported = set(normalize_scan_row(sample, 20))
    # 20d_foreign_net_lot 這類帶天數前綴的欄位在 normalize 後改名為 foreign_net_lot
    renamed = {column.replace("20d_", "") for column in columns}
    missing = sorted(
        column
        for column in columns
        if column not in exported
        and column.replace("20d_", "") not in exported
        and column not in DELIBERATELY_DROPPED
    )
    assert not missing, (
        f"以下 scan 欄位沒進 normalize_scan_row 白名單，發布後會靜默變成 null：{missing}\n"
        f"要嘛加進 normalize_scan_row，要嘛加進本測試的 DELIBERATELY_DROPPED 並註明理由。"
    )
    assert renamed  # 保護上面的改名推導不被誤刪


@pytest.mark.skipif(not REPORT.exists(), reason="需要 reports/scan_all_20d.csv")
def test_holding_alert_inputs_survive_normalization():
    """持股警示與外資動向排行實際依賴的欄位，用真實資料確認不是 None。"""
    with REPORT.open(encoding="utf-8-sig") as f:
        rows = [normalize_scan_row(row, 20) for _, row in zip(range(300), csv.DictReader(f))]
    for field in ("latest_foreign_net_lot", "latest_trust_net_lot", "close_vs_avg_pct", "volume_ratio_1d"):
        assert any(row.get(field) is not None for row in rows), f"{field} 在前 300 檔全是 None"

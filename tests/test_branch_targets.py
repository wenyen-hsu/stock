"""分點抓取名單：各排行聯集取代單一總分排序的回歸測試。"""
import csv
import tempfile
from pathlib import Path

from stock_chip.revenue import union_ranking_stock_ids


def _write_ranking(dir_path: Path, name: str, stock_ids: list[str]) -> None:
    with (dir_path / f"ranking_{name}_20d.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["stock_id", "name"])
        writer.writeheader()
        for stock_id in stock_ids:
            writer.writerow({"stock_id": stock_id, "name": f"股{stock_id}"})


def test_union_covers_every_ranking_top_n():
    """每個排行的前 N 名都必須進入名單——單一 total_score 選法會漏掉
    錯殺價值/爆量榜的標的（實測前 20 只命中 1 檔）。"""
    tmp = Path(tempfile.mkdtemp())
    _write_ranking(tmp, "total_score", ["1001", "1002", "1003"])
    _write_ranking(tmp, "mispriced_value", ["2001", "2002", "2003"])
    _write_ranking(tmp, "volume_expansion", ["3001", "3002", "3003"])
    ids = union_ranking_stock_ids(tmp, 20, 3)
    for expected in ("1001", "1002", "1003", "2001", "2002", "2003", "3001", "3002", "3003"):
        assert expected in ids


def test_union_is_interleaved_by_rank():
    """交錯排列：各榜第 1 名排在所有榜第 2 名之前，
    這樣下游 --limit 截斷時仍覆蓋每個榜的前段。"""
    tmp = Path(tempfile.mkdtemp())
    _write_ranking(tmp, "aaa", ["A1", "A2", "A3"])
    _write_ranking(tmp, "bbb", ["B1", "B2", "B3"])
    ids = union_ranking_stock_ids(tmp, 20, 3)
    assert ids[:2] == ["A1", "B1"]
    assert ids[2:4] == ["A2", "B2"]
    # 截斷到 4 檔時，兩個榜的前 2 名都在
    assert set(ids[:4]) == {"A1", "B1", "A2", "B2"}


def test_union_dedupes_across_rankings():
    tmp = Path(tempfile.mkdtemp())
    _write_ranking(tmp, "aaa", ["X1", "X2"])
    _write_ranking(tmp, "bbb", ["X1", "X3"])
    ids = union_ranking_stock_ids(tmp, 20, 2)
    assert ids.count("X1") == 1
    assert set(ids) == {"X1", "X2", "X3"}


def test_union_respects_per_ranking_limit():
    tmp = Path(tempfile.mkdtemp())
    _write_ranking(tmp, "aaa", [f"A{i}" for i in range(10)])
    ids = union_ranking_stock_ids(tmp, 20, 3)
    assert ids == ["A0", "A1", "A2"]


def test_union_empty_when_no_reports():
    assert union_ranking_stock_ids(Path(tempfile.mkdtemp()), 20, 10) == []
    assert union_ranking_stock_ids(Path("/nonexistent"), 20, 10) == []


def test_union_ignores_other_day_windows():
    """只讀對應天期的排行 CSV（20 日名單不該混入 5 日）。"""
    tmp = Path(tempfile.mkdtemp())
    _write_ranking(tmp, "aaa", ["A1"])
    with (tmp / "ranking_bbb_5d.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["stock_id", "name"])
        writer.writeheader()
        writer.writerow({"stock_id": "B1", "name": "五日榜"})
    ids = union_ranking_stock_ids(tmp, 20, 5)
    assert ids == ["A1"]


def test_daily_max_requests_caps_one_run():
    """新增股票要回補 days 天，名單一擴大單次執行會爆長（實測 200 檔 × 20 天
    ≈ 66 分鐘）。上限讓回補分散到多日，每日管線時間可預期。"""
    import datetime as dt
    from stock_chip.branch import run_branch_daily
    from stock_chip.official import connect_db

    tmp = Path(tempfile.mkdtemp()) / "branch.sqlite"
    with connect_db(tmp) as conn:
        for i in range(5):
            date = f"2026-07-{20 + i:02d}"
            conn.execute(
                "INSERT OR IGNORE INTO trading_days(date, market, price_count, institutional_count,"
                " margin_count, updated_at) VALUES (?, 'TWSE', 1, 1, 1, 'now')",
                (date,),
            )
            for stock_id in ("1101", "1102"):
                conn.execute(
                    "INSERT INTO daily_prices(date, stock_id, name, close, updated_at)"
                    " VALUES (?, ?, ?, 10.0, 'now')",
                    (date, stock_id, f"股{stock_id}"),
                )
        conn.commit()

    # 上限 3：兩檔 × 五天共 10 個請求，實際只該發 3 個就收工
    result = run_branch_daily(
        tmp, days=5, top_n=5, stock_ids=["1101", "1102"],
        sleep_seconds=0.0, retry_sleeps=(), max_requests=3,
    )
    assert result["request_count"] == 3
    assert result["budget_exhausted"] is True


def test_daily_without_cap_is_unbounded():
    """未設上限時行為不變（回歸保護）。"""
    from stock_chip.branch import run_branch_daily
    from stock_chip.official import connect_db

    tmp = Path(tempfile.mkdtemp()) / "branch2.sqlite"
    with connect_db(tmp) as conn:
        for i in range(2):
            date = f"2026-07-{20 + i:02d}"
            conn.execute(
                "INSERT OR IGNORE INTO trading_days(date, market, price_count, institutional_count,"
                " margin_count, updated_at) VALUES (?, 'TWSE', 1, 1, 1, 'now')",
                (date,),
            )
            conn.execute(
                "INSERT INTO daily_prices(date, stock_id, name, close, updated_at)"
                " VALUES (?, '1101', '台泥', 10.0, 'now')",
                (date,),
            )
        conn.commit()
    result = run_branch_daily(
        tmp, days=2, top_n=5, stock_ids=["1101"],
        sleep_seconds=0.0, retry_sleeps=(), max_requests=0,
    )
    assert result["budget_exhausted"] is False
    assert result["request_count"] == 2

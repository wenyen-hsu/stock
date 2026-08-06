"""管線側新聞抓取：名單解析與大量抓取的護欄。

個股新聞原本只有本機 GUI 按鈕會抓，靜態站無法寫入，導致線上每檔都是「無」。
改由管線預抓約 400 檔後，news.py 從「單機小量」變成「CI 大量」情境，
下列護欄是為此加的，破壞了會靜默地讓名單後段沒新聞。
"""
import argparse
import csv
import sqlite3

import pytest

from stock_chip import news


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        db="data/stock_chip.sqlite",
        watchlist="",
        include_db_watchlist=False,
        days=20,
        from_rankings=0,
        from_scan=0,
        reports_dir="reports",
        scan_report="reports/scan_all_20d.csv",
        max_requests=0,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _write_ranking(dir_path, name: str, stock_ids: list[str]) -> None:
    with (dir_path / f"ranking_{name}_20d.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["stock_id", "name"])
        writer.writeheader()
        for stock_id in stock_ids:
            writer.writerow({"stock_id": stock_id, "name": f"股{stock_id}"})


def test_targets_put_watchlist_first_so_budget_cuts_the_tail(tmp_path):
    """--max-requests 削掉的必須是低優先標的：自選股永遠排在排行聯集之前。"""
    tmp = tmp_path
    _write_ranking(tmp, "total_score", ["1001", "1002", "1003"])
    ids = news.resolve_targets(_args(watchlist="9999", from_rankings=3, reports_dir=str(tmp), max_requests=2))
    assert ids == ["9999", "1001"]


def test_targets_dedupe_watchlist_against_rankings(tmp_path):
    tmp = tmp_path
    _write_ranking(tmp, "total_score", ["1001", "1002"])
    ids = news.resolve_targets(_args(watchlist="1002", from_rankings=2, reports_dir=str(tmp)))
    assert ids == ["1002", "1001"]


def test_targets_include_db_watchlist(tmp_path):
    db_path = tmp_path / "t.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE user_watchlist (stock_id TEXT)")
        conn.executemany("INSERT INTO user_watchlist VALUES (?)", [("2330",), ("2454",)])
    ids = news.resolve_targets(_args(db=str(db_path), include_db_watchlist=True))
    assert ids == ["2330", "2454"]


def test_db_watchlist_missing_table_is_not_fatal(tmp_path):
    """尚未用過 GUI 的環境沒有 user_watchlist；CI 不該因此整步驟失敗。"""
    db_path = tmp_path / "empty.sqlite"
    sqlite3.connect(db_path).close()
    assert news.db_watchlist_ids(db_path) == []


def test_empty_streak_aborts_on_suspected_throttling(monkeypatch, tmp_path):
    """Yahoo 被限速時回的是空 feed 而非錯誤——連續空回必須中止，
    否則名單後段會靜默地全部沒新聞，而步驟仍顯示成功。"""
    calls: list[str] = []

    def fake_fetch(stock_id, limit=5, fetch_content=True):
        calls.append(stock_id)
        return [
            news.NewsItem(stock_id, "標題", f"https://x/{stock_id}", "Yahoo股市", "", "", "", "now")
        ] if stock_id == "A" else []

    monkeypatch.setattr(news, "fetch_yahoo_news", fake_fetch)
    result = news.refresh_many_stock_news(
        tmp_path / "n.sqlite",
        ["A", "B", "C", "D", "E", "F"],
        sleep_seconds=0,
        empty_streak_abort=2,
    )
    assert calls == ["A", "B", "C"]
    assert "中止剩餘 3 檔" in result["aborted"]


def test_no_abort_before_anything_succeeds(monkeypatch, tmp_path):
    """名單開頭剛好都是冷門股全空，不算限速；got_any 為假時不中止。"""
    monkeypatch.setattr(news, "fetch_yahoo_news", lambda *a, **k: [])
    result = news.refresh_many_stock_news(
        tmp_path / "n.sqlite", ["A", "B", "C"], sleep_seconds=0, empty_streak_abort=2
    )
    assert result["aborted"] == ""
    assert result["stock_count"] == 3
    assert result["empty_count"] == 3


def test_failures_are_counted_not_raised(monkeypatch, tmp_path):
    """單檔逾時只記錄；400 檔規模下必然發生，不該中斷整批。"""
    def fake_fetch(stock_id, limit=5, fetch_content=True):
        if stock_id == "B":
            raise RuntimeError("timeout")
        return []

    monkeypatch.setattr(news, "fetch_yahoo_news", fake_fetch)
    result = news.refresh_many_stock_news(
        tmp_path / "n.sqlite", ["A", "B", "C"], sleep_seconds=0, empty_streak_abort=0
    )
    assert result["stock_count"] == 3
    assert result["failed_count"] == 1


def test_targets_empty_raises_in_main(monkeypatch):
    monkeypatch.setattr(news, "parse_args", lambda: _args(empty_streak=0, limit=5, sleep=0, content=False, fail_tolerance=0.0))
    with pytest.raises(SystemExit):
        news.main()

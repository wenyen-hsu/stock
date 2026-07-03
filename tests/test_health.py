"""來源健康檢查：以臨時 DB 造出 ok / fail / not_deployed 三種狀態。"""
import datetime as dt
import tempfile
from pathlib import Path

from stock_chip.health import collect_health, health_markdown, trading_days_back
from stock_chip.official import connect_db


def make_db(rows_per_day: int = 900) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "health.sqlite"
    dates = [(dt.date(2026, 6, 20) + dt.timedelta(days=i)).isoformat() for i in range(10)]
    with connect_db(tmp) as conn:
        conn.executemany(
            "INSERT INTO trading_days(date, price_count, institutional_count, updated_at) VALUES (?,1,1,'now')",
            [(d,) for d in dates],
        )
        latest = dates[-1]
        for i in range(rows_per_day):
            stock_id = f"{1000 + i}"
            conn.execute(
                "INSERT INTO daily_prices(date, stock_id, name, close, updated_at) VALUES (?,?,?,?,'now')",
                (latest, stock_id, "x", 10.0),
            )
            conn.execute(
                "INSERT INTO institutional_trades(date, stock_id, name, updated_at) VALUES (?,?,?,'now')",
                (latest, stock_id, "x"),
            )
        conn.commit()
    return tmp


def test_collect_health_states():
    db = make_db()
    payload = collect_health(db)
    by_key = {source["key"]: source for source in payload["sources"]}
    assert by_key["daily_prices"]["status"] == "ok"
    assert by_key["institutional_trades"]["status"] == "ok"
    # 融資融券完全沒資料 → fail
    assert by_key["margin_trades"]["status"] == "fail"
    # 每日明細/快照是 warn_only
    assert by_key["broker_branch_daily"]["status"] == "warn"
    assert payload["ok"] is False
    assert payload["fail_count"] >= 1


def test_min_rows_threshold():
    db = make_db(rows_per_day=10)  # 低於 800 門檻
    payload = collect_health(db)
    by_key = {source["key"]: source for source in payload["sources"]}
    assert by_key["daily_prices"]["status"] == "fail"
    assert "門檻" in by_key["daily_prices"]["note"]


def test_empty_db_reports_uninitialized():
    tmp = Path(tempfile.mkdtemp()) / "empty.sqlite"
    with connect_db(tmp):
        pass
    payload = collect_health(tmp)
    assert payload["ok"] is False
    assert payload["latest_trading_day"] is None


def test_markdown_render():
    payload = collect_health(make_db())
    text = health_markdown(payload)
    assert "| 來源 |" in text
    assert "日行情" in text


def test_trading_days_back():
    db = make_db()
    with connect_db(db) as conn:
        latest = conn.execute("SELECT MAX(date) FROM trading_days").fetchone()[0]
        assert trading_days_back(conn, latest, 0) == latest
        one_back = trading_days_back(conn, latest, 1)
        assert one_back < latest

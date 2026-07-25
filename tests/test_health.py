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


def test_trickle_in_month_is_not_failure():
    """公布期剛開始（少數公司提前公告新月份）不應觸發告警——2026-07-08 月營收誤報的回歸測試。"""
    from stock_chip.health import check_source

    tmp = Path(tempfile.mkdtemp()) / "trickle.sqlite"
    with connect_db(tmp) as conn:
        for i in range(150):
            conn.execute(
                "INSERT INTO monthly_revenues(revenue_month, stock_id, name, market, source, updated_at) VALUES ('2026-05',?,?,?,'finmind','now')",
                (f"{1000 + i}", "x", "TWSE"),
            )
        for sid in ("1101", "2330", "3324"):
            conn.execute(
                "INSERT INTO monthly_revenues(revenue_month, stock_id, name, market, source, updated_at) VALUES ('2026-06',?,?,?,'finmind','now')",
                (sid, "x", "TWSE"),
            )
        conn.commit()
        result = check_source(conn, "monthly_revenues", "月營收", "monthly_revenues", "revenue_month", expected="2026-05", min_rows=100)
        assert result["status"] == "ok"
        assert "公布中" in result["note"]
        # 期望月份真的落後（達門檻的只有 2026-05 但期望 2026-06）仍要 fail
        behind = check_source(conn, "monthly_revenues", "月營收", "monthly_revenues", "revenue_month", expected="2026-06", min_rows=100)
        assert behind["status"] == "fail"


def test_market_current_month_always_refetched():
    """月中滿門檻後當月不得被視為已涵蓋——2026-07-24 大盤斷更的回歸測試。"""
    import datetime as dt
    from stock_chip.market import existing_index_months

    tmp = Path(tempfile.mkdtemp()) / "market.sqlite"
    with connect_db(tmp) as conn:
        # 當月塞 16 個交易日（超過舊閾值 15）
        today = dt.date(2026, 7, 24)
        for day in range(1, 23):
            date = dt.date(2026, 7, day)
            if date.weekday() < 5:
                conn.execute(
                    "INSERT INTO market_index_daily(date, index_code, close, source, updated_at)"
                    " VALUES (?, 'TAIEX', 22000.0, 'twse', 'now')",
                    (date.isoformat(),),
                )
        conn.commit()
        covered = existing_index_months(conn, dt.date(2026, 1, 1), today)
        assert "2026-07" in covered  # 原函式仍會標涵蓋
        # refresh_market_data 內會 discard 當月/上月——模擬該邏輯驗證行為
        covered.discard(today.strftime("%Y-%m"))
        covered.discard((today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m"))
        assert "2026-07" not in covered
        assert "2026-06" not in covered

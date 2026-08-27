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
                    "INSERT INTO market_index_daily(date, index_code, index_name, close, source, updated_at)"
                    " VALUES (?, 'TAIEX', '加權指數', 22000.0, 'twse', 'now')",
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


def test_freshness_detects_stalled_ingest():
    """最新交易日整天未入庫時必須被抓到——其他檢查以 trading_days 為期望，
    行情與 trading_days 一起停住便比不出異常（2026-07-30 事故盲點）。"""
    import datetime as dt

    from stock_chip.health import check_freshness

    # 週五檢查、資料到週四 → 落後 1 個平日，正常
    assert check_freshness("2026-07-30", dt.date(2026, 7, 31))["status"] == "ok"
    # 週五檢查、資料停在週三 → 落後 2 個平日，warn
    warn = check_freshness("2026-07-29", dt.date(2026, 7, 31))
    assert warn["status"] == "warn" and "落後 2 個平日" in warn["note"]
    # 週一檢查、資料停在上週二 → 落後 4 個平日（週末不計），fail
    fail = check_freshness("2026-07-28", dt.date(2026, 8, 3))
    assert fail["status"] == "fail" and "很可能中斷" in fail["note"]
    # 落後 3 個平日仍只是 warn：台股連假（例如週一補假）會造成此落差
    assert check_freshness("2026-07-29", dt.date(2026, 8, 3))["status"] == "warn"
    # 週一檢查、資料到上週五 → 落後 1 個平日（週末不計），正常
    assert check_freshness("2026-07-31", dt.date(2026, 8, 3))["status"] == "ok"


def test_weekdays_between_skips_weekend():
    import datetime as dt

    from stock_chip.health import weekdays_between

    assert weekdays_between(dt.date(2026, 7, 31), dt.date(2026, 8, 3)) == 1  # 五→一
    assert weekdays_between(dt.date(2026, 7, 29), dt.date(2026, 7, 31)) == 2  # 三→五
    assert weekdays_between(dt.date(2026, 7, 31), dt.date(2026, 7, 31)) == 0


# ---------- 分點排行的期望值必須配合架構 ----------
# 2026-08-06（commit 9712b17d）把分點抓取拆成獨立 workflow 後，它在主管線之後
# 才跑、要兩小時。健檢執行的當下分點必然落後兩個交易日，但期望值還留在 one_back，
# 於是每個交易日都必定 fail 一次、開一則 issue，再由分點跑完後的重新匯出關掉。
# 實測 #7(8/14)、#15(8/19)、#19(8/27) 唯一的 fail 都是這項；拆分前的 #3(7/24) 正常。

def _db_with_branch_topn(as_of: str) -> Path:
    """在 make_db 的基礎上補一批分點排行資料。"""
    db = make_db()
    with connect_db(db) as conn:
        for i in range(150):
            conn.execute(
                "INSERT INTO broker_branch_topn(as_of_date, from_date, to_date,"
                " window_days, stock_id, name, rank_side, rank_no, broker_name,"
                " buy_lot, sell_lot, net_lot, source, source_url, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'now')",
                (as_of, as_of, as_of, 20, f"{1000 + i}", "x", "buy", i + 1,
                 "某分點", 10.0, 0.0, 10.0, "moneydj", "http://x"),
            )
        conn.commit()
    return db


def _branch_status(as_of: str) -> str:
    payload = collect_health(_db_with_branch_topn(as_of))
    return {s["key"]: s for s in payload["sources"]}["broker_branch_topn"]["status"]


def _trading_day_back(steps: int) -> str:
    """從 DB 實際的最新交易日回推，不把 make_db() 的日期區間寫死在測試裡。"""
    with connect_db(make_db()) as conn:
        latest = conn.execute("SELECT MAX(date) FROM trading_days").fetchone()[0]
        return trading_days_back(conn, latest, steps)


def test_branch_topn_ok_when_two_trading_days_behind():
    """主管線跑健檢的當下就是這個狀態——不能因此每晚開一則 issue。"""
    assert _branch_status(_trading_day_back(2)) == "ok", (
        "落後兩個交易日是拆分後的正常狀態；若這裡是 fail，"
        "每個交易日都會開一則自動關閉的 issue，久了沒人會看告警"
    )


def test_branch_topn_still_fails_when_genuinely_stale():
    """放寬一天不能放棄偵測：分點真的斷一天就要抓到。"""
    assert _branch_status(_trading_day_back(3)) == "fail"


def test_branch_topn_and_daily_share_the_same_lag_assumption():
    """兩者由同一個 workflow 產出，期望值不該一鬆一緊。

    先前 daily 用 two_back、topn 用 one_back，兩張表實際上總是同一天——
    差異純粹是拆分時漏改，不是刻意的設計。

    比對 payload 裡的 expected 而不是讀原始碼：這裡原本用 inspect.getsource()
    抓字串，換個引號或抽出 helper 就會無關地壞掉，而且驗的是寫法不是行為。
    """
    payload = collect_health(_db_with_branch_topn(_trading_day_back(2)))
    by_key = {s["key"]: s for s in payload["sources"]}
    assert by_key["broker_branch_topn"]["expected"] == by_key["broker_branch_daily"]["expected"], (
        "分點排行與分點每日明細由同一個 workflow 產出，期望的資料日必須一致"
    )

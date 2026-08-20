"""抓取失敗要留下外部證據，健檢才不必靠日曆猜。

本專案踩過三次同一個形狀：抓取失敗 → 被 continue-on-error 吞掉 → 管線報成功
→ 資料悄悄缺一塊而頁面完全正常。2026-08-18 是最近一次：TWSE 逾時讓整個交易日
沒進站，健檢卻因為「落後一天算正常」而放行。

關鍵是抓取端**知道**自己失敗了，只是把訊息 print 掉。這組測試釘住那個訊號
從記錄到偵測、再到重跑自癒的完整路徑。
"""
import datetime as dt
import sqlite3

import pytest

from stock_chip import health
from stock_chip.official import (
    clear_fetch_failure,
    init_schema,
    record_fetch_failure,
    unresolved_fetch_failures,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


# ---------- 記錄與自癒 ----------

def test_failure_is_recorded(conn):
    record_fetch_failure(conn, "official", "2026-08-18", "Read timed out")
    rows = unresolved_fetch_failures(conn)
    assert len(rows) == 1
    assert rows[0]["source"] == "official"
    assert rows[0]["target_date"] == "2026-08-18"
    assert "timed out" in rows[0]["error"]


def test_retry_clears_it(conn):
    """8/18 那次是下一輪重跑補回來的——沒有這步，一次逾時會讓健檢永遠紅著。"""
    record_fetch_failure(conn, "official", "2026-08-18", "Read timed out")
    clear_fetch_failure(conn, "official", "2026-08-18")
    assert unresolved_fetch_failures(conn) == []


def test_same_day_failure_is_not_duplicated(conn):
    """同一天重試再失敗只更新，不會每輪累積一筆。"""
    record_fetch_failure(conn, "official", "2026-08-18", "第一次")
    record_fetch_failure(conn, "official", "2026-08-18", "第二次")
    rows = unresolved_fetch_failures(conn)
    assert len(rows) == 1
    assert rows[0]["error"] == "第二次"


def test_sources_are_tracked_separately(conn):
    """上市處置股掛了、上櫃正常，是分開的兩件事，不能互相覆蓋。"""
    record_fetch_failure(conn, "daytrade_disposal", "2026-08-18", "twse 掛了")
    record_fetch_failure(conn, "official", "2026-08-18", "行情逾時")
    clear_fetch_failure(conn, "official", "2026-08-18")
    rows = unresolved_fetch_failures(conn)
    assert [r["source"] for r in rows] == ["daytrade_disposal"]


def test_error_text_is_truncated(conn):
    record_fetch_failure(conn, "official", "2026-08-18", "x" * 5000)
    assert len(unresolved_fetch_failures(conn)[0]["error"]) <= 300


# ---------- 健檢讀得到 ----------

def test_health_is_ok_when_there_are_no_failures(conn):
    out = health.check_fetch_failures(conn)
    assert out["status"] == "ok"
    assert out["rows_at_latest"] == 0


def test_health_fails_and_names_the_date_and_source(conn):
    record_fetch_failure(conn, "official", "2026-08-18", "Read timed out")
    out = health.check_fetch_failures(conn)
    assert out["status"] == "fail", "抓取失敗必須是 fail 才會開 issue，warn 不會"
    assert "official" in out["note"]
    assert "2026-08-18" in out["note"]


def test_health_recovers_after_a_successful_retry(conn):
    record_fetch_failure(conn, "official", "2026-08-18", "Read timed out")
    assert health.check_fetch_failures(conn)["status"] == "fail"
    clear_fetch_failure(conn, "official", "2026-08-18")
    assert health.check_fetch_failures(conn)["status"] == "ok"


def test_many_failures_are_summarised_not_dumped(conn):
    for day in range(10, 20):
        record_fetch_failure(conn, "official", f"2026-08-{day}", "boom")
    out = health.check_fetch_failures(conn)
    assert out["rows_at_latest"] == 10
    assert "另有" in out["note"], "超過 4 筆要收斂，不然 issue 內文會爆掉"


# ---------- 這是 2026-08-18 那次的重現 ----------

def test_the_2026_08_18_blind_spot_is_now_covered(conn):
    """freshness 放行的那個情境，現在必須被抓到。

    當時：最新交易日 2026-08-17，系統日期 2026-08-18，落後 1 個平日 → ok。
    那個容忍本身是對的（假日與盤後未更新都長這樣），問題在於它分不出
    「今天抓失敗了」。所以不動 freshness，改由抓取端的紀錄補上。
    """
    freshness = health.check_freshness("2026-08-17", today=dt.date(2026, 8, 18))
    assert freshness["status"] == "ok", "freshness 的容忍維持原樣，不該為此收緊"

    record_fetch_failure(conn, "official", "2026-08-18", "Read timed out")
    assert health.check_fetch_failures(conn)["status"] == "fail"


# ---------- 週末與假日不能製造噪音（行為測試，不是抓字串）----------
# 這一節原本是用 inspect.getsource() 找 "if missing_day:" 這個字串。
# Copilot 指出那是結構性脆弱測試：改個變數名就誤報，而且根本沒驗到行為——
# 它確實沒抓到真正的 bug。原本的判斷只看「日期比已入庫的新」，於是：
#   週六手動跑    → 週六 > 上週五 → 記成抓取失敗
#   週一國定假日  → 週一 > 上週五 → 記成抓取失敗
# 排除週末也修不好第二種，因為台股假日推不出來。真正的判別依據是來源自己
# 的回答：NoTradingDataError（明確說這天沒資料）vs 其他例外（拿不到回答）。

def _run_update(tmp_path, monkeypatch, end_date, failures):
    """跑 update_recent，用 failures 指定哪些日期要丟出哪種例外。"""
    from stock_chip import official
    from stock_chip.official import connect_db

    db = tmp_path / "u.sqlite"
    seeded = {}

    def fake_update_day(conn, date, **kwargs):
        iso = date.isoformat()
        if iso in failures:
            raise failures[iso]
        seeded[iso] = True
        official.mark_trading_day(conn, date, 2000, 1800, 1800)
        return 2000, 1800, 1800

    monkeypatch.setattr(official, "update_day", fake_update_day)
    with connect_db(db) as c:
        try:
            official.update_recent(c, 2, end_date)
        except official.ProbeError:
            pass          # 天數不足不是這裡要驗的
        c.commit()
    return db


def test_weekend_run_does_not_record_a_failure(tmp_path, monkeypatch):
    """週六手動跑：週六與週日都會被跳過，但它們不是抓取失敗。

    我在 2026-08-15（週六）手動觸發過管線，正是這個情境。
    """
    from stock_chip.official import NoTradingDataError, connect_db

    saturday = dt.date(2026, 8, 15)
    db = _run_update(tmp_path, monkeypatch, saturday, {
        "2026-08-15": NoTradingDataError("TWSE price response is not OK: 很抱歉，沒有符合條件的資料!"),
    })
    with connect_db(db) as c:
        assert unresolved_fetch_failures(c) == [], "週末被跳過不該記成抓取失敗"
        assert health.check_fetch_failures(c)["status"] == "ok"


def test_public_holiday_does_not_record_a_failure(tmp_path, monkeypatch):
    """國定假日：日期比已入庫的新，但來源明確說沒有資料。

    這是「只排除週末」修不好的那一半——台股假日無法從星期幾推知。
    """
    from stock_chip.official import NoTradingDataError, connect_db

    monday = dt.date(2026, 8, 17)   # 平日，假設當天休市
    db = _run_update(tmp_path, monkeypatch, monday, {
        "2026-08-17": NoTradingDataError("very sorry, no data"),
    })
    with connect_db(db) as c:
        assert unresolved_fetch_failures(c) == [], "平日的休市日也不該記成抓取失敗"


def test_a_real_fetch_failure_is_still_recorded(tmp_path, monkeypatch):
    """逾時＝拿不到回答，必須記錄。這是 2026-08-18 的情境。"""
    from stock_chip.official import connect_db

    tuesday = dt.date(2026, 8, 18)
    db = _run_update(tmp_path, monkeypatch, tuesday, {
        "2026-08-18": TimeoutError("HTTPSConnectionPool: Read timed out"),
    })
    with connect_db(db) as c:
        rows = unresolved_fetch_failures(c)
        assert [r["target_date"] for r in rows] == ["2026-08-18"]
        assert health.check_fetch_failures(c)["status"] == "fail"


def test_backfilling_old_gaps_is_not_an_alert(tmp_path, monkeypatch):
    """比已入庫的最新日還舊的失敗不告警——那是在補歷史，不是當日缺料。"""
    from stock_chip.official import connect_db

    db = _run_update(tmp_path, monkeypatch, dt.date(2026, 8, 19), {
        "2026-08-17": TimeoutError("timed out"),
    })
    with connect_db(db) as c:
        assert unresolved_fetch_failures(c) == []


# ---------- 接線：run_daytrade 真的會記錄與清除 ----------
# 上面那些是單元層。真正會出錯的地方是 run_daytrade 裡的接線——
# 呼叫了但沒 commit、清除寫錯來源名、只記錄某一半，單元測試都抓不到。

def _fake_disposal(stock_id: str, market: str) -> dict:
    return {"stock_id": stock_id, "name": f"股{stock_id}", "market": market,
            "announce_date": "2026-08-19", "start_date": "2026-08-19",
            "end_date": "2026-08-25", "reason": "連續三次", "measure": "",
            "source": "twse_punish" if market == "TWSE" else "tpex_disposal"}


def _prepare(tmp_path):
    from stock_chip.official import connect_db
    db = tmp_path / "t.sqlite"
    with connect_db(db) as c:
        c.execute(
            "INSERT INTO trading_days(date, price_count, institutional_count,"
            " margin_count, updated_at) VALUES('2026-08-19', 2000, 1800, 1800, 'x')"
        )
        c.commit()
    return db


def test_run_daytrade_records_a_disposal_source_failure(tmp_path, monkeypatch):
    """上市處置股掛掉、上櫃正常：筆數仍是正的，但必須留下失敗紀錄。

    這正是最危險的情境——只看筆數會以為一切正常，實際上少了一半的排除名單，
    做不了當沖的股票會混進候選池。
    """
    from stock_chip import daytrade as dt_mod
    from stock_chip.official import connect_db

    db = _prepare(tmp_path)
    monkeypatch.setattr(dt_mod, "fetch_twse_day_trade", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("當沖統計掛了")))
    monkeypatch.setattr(dt_mod, "fetch_disposals", lambda *a, **k: ([_fake_disposal("6488", "TPEX")], ["twse_punish: boom"]))
    monkeypatch.setattr(dt_mod, "fetch_tpex_ceiling", lambda *a, **k: [])

    dt_mod.run_daytrade(db)

    with connect_db(db) as c:
        rows = {r["source"]: r for r in unresolved_fetch_failures(c)}
        assert "daytrade_disposal" in rows, "上市處置股失敗必須被記下來"
        assert "twse_punish" in rows["daytrade_disposal"]["error"]
        assert "daytrade_stats" in rows, "當沖統計失敗也要記"
        assert health.check_fetch_failures(c)["status"] == "fail"
        # 資料仍有寫入：失敗紀錄不該擋掉抓得到的那一半
        assert c.execute("SELECT COUNT(*) FROM disposal_stocks").fetchone()[0] == 1


def test_run_daytrade_clears_failures_after_a_clean_run(tmp_path, monkeypatch):
    """全部成功的一輪要把舊紀錄清乾淨，否則健檢會一直紅著。"""
    from stock_chip import daytrade as dt_mod
    from stock_chip.official import connect_db

    db = _prepare(tmp_path)
    with connect_db(db) as c:
        for src in ("daytrade_stats", "daytrade_disposal", "daytrade_ceiling"):
            record_fetch_failure(c, src, "2026-08-19", "昨天掛了")
        c.commit()

    monkeypatch.setattr(dt_mod, "fetch_twse_day_trade", lambda *a, **k: [
        {"date": "2026-08-19", "stock_id": "2330", "name": "台積電", "market": "TWSE",
         "day_trade_volume": 1000.0, "day_trade_buy_amount": None,
         "day_trade_sell_amount": None, "suspended_note": ""}])
    monkeypatch.setattr(dt_mod, "fetch_disposals", lambda *a, **k: ([_fake_disposal("2330", "TWSE")], []))
    monkeypatch.setattr(dt_mod, "fetch_tpex_ceiling", lambda *a, **k: [
        {"date": "2026-08-19", "stock_id": "6488", "name": "環球晶", "market": "TPEX",
         "close": 100.0, "change": 10.0, "total_volume": 1.0,
         "ceiling_traded_volume": 1.0, "ceiling_order_volume": 2.0, "queue_volume": 1.0}])

    dt_mod.run_daytrade(db)

    with connect_db(db) as c:
        assert unresolved_fetch_failures(c) == []
        assert health.check_fetch_failures(c)["status"] == "ok"

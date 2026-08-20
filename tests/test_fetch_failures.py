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


def test_holidays_do_not_create_noise(conn):
    """假日跳過不該被記錄——只有『比已入庫的新卻抓不到』才算異常。

    這條釘住 official.py 的判斷條件：若哪天改成無差別記錄，
    每逢週末健檢就會紅，然後大家開始忽略它。
    """
    import inspect

    from stock_chip import official

    source = inspect.getsource(official.update_recent)
    assert "if missing_day:" in source, (
        "record_fetch_failure 必須只在 missing_day 成立時呼叫，"
        "否則假日跳過也會被記成失敗"
    )


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

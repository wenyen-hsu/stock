"""週報：週界線、漲幅分流、資金流向、下週事件行事曆。"""
import datetime as dt
import sqlite3
import tempfile
from pathlib import Path

from stock_chip.weekly import (
    LIQUID_TURNOVER_100M,
    extract_event_dates,
    last_complete_week,
    market_flow,
    sector_flows,
    split_gainers,
    upcoming_events,
    week_returns,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE trading_days (date TEXT PRIMARY KEY, market TEXT NOT NULL,
            price_count INTEGER, institutional_count INTEGER, margin_count INTEGER,
            updated_at TEXT NOT NULL);
        CREATE TABLE daily_prices (date TEXT, stock_id TEXT, name TEXT, close REAL,
            volume REAL, PRIMARY KEY (date, stock_id));
        CREATE TABLE stocks (stock_id TEXT PRIMARY KEY, name TEXT, market TEXT);
        CREATE TABLE institutional_trades (date TEXT, stock_id TEXT, foreign_net REAL,
            trust_net REAL, PRIMARY KEY (date, stock_id));
        CREATE TABLE market_index_daily (date TEXT, index_code TEXT, close REAL);
        CREATE TABLE futures_institution_oi (date TEXT, product_code TEXT,
            institution TEXT, oi_net REAL);
        CREATE TABLE margin_trades (date TEXT, stock_id TEXT, margin_balance REAL);
        CREATE TABLE mops_events (event_id TEXT PRIMARY KEY, event_date TEXT,
            stock_id TEXT, company_name TEXT, title TEXT, detail TEXT);
        CREATE TABLE dividend_events (ex_date TEXT, stock_id TEXT, name TEXT,
            cash_dividend REAL, stock_dividend_per_share REAL, source TEXT);
        """
    )
    return conn


def _add_days(conn: sqlite3.Connection, dates: list[str]) -> None:
    conn.executemany(
        "INSERT INTO trading_days (date, market, updated_at) VALUES (?, 'TWSE', 'x')",
        [(d,) for d in dates],
    )


# ---------- 週界線 ----------

def test_last_complete_week_returns_the_week_before_the_running_one():
    """週三看：本週還在進行中，要拿上週。"""
    conn = _conn()
    _add_days(conn, ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
                     "2026-07-27", "2026-07-28"])  # 本週才進行到週二
    assert last_complete_week(conn, today=dt.date(2026, 7, 29)) == ("2026-07-20", "2026-07-24", 5)


def test_last_complete_week_uses_the_just_finished_week_on_the_weekend():
    """週六看：資料只到週五、還沒有下一週的交易日，仍必須拿「剛收完的那一週」。
    只用「有沒有更晚的週」判斷會退回上上週——週末正是最常讀週報的時候。"""
    conn = _conn()
    _add_days(conn, ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
                     "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"])
    assert last_complete_week(conn, today=dt.date(2026, 8, 1)) == ("2026-07-27", "2026-07-31", 5)


def test_last_complete_week_does_not_treat_a_running_week_as_done():
    """週五盤後、當日資料尚未進站時，本週不可被當成完整週。"""
    conn = _conn()
    _add_days(conn, ["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24",
                     "2026-07-27", "2026-07-28", "2026-07-29"])
    assert last_complete_week(conn, today=dt.date(2026, 7, 31)) == ("2026-07-20", "2026-07-24", 5)


def test_last_complete_week_handles_holiday_short_week():
    """連假造成的短週仍算一週，只是 trading_day_count 較少。"""
    conn = _conn()
    _add_days(conn, ["2026-06-15", "2026-06-16", "2026-06-17",  # 週四五放假
                     "2026-06-22", "2026-06-23"])
    assert last_complete_week(conn, today=dt.date(2026, 6, 24)) == ("2026-06-15", "2026-06-17", 3)


def test_last_complete_week_crosses_year_boundary():
    """ISO 週跨年：2025-12-29~31 與 2026-01-01~02 同屬 ISO 2026-W01，
    不可因西元年不同而被切成兩週。"""
    conn = _conn()
    _add_days(conn, ["2025-12-22", "2025-12-23",
                     "2025-12-29", "2025-12-30", "2025-12-31", "2026-01-02",
                     "2026-01-05"])
    start, end, count = last_complete_week(conn, today=dt.date(2026, 1, 6))
    assert (start, end, count) == ("2025-12-29", "2026-01-02", 4)
    assert dt.date.fromisoformat(start).isocalendar()[:2] == dt.date.fromisoformat(end).isocalendar()[:2]


def test_last_complete_week_needs_two_weeks_of_data():
    conn = _conn()
    _add_days(conn, ["2026-07-27", "2026-07-28"])
    assert last_complete_week(conn, today=dt.date(2026, 7, 29)) is None


# ---------- 週報酬 ----------

def _price(conn, date, stock_id, close, volume=1_000_000):
    conn.execute("INSERT INTO daily_prices VALUES (?,?,?,?,?)", (date, stock_id, f"股{stock_id}", close, volume))


def test_week_returns_measures_first_to_last_close():
    conn = _conn()
    _price(conn, "2026-07-20", "1001", 100.0)
    _price(conn, "2026-07-24", "1001", 110.0)
    conn.execute("INSERT INTO stocks VALUES ('1001', '測試股', 'TWSE')")
    conn.execute("INSERT INTO institutional_trades VALUES ('2026-07-20','1001',5000,1000)")
    conn.execute("INSERT INTO institutional_trades VALUES ('2026-07-24','1001',3000,-500)")
    out = week_returns(conn, "2026-07-20", "2026-07-24")
    assert out["1001"]["week_return_pct"] == 10.0
    # 法人買賣超含起始日：週一買的也是這週的錢
    assert out["1001"]["foreign_net_lot"] == 8
    assert out["1001"]["trust_net_lot"] == 0  # (1000-500)/1000 四捨五入
    assert out["1001"]["name"] == "測試股"


def test_week_returns_skips_stocks_without_both_ends():
    """週中才上市的股票沒有起始日收盤，不能拿它跟別人比報酬。"""
    conn = _conn()
    _price(conn, "2026-07-22", "1002", 50.0)
    _price(conn, "2026-07-24", "1002", 60.0)
    assert week_returns(conn, "2026-07-20", "2026-07-24") == {}


# ---------- 漲幅分流 ----------

def _perf(stock_id, ret, turnover, foreign=0, trust=0, industry="半導體業", sub=""):
    return {
        "stock_id": stock_id, "name": f"股{stock_id}", "market": "TWSE",
        "week_return_pct": ret, "avg_turnover_100m": turnover,
        "foreign_net_lot": foreign, "trust_net_lot": trust,
        "industry": industry, "sub_industry": sub, "multifactor_score": 50.0,
        "week_volume_lot": 1000, "close": 100.0, "start_close": 100.0,
    }


def test_speculative_movers_do_not_crowd_out_the_mainstream_list():
    """實測近 5 日漲幅前 10 有 8 檔是低量小型股（長亨 +62.94%、外資 0 張）。
    純漲幅榜會把它們排在台積電之類前面，讀者會誤以為主流資金在追。"""
    perf = {
        "4546": _perf("4546", 62.9, 0.05, foreign=0),      # 飆股：無量無法人
        "2330": _perf("2330", 3.1, 500.0, foreign=20000),  # 主流：有量有法人
        "2317": _perf("2317", 5.4, 200.0, foreign=8000),
    }
    out = split_gainers(perf)
    assert [row["stock_id"] for row in out["mainstream"]] == ["2317", "2330"]
    assert [row["stock_id"] for row in out["speculative"]] == ["4546"]


def test_liquid_but_institution_free_rally_counts_as_speculative():
    """有量但法人沒買（散戶行情）也歸投機側，不是主流認同。"""
    perf = {"9999": _perf("9999", 20.0, LIQUID_TURNOVER_100M + 5, foreign=-100, trust=0)}
    out = split_gainers(perf)
    assert not out["mainstream"]
    assert out["speculative"][0]["stock_id"] == "9999"


def test_losers_apply_the_liquidity_filter():
    perf = {
        "1111": _perf("1111", -12.0, 0.01),   # 冷門股跌深，沒有參考價值
        "2222": _perf("2222", -8.0, 10.0),
    }
    assert [row["stock_id"] for row in split_gainers(perf)["losers"]] == ["2222"]


# ---------- 族群 ----------

def test_sector_flows_need_minimum_members_and_split_by_direction():
    perf = {
        "1": _perf("1", 3.0, 10.0, foreign=5000, sub="AI 伺服器"),
        "2": _perf("2", 2.0, 10.0, foreign=3000, sub="AI 伺服器"),
        "3": _perf("3", 1.0, 10.0, foreign=2000, sub="AI 伺服器"),
        "4": _perf("4", -2.0, 10.0, foreign=-9000, sub="面板"),
        "5": _perf("5", -1.0, 10.0, foreign=-4000, sub="面板"),
        "6": _perf("6", -1.0, 10.0, foreign=-1000, sub="面板"),
        "7": _perf("7", 9.0, 10.0, foreign=9999, sub="只有一檔"),  # 未達門檻
    }
    out = sector_flows(perf)
    assert [item["name"] for item in out["inflow"]] == ["AI 伺服器"]
    assert [item["name"] for item in out["outflow"]] == ["面板"]
    assert out["inflow"][0]["member_count"] == 3
    assert out["inflow"][0]["up_ratio_pct"] == 100.0


# ---------- 大盤資金 ----------

def test_market_flow_reads_index_futures_and_margin():
    conn = _conn()
    conn.executemany("INSERT INTO market_index_daily VALUES (?,?,?)",
                     [("2026-07-20", "TAIEX", 20000.0), ("2026-07-24", "TAIEX", 20600.0)])
    conn.executemany("INSERT INTO futures_institution_oi VALUES (?,?,?,?)",
                     [("2026-07-20", "TXF", "foreign", -10000), ("2026-07-24", "TXF", "foreign", 2000)])
    conn.executemany("INSERT INTO margin_trades VALUES (?,?,?)",
                     [("2026-07-20", "a", 1_000_000), ("2026-07-24", "a", 1_200_000)])
    out = market_flow(conn, "2026-07-20", "2026-07-24")
    assert out["taiex_return_pct"] == 3.0
    assert out["foreign_futures_oi_change"] == 12000
    assert out["margin_balance_change_lot"] == 200


def test_market_flow_tolerates_missing_sources():
    """期貨或大盤某天沒進站時回 None，不能讓整份週報炸掉。"""
    out = market_flow(_conn(), "2026-07-20", "2026-07-24")
    assert out["taiex_return_pct"] is None
    assert out["foreign_futures_oi_change"] is None


# ---------- 下週行事曆 ----------

def test_extract_event_dates_reads_named_fields():
    assert extract_event_dates("1.召開法人說明會之日期:115/08/07 2.時間:14 時") == [("法說會", "2026-08-07")]
    assert extract_event_dates("1.董事會預計召開日期：115/08/12 2.提報") == [("財報董事會", "2026-08-12")]


def test_extract_event_dates_ignores_unnamed_dates():
    """負向案例：naive 全文抓日期會把這些收進行事曆（實測誤判成併購事件）。
    只認具名欄位就不會。"""
    for detail in (
        "1.事實發生日:115/08/07 2.公告本公司盈利警告",
        "1.事實發生日:115/08/07 2.接獲勞工局停工函",
        "1.董事會召集通知日:115/08/07 2.其他事項",  # 通知日不是事件日
    ):
        assert extract_event_dates(detail) == []


def test_upcoming_events_filters_window_and_dedupes():
    conn = _conn()
    rows = [
        ("e1", "2026-08-01", "2385", "群光", "法說會公告", "1.召開法人說明會之日期:115/08/07"),
        ("e2", "2026-08-02", "2385", "群光", "更正法說會公告", "1.召開法人說明會之日期:115/08/07"),
        ("e3", "2026-08-01", "1234", "遠期", "法說會公告", "1.召開法人說明會之日期:115/09/30"),
    ]
    conn.executemany("INSERT INTO mops_events VALUES (?,?,?,?,?,?)", rows)
    out = upcoming_events(conn, "2026-08-03", "2026-08-09")
    assert len(out) == 1, "同一事件被公告兩次應只留一筆；窗外事件應排除"
    assert out[0]["stock_id"] == "2385" and out[0]["event_type"] == "法說會"


def test_upcoming_events_works_with_only_the_dividend_table():
    """兩個來源要各自檢查存在與否。先前 build_weekly_report 用 mops_events
    是否存在來 gate 整段，只有除權息預告表的資料庫會拿到空行事曆。"""
    conn = _conn()
    conn.execute("DROP TABLE mops_events")
    conn.execute("INSERT INTO dividend_events VALUES ('2026-08-06','6125','廣運',0.5,0.0,'tpex_exright')")
    out = upcoming_events(conn, "2026-08-03", "2026-08-09")
    assert [row["stock_id"] for row in out] == ["6125"]


def test_upcoming_events_works_with_only_the_mops_table():
    conn = _conn()
    conn.execute("DROP TABLE dividend_events")
    conn.execute("INSERT INTO mops_events VALUES ('e1','2026-08-01','2385','群光','t','1.召開法人說明會之日期:115/08/07')")
    out = upcoming_events(conn, "2026-08-03", "2026-08-09")
    assert [row["stock_id"] for row in out] == ["2385"]


def test_upcoming_events_merges_tpex_dividend_forecast():
    """TPEx 除權息預告表補 MOPS 沒公告到的上櫃個股。"""
    conn = _conn()
    conn.execute("INSERT INTO dividend_events VALUES ('2026-08-06','6125','廣運',0.5,0.0,'tpex_exright')")
    out = upcoming_events(conn, "2026-08-03", "2026-08-09")
    assert [(row["stock_id"], row["event_type"]) for row in out] == [("6125", "除權息交易日")]


def test_upcoming_events_sorted_by_date_then_importance():
    conn = _conn()
    conn.executemany(
        "INSERT INTO mops_events VALUES (?,?,?,?,?,?)",
        [
            ("a", "2026-08-01", "1111", "甲", "t", "1.普通股現金股利發放日期:115/08/05"),
            ("b", "2026-08-01", "2222", "乙", "t", "1.召開法人說明會之日期:115/08/05"),
            ("c", "2026-08-01", "3333", "丙", "t", "1.召開法人說明會之日期:115/08/04"),
        ],
    )
    out = upcoming_events(conn, "2026-08-03", "2026-08-09")
    assert [(row["date"], row["event_type"]) for row in out] == [
        ("2026-08-04", "法說會"),
        ("2026-08-05", "法說會"),
        ("2026-08-05", "股利發放日"),
    ]

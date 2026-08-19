"""當沖候選池：來源解析、硬門檻、三種風格排序。

這是「盤前候選」不是「進出訊號」——資料全是盤後的。測試釘住的是選股邏輯，
不是任何預測能力。

端點與欄位都經探測 workflow 實測（2026-08）：TWTB4U 回 tables 結構且
沒有比率欄位、上櫃沒有當沖統計、處置期間 TWSE 用全形『～』TPEx 用半形『~』。
"""
import sqlite3

import pytest

from stock_chip import daytrade as dt_mod


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE daily_prices (date TEXT, stock_id TEXT, high REAL, low REAL,
            close REAL, volume REAL, PRIMARY KEY (date, stock_id));
        CREATE TABLE day_trade_stats (date TEXT, stock_id TEXT, day_trade_volume REAL,
            PRIMARY KEY (date, stock_id));
        CREATE TABLE disposal_stocks (stock_id TEXT, name TEXT, market TEXT,
            announce_date TEXT, start_date TEXT, end_date TEXT, reason TEXT,
            measure TEXT, source TEXT, PRIMARY KEY (stock_id, start_date, source));
        CREATE TABLE ceiling_queue (date TEXT, stock_id TEXT, queue_volume REAL,
            PRIMARY KEY (date, stock_id));
        """
    )
    return conn


# ---------- 日期與期間解析 ----------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1150812", "2026-08-12"),      # TPEx 緊湊格式
        ("115/08/12", "2026-08-12"),    # TWSE 斜線格式
        ("115年08月12日", "2026-08-12"),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_roc_accepts_every_observed_format(raw, expected):
    assert dt_mod.parse_roc(raw) == expected


def test_parse_period_handles_both_tilde_widths():
    """TWSE 用全形『～』、TPEx 用半形『~』。只吃一種會讓另一邊的處置股
    全部漏掉，而處置股是硬排除條件——漏掉等於把不能當沖的標的放進名單。"""
    assert dt_mod.parse_period("115/08/12～115/08/18") == ("2026-08-12", "2026-08-18")
    assert dt_mod.parse_period("1150813~1150821") == ("2026-08-13", "2026-08-21")
    assert dt_mod.parse_period("") == (None, None)


def test_to_number_strips_thousands_separator():
    assert dt_mod.to_number("4,273,000") == 4273000.0
    assert dt_mod.to_number("--") is None


# ---------- 當沖比率 ----------

def test_day_trade_ratio_is_computed_from_our_own_volume():
    """TWTB4U 只給當沖張數不給比率，比率用我們自己的 daily_prices.volume 算，
    不依賴對方的定義。"""
    conn = _conn()
    conn.execute("INSERT INTO daily_prices VALUES ('2026-08-12','2330',110,90,100,1000000)")
    conn.execute("INSERT INTO day_trade_stats VALUES ('2026-08-12','2330',350000)")
    assert dt_mod.day_trade_ratios(conn, "2026-08-12") == {"2330": 35.0}


def test_day_trade_ratio_skips_zero_volume():
    conn = _conn()
    conn.execute("INSERT INTO daily_prices VALUES ('2026-08-12','1111',10,10,10,0)")
    conn.execute("INSERT INTO day_trade_stats VALUES ('2026-08-12','1111',500)")
    assert dt_mod.day_trade_ratios(conn, "2026-08-12") == {}


def test_amplitude_uses_high_low_over_close():
    conn = _conn()
    conn.execute("INSERT INTO daily_prices VALUES ('2026-08-12','2330',110,90,100,1)")
    assert dt_mod.amplitude_pct(conn, "2026-08-12") == {"2330": 20.0}


# ---------- 處置股 ----------

def test_disposal_blocks_only_within_the_period():
    """處置期間人工撮合（約 2 分鐘一次），當沖做不了。期間外不該擋。"""
    conn = _conn()
    conn.execute(
        "INSERT INTO disposal_stocks VALUES ('3163','波若威','TPEX','2026-08-12',"
        "'2026-08-13','2026-08-21','連續5個營業日','','tpex_disposal')"
    )
    assert dt_mod.disposal_ids(conn, "2026-08-15") == {"3163"}
    assert dt_mod.disposal_ids(conn, "2026-08-12") == set()   # 尚未開始
    assert dt_mod.disposal_ids(conn, "2026-08-22") == set()   # 已結束


# ---------- 硬門檻 ----------

def _row(stock_id, market="TWSE", close=100.0, turnover=10.0, **extra):
    base = {
        "stock_id": stock_id, "name": f"股{stock_id}", "market": market,
        "close": str(close), "avg_turnover_100m": str(turnover),
        "industry": "半導體業", "sub_industry": "", "volume_ratio_1d": "2",
        "latest_foreign_net_lot": "0", "latest_trust_net_lot": "0",
        "top_buy_is_day_trader": "0", "top_buy_branch_name": "",
        "short_balance_change_lot": "0",
    }
    base.update({k: str(v) for k, v in extra.items()})
    return base


def _build(rows, ratios=None, changes=None, blocked=None, queues=None, strong=None):
    return dt_mod.build_candidates(
        rows, changes or {}, {}, ratios or {}, queues or {}, blocked or set(), strong or set()
    )


def test_twse_requires_day_trade_ratio_not_just_turnover():
    """上市有當沖比率就要用它。日均額大但沒人當沖的股票（中華電那類）
    盤中沒有當沖需要的流動性與波動。"""
    rows = [_row("2412", turnover=30.0), _row("2330", turnover=30.0)]
    out = _build(rows, ratios={"2412": 3.0, "2330": 35.0})
    assert [r["stock_id"] for r in out] == ["2330"]
    assert out[0]["gate_basis"] == "day_trade_pct"


def test_tpex_falls_back_to_turnover_and_says_so():
    """上櫃沒有當沖統計（第二輪探測確認 TPEx 端點裡沒有這份資料），
    只能用日均額代理，且必須在 gate_basis 標明，不能被當成同一個標準。"""
    out = _build([_row("6488", market="TPEX", turnover=8.0)], ratios={})
    assert [r["stock_id"] for r in out] == ["6488"]
    assert out[0]["gate_basis"] == "turnover_proxy"


def test_tpex_threshold_is_higher_than_twse():
    """代理指標較弱，門檻拉高補償。日均額 4 億在上市可過、上櫃不行。"""
    assert dt_mod.MIN_TURNOVER_TPEX > dt_mod.MIN_TURNOVER_TWSE
    out = _build([_row("6488", market="TPEX", turnover=4.0)], ratios={})
    assert out == []


def test_price_range_excludes_both_ends():
    rows = [_row("1111", close=5.0), _row("2222", close=800.0), _row("3333", close=100.0)]
    out = _build(rows, ratios={k: 30.0 for k in ("1111", "2222", "3333")})
    assert [r["stock_id"] for r in out] == ["3333"]


def test_disposal_stocks_are_excluded_from_the_pool():
    out = _build([_row("3163")], ratios={"3163": 40.0}, blocked={"3163"})
    assert out == []


# ---------- 三種風格 ----------

def _cand(stock_id, **kw):
    base = {
        "stock_id": stock_id, "name": f"股{stock_id}", "chg_1d_pct": 0.0,
        "amplitude_pct": 3.0, "volume_ratio_1d": 1.0, "foreign_net_lot": 0.0,
        "trust_net_lot": 0.0, "is_day_trader_branch": False, "in_strong_sector": False,
        "ceiling_queue_lot": None,
    }
    base.update(kw)
    return base


def test_pressure_ranking_only_contains_day_trader_branches():
    """『隔日沖賣壓』榜的存在理由就是找隔日沖進駐的標的，沒進駐的不該出現。"""
    out = dt_mod.rank_candidates([
        _cand("1001", is_day_trader_branch=True, volume_ratio_1d=5, chg_1d_pct=8),
        _cand("1002", is_day_trader_branch=False, volume_ratio_1d=9, chg_1d_pct=9),
    ])
    assert [r["stock_id"] for r in out["pressure"]] == ["1001"]


def test_day_trader_flag_flips_sign_between_styles():
    """同一個訊號在不同風格是相反的：追強要扣分（明天有賣壓），
    賣壓榜是必要條件。這是三個榜分開做的核心價值。"""
    flagged = _cand("1001", is_day_trader_branch=True, volume_ratio_1d=5, chg_1d_pct=5)
    clean = _cand("1002", is_day_trader_branch=False, volume_ratio_1d=5, chg_1d_pct=5)
    out = dt_mod.rank_candidates([flagged, clean])
    assert [r["stock_id"] for r in out["momentum"]] == ["1002", "1001"]
    assert [r["stock_id"] for r in out["pressure"]] == ["1001"]


def test_reversal_only_takes_yesterday_losers():
    out = dt_mod.rank_candidates([
        _cand("1001", chg_1d_pct=-6.0),
        _cand("1002", chg_1d_pct=4.0),
    ])
    assert [r["stock_id"] for r in out["reversal"]] == ["1001"]


def test_momentum_only_takes_yesterday_winners():
    """追強與接刀對稱：只收昨日上漲。平盤、下跌都不該出現——2026-08-18
    名單裡 1815／5351／6182 就是昨跌或平卻進了追強榜。"""
    out = dt_mod.rank_candidates([
        _cand("1815", chg_1d_pct=-1.2, volume_ratio_1d=9, amplitude_pct=8),
        _cand("5351", chg_1d_pct=0.0, volume_ratio_1d=9, amplitude_pct=8),
        _cand("2330", chg_1d_pct=2.5, volume_ratio_1d=2, amplitude_pct=3),
    ])
    assert [r["stock_id"] for r in out["momentum"]] == ["2330"]


def test_momentum_marks_yesterday_overheated_without_dropping():
    """近漲停只標「昨過熱」，不踢出榜、也不另訂權重。"""
    hot = _cand("2359", chg_1d_pct=9.5, volume_ratio_1d=2, amplitude_pct=10)
    warm = _cand("2330", chg_1d_pct=3.0, volume_ratio_1d=2, amplitude_pct=4)
    out = dt_mod.rank_candidates([hot, warm])
    by_id = {r["stock_id"]: r for r in out["momentum"]}
    assert set(by_id) == {"2359", "2330"}
    assert by_id["2359"]["yesterday_overheated"] is True
    assert by_id["2330"]["yesterday_overheated"] is False


def test_reversal_penalises_high_volume_selloff():
    """爆量下跌通常還有後續賣壓，不是接刀的好對象。"""
    quiet = _cand("1001", chg_1d_pct=-6.0, volume_ratio_1d=1.0)
    heavy = _cand("1002", chg_1d_pct=-6.0, volume_ratio_1d=5.0)
    out = dt_mod.rank_candidates([quiet, heavy])
    assert [r["stock_id"] for r in out["reversal"]] == ["1001", "1002"]


def test_momentum_rewards_ceiling_queue():
    """漲停鎖死仍有排隊買量，是盤後資料裡少數直接指向明天開盤買盤的訊號。"""
    queued = _cand("1001", chg_1d_pct=9.9, ceiling_queue_lot=14498)
    plain = _cand("1002", chg_1d_pct=9.9)
    out = dt_mod.rank_candidates([queued, plain])
    assert [r["stock_id"] for r in out["momentum"]] == ["1001", "1002"]


def test_rankings_share_one_pool():
    """三個榜共用候選池，只是排序不同——所以硬門檻只需維護一份。"""
    pool = [_cand(str(i), chg_1d_pct=(i % 5) - 2, is_day_trader_branch=(i % 3 == 0))
            for i in range(1, 40)]
    out = dt_mod.rank_candidates(pool)
    ids = {r["stock_id"] for name in out for r in out[name]}
    assert ids <= {c["stock_id"] for c in pool}
    assert all(len(out[name]) <= dt_mod.TOP_N for name in out)


# ---------- 當沖統計整份缺漏時的降級 ----------
# 2026-08-13/14 兩晚的真實事故：TWTB4U 的路徑寫成 /rwd/zh/afterTrading/，
# TWSE 對不存在的路徑回「HTTP 200 + text/html 的 404 頁」，raise_for_status()
# 擋不住，錯誤到 .json() 才炸，被 continue-on-error 吞掉。結果 ratios 全空，
# `ratio is None` 對每檔上市股都成立，整個上市市場從榜單無聲消失——頁面看起來
# 完全正常，只是 54 檔候選全是上櫃。

class _FakeResponse:
    def __init__(self, body, content_type, status=200, url="https://x/y"):
        self.text = body
        self.headers = {"content-type": content_type}
        self.status_code = status
        self.url = url

    def json(self):
        import json as _json
        return _json.loads(self.text)


def test_html_404_with_status_200_is_named_not_left_as_json_error():
    """狀態碼是 200，錯誤訊息必須自己說出「這是 404 頁、路徑可能錯了」。"""
    body = '<!DOCTYPE html>\n<html lang="zh-Hant-tw"><head><title>404</title>'
    with pytest.raises(RuntimeError) as err:
        dt_mod.parse_twse_json(_FakeResponse(body, "text/html"), "TWTB4U")
    message = str(err.value)
    assert "404" in message and "路徑" in message
    assert "text/html" in message


def test_stat_not_ok_is_rejected():
    response = _FakeResponse('{"stat":"很抱歉，沒有符合條件的資料!"}', "application/json")
    with pytest.raises(RuntimeError, match="stat="):
        dt_mod.parse_twse_json(response, "TWTB4U")


def test_valid_payload_passes_through():
    response = _FakeResponse('{"stat":"OK","tables":[]}', "application/json;charset=UTF-8")
    assert dt_mod.parse_twse_json(response, "TWTB4U")["stat"] == "OK"


def test_twse_survives_missing_stats_instead_of_vanishing():
    """統計缺漏時上市不能整批消失——改用代理門檻，且標成 fallback。"""
    rows = [_row("2330", turnover=8.0), _row("6488", market="TPEX", turnover=8.0)]
    out = _build(rows, ratios={})
    assert {r["stock_id"] for r in out} == {"2330", "6488"}
    basis = {r["stock_id"]: r["gate_basis"] for r in out}
    assert basis["2330"] == "turnover_proxy_fallback"
    assert basis["6488"] == "turnover_proxy"


def test_fallback_uses_the_higher_proxy_threshold():
    """降級後上市套的是代理門檻（較高的那個），不是原本的上市日均額門檻。"""
    assert dt_mod.MIN_TURNOVER_TWSE < 4.0 < dt_mod.MIN_TURNOVER_TPEX
    assert _build([_row("2330", turnover=4.0)], ratios={}) == []


def test_normal_path_is_unaffected_by_the_fallback():
    """只要統計有資料，上市仍走當沖比率——降級不能把正常路徑一起放寬。"""
    out = _build([_row("2330", turnover=30.0), _row("2412", turnover=30.0)],
                 ratios={"2330": 35.0, "2412": 3.0})
    assert [r["stock_id"] for r in out] == ["2330"]
    assert out[0]["gate_basis"] == "day_trade_pct"

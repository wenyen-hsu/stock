"""外資動向排行：當日買超 / 當日賣超 / 連續買超。

需求來自「想知道外資每日都在大買哪些股票」——券商 App 的當日買賣超排行只看
一天，看不出誰是天天在買，所以另外做一個連續買超榜。
"""
from stock_chip.scan import build_rankings

DAYS = 20


def _row(stock_id: str, **overrides) -> dict:
    base = {
        "days": DAYS,
        "stock_id": stock_id,
        "name": f"股{stock_id}",
        "market": "TWSE",
        "latest_foreign_net": 0,
        f"{DAYS}d_foreign_net": 0,
        f"{DAYS}d_trust_net": 0,
        f"{DAYS}d_inst_net": 0,
        f"{DAYS}d_volume": 1_000_000,
        "foreign_buy_streak": 0,
        "avg_turnover_100m": 5.0,
        "chip_score": 0.0,
        "close_vs_avg_pct": 0.0,
    }
    base.update(overrides)
    return base


def _ranking(rows: list[dict], name: str) -> list[str]:
    return [row["stock_id"] for row in build_rankings(rows, DAYS)[name]]


def test_day_buy_ranks_by_single_day_net_and_excludes_sellers():
    rows = [
        _row("1001", latest_foreign_net=5_000_000),
        _row("1002", latest_foreign_net=9_000_000),
        _row("1003", latest_foreign_net=-8_000_000),
    ]
    assert _ranking(rows, "foreign_day_buy") == ["1002", "1001"]


def test_day_sell_ranks_most_negative_first():
    rows = [
        _row("1001", latest_foreign_net=-2_000_000),
        _row("1002", latest_foreign_net=-9_000_000),
        _row("1003", latest_foreign_net=3_000_000),
    ]
    assert _ranking(rows, "foreign_day_sell") == ["1002", "1001"]


def test_streak_ranks_by_days_then_cumulative():
    rows = [
        _row("1001", foreign_buy_streak=5, **{f"{DAYS}d_foreign_net": 100_000}),
        _row("1002", foreign_buy_streak=5, **{f"{DAYS}d_foreign_net": 900_000}),
        _row("1003", foreign_buy_streak=9, **{f"{DAYS}d_foreign_net": 50_000}),
    ]
    assert _ranking(rows, "foreign_streak_buy") == ["1003", "1002", "1001"]


def test_streak_requires_at_least_three_days():
    rows = [
        _row("1001", foreign_buy_streak=2, **{f"{DAYS}d_foreign_net": 900_000}),
        _row("1002", foreign_buy_streak=3, **{f"{DAYS}d_foreign_net": 900_000}),
    ]
    assert _ranking(rows, "foreign_streak_buy") == ["1002"]


def test_streak_drops_daily_dribble_that_never_adds_up():
    """實測案例：8027 鈦昇連買 12 天，20 日累計卻只有 196 張（占期間成交量
    0.30%）。日均額門檻擋不掉它（日均 7.6 億，很流動），要擋的是「買超相對
    成交量太小」——天天買一點不是買盤，是雜訊。"""
    rows = [
        # 連買 12 天但只佔成交量 0.3%
        _row("8027", foreign_buy_streak=12, **{f"{DAYS}d_foreign_net": 195_510, f"{DAYS}d_volume": 66_274_000}),
        # 連買 10 天且佔成交量 9.5%
        _row("2412", foreign_buy_streak=10, **{f"{DAYS}d_foreign_net": 42_866_000, f"{DAYS}d_volume": 452_000_000}),
    ]
    assert _ranking(rows, "foreign_streak_buy") == ["2412"]


def test_streak_filter_reads_volume_key_present_on_ranking_rows():
    """占量必須就地由 {days}d_volume 算。foreign_net_volume_pct 只存在於 CSV
    匯出列，排行階段沒有這個 key——寫成 row.get(...) 會恆為 None 而把整個榜
    濾成空的（開發時真的先寫錯過一次）。"""
    rows = [_row("1001", foreign_buy_streak=5, **{f"{DAYS}d_foreign_net": 900_000})]
    assert _ranking(rows, "foreign_streak_buy") == ["1001"]


def test_streak_requires_liquidity():
    rows = [
        _row("1001", foreign_buy_streak=5, avg_turnover_100m=0.1, **{f"{DAYS}d_foreign_net": 900_000}),
        _row("1002", foreign_buy_streak=5, avg_turnover_100m=1.0, **{f"{DAYS}d_foreign_net": 900_000}),
    ]
    assert _ranking(rows, "foreign_streak_buy") == ["1002"]

"""掃描層純函式的回歸測試：分數、日期邊界與匯出欄位凍結。"""
import datetime as dt

from stock_chip.financials import parse_amount, published_quarters, single_quarter_values
from stock_chip.revenue import expected_revenue_month
from stock_chip.scan import (
    bounded,
    branch_score_row,
    compute_rsi,
    dispersion_score_row,
    fundamental_score_row,
    to_export_row,
)


def test_expected_revenue_month_boundary():
    # 每月 12 日前保守往前多推一個月
    assert expected_revenue_month(dt.date(2026, 7, 11)) == "2026-05"
    assert expected_revenue_month(dt.date(2026, 7, 12)) == "2026-06"
    assert expected_revenue_month(dt.date(2026, 1, 5)) == "2025-11"


def test_bounded():
    assert bounded(5, 0, 3) == 3
    assert bounded(-5, -3, 3) == -3
    assert bounded(1, -3, 3) == 1


def test_compute_rsi_needs_period_plus_one():
    closes = list(range(1, 15))  # 14 筆不夠（需要 15）
    assert compute_rsi([float(v) for v in closes]) is None
    closes = [float(v) for v in range(1, 16)]  # 全漲 → RSI 100
    assert compute_rsi(closes) == 100.0


def _branch_row(**overrides):
    row = {
        "close": 110.0,
        "observed_days": 20,
        "days": 20,
        "20d_volume": 1_000_000,
        "top_buy_branch_net_lot": 500.0,
        "top_sell_branch_net_lot": 100.0,
        "top_buy_branch_avg_price": None,
        "top_buy_branch_est_cost": None,
        "top_sell_branch_avg_price": None,
        "top_buy_is_day_trader": 0,
    }
    row.update(overrides)
    return row


def test_branch_score_day_trader_penalty():
    normal = branch_score_row(_branch_row())
    day_trader = branch_score_row(_branch_row(top_buy_is_day_trader=1))
    assert normal > 0
    assert day_trader < 0


def test_branch_score_est_cost_fallback():
    # 無均價、無推估成本 → 只有量能占比分
    without_cost = branch_score_row(_branch_row())
    # 推估成本接近收盤 → 進入 -3%..5% 加 10 分區
    with_cost = branch_score_row(_branch_row(top_buy_branch_est_cost=108.0))
    assert with_cost > without_cost


def test_branch_score_streak_bonus():
    base = branch_score_row(_branch_row())
    with_streak = branch_score_row(_branch_row(branch_buy_streak=5))
    assert with_streak - base == 4.0  # min(5,5)*0.8


def test_dispersion_score_directions():
    assert dispersion_score_row({}) == 0.0
    up = dispersion_score_row({"big_holder_change_1w": 0.5, "retail_change_1w": -0.2})
    down = dispersion_score_row({"big_holder_change_1w": -0.5, "retail_change_1w": 0.2})
    assert up > 0 > down


def test_fundamental_score_missing_is_zero():
    assert fundamental_score_row({}) == 0.0
    assert fundamental_score_row({"net_margin_pct": 25.0, "gross_margin_streak": 3, "eps_ttm": 5.0}) > 0
    assert fundamental_score_row({"net_margin_pct": -5.0, "eps_ttm": -1.0}) < 0


def test_single_quarter_differencing():
    cumulative = [
        {"year_quarter": "2025Q1", "revenue": 100.0, "gross_profit": 50.0, "operating_income": 40.0, "net_income": 30.0, "eps": 1.0},
        {"year_quarter": "2025Q2", "revenue": 220.0, "gross_profit": 105.0, "operating_income": 82.0, "net_income": 63.0, "eps": 2.2},
    ]
    single = single_quarter_values(cumulative)
    q2 = single[1]
    assert q2["revenue"] == 120.0
    assert abs(q2["eps"] - 1.2) < 1e-9
    assert q2["gross_margin_pct"] == round(55.0 / 120.0 * 100, 2)


def test_single_quarter_missing_prior_leaves_none():
    single = single_quarter_values(
        [{"year_quarter": "2025Q3", "revenue": 350.0, "gross_profit": 170.0, "operating_income": 130.0, "net_income": 100.0, "eps": 3.5}]
    )
    assert single[0]["revenue"] is None  # 缺前季不可誤標累計值為單季


def test_published_quarters_deadlines():
    assert published_quarters(dt.date(2026, 7, 3), count=2) == [(2026, 1), (2025, 4)]
    assert published_quarters(dt.date(2026, 5, 10), count=1) == [(2025, 4)]


def test_parse_amount_variants():
    assert parse_amount("1,234") == 1234.0
    assert parse_amount("(500)") == -500.0
    assert parse_amount("-1,636") == -1636.0
    assert parse_amount("--") is None
    assert parse_amount("") is None


def test_to_export_row_field_freeze():
    """凍結 CSV 欄位集合：欄位增減會改變所有下游 JSON/報表 schema，需有意識地更新。"""
    row = {
        "days": 20, "observed_days": 20, "latest_date": "2026-07-03", "stock_id": "2330",
        "name": "台積電", "market": "TWSE", "close": 1000.0,
        "20d_avg_price": 990.0, "close_vs_avg_pct": 1.0, "20d_volume": 1_000_000,
        "latest_volume": 50_000, "volume_avg": 50_000, "volume_5d_avg": 50_000,
        "volume_ratio_1d": 1.0, "volume_ratio_5d": 1.0,
        "20d_foreign_net": 1000, "20d_trust_net": 500, "20d_dealer_net": 0, "20d_inst_net": 1500,
        "latest_foreign_net": 100, "latest_trust_net": 50,
        "foreign_buy_streak": 1, "foreign_sell_streak": 0, "trust_buy_streak": 1, "trust_sell_streak": 0,
        "chip_score": 10.0,
    }
    exported = to_export_row(row, 20)
    expected_new_fields = {
        "disp_date", "big_holder_pct", "big_holder_change_1w", "big_holder_change_4w",
        "holder_400_pct", "retail_pct", "retail_change_1w", "dispersion_score",
        "fin_quarter", "gross_margin_pct", "operating_margin_pct", "net_margin_pct",
        "gross_margin_streak", "eps_ttm", "pe_ttm", "fundamental_score",
        "top_buy_branch_est_cost", "branch_buy_streak", "branch_streak_broker", "top_buy_is_day_trader",
    }
    assert expected_new_fields <= set(exported.keys())
    assert {"mispriced_score", "pe_percentile", "eps_yoy_pct", "mispriced_reason"} <= set(exported.keys())
    assert len(exported) == 113, f"CSV 欄位數改變：{len(exported)}（若為刻意調整請同步更新此測試）"

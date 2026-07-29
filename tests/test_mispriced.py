"""錯殺價值分：資格門檻、獲利動能、便宜程度與價值陷阱扣分的回歸測試。"""
from stock_chip.scan import mispriced_value_breakdown


def _row(**overrides):
    """合格的錯殺標的基準：獲利轉強 + 股價被殺。"""
    row = {
        "days": 20,
        "observed_days": 20,
        "20d_volume": 20_000_000,  # 日均 1000 張
        "20d_inst_net": 0,
        "eps_ttm": 8.0,
        "eps_single_q": 2.5,
        "eps_yoy_pct": 40.0,
        "gross_margin_streak": 2,
        "operating_margin_yoy_pt": 2.5,
        "revenue_yoy_pct": 25.0,
        "pe_ratio": 11.0,
        "pe_percentile": 8.0,
        "close_vs_52w_high_pct": -42.0,
        "dividend_yield": 4.0,
        "pb_ratio": 1.3,
    }
    row.update(overrides)
    return row


def test_ineligible_when_loss_making():
    result = mispriced_value_breakdown(_row(eps_ttm=-1.0))
    assert result["mispriced_eligible"] == 0
    assert result["mispriced_score"] == 0.0
    assert "EPS" in result["mispriced_reason"]


def test_ineligible_when_latest_quarter_loss():
    assert mispriced_value_breakdown(_row(eps_single_q=-0.5))["mispriced_eligible"] == 0


def test_ineligible_when_illiquid():
    # 日均量 100 張 < 500 門檻（跌深冷門股是流動性陷阱）
    result = mispriced_value_breakdown(_row(**{"20d_volume": 2_000_000}))
    assert result["mispriced_eligible"] == 0
    assert "日均量" in result["mispriced_reason"]


def test_strong_mispriced_scores_high():
    result = mispriced_value_breakdown(_row())
    assert result["mispriced_eligible"] == 1
    assert result["mispriced_score"] > 25
    assert result["mispriced_earnings_score"] > 0
    assert result["mispriced_cheap_score"] > 0
    assert result["mispriced_trap_penalty"] == 0.0


def test_value_trap_penalised():
    """便宜但營收與毛利同步走弱 → 陷阱扣分且理由標警語。"""
    trap = mispriced_value_breakdown(_row(revenue_yoy_pct=-20.0, gross_margin_streak=-3, eps_yoy_pct=-15.0))
    healthy = mispriced_value_breakdown(_row())
    assert trap["mispriced_trap_penalty"] <= -6
    assert trap["mispriced_score"] < healthy["mispriced_score"]
    assert trap["mispriced_reason"].startswith("⚠")


def test_pe_percentile_dominates_absolute_pe():
    """PE 絕對值相同，但相對自身歷史更便宜者分數更高。"""
    cheap_vs_history = mispriced_value_breakdown(_row(pe_percentile=5.0))
    expensive_vs_history = mispriced_value_breakdown(_row(pe_percentile=80.0))
    assert cheap_vs_history["mispriced_cheap_score"] > expensive_vs_history["mispriced_cheap_score"]


def test_reason_is_human_readable():
    reason = mispriced_value_breakdown(_row())["mispriced_reason"]
    for fragment in ("單季 EPS 年增", "毛利率連", "月營收年增", "PE", "距 52 週高點"):
        assert fragment in reason


def test_institutional_selling_penalised():
    selling = mispriced_value_breakdown(_row(**{"20d_inst_net": -1_000_000}))
    neutral = mispriced_value_breakdown(_row())
    assert selling["mispriced_trap_penalty"] < neutral["mispriced_trap_penalty"]


def test_normalize_scan_row_keeps_mispriced_fields():
    """CSV→JSON 的欄位白名單必須含錯殺欄位，否則前端拿不到分數與理由
    （2026-07-29 首次發布時 mispriced_score 全空的回歸測試）。"""
    from stock_chip.gui import normalize_scan_row

    csv_row = {
        "latest_date": "2026-07-29", "stock_id": "2385", "name": "群光", "market": "TWSE",
        "observed_days": "20", "close": "80", "days": "20",
        "pe_ratio": "9.8", "pe_percentile": "6.0", "eps_yoy_pct": "42.0",
        "mispriced_score": "31.5", "mispriced_reason": "單季 EPS 年增 42%",
        "close_vs_52w_high_pct": "-38.0", "revenue_yoy_pct": "22.0", "gross_margin_streak": "3",
    }
    normalized = normalize_scan_row(csv_row, 20)
    assert normalized["mispriced_score"] == 31.5
    assert normalized["pe_percentile"] == 6.0
    assert normalized["eps_yoy_pct"] == 42.0
    assert normalized["mispriced_reason"] == "單季 EPS 年增 42%"

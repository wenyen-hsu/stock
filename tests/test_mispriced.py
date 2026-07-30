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


def test_cyclical_peak_flagged_and_penalised():
    """記憶體/航運等循環股在獲利高峰時 PE 最低，低 PE 是警訊不是錯殺
    （2026-07-29 首發時前 20 名有 6 檔記憶體模組股的實測發現）。"""
    peak = mispriced_value_breakdown(_row(eps_yoy_pct=2600.0, eps_ttm_at_high=1, pe_percentile=2.0))
    steady = mispriced_value_breakdown(_row(eps_yoy_pct=45.0, eps_ttm_at_high=0, pe_percentile=2.0))
    assert peak["mispriced_cyclical_peak"] == 1
    assert peak["mispriced_trap_penalty"] <= -5
    assert "循環高峰" in peak["mispriced_reason"]
    # 穩健成長不該被誤標，且分數要高於循環高峰股
    assert steady["mispriced_cyclical_peak"] == 0
    assert steady["mispriced_score"] > peak["mispriced_score"]


def test_extreme_growth_rewarded_less_than_steady():
    """年增 3000%（低基期反彈）獲利分不應高於年增 60%（穩健成長）。"""
    explosive = mispriced_value_breakdown(_row(eps_yoy_pct=3000.0, eps_ttm_at_high=0))
    steady = mispriced_value_breakdown(_row(eps_yoy_pct=60.0, eps_ttm_at_high=0))
    assert explosive["mispriced_earnings_score"] < steady["mispriced_earnings_score"]


def test_high_ttm_alone_is_not_a_peak_flag():
    """只有 TTM 在高點但年增溫和、PE 未在極低分位 → 不算循環高峰。"""
    result = mispriced_value_breakdown(_row(eps_yoy_pct=40.0, eps_ttm_at_high=1, pe_percentile=35.0))
    assert result["mispriced_cyclical_peak"] == 0


def test_pe_history_percentile_and_median():
    """PE 一年分位與中位數由日線歷史算出，供「跟過去比差多少」判讀。"""
    import sqlite3
    import tempfile
    from pathlib import Path

    from stock_chip.official import connect_db
    from stock_chip.scan import load_history_stats

    tmp = Path(tempfile.mkdtemp()) / "pe.sqlite"
    with connect_db(tmp) as conn:
        # 120 個交易日：前 100 天 PE 25 倍、最後 20 天殺到 12 倍
        dates = []
        for i in range(120):
            date = f"2026-{(i // 21) + 1:02d}-{(i % 21) + 1:02d}"
            dates.append(date)
            conn.execute(
                "INSERT OR IGNORE INTO trading_days(date, market, price_count, institutional_count,"
                " margin_count, updated_at) VALUES (?, 'TWSE', 1, 1, 1, 'now')",
                (date,),
            )
            pe = 25.0 if i < 100 else 12.0
            conn.execute(
                "INSERT INTO daily_prices(date, stock_id, name, close, high, low, pe_ratio, updated_at)"
                " VALUES (?, '2385', '群光', 100.0, 101.0, 99.0, ?, 'now')",
                (date, pe),
            )
        conn.commit()
        stats = load_history_stats(conn, dates[-1])
        item = stats.get("2385", {})
        # 現在 12 倍，過去 100 天都在 25 倍 → 分位極低、明顯低於中位
        assert item["pe_percentile"] == 0.0
        assert item["pe_median_1y"] == 25.0
        assert item["pe_vs_median_pct"] == -52.0

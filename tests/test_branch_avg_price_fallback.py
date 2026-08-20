"""分點均價的回退，以及「外資近5日買超」在 20 日窗口下必為空。

兩個 bug 都是同一種形狀：程式沒有出錯、管線報成功、頁面照常渲染，但某個東西
結構上永遠不會有值——所以沒有人會發現它壞了。跟先前的 normalize_scan_row
白名單、TWSE 的「200 版 404」是同一類。
"""
import re
from pathlib import Path

from stock_chip.scan import build_rankings, confluence_breakdown

DAYS = 20
INDEX_HTML = Path("stock_chip/static/index.html")


def _row(stock_id: str = "2330", **overrides) -> dict:
    """一列剛好能通過 confluence_breakdown 前置條件的資料。"""
    base = {
        "days": DAYS,
        "observed_days": DAYS,
        "stock_id": stock_id,
        "close": 100.0,
        "base_score": 10.0,
        f"{DAYS}d_volume": 100_000_000,
        f"{DAYS}d_inst_net": 5_000_000,
        f"{DAYS}d_foreign_net": 4_000_000,
        f"{DAYS}d_trust_net": 1_000_000,
        "top_buy_branch_net_lot": 20_000,
        "top_buy_branch_avg_price": None,   # MoneyDJ 區間表沒有這個欄位，永遠是 None
        "top_buy_branch_est_cost": 96.0,
        "close_vs_avg_pct": 2.0,
        "branch_buy_streak": 3,
        "foreign_buy_streak": 3,
        "trust_buy_streak": 2,
        "revenue_momentum_score": 0.0,
    }
    base.update(overrides)
    return base


# ---------- 分點均價回退 ----------

def test_est_cost_is_used_when_avg_price_is_missing():
    """來源沒有均價欄位時要退回推估成本，否則整條計算靜靜地失效。"""
    out = confluence_breakdown(_row())
    assert out["close_vs_top_buy_avg_pct"] is not None, (
        "close_vs_top_buy_avg_pct 不該是 None——這正是線上每一份排行 JSON "
        "整欄都是 null 的原因"
    )
    # (100 - 96) / 96 * 100 = 4.17
    assert out["close_vs_top_buy_avg_pct"] == 4.17


def test_avg_price_still_wins_when_present():
    """真的有均價時要用均價，回退不能蓋掉正牌資料。"""
    out = confluence_breakdown(_row(top_buy_branch_avg_price=80.0))
    # (100 - 80) / 80 * 100 = 25.0，而不是用 est_cost 算出的 4.17
    assert out["close_vs_top_buy_avg_pct"] == 25.0


def test_reason_text_mentions_branch_average():
    """理由字串裡的『收盤距分點均價』先前從來沒出現過。"""
    out = confluence_breakdown(_row())
    assert "收盤距分點均價" in out["selection_reason"]


def test_price_score_actually_moves():
    """加減分要真的生效：貼著分點成本（+）與遠離分點成本（-）分數必須不同。

    先前 buy_avg 恆為 None，這段分支永遠不執行，兩者分數會一模一樣。
    """
    near = confluence_breakdown(_row(close=98.0))     # 距成本 +2%，落在 -3~5 的加分區
    far = confluence_breakdown(_row(close=130.0))     # 距成本 +35%，落在 >12 的扣分區
    assert near["confluence_price_score"] != far["confluence_price_score"]
    assert near["confluence_price_score"] > far["confluence_price_score"]


def test_no_price_data_still_returns_none():
    """兩個來源都沒值時仍要是 None，不能因為加了回退就掰出數字。"""
    out = confluence_breakdown(_row(top_buy_branch_avg_price=None, top_buy_branch_est_cost=None))
    assert out["close_vs_top_buy_avg_pct"] is None


# ---------- 外資近5日買超：20 日窗口結構上必為空 ----------

def _growth_row(stock_id: str, days: int) -> dict:
    return {
        "days": days,
        "observed_days": days,
        "stock_id": stock_id,
        "name": f"股{stock_id}",
        "market": "TWSE",
        "chip_score": 1.0,
        "total_score": 1.0,
        "latest_foreign_net": 0,
        f"{days}d_foreign_net": 5_000_000,
        f"{days}d_trust_net": 0,
        f"{days}d_inst_net": 5_000_000,
        f"{days}d_volume": 100_000_000,
        "foreign_buy_streak": 0,
        "avg_turnover_100m": 5.0,
        "close_vs_avg_pct": 0.0,
        "revenue_yoy_pct": 20.0,
        "revenue_mom_pct": 10.0,
    }


def test_ranking_is_empty_in_the_20d_window_by_construction():
    """篩選條件寫死 row['days'] == 5，20 日掃描的每一列都不符合。

    這不是資料不足，是結構上不可能有值——所以 20d 的檔案永遠是空的，
    前端必須知道要去讀 5d 的那份。
    """
    rows = [_growth_row("2330", 20), _growth_row("2317", 20)]
    assert build_rankings(rows, 20)["foreign_5d_revenue_growth"] == []


def test_ranking_has_rows_in_the_5d_window():
    rows = [_growth_row("2330", 5), _growth_row("2317", 5)]
    out = build_rankings(rows, 5)["foreign_5d_revenue_growth"]
    assert {r["stock_id"] for r in out} == {"2330", "2317"}


def test_frontend_forces_the_5d_file_for_this_ranking():
    """本機 server 有這個 override，靜態站先前漏了——兩種模式行為必須一致。

    本機那條在 gui_http.py，靜態那條在前端 JS，語言不同無法共用，
    所以用測試把兩邊釘在一起。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert re.search(
        r'ranking\s*===\s*"foreign_5d_revenue_growth"\s*\?\s*"5"', html
    ), "靜態站的 /api/ranking 分支必須把 foreign_5d_revenue_growth 導到 5d 檔案"

    server = Path("stock_chip/gui_http.py").read_text(encoding="utf-8")
    assert 'ranking == "foreign_5d_revenue_growth"' in server, (
        "本機 server 的 override 不見了——若真的要移除，靜態站那條也要一起移除"
    )

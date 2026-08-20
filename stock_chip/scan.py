from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

from stock_chip.official import connect_db, shares_to_lots


DEFAULT_WATCHLIST = ["2376", "2382", "2324", "6196"]


def recent_dates(conn: sqlite3.Connection, days: int) -> list[str]:
    rows = conn.execute(
        "SELECT date FROM trading_days ORDER BY date DESC LIMIT ?",
        (days,),
    ).fetchall()
    return sorted(row[0] for row in rows)


def load_window(conn: sqlite3.Connection, dates: list[str]) -> list[dict[str, Any]]:
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"""
        SELECT
            p.date,
            p.stock_id,
            s.name,
            s.market,
            p.close,
            p.high,
            p.low,
            p.volume,
            p.turnover,
            p.avg_price,
            p.pe_ratio,
            p.dividend_yield,
            p.pb_ratio,
            COALESCE(i.foreign_net, 0) AS foreign_net,
            COALESCE(i.trust_net, 0) AS trust_net,
            COALESCE(i.dealer_net, 0) AS dealer_net,
            m.margin_prev_balance,
            m.margin_balance,
            m.short_prev_balance,
            m.short_balance
        FROM daily_prices p
        JOIN stocks s
            ON s.stock_id = p.stock_id
        LEFT JOIN institutional_trades i
            ON i.date = p.date
           AND i.stock_id = p.stock_id
        LEFT JOIN margin_trades m
            ON m.date = p.date
           AND m.stock_id = p.stock_id
        WHERE p.date IN ({placeholders})
        ORDER BY p.stock_id, p.date
        """,
        dates,
    ).fetchall()
    columns = [
        "date",
        "stock_id",
        "name",
        "market",
        "close",
        "high",
        "low",
        "volume",
        "turnover",
        "avg_price",
        "pe_ratio",
        "dividend_yield",
        "pb_ratio",
        "foreign_net",
        "trust_net",
        "dealer_net",
        "margin_prev_balance",
        "margin_balance",
        "short_prev_balance",
        "short_balance",
    ]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def load_branch_leaders(
    conn: sqlite3.Connection,
    as_of_date: str,
    days: int,
) -> dict[str, dict[str, Any]]:
    # 分點資料可能比行情晚一個交易日（來源尚未更新當日資料時），
    # 找不到精確日期就回退到最近一次已抓到的日期，避免欄位整批變空。
    latest = conn.execute(
        """
        SELECT MAX(as_of_date)
        FROM broker_branch_topn
        WHERE window_days = ?
          AND as_of_date <= ?
        """,
        (days, as_of_date),
    ).fetchone()
    effective_date = latest[0] if latest and latest[0] else as_of_date
    rows = conn.execute(
        """
        SELECT
            stock_id,
            rank_side,
            broker_name,
            buy_lot,
            sell_lot,
            net_lot,
            avg_price,
            source
        FROM broker_branch_topn
        WHERE as_of_date = ?
          AND window_days = ?
          AND rank_no = 1
        """,
        (effective_date, days),
    ).fetchall()
    leaders: dict[str, dict[str, Any]] = {}
    columns = [
        "stock_id",
        "rank_side",
        "broker_name",
        "buy_lot",
        "sell_lot",
        "net_lot",
        "avg_price",
        "source",
    ]
    for raw in rows:
        row = dict(zip(columns, raw, strict=True))
        stock = leaders.setdefault(row["stock_id"], {})
        prefix = "top_buy_branch" if row["rank_side"] == "buy" else "top_sell_branch"
        stock[f"{prefix}_name"] = row["broker_name"]
        stock[f"{prefix}_buy_lot"] = row["buy_lot"]
        stock[f"{prefix}_sell_lot"] = row["sell_lot"]
        stock[f"{prefix}_net_lot"] = row["net_lot"]
        stock[f"{prefix}_avg_price"] = row["avg_price"]
        stock["branch_source"] = row["source"]
    return leaders


HISTORY_TRADING_DAYS = 252


def load_history_stats(conn: sqlite3.Connection, latest_date: str) -> dict[str, dict[str, Any]]:
    """Long-horizon stats from accumulated daily history (52w high, 6m/12m returns, MA levels, RS vs TAIEX)."""
    date_rows = conn.execute(
        "SELECT date FROM trading_days WHERE date <= ? ORDER BY date DESC LIMIT ?",
        (latest_date, HISTORY_TRADING_DAYS),
    ).fetchall()
    dates = sorted(row[0] for row in date_rows)
    if len(dates) < 30:
        return {}
    date_from = dates[0]

    index_rows = conn.execute(
        """
        SELECT date, close FROM market_index_daily
        WHERE index_code = 'TAIEX' AND close IS NOT NULL AND date >= ? AND date <= ?
        ORDER BY date
        """,
        (date_from, latest_date),
    ).fetchall()
    index_closes = [row[1] for row in index_rows]

    def horizon_return(closes: list[float], periods: int) -> float | None:
        if len(closes) < periods + 1 or not closes[-(periods + 1)]:
            return None
        return (closes[-1] - closes[-(periods + 1)]) / closes[-(periods + 1)] * 100

    index_return_6m = horizon_return(index_closes, 120)
    index_return_12m = horizon_return(index_closes, 240)

    rows = conn.execute(
        """
        SELECT stock_id, close, high, low, pe_ratio FROM daily_prices
        WHERE date >= ? AND date <= ?
        ORDER BY stock_id, date
        """,
        (date_from, latest_date),
    ).fetchall()
    series: dict[str, dict[str, list[float]]] = {}
    for stock_id, close, high, low, pe_ratio in rows:
        if close is None:
            continue
        bucket = series.setdefault(stock_id, {"close": [], "high": [], "low": [], "pe": []})
        bucket["close"].append(close)
        bucket["high"].append(high if high is not None else close)
        bucket["low"].append(low if low is not None else close)
        if pe_ratio is not None and pe_ratio > 0:
            bucket["pe"].append(pe_ratio)

    output: dict[str, dict[str, Any]] = {}
    for stock_id, bucket in series.items():
        closes = bucket["close"]
        history_days = len(closes)
        close = closes[-1]
        item: dict[str, Any] = {"history_days": history_days}

        return_6m = horizon_return(closes, 120)
        return_12m = horizon_return(closes, 240)
        item["return_6m_pct"] = round(return_6m, 2) if return_6m is not None else None
        item["return_12m_pct"] = round(return_12m, 2) if return_12m is not None else None
        item["rs_6m_pct"] = (
            round(return_6m - index_return_6m, 2)
            if return_6m is not None and index_return_6m is not None
            else None
        )
        item["rs_12m_pct"] = (
            round(return_12m - index_return_12m, 2)
            if return_12m is not None and index_return_12m is not None
            else None
        )

        # PE 歷史分位：現在的本益比落在自己過去一年的第幾百分位。
        # 「PE 15 倍」本身無意義，「過去一年多在 20-30 倍、現在剩 14 倍」才是被殺的證據。
        pe_history = bucket["pe"]
        if len(pe_history) >= 60:
            current_pe = pe_history[-1]
            below = sum(1 for value in pe_history if value < current_pe)
            item["pe_percentile"] = round(below / len(pe_history) * 100, 1)
            median = sorted(pe_history)[len(pe_history) // 2]
            item["pe_median_1y"] = round(median, 2)
            item["pe_vs_median_pct"] = round((current_pe - median) / median * 100, 1) if median else None
        else:
            item["pe_percentile"] = None
            item["pe_median_1y"] = None
            item["pe_vs_median_pct"] = None

        # Need at least half a year of history before calling it a 52-week level.
        if history_days >= 120:
            high_52w = max(bucket["high"])
            low_52w = min(bucket["low"])
            item["close_vs_52w_high_pct"] = round((close - high_52w) / high_52w * 100, 2) if high_52w else None
            item["close_vs_52w_low_pct"] = round((close - low_52w) / low_52w * 100, 2) if low_52w else None
        else:
            item["close_vs_52w_high_pct"] = None
            item["close_vs_52w_low_pct"] = None

        for period, key in ((60, "close_vs_ma60_pct"), (240, "close_vs_ma240_pct")):
            if history_days >= period:
                ma = sum(closes[-period:]) / period
                item[key] = round((close - ma) / ma * 100, 2) if ma else None
            else:
                item[key] = None

        item["long_momentum_score"] = long_momentum_score_row(item)
        output[stock_id] = item
    return output


def long_momentum_score_row(row: dict[str, Any]) -> float:
    """Long-horizon trend score: 6m return, RS vs market, 52w-high proximity, MA240 regime."""
    return_6m = row.get("return_6m_pct")
    rs_6m = row.get("rs_6m_pct")
    vs_52w_high = row.get("close_vs_52w_high_pct")
    vs_ma240 = row.get("close_vs_ma240_pct")
    if return_6m is None and vs_52w_high is None and vs_ma240 is None:
        return 0.0
    score = 0.0
    if return_6m is not None:
        score += bounded(return_6m, -30, 60) * 0.1
    if rs_6m is not None:
        score += bounded(rs_6m, -30, 60) * 0.12
    if vs_52w_high is not None:
        if vs_52w_high >= -3:
            score += 6.0
        elif vs_52w_high <= -30:
            score -= 4.0
    if vs_ma240 is not None:
        if vs_ma240 > 0:
            score += 4.0
        else:
            score -= 3.0
    return round(bounded(score, -12.0, 22.0), 2)


def load_profiles(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT stock_id, industry_name, sub_industry FROM stock_profiles"
        ).fetchall()
    except sqlite3.OperationalError:
        try:
            rows = [(r[0], r[1], "") for r in conn.execute("SELECT stock_id, industry_name FROM stock_profiles")]
        except sqlite3.OperationalError:
            return {}
    return {row[0]: {"industry": row[1] or "", "sub_industry": row[2] or ""} for row in rows}


def load_revenue_momentum(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT stock_id, revenue_month, mom_pct, yoy_pct, cumulative_yoy_pct
        FROM monthly_revenues
        ORDER BY stock_id, revenue_month DESC
        """
    ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    for stock_id, revenue_month, mom_pct, yoy_pct, cumulative_yoy_pct in rows:
        if stock_id in output:
            continue
        output[stock_id] = {
            "revenue_month": revenue_month,
            "revenue_mom_pct": mom_pct,
            "revenue_yoy_pct": yoy_pct,
            "revenue_cumulative_yoy_pct": cumulative_yoy_pct,
        }
    return output


def load_shareholding_dispersion(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """TDCC 每週股權分散：千張大戶/400張/散戶比率與近 1 週、4 週變化。"""
    try:
        dates = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT data_date FROM shareholding_dispersion ORDER BY data_date DESC LIMIT 5"
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return {}
    if not dates:
        return {}
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"""
        SELECT data_date, stock_id, level, holder_count, share_pct
        FROM shareholding_dispersion
        WHERE data_date IN ({placeholders})
          AND level != 16
        """,
        dates,
    ).fetchall()
    # by_stock[stock_id][data_date] = {"big": pct, "b400": pct, "retail": pct, "holders": n}
    by_stock: dict[str, dict[str, dict[str, float]]] = {}
    for data_date, stock_id, level, holder_count, share_pct in rows:
        bucket = by_stock.setdefault(stock_id, {}).setdefault(
            data_date, {"big": None, "b400": 0.0, "retail": 0.0, "holders": None}
        )
        if share_pct is None:
            continue
        if level == 15:
            bucket["big"] = share_pct
            bucket["b400"] += share_pct
        elif 12 <= level <= 14:
            bucket["b400"] += share_pct
        elif level <= 9:
            bucket["retail"] += share_pct
        if level == 17:
            bucket["holders"] = holder_count

    latest_date = dates[0]
    output: dict[str, dict[str, Any]] = {}
    for stock_id, weeks in by_stock.items():
        latest = weeks.get(latest_date)
        if latest is None or latest["big"] is None:
            continue
        ordered = sorted(weeks)
        prev = weeks.get(ordered[-2]) if len(ordered) >= 2 else None
        oldest = weeks.get(ordered[0]) if len(ordered) >= 4 else None
        change_1w = (
            round(latest["big"] - prev["big"], 2)
            if prev is not None and prev["big"] is not None
            else None
        )
        change_4w = (
            round(latest["big"] - oldest["big"], 2)
            if oldest is not None and oldest["big"] is not None
            else None
        )
        retail_change_1w = (
            round(latest["retail"] - prev["retail"], 2) if prev is not None else None
        )
        output[stock_id] = {
            "disp_date": latest_date,
            "big_holder_pct": round(latest["big"], 2),
            "holder_400_pct": round(latest["b400"], 2),
            "retail_pct": round(latest["retail"], 2),
            "total_holders": latest["holders"],
            "big_holder_change_1w": change_1w,
            "big_holder_change_4w": change_4w,
            "retail_change_1w": retail_change_1w,
        }
    return output


_DAY_TRADER_BRANCHES: set[str] | None = None


def load_day_trader_branches() -> set[str]:
    """人工維護的隔日沖分點清單（stock_chip/day_traders.json，可自行增修）。"""
    global _DAY_TRADER_BRANCHES
    if _DAY_TRADER_BRANCHES is None:
        import json

        path = Path(__file__).parent / "day_traders.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _DAY_TRADER_BRANCHES = {name.strip() for name in data.get("branches", []) if name.strip()}
        except (OSError, json.JSONDecodeError):
            _DAY_TRADER_BRANCHES = set()
    return _DAY_TRADER_BRANCHES


def load_branch_streaks(
    conn: sqlite3.Connection,
    branch_leaders: dict[str, dict[str, Any]],
    dates: list[str],
) -> dict[str, dict[str, Any]]:
    """區間 top 買超分點在 broker_branch_daily 的尾端連續買超天數與推估成本。

    連買：從最近一日往回，net_lot>0 連續天數（缺日斷鏈）。
    推估成本：窗口內 Σ(單日買超 × 當日成交均價)/Σ(買超)，需 ≥3 天資料；
    補回 MoneyDJ 區間表沒有均價欄位的缺口。"""
    if not branch_leaders or not dates:
        return {}
    top_buy = {
        stock_id: info.get("top_buy_branch_name")
        for stock_id, info in branch_leaders.items()
        if info.get("top_buy_branch_name")
    }
    if not top_buy:
        return {}
    date_set = list(dates)
    placeholders_dates = ",".join("?" for _ in date_set)
    output: dict[str, dict[str, Any]] = {}
    items = list(top_buy.items())
    for start in range(0, len(items), 300):
        chunk = items[start : start + 300]
        stock_ids = [stock_id for stock_id, _ in chunk]
        placeholders_ids = ",".join("?" for _ in stock_ids)
        rows = conn.execute(
            f"""
            SELECT stock_id, broker_name, trade_date, net_lot
            FROM broker_branch_daily
            WHERE stock_id IN ({placeholders_ids})
              AND trade_date IN ({placeholders_dates})
            ORDER BY stock_id, trade_date
            """,
            [*stock_ids, *date_set],
        ).fetchall()
        daily: dict[tuple[str, str], dict[str, float]] = {}
        for stock_id, broker_name, trade_date, net_lot in rows:
            if broker_name != top_buy.get(stock_id):
                continue
            daily.setdefault((stock_id, broker_name), {})[trade_date] = net_lot
        avg_price_rows = conn.execute(
            f"""
            SELECT stock_id, date, avg_price
            FROM daily_prices
            WHERE stock_id IN ({placeholders_ids})
              AND date IN ({placeholders_dates})
              AND avg_price IS NOT NULL
            """,
            [*stock_ids, *date_set],
        ).fetchall()
        prices: dict[str, dict[str, float]] = {}
        for stock_id, date, avg_price in avg_price_rows:
            prices.setdefault(stock_id, {})[date] = avg_price
        for (stock_id, broker_name), day_nets in daily.items():
            streak = 0
            for trade_date in reversed(date_set):
                net = day_nets.get(trade_date)
                if net is None or net <= 0:
                    break
                streak += 1
            buy_days = [(d, net) for d, net in day_nets.items() if net and net > 0]
            est_cost = None
            priced = [(net, prices.get(stock_id, {}).get(d)) for d, net in buy_days]
            priced = [(net, price) for net, price in priced if price]
            if len(priced) >= 3:
                total_lots = sum(net for net, _ in priced)
                if total_lots > 0:
                    est_cost = round(sum(net * price for net, price in priced) / total_lots, 2)
            output[stock_id] = {
                "branch_buy_streak": streak,
                "branch_streak_broker": broker_name,
                "top_buy_branch_est_cost": est_cost,
            }
    return output


def load_fundamentals(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """MOPS 季度財報：最新季三率、毛利率連升/連降季數、近四季 EPS 與自算本益比。

    quarterly_financials 存的是累計值；此處差分成單季後再算比率。"""
    from stock_chip.financials import single_quarter_values

    try:
        raw = conn.execute(
            """
            SELECT stock_id, year_quarter, revenue, gross_profit, operating_income, net_income, eps
            FROM quarterly_financials
            ORDER BY stock_id, year_quarter
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    by_stock: dict[str, list[dict[str, Any]]] = {}
    columns = ["year_quarter", "revenue", "gross_profit", "operating_income", "net_income", "eps"]
    for row in raw:
        by_stock.setdefault(row[0], []).append(dict(zip(columns, row[1:], strict=True)))

    output: dict[str, dict[str, Any]] = {}
    for stock_id, cumulative_rows in by_stock.items():
        quarters = single_quarter_values(cumulative_rows)
        if not quarters:
            continue
        latest = quarters[-1]
        gross_margins = [q["gross_margin_pct"] for q in quarters if q["gross_margin_pct"] is not None]
        streak = 0
        if len(gross_margins) >= 2:
            direction = 0
            for prev, cur in zip(gross_margins, gross_margins[1:], strict=False):
                step = 1 if cur > prev else -1 if cur < prev else 0
                if step == 0:
                    direction = 0
                    streak = 0
                elif step == direction:
                    streak += step
                else:
                    direction = step
                    streak = step
        eps_values = [q["eps"] for q in quarters[-4:] if q["eps"] is not None]
        eps_ttm = round(sum(eps_values), 2) if len(eps_values) == 4 else None
        # TTM EPS 是否落在可得歷史的高點：循環股（記憶體/航運/鋼鐵）在獲利
        # 高峰時 PE 反而最低，「便宜」是市場預期獲利即將反轉，不是錯殺。
        ttm_series: list[float] = []
        for end in range(4, len(quarters) + 1):
            window = [q["eps"] for q in quarters[end - 4:end] if q["eps"] is not None]
            if len(window) == 4:
                ttm_series.append(sum(window))
        eps_ttm_at_high = None
        if len(ttm_series) >= 3 and eps_ttm is not None:
            eps_ttm_at_high = 1 if eps_ttm >= max(ttm_series) - 1e-9 else 0
        # 單季 EPS 年增：與去年同季比（差 4 季），避開淡旺季造成的季增誤判
        eps_yoy = None
        if len(quarters) >= 5:
            current_eps = latest["eps"]
            year_ago_eps = quarters[-5]["eps"]
            if current_eps is not None and year_ago_eps is not None and year_ago_eps > 0:
                eps_yoy = round((current_eps - year_ago_eps) / year_ago_eps * 100, 1)
        # 營益率年增（百分點）：本業賺錢能力的方向
        operating_margin_yoy_pt = None
        if len(quarters) >= 5:
            current_om = latest["operating_margin_pct"]
            year_ago_om = quarters[-5]["operating_margin_pct"]
            if current_om is not None and year_ago_om is not None:
                operating_margin_yoy_pt = round(current_om - year_ago_om, 2)
        output[stock_id] = {
            "fin_quarter": latest["year_quarter"],
            "gross_margin_pct": latest["gross_margin_pct"],
            "operating_margin_pct": latest["operating_margin_pct"],
            "net_margin_pct": latest["net_margin_pct"],
            "gross_margin_streak": streak,
            "eps_ttm": eps_ttm,
            "eps_single_q": latest["eps"],
            "eps_yoy_pct": eps_yoy,
            "eps_ttm_at_high": eps_ttm_at_high,
            "operating_margin_yoy_pt": operating_margin_yoy_pt,
        }
    return output


def consecutive_count(rows: list[dict[str, Any]], key: str, direction: int) -> int:
    count = 0
    for row in reversed(rows):
        value = row[key] or 0
        if direction > 0 and value > 0:
            count += 1
        elif direction < 0 and value < 0:
            count += 1
        else:
            break
    return count


def bounded(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Cutler RSI over the most recent `period` price changes."""
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1) :]
    gains = 0.0
    losses = 0.0
    for prev, cur in zip(window, window[1:], strict=False):
        change = cur - prev
        if change > 0:
            gains += change
        else:
            losses -= change
    if gains + losses == 0:
        return 50.0
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def compute_annualized_volatility_pct(closes: list[float]) -> float | None:
    """Annualized standard deviation of daily returns, in percent."""
    if len(closes) < 6:
        return None
    returns = [
        (cur - prev) / prev
        for prev, cur in zip(closes, closes[1:], strict=False)
        if prev
    ]
    if len(returns) < 5:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round((variance**0.5) * (252**0.5) * 100, 2)


def momentum_score_row(row: dict[str, Any]) -> float:
    """Trend-following score: period return, RSI zone, distance to period high, volatility penalty."""
    period_return = row.get("period_return_pct")
    rsi = row.get("rsi14")
    vs_high = row.get("close_vs_high_pct")
    volatility = row.get("volatility_pct")
    if period_return is None and rsi is None and vs_high is None:
        return 0.0
    score = 0.0
    if period_return is not None:
        score += bounded(period_return, -10, 15) * 0.6
    if rsi is not None:
        if rsi >= 75:
            score -= 4.0
        elif rsi >= 55:
            score += 4.0
        elif rsi < 30:
            score += 1.5
    if vs_high is not None:
        if vs_high >= -2:
            score += 5.0
        elif vs_high <= -15:
            score -= 3.0
    if volatility is not None and volatility > 65:
        score -= 3.0
    return round(bounded(score, -15.0, 20.0), 2)


def valuation_score_row(row: dict[str, Any]) -> float:
    """Light-weight value tilt from exchange-disclosed PE, PB and dividend yield."""
    pe = row.get("pe_ratio")
    pb = row.get("pb_ratio")
    dividend_yield = row.get("dividend_yield")
    if pe is None and pb is None and dividend_yield is None:
        return 0.0
    score = 0.0
    if pe is not None and pe > 0:
        if pe <= 12:
            score += 3.0
        elif pe <= 18:
            score += 1.5
        elif pe >= 40:
            score -= 2.0
    if pb is not None and pb > 0:
        if pb <= 1.5:
            score += 2.0
        elif pb >= 6:
            score -= 1.5
    if dividend_yield is not None:
        if dividend_yield >= 5:
            score += 3.0
        elif dividend_yield >= 3:
            score += 1.5
    return round(bounded(score, -4.0, 8.0), 2)


def fundamental_score_row(row: dict[str, Any]) -> float:
    """獲利品質：淨利率水準 + 毛利率趨勢 + 近四季獲利為正。缺財報時為 0。"""
    net_margin = row.get("net_margin_pct")
    streak = row.get("gross_margin_streak")
    eps_ttm = row.get("eps_ttm")
    if net_margin is None and streak is None and eps_ttm is None:
        return 0.0
    score = 0.0
    if net_margin is not None:
        if net_margin >= 20:
            score += 3.0
        elif net_margin >= 10:
            score += 1.5
        elif net_margin < 0:
            score -= 3.0
    if streak:
        score += bounded(streak * 1.2, -3.5, 3.5)
    if eps_ttm is not None:
        if eps_ttm > 0:
            score += 1.5
        else:
            score -= 2.0
    return round(bounded(score, -8.0, 8.0), 2)


def dispersion_score_row(row: dict[str, Any]) -> float:
    """TDCC 千張大戶比率變化：大戶增持加分、散戶減少小幅加分，反向扣分。"""
    change_1w = row.get("big_holder_change_1w")
    retail_change = row.get("retail_change_1w")
    if change_1w is None and retail_change is None:
        return 0.0
    score = 0.0
    if change_1w is not None:
        score += bounded(change_1w * 8, -4.0, 4.0)
    change_4w = row.get("big_holder_change_4w")
    if change_4w is not None:
        score += bounded(change_4w * 3, -1.5, 1.5)
    if retail_change is not None and change_1w is not None:
        # 大戶接、散戶出的換手結構最有意義
        if change_1w > 0 and retail_change < 0:
            score += 0.5
    return round(bounded(score, -6.0, 6.0), 2)


def score_row(row: dict[str, Any]) -> float:
    if row["observed_days"] < row["days"]:
        return 0.0
    volume = row[f"{row['days']}d_volume"] or 0
    if volume <= 0:
        return 0.0
    foreign_ratio = row[f"{row['days']}d_foreign_net"] / volume * 100
    trust_ratio = row[f"{row['days']}d_trust_net"] / volume * 100
    inst_ratio = row[f"{row['days']}d_inst_net"] / volume * 100
    close_vs_avg_pct = row["close_vs_avg_pct"]

    score = 0.0
    score += bounded(foreign_ratio, -10, 10) * 2.0
    score += bounded(trust_ratio, -10, 10) * 2.5
    score += bounded(inst_ratio, -15, 15) * 0.8
    score += min(row["foreign_buy_streak"], 5) * 2.0
    score += min(row["trust_buy_streak"], 5) * 2.5
    score -= min(row["foreign_sell_streak"], 5) * 2.0
    score -= min(row["trust_sell_streak"], 5) * 2.5

    if row[f"{row['days']}d_inst_net"] > 0 and close_vs_avg_pct is not None:
        if -3 <= close_vs_avg_pct <= 3:
            score += 6.0
        elif close_vs_avg_pct < -3:
            score += 3.0
        elif close_vs_avg_pct > 8:
            score -= 4.0
    return round(score, 2)


def branch_score_row(row: dict[str, Any]) -> float:
    if row["observed_days"] < row["days"]:
        return 0.0
    close = row["close"]
    buy_net = row.get("top_buy_branch_net_lot")
    # MoneyDJ 區間表沒有均價欄位，改用每日明細推估的成本（top_buy_branch_est_cost）
    buy_avg = row.get("top_buy_branch_avg_price") or row.get("top_buy_branch_est_cost")
    sell_net = row.get("top_sell_branch_net_lot")
    sell_avg = row.get("top_sell_branch_avg_price")
    if close is None or buy_net is None:
        return 0.0

    score = 0.0
    volume_lot = (row[f"{row['days']}d_volume"] or 0) / 1000
    is_day_trader = bool(row.get("top_buy_is_day_trader"))
    if volume_lot > 0:
        if is_day_trader:
            # 隔日沖分點的大買超是短線籌碼，不加分反而扣分
            score -= 6.0
        else:
            score += bounded(buy_net / volume_lot * 100, 0, 20) * 1.2
        if sell_net is not None:
            score -= bounded(abs(sell_net) / volume_lot * 100, 0, 20) * 0.5
    streak = row.get("branch_buy_streak")
    if streak and not is_day_trader:
        score += min(streak, 5) * 0.8

    close_vs_buy_avg = (close - buy_avg) / buy_avg * 100 if buy_avg else None
    if close_vs_buy_avg is not None:
        if -3 <= close_vs_buy_avg <= 5:
            score += 10.0
        elif close_vs_buy_avg < -3:
            score += 4.0
        elif close_vs_buy_avg > 12:
            score -= 6.0

    if sell_avg and close > sell_avg * 1.05:
        score += 2.0
    return round(score, 2)


def revenue_momentum_score(row: dict[str, Any]) -> float:
    yoy = row.get("revenue_yoy_pct")
    mom = row.get("revenue_mom_pct")
    cumulative = row.get("revenue_cumulative_yoy_pct")
    if yoy is None and mom is None and cumulative is None:
        return 0.0
    score = 0.0
    if yoy is not None:
        score += bounded(yoy, -30, 60) * 0.18
    if mom is not None:
        score += bounded(mom, -25, 40) * 0.12
    if cumulative is not None:
        score += bounded(cumulative, -30, 60) * 0.16
    return round(bounded(score, -10, 24), 2)


def margin_score_row(row: dict[str, Any]) -> float:
    margin_change = row.get("margin_balance_change_lot")
    short_change = row.get("short_balance_change_lot")
    volume_lot = (row.get(f"{row['days']}d_volume") or 0) / 1000
    close_vs_avg_pct = row.get("close_vs_avg_pct")
    if volume_lot <= 0:
        return 0.0

    score = 0.0
    if margin_change is not None:
        margin_ratio = margin_change / volume_lot * 100
        if margin_change < 0:
            score += bounded(abs(margin_ratio), 0, 8) * 1.0
        else:
            score -= bounded(margin_ratio, 0, 8) * 0.8
    if short_change is not None:
        short_ratio = short_change / volume_lot * 100
        if short_change > 0 and close_vs_avg_pct is not None and close_vs_avg_pct >= 0:
            score += bounded(short_ratio, 0, 5) * 0.6
        elif short_change < 0:
            score -= bounded(abs(short_ratio), 0, 5) * 0.3
    return round(bounded(score, -10, 10), 2)


def volume_signal_row(row: dict[str, Any]) -> str:
    ratio_1d = row.get("volume_ratio_1d")
    ratio_5d = row.get("volume_ratio_5d")
    if ratio_1d is None and ratio_5d is None:
        return "無量能資料"
    if (ratio_1d or 0) >= 2.0:
        return "強放量"
    if (ratio_1d or 0) >= 1.5 or (ratio_5d or 0) >= 1.3:
        return "放量"
    if (ratio_1d or 0) <= 0.6 and (ratio_5d or 1) <= 0.8:
        return "量縮"
    return "正常"


def volume_score_row(row: dict[str, Any]) -> float:
    if row["observed_days"] < max(5, min(row["days"], 5)):
        return 0.0
    ratio_1d = row.get("volume_ratio_1d")
    ratio_5d = row.get("volume_ratio_5d")
    if ratio_1d is None and ratio_5d is None:
        return 0.0
    score = 0.0
    if ratio_1d is not None:
        score += bounded((ratio_1d - 1.0) * 12.0, -8.0, 18.0)
    if ratio_5d is not None:
        score += bounded((ratio_5d - 1.0) * 16.0, -8.0, 18.0)
    if row.get(f"{row['days']}d_inst_net", 0) > 0 and ratio_1d is not None and ratio_1d >= 1.2:
        score += 3.0
    return round(bounded(score, -12.0, 30.0), 2)


def branch_status(row: dict[str, Any]) -> str:
    if row.get("top_buy_branch_name"):
        return "已取得"
    return "未取得"


def base_reason(row: dict[str, Any]) -> str:
    parts: list[str] = []
    if row.get(f"{row['days']}d_foreign_net", 0) > 0:
        parts.append("外資買超")
    if row.get(f"{row['days']}d_trust_net", 0) > 0:
        parts.append("投信買超")
    if row.get("margin_balance_change_lot") is not None:
        if row["margin_balance_change_lot"] < 0:
            parts.append("融資下降")
        elif row["margin_balance_change_lot"] > 0:
            parts.append("融資增加")
    signal = row.get("volume_signal")
    if signal in {"強放量", "放量"}:
        parts.append(signal)
    vs_high = row.get("close_vs_high_pct")
    if vs_high is not None and vs_high >= -2:
        parts.append("接近區間高點")
    vs_52w = row.get("close_vs_52w_high_pct")
    if vs_52w is not None and vs_52w >= -2:
        parts.append("逼近52週高點")
    vs_ma240 = row.get("close_vs_ma240_pct")
    if vs_ma240 is not None and vs_ma240 < 0:
        parts.append("年線之下")
    rsi = row.get("rsi14")
    if rsi is not None and rsi >= 75:
        parts.append("RSI過熱")
    volatility = row.get("volatility_pct")
    if volatility is not None and volatility > 65:
        parts.append("波動偏高")
    branch_streak = row.get("branch_buy_streak")
    if branch_streak and branch_streak >= 3 and not row.get("top_buy_is_day_trader"):
        parts.append(f"主力分點連{branch_streak}買")
    if row.get("top_buy_is_day_trader"):
        parts.append("隔日沖分點買超")
    margin_streak = row.get("gross_margin_streak")
    if margin_streak is not None:
        if margin_streak >= 2:
            parts.append(f"毛利率連{margin_streak}升")
        elif margin_streak <= -2:
            parts.append(f"毛利率連{-margin_streak}降")
    big_change = row.get("big_holder_change_1w")
    if big_change is not None:
        if big_change >= 0.3:
            parts.append("千張大戶增持")
        elif big_change <= -0.3:
            parts.append("千張大戶減持")
    rev = revenue_reason(row)
    if rev:
        parts.append(rev)
    return "；".join(parts)


def mispriced_value_breakdown(row: dict[str, Any]) -> dict[str, Any]:
    """錯殺價值：獲利在轉強、股價卻被殺到自身歷史低估區的個股。

    大跌時低本益比有兩種：錯殺（獲利向上、被情緒帶下來）與價值陷阱
    （便宜是因為基本面正在壞）。此處以獲利動能剔除後者，再以「PE 自身
    歷史分位」而非絕對倍數衡量便宜——PE 15 倍本身無意義，
    「過去一年多在 20-30 倍、現在剩 14 倍」才是被殺的證據。

    景氣循環股（航運/鋼鐵/面板）低 PE 常出現在獲利高峰，EPS 年增門檻
    可擋掉衰退期，但無法完全免疫，前端說明會註記。
    """
    empty = {
        "mispriced_score": 0.0,
        "mispriced_earnings_score": 0.0,
        "mispriced_cheap_score": 0.0,
        "mispriced_trap_penalty": 0.0,
        "mispriced_eligible": 0,
        "mispriced_reason": "",
    }
    eps_ttm = row.get("eps_ttm")
    eps_single = row.get("eps_single_q")
    volume_lot = (row.get(f"{row['days']}d_volume") or 0) / 1000 / max(row.get("observed_days") or 1, 1)

    # 資格門檻：虧損股談本益比無意義；日均量過低是流動性陷阱（進得去出不來）
    if eps_ttm is None or eps_ttm <= 0:
        return {**empty, "mispriced_reason": "近四季 EPS 未轉正或缺財報"}
    if eps_single is not None and eps_single <= 0:
        return {**empty, "mispriced_reason": "最新單季虧損"}
    if volume_lot < 500:
        return {**empty, "mispriced_reason": f"日均量 {volume_lot:.0f} 張偏低（門檻 500）"}

    eps_yoy = row.get("eps_yoy_pct")
    streak = row.get("gross_margin_streak") or 0
    om_yoy = row.get("operating_margin_yoy_pt")
    revenue_yoy = row.get("revenue_yoy_pct")
    pe = row.get("pe_ratio")
    pe_pct = row.get("pe_percentile")
    vs_high = row.get("close_vs_52w_high_pct")
    dividend_yield = row.get("dividend_yield")
    pb = row.get("pb_ratio")

    # 第 2 層：獲利愈來愈強
    earnings = 0.0
    if eps_yoy is not None:
        # 年增 ≥300% 幾乎都是去年低基期（記憶體/航運等循環股的谷底反彈），
        # 給分不應高於穩健成長——遞減處理避免把循環高峰捧上榜首
        earnings += (
            6.0 if eps_yoy >= 300
            else 8.0 if eps_yoy >= 50
            else 6.0 if eps_yoy >= 25
            else 4.0 if eps_yoy >= 10
            else 2.0 if eps_yoy > 0
            else -6.0
        )
    earnings += bounded(streak * 1.5, -4.0, 4.0)
    if om_yoy is not None:
        earnings += 3.0 if om_yoy >= 2 else 1.5 if om_yoy >= 0 else -3.0 if om_yoy <= -2 else 0.0
    # 財報最新僅到上一季，月營收補時效性（大跌當下要看最新的獲利證據）
    if revenue_yoy is not None:
        earnings += 5.0 if revenue_yoy >= 20 else 3.0 if revenue_yoy >= 10 else 1.0 if revenue_yoy > 0 else -5.0 if revenue_yoy <= -10 else -2.0

    # 第 3 層：股價被殺到多低
    cheap = 0.0
    if pe_pct is not None:
        cheap += 8.0 if pe_pct <= 10 else 6.0 if pe_pct <= 25 else 3.0 if pe_pct <= 40 else 0.0
    if pe is not None and pe > 0:
        cheap += 4.0 if pe <= 10 else 2.0 if pe <= 15 else -3.0 if pe >= 30 else 0.0
    if vs_high is not None:
        cheap += 5.0 if vs_high <= -40 else 3.5 if vs_high <= -25 else 2.0 if vs_high <= -15 else 0.0
    if dividend_yield is not None:
        cheap += 2.0 if dividend_yield >= 5 else 1.0 if dividend_yield >= 3 else 0.0
    if pb is not None and 0 < pb <= 1.5:
        cheap += 1.5

    # 陷阱扣分：營收與毛利同步走弱＝便宜有理由，不是錯殺
    trap = 0.0
    # 循環高峰疑慮：TTM 獲利在多年高點 + PE 在歷史低分位 + 年增來自低基期
    # ——這組合是記憶體/航運/鋼鐵在循環頂點的典型特徵，低 PE 反是警訊
    cyclical_peak = (
        row.get("eps_ttm_at_high") == 1
        and pe_pct is not None and pe_pct <= 20
        and eps_yoy is not None and eps_yoy >= 300
    )
    if cyclical_peak:
        trap -= 5.0
    if (revenue_yoy is not None and revenue_yoy < -10) and streak < 0:
        trap -= 6.0
    if (eps_yoy is not None and eps_yoy < 0) and (revenue_yoy is not None and revenue_yoy < 0):
        trap -= 4.0
    inst_net = row.get(f"{row['days']}d_inst_net") or 0
    volume = row.get(f"{row['days']}d_volume") or 0
    if volume > 0 and inst_net / volume * 100 <= -3:
        trap -= 2.0

    score = round(bounded(earnings + cheap + trap, -20.0, 40.0), 2)
    return {
        "mispriced_score": score,
        "mispriced_earnings_score": round(earnings, 2),
        "mispriced_cheap_score": round(cheap, 2),
        "mispriced_trap_penalty": round(trap, 2),
        "mispriced_eligible": 1,
        "mispriced_cyclical_peak": 1 if cyclical_peak else 0,
        "mispriced_reason": mispriced_reason(row, earnings, cheap, trap, cyclical_peak),
    }


def mispriced_reason(row: dict[str, Any], earnings: float, cheap: float, trap: float,
                     cyclical_peak: bool = False) -> str:
    """把入選原因寫成人話，讓榜單自帶判讀而不只是分數。"""
    parts: list[str] = []
    eps_yoy = row.get("eps_yoy_pct")
    if eps_yoy is not None and eps_yoy > 0:
        parts.append(f"單季 EPS 年增 {eps_yoy:.0f}%")
    streak = row.get("gross_margin_streak") or 0
    if streak > 0:
        parts.append(f"毛利率連 {streak} 季升")
    elif streak < 0:
        parts.append(f"毛利率連 {abs(streak)} 季降")
    revenue_yoy = row.get("revenue_yoy_pct")
    if revenue_yoy is not None:
        parts.append(f"月營收年{'增' if revenue_yoy >= 0 else '減'} {abs(revenue_yoy):.0f}%")
    pe = row.get("pe_ratio")
    pe_pct = row.get("pe_percentile")
    if pe is not None and pe > 0:
        parts.append(f"PE {pe:.1f}" + (f"（一年 {pe_pct:.0f}% 分位）" if pe_pct is not None else ""))
    vs_high = row.get("close_vs_52w_high_pct")
    if vs_high is not None and vs_high < 0:
        parts.append(f"距 52 週高點 {vs_high:.0f}%")
    text = "、".join(parts)
    if cyclical_peak:
        return f"⚠ 循環高峰疑慮（獲利在多年高點、年增來自低基期，低 PE 可能反映市場預期獲利反轉）：{text}"
    if trap <= -4:
        return f"⚠ 便宜但基本面轉弱：{text}"
    return text


def confluence_breakdown(row: dict[str, Any]) -> dict[str, Any]:
    """Explain cases where institutional flow and top branch buying point the same way."""
    empty = {
        "confluence_score": 0.0,
        "confluence_inst_score": 0.0,
        "confluence_branch_score": 0.0,
        "confluence_price_score": 0.0,
        "confluence_streak_score": 0.0,
        "confluence_direction_score": 0.0,
        "revenue_momentum_score": row.get("revenue_momentum_score", 0.0),
        "selection_score": row.get("base_score", 0.0),
        "complete_chip_score": row.get("base_score", 0.0),
        "inst_net_volume_pct": None,
        "top_buy_branch_volume_pct": None,
        "close_vs_top_buy_avg_pct": None,
        "selection_reason": "資料不足或法人與分點未同向",
    }
    if row["observed_days"] < row["days"]:
        empty["selection_reason"] = revenue_reason(row) or empty["selection_reason"]
        return empty
    volume = row[f"{row['days']}d_volume"] or 0
    volume_lot = volume / 1000
    inst_net = row[f"{row['days']}d_inst_net"] or 0
    foreign_net = row[f"{row['days']}d_foreign_net"] or 0
    trust_net = row[f"{row['days']}d_trust_net"] or 0
    buy_net_lot = row.get("top_buy_branch_net_lot") or 0
    close_vs_avg_pct = row.get("close_vs_avg_pct")
    close = row.get("close")
    # 與 branch_score_row 用同一個回退：MoneyDJ 區間表沒有均價欄位，
    # top_buy_branch_avg_price 結構上永遠是 None，均價要用每日明細推估的成本。
    # 少了這個回退，close_vs_branch_avg 恆為 None，下面那段加減分與
    # 「收盤距分點均價」的理由字串從上線起就沒生效過，
    # close_vs_top_buy_avg_pct 也在每一份排行 JSON 裡整欄是 null。
    buy_avg = row.get("top_buy_branch_avg_price") or row.get("top_buy_branch_est_cost")

    if volume <= 0 or inst_net <= 0 or buy_net_lot <= 0:
        empty["selection_reason"] = revenue_reason(row) or empty["selection_reason"]
        return empty

    inst_ratio = inst_net / volume * 100
    branch_ratio = buy_net_lot / volume_lot * 100 if volume_lot else 0
    close_vs_branch_avg = (close - buy_avg) / buy_avg * 100 if close is not None and buy_avg else None
    inst_score = bounded(inst_ratio, 0, 15) * 1.5
    branch_score = bounded(branch_ratio, 0, 20) * 1.2
    direction_score = 0.0
    if foreign_net > 0:
        direction_score += 6.0
    if trust_net > 0:
        direction_score += 8.0
    streak_score = min(row["foreign_buy_streak"], 5) * 1.2
    streak_score += min(row["trust_buy_streak"], 5) * 1.6
    price_score = 0.0
    if close_vs_avg_pct is not None:
        if -3 <= close_vs_avg_pct <= 5:
            price_score += 6.0
        elif close_vs_avg_pct > 12:
            price_score -= 5.0
    if close_vs_branch_avg is not None:
        if -3 <= close_vs_branch_avg <= 5:
            price_score += 8.0
        elif close_vs_branch_avg > 12:
            price_score -= 6.0

    reasons: list[str] = []
    if foreign_net > 0 and trust_net > 0:
        reasons.append("外資與投信同買")
    elif foreign_net > 0:
        reasons.append("外資主導買超")
    elif trust_net > 0:
        reasons.append("投信主導買超")
    reasons.append(f"前大買超分點約占成交 {round(branch_ratio, 2)}%")
    if close_vs_avg_pct is not None:
        reasons.append(f"收盤距區間均價 {round(close_vs_avg_pct, 2)}%")
    if close_vs_branch_avg is not None:
        reasons.append(f"收盤距分點均價 {round(close_vs_branch_avg, 2)}%")
    if row["foreign_buy_streak"] or row["trust_buy_streak"]:
        reasons.append(f"連買：外{row['foreign_buy_streak']} / 投{row['trust_buy_streak']}")
    rev_reason = revenue_reason(row)
    if rev_reason:
        reasons.append(rev_reason)

    score = inst_score + branch_score + direction_score + streak_score + price_score
    revenue_score = row.get("revenue_momentum_score", 0.0) or 0.0
    return {
        "confluence_score": round(score, 2),
        "confluence_inst_score": round(inst_score, 2),
        "confluence_branch_score": round(branch_score, 2),
        "confluence_price_score": round(price_score, 2),
        "confluence_streak_score": round(streak_score, 2),
        "confluence_direction_score": round(direction_score, 2),
        "revenue_momentum_score": round(revenue_score, 2),
        "selection_score": row.get("base_score", 0.0),
        "complete_chip_score": round((row.get("base_score", 0.0) or 0.0) + score, 2),
        "inst_net_volume_pct": round(inst_ratio, 2),
        "top_buy_branch_volume_pct": round(branch_ratio, 2),
        "close_vs_top_buy_avg_pct": round(close_vs_branch_avg, 2) if close_vs_branch_avg is not None else None,
        "selection_reason": "；".join(reasons),
    }


def revenue_reason(row: dict[str, Any]) -> str:
    if not row.get("revenue_month"):
        return ""
    parts = [f"營收 {row['revenue_month']}"]
    if row.get("revenue_mom_pct") is not None:
        parts.append(f"月增 {round(row['revenue_mom_pct'], 2)}%")
    if row.get("revenue_yoy_pct") is not None:
        parts.append(f"年增 {round(row['revenue_yoy_pct'], 2)}%")
    if row.get("revenue_cumulative_yoy_pct") is not None:
        parts.append(f"累計年增 {round(row['revenue_cumulative_yoy_pct'], 2)}%")
    return "，".join(parts)


def confluence_score_row(row: dict[str, Any]) -> float:
    return float(confluence_breakdown(row)["confluence_score"])


def aggregate(
    rows: list[dict[str, Any]],
    days: int,
    latest_date: str,
    branch_leaders: dict[str, dict[str, Any]] | None = None,
    revenue_momentum: dict[str, dict[str, Any]] | None = None,
    history_stats: dict[str, dict[str, Any]] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    dispersion: dict[str, dict[str, Any]] | None = None,
    fundamentals: dict[str, dict[str, Any]] | None = None,
    branch_streaks: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    branch_leaders = branch_leaders or {}
    revenue_momentum = revenue_momentum or {}
    history_stats = history_stats or {}
    profiles = profiles or {}
    dispersion = dispersion or {}
    fundamentals = fundamentals or {}
    branch_streaks = branch_streaks or {}
    day_trader_branches = load_day_trader_branches()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["stock_id"], []).append(row)

    output: list[dict[str, Any]] = []
    for stock_id, stock_rows in grouped.items():
        stock_rows = sorted(stock_rows, key=lambda row: row["date"])
        latest = stock_rows[-1]
        if latest["date"] != latest_date:
            continue
        volume = sum(row["volume"] or 0 for row in stock_rows)
        latest_volume = latest["volume"] or 0
        avg_volume = volume / len(stock_rows) if stock_rows else 0
        recent5_rows = stock_rows[-min(5, len(stock_rows)) :]
        volume_5d_avg = (
            sum(row["volume"] or 0 for row in recent5_rows) / len(recent5_rows)
            if recent5_rows
            else 0
        )
        volume_ratio_1d = latest_volume / avg_volume if avg_volume else None
        volume_ratio_5d = volume_5d_avg / avg_volume if avg_volume else None
        turnover = sum(row["turnover"] or 0 for row in stock_rows)
        avg_price = turnover / volume if volume else None
        close = latest["close"]
        close_vs_avg_pct = None
        if close is not None and avg_price:
            close_vs_avg_pct = (close - avg_price) / avg_price * 100

        foreign_net = sum(row["foreign_net"] or 0 for row in stock_rows)
        trust_net = sum(row["trust_net"] or 0 for row in stock_rows)
        dealer_net = sum(row["dealer_net"] or 0 for row in stock_rows)

        closes = [row["close"] for row in stock_rows if row["close"] is not None]
        period_return_pct = None
        if len(closes) >= 2 and closes[0]:
            period_return_pct = (closes[-1] - closes[0]) / closes[0] * 100
        highs = [row["high"] if row["high"] is not None else row["close"] for row in stock_rows]
        lows = [row["low"] if row["low"] is not None else row["close"] for row in stock_rows]
        highs = [value for value in highs if value is not None]
        lows = [value for value in lows if value is not None]
        close_vs_high_pct = None
        close_vs_low_pct = None
        if close is not None and highs and max(highs):
            close_vs_high_pct = (close - max(highs)) / max(highs) * 100
        if close is not None and lows and min(lows):
            close_vs_low_pct = (close - min(lows)) / min(lows) * 100
        avg_turnover_100m = (turnover / len(stock_rows) / 1e8) if stock_rows else None

        item = {
            "days": days,
            "observed_days": len(stock_rows),
            "latest_date": latest["date"],
            "stock_id": stock_id,
            "name": latest["name"],
            "market": latest["market"],
            "close": close,
            "pe_ratio": latest.get("pe_ratio"),
            "dividend_yield": latest.get("dividend_yield"),
            "pb_ratio": latest.get("pb_ratio"),
            f"{days}d_avg_price": avg_price,
            "close_vs_avg_pct": close_vs_avg_pct,
            f"{days}d_volume": volume,
            "latest_volume": latest_volume,
            "volume_avg": avg_volume,
            "volume_5d_avg": volume_5d_avg,
            "volume_ratio_1d": volume_ratio_1d,
            "volume_ratio_5d": volume_ratio_5d,
            f"{days}d_foreign_net": foreign_net,
            f"{days}d_trust_net": trust_net,
            f"{days}d_dealer_net": dealer_net,
            f"{days}d_inst_net": foreign_net + trust_net,
            "latest_foreign_net": latest["foreign_net"],
            "latest_trust_net": latest["trust_net"],
            "foreign_buy_streak": consecutive_count(stock_rows, "foreign_net", 1),
            "foreign_sell_streak": consecutive_count(stock_rows, "foreign_net", -1),
            "trust_buy_streak": consecutive_count(stock_rows, "trust_net", 1),
            "trust_sell_streak": consecutive_count(stock_rows, "trust_net", -1),
            "period_return_pct": round(period_return_pct, 2) if period_return_pct is not None else None,
            "rsi14": compute_rsi(closes),
            "volatility_pct": compute_annualized_volatility_pct(closes),
            "close_vs_high_pct": round(close_vs_high_pct, 2) if close_vs_high_pct is not None else None,
            "close_vs_low_pct": round(close_vs_low_pct, 2) if close_vs_low_pct is not None else None,
            "avg_turnover_100m": round(avg_turnover_100m, 3) if avg_turnover_100m is not None else None,
        }
        item.update(branch_leaders.get(stock_id, {}))
        item.update(revenue_momentum.get(stock_id, {}))
        item.update(history_stats.get(stock_id, {}))
        item.update(profiles.get(stock_id, {}))
        item.update(dispersion.get(stock_id, {}))
        item.update(fundamentals.get(stock_id, {}))
        item.update(branch_streaks.get(stock_id, {}))
        item["top_buy_is_day_trader"] = (
            1 if item.get("top_buy_branch_name") in day_trader_branches else 0
        )
        item.setdefault("industry", "")
        item.setdefault("sub_industry", "")
        item.setdefault("long_momentum_score", 0.0)
        margin_values = [row for row in stock_rows if row.get("margin_balance") is not None]
        margin_balance_change = None
        short_balance_change = None
        if margin_values:
            latest_margin = margin_values[-1]
            oldest_margin = margin_values[0]
            if latest_margin.get("margin_balance") is not None and oldest_margin.get("margin_prev_balance") is not None:
                margin_balance_change = latest_margin["margin_balance"] - oldest_margin["margin_prev_balance"]
            if latest_margin.get("short_balance") is not None and oldest_margin.get("short_prev_balance") is not None:
                short_balance_change = latest_margin["short_balance"] - oldest_margin["short_prev_balance"]
        item["margin_balance_change_lot"] = margin_balance_change
        item["short_balance_change_lot"] = short_balance_change
        item["revenue_momentum_score"] = revenue_momentum_score(item)
        item["branch_score"] = branch_score_row(item)
        item["chip_score"] = score_row(item)
        item["margin_score"] = margin_score_row(item)
        item["volume_score"] = volume_score_row(item)
        item["volume_signal"] = volume_signal_row(item)
        item["momentum_score"] = momentum_score_row(item)
        item["valuation_score"] = valuation_score_row(item)
        item["dispersion_score"] = dispersion_score_row(item)
        eps_ttm = item.get("eps_ttm")
        item["pe_ttm"] = round(close / eps_ttm, 2) if close is not None and eps_ttm and eps_ttm > 0 else None
        item["fundamental_score"] = fundamental_score_row(item)
        item["base_score"] = round(item["chip_score"] + item["margin_score"] + item["revenue_momentum_score"], 2)
        item["multifactor_score"] = round(
            item["base_score"]
            + item["momentum_score"]
            + item["long_momentum_score"]
            + item["valuation_score"]
            + item["volume_score"] * 0.5
            + item["dispersion_score"]
            + item["fundamental_score"],
            2,
        )
        item["branch_status"] = branch_status(item)
        item["base_reason"] = base_reason(item)
        item.update(confluence_breakdown(item))
        item.update(mispriced_value_breakdown(item))
        item["total_score"] = item["base_score"]
        output.append(item)
    return output


def to_export_row(row: dict[str, Any], days: int) -> dict[str, Any]:
    avg_price = row[f"{days}d_avg_price"]
    close_vs_avg_pct = row["close_vs_avg_pct"]
    volume = row[f"{days}d_volume"] or 0
    foreign_net = row[f"{days}d_foreign_net"] or 0
    trust_net = row[f"{days}d_trust_net"] or 0
    inst_net = row[f"{days}d_inst_net"] or 0
    return {
        "latest_date": row["latest_date"],
        "stock_id": row["stock_id"],
        "name": row["name"],
        "market": row["market"],
        "industry": row.get("industry", ""),
        "sub_industry": row.get("sub_industry", ""),
        "observed_days": row["observed_days"],
        "close": row["close"],
        "pe_ratio": row.get("pe_ratio"),
        "dividend_yield": row.get("dividend_yield"),
        "pb_ratio": row.get("pb_ratio"),
        "period_return_pct": row.get("period_return_pct"),
        "rsi14": row.get("rsi14"),
        "volatility_pct": row.get("volatility_pct"),
        "close_vs_high_pct": row.get("close_vs_high_pct"),
        "close_vs_low_pct": row.get("close_vs_low_pct"),
        "avg_turnover_100m": row.get("avg_turnover_100m"),
        "momentum_score": row.get("momentum_score", 0.0),
        "history_days": row.get("history_days"),
        "return_6m_pct": row.get("return_6m_pct"),
        "return_12m_pct": row.get("return_12m_pct"),
        "rs_6m_pct": row.get("rs_6m_pct"),
        "rs_12m_pct": row.get("rs_12m_pct"),
        "close_vs_52w_high_pct": row.get("close_vs_52w_high_pct"),
        "close_vs_52w_low_pct": row.get("close_vs_52w_low_pct"),
        "close_vs_ma60_pct": row.get("close_vs_ma60_pct"),
        "close_vs_ma240_pct": row.get("close_vs_ma240_pct"),
        "long_momentum_score": row.get("long_momentum_score", 0.0),
        "valuation_score": row.get("valuation_score", 0.0),
        "disp_date": row.get("disp_date"),
        "big_holder_pct": row.get("big_holder_pct"),
        "big_holder_change_1w": row.get("big_holder_change_1w"),
        "big_holder_change_4w": row.get("big_holder_change_4w"),
        "holder_400_pct": row.get("holder_400_pct"),
        "retail_pct": row.get("retail_pct"),
        "retail_change_1w": row.get("retail_change_1w"),
        "dispersion_score": row.get("dispersion_score", 0.0),
        "fin_quarter": row.get("fin_quarter"),
        "gross_margin_pct": row.get("gross_margin_pct"),
        "operating_margin_pct": row.get("operating_margin_pct"),
        "net_margin_pct": row.get("net_margin_pct"),
        "gross_margin_streak": row.get("gross_margin_streak"),
        "eps_ttm": row.get("eps_ttm"),
        "pe_ttm": row.get("pe_ttm"),
        "pe_percentile": row.get("pe_percentile"),
        "pe_median_1y": row.get("pe_median_1y"),
        "pe_vs_median_pct": row.get("pe_vs_median_pct"),
        "eps_single_q": row.get("eps_single_q"),
        "eps_yoy_pct": row.get("eps_yoy_pct"),
        "operating_margin_yoy_pt": row.get("operating_margin_yoy_pt"),
        "mispriced_score": row.get("mispriced_score"),
        "mispriced_earnings_score": row.get("mispriced_earnings_score"),
        "mispriced_cheap_score": row.get("mispriced_cheap_score"),
        "mispriced_trap_penalty": row.get("mispriced_trap_penalty"),
        "mispriced_cyclical_peak": row.get("mispriced_cyclical_peak"),
        "eps_ttm_at_high": row.get("eps_ttm_at_high"),
        "mispriced_reason": row.get("mispriced_reason"),
        "fundamental_score": row.get("fundamental_score", 0.0),
        "multifactor_score": row.get("multifactor_score", row.get("base_score", row["chip_score"])),
        f"{days}d_avg_price": round(avg_price, 4) if avg_price is not None else None,
        "close_vs_avg_pct": round(close_vs_avg_pct, 2) if close_vs_avg_pct is not None else None,
        f"{days}d_volume_lot": shares_to_lots(volume),
        "latest_volume_lot": shares_to_lots(row.get("latest_volume")),
        "volume_avg_lot": shares_to_lots(row.get("volume_avg")),
        "volume_5d_avg_lot": shares_to_lots(row.get("volume_5d_avg")),
        "volume_ratio_1d": round(row["volume_ratio_1d"], 2) if row.get("volume_ratio_1d") is not None else None,
        "volume_ratio_5d": round(row["volume_ratio_5d"], 2) if row.get("volume_ratio_5d") is not None else None,
        "volume_score": row.get("volume_score", 0.0),
        "volume_signal": row.get("volume_signal", ""),
        f"{days}d_foreign_net_lot": shares_to_lots(foreign_net),
        f"{days}d_trust_net_lot": shares_to_lots(trust_net),
        f"{days}d_inst_net_lot": shares_to_lots(inst_net),
        "foreign_net_volume_pct": round(foreign_net / volume * 100, 2) if volume else None,
        "trust_net_volume_pct": round(trust_net / volume * 100, 2) if volume else None,
        "foreign_buy_streak": row["foreign_buy_streak"],
        "foreign_sell_streak": row["foreign_sell_streak"],
        "trust_buy_streak": row["trust_buy_streak"],
        "trust_sell_streak": row["trust_sell_streak"],
        "latest_foreign_net_lot": shares_to_lots(row["latest_foreign_net"]),
        "latest_trust_net_lot": shares_to_lots(row["latest_trust_net"]),
        "top_buy_branch_name": row.get("top_buy_branch_name"),
        "top_buy_branch_net_lot": row.get("top_buy_branch_net_lot"),
        "top_buy_branch_avg_price": row.get("top_buy_branch_avg_price"),
        "top_buy_branch_est_cost": row.get("top_buy_branch_est_cost"),
        "branch_buy_streak": row.get("branch_buy_streak"),
        "branch_streak_broker": row.get("branch_streak_broker"),
        "top_buy_is_day_trader": row.get("top_buy_is_day_trader", 0),
        "top_sell_branch_name": row.get("top_sell_branch_name"),
        "top_sell_branch_net_lot": row.get("top_sell_branch_net_lot"),
        "top_sell_branch_avg_price": row.get("top_sell_branch_avg_price"),
        "branch_score": row.get("branch_score", 0.0),
        "chip_score": row["chip_score"],
        "margin_balance_change_lot": row.get("margin_balance_change_lot"),
        "short_balance_change_lot": row.get("short_balance_change_lot"),
        "margin_score": row.get("margin_score", 0.0),
        "base_score": row.get("base_score", row["chip_score"]),
        "branch_status": row.get("branch_status", ""),
        "confluence_score": row.get("confluence_score", 0.0),
        "complete_chip_score": row.get("complete_chip_score", row.get("base_score", row["chip_score"])),
        "confluence_inst_score": row.get("confluence_inst_score", 0.0),
        "confluence_branch_score": row.get("confluence_branch_score", 0.0),
        "confluence_price_score": row.get("confluence_price_score", 0.0),
        "confluence_streak_score": row.get("confluence_streak_score", 0.0),
        "confluence_direction_score": row.get("confluence_direction_score", 0.0),
        "revenue_momentum_score": row.get("revenue_momentum_score", 0.0),
        "selection_score": row.get("selection_score", row.get("confluence_score", 0.0)),
        "revenue_month": row.get("revenue_month"),
        "revenue_mom_pct": row.get("revenue_mom_pct"),
        "revenue_yoy_pct": row.get("revenue_yoy_pct"),
        "revenue_cumulative_yoy_pct": row.get("revenue_cumulative_yoy_pct"),
        "inst_net_volume_pct": row.get("inst_net_volume_pct"),
        "top_buy_branch_volume_pct": row.get("top_buy_branch_volume_pct"),
        "close_vs_top_buy_avg_pct": row.get("close_vs_top_buy_avg_pct"),
        "selection_reason": row.get("selection_reason", ""),
        "base_reason": row.get("base_reason", ""),
        "total_score": row.get("total_score", row["chip_score"]),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_rankings(rows: list[dict[str, Any]], days: int) -> dict[str, list[dict[str, Any]]]:
    """各排行的選股與排序邏輯。與 CSV 匯出分開，好讓排序規則能單獨測試
    ——匯出列需要上百個欄位，混在一起就只能拿真實掃描結果來測。"""
    return {
        "total_score": sorted(rows, key=lambda row: row.get("total_score", row["chip_score"]), reverse=True),
        "multifactor_score": sorted(
            rows,
            key=lambda row: row.get("multifactor_score", row.get("total_score", row["chip_score"])),
            reverse=True,
        ),
        "momentum_inst_buy": sorted(
            [
                row
                for row in rows
                if (row.get("period_return_pct") or 0) > 0
                and row[f"{days}d_inst_net"] > 0
                and (row.get("momentum_score") or 0) > 0
                and (row.get("avg_turnover_100m") or 0) >= 0.3
            ],
            key=lambda row: (row.get("momentum_score") or 0, row[f"{days}d_inst_net"]),
            reverse=True,
        ),
        "mispriced_value": sorted(
            [
                row
                for row in rows
                if row.get("mispriced_eligible")
                and (row.get("mispriced_score") or 0) > 0
                and (row.get("pe_percentile") is not None or (row.get("close_vs_52w_high_pct") or 0) <= -15)
            ],
            key=lambda row: (row.get("mispriced_score") or 0, -(row.get("pe_percentile") or 100)),
            reverse=True,
        ),
        "value_dividend": sorted(
            [
                row
                for row in rows
                if (row.get("dividend_yield") or 0) >= 3
                and (row.get("pe_ratio") or 0) > 0
                and (row.get("pe_ratio") or 999) <= 20
                and row[f"{days}d_inst_net"] >= 0
                and (row.get("avg_turnover_100m") or 0) >= 0.3
            ],
            key=lambda row: (row.get("valuation_score") or 0, row.get("dividend_yield") or 0),
            reverse=True,
        ),
        "big_holder_increase": sorted(
            [
                row
                for row in rows
                if (row.get("big_holder_change_1w") or 0) > 0
                and row[f"{days}d_inst_net"] > 0
                and (row.get("avg_turnover_100m") or 0) >= 0.3
            ],
            key=lambda row: (row.get("big_holder_change_1w") or 0, row[f"{days}d_inst_net"]),
            reverse=True,
        ),
        "high_52w_inst_buy": sorted(
            [
                row
                for row in rows
                if (row.get("close_vs_52w_high_pct") is not None)
                and row["close_vs_52w_high_pct"] >= -3
                and row[f"{days}d_inst_net"] > 0
                and (row.get("avg_turnover_100m") or 0) >= 0.3
            ],
            key=lambda row: (row.get("long_momentum_score") or 0, row[f"{days}d_inst_net"]),
            reverse=True,
        ),
        "chip_score": sorted(rows, key=lambda row: row["chip_score"], reverse=True),
        "selection_score": sorted(
            rows,
            key=lambda row: row.get("selection_score", 0.0),
            reverse=True,
        ),
        "foreign_buy": sorted(rows, key=lambda row: row[f"{days}d_foreign_net"], reverse=True),
        # 當日外資買超排行：等同各家券商 App 的「今日外資買賣超排行」。
        # 只看單日，是最快的風向球，但單日大買常常是隔日就跑的過路資金，
        # 所以另外配一個「連續買超」榜看誰是真的天天在買。
        "foreign_day_buy": sorted(
            [row for row in rows if (row.get("latest_foreign_net") or 0) > 0],
            key=lambda row: row.get("latest_foreign_net") or 0,
            reverse=True,
        ),
        "foreign_day_sell": sorted(
            [row for row in rows if (row.get("latest_foreign_net") or 0) < 0],
            key=lambda row: row.get("latest_foreign_net") or 0,
        ),
        # 外資連續買超：連幾天沒有間斷地買。這才是「每日都在大買」。
        # 排序以連買天數為主、期間累計張數為輔。
        #
        # 占量門檻不可省：實測 8027 鈦昇連買 12 天，20 日累計卻只有 196 張
        # （占期間成交量 0.30%），純看天數會把它排在中華電（42,866 張、9.48%）
        # 之前——「天天買一點」不是買盤，是雜訊。日均額門檻擋不掉這種股
        # （鈦昇日均 7.6 億，很流動），要擋的是「買超相對成交量太小」。
        # 0.5% 濾掉 197 → 186 檔，剛好切掉這類個案而不誤傷真正的連續買盤。
        # 注意占量要就地由 {days}d_volume 算：foreign_net_volume_pct 只存在於
        # CSV 匯出列，排行階段的 row 沒有這個 key，寫成 row.get(...) 會恆為
        # None 而把整個榜濾成空的。
        "foreign_streak_buy": sorted(
            [
                row
                for row in rows
                if (row.get("foreign_buy_streak") or 0) >= 3
                and row[f"{days}d_foreign_net"] > 0
                and (row.get("avg_turnover_100m") or 0) >= 0.3
                and row[f"{days}d_foreign_net"] >= (row[f"{days}d_volume"] or 0) * 0.005
            ],
            key=lambda row: (row.get("foreign_buy_streak") or 0, row[f"{days}d_foreign_net"]),
            reverse=True,
        ),
        "trust_buy": sorted(rows, key=lambda row: row[f"{days}d_trust_net"], reverse=True),
        "inst_buy": sorted(rows, key=lambda row: row[f"{days}d_inst_net"], reverse=True),
        "foreign_trust_same_buy": sorted(
            [
                row
                for row in rows
                if row[f"{days}d_foreign_net"] > 0 and row[f"{days}d_trust_net"] > 0
            ],
            key=lambda row: row[f"{days}d_inst_net"],
            reverse=True,
        ),
        "near_avg_with_inst_buy": sorted(
            [
                row
                for row in rows
                if row[f"{days}d_inst_net"] > 0
                and row["close_vs_avg_pct"] is not None
                and -3 <= row["close_vs_avg_pct"] <= 3
            ],
            key=lambda row: row.get("total_score", row["chip_score"]),
            reverse=True,
        ),
        "foreign_5d_revenue_growth": sorted(
            [
                row
                for row in rows
                if row["days"] == 5
                and row[f"{days}d_foreign_net"] > 0
                and (row.get("revenue_yoy_pct") or 0) > 0
                and (row.get("revenue_mom_pct") or 0) > 0
            ],
            key=lambda row: (row.get("total_score", row["chip_score"]), row[f"{days}d_foreign_net"]),
            reverse=True,
        ),
        "inst_buy_volume": sorted(
            [
                row
                for row in rows
                if row[f"{days}d_inst_net"] > 0
                and (row.get(f"{days}d_volume") or 0) > 0
                and (row[f"{days}d_foreign_net"] / row[f"{days}d_volume"] * 100) > 0.5
            ],
            key=lambda row: (row[f"{days}d_foreign_net"] / (row.get(f"{days}d_volume") or 1) * 100, row[f"{days}d_inst_net"]),
            reverse=True,
        ),
        "volume_expansion": sorted(
            [
                row
                for row in rows
                if (row.get("volume_score") or 0) > 0
                and (row.get("volume_ratio_1d") is not None or row.get("volume_ratio_5d") is not None)
            ],
            key=lambda row: (
                row.get("volume_score") or 0,
                row.get("volume_ratio_1d") or 0,
                row.get(f"{days}d_volume") or 0,
            ),
            reverse=True,
        ),
        "revenue_volume_breakout": sorted(
            [
                row
                for row in rows
                if (row.get("revenue_yoy_pct") or 0) > 0
                and (row.get("revenue_mom_pct") or 0) > 0
                and (row.get("volume_score") or 0) > 0
            ],
            key=lambda row: ((row.get("revenue_momentum_score") or 0), row.get("volume_score") or 0),
            reverse=True,
        ),
        "margin_down_foreign_buy": sorted(
            [
                row
                for row in rows
                if row[f"{days}d_foreign_net"] > 0
                and (row.get("margin_balance_change_lot") or 0) < 0
            ],
            key=lambda row: (row[f"{days}d_foreign_net"], -(row.get("margin_balance_change_lot") or 0)),
            reverse=True,
        ),
    }


def export_rankings(output_dir: Path, rows: list[dict[str, Any]], days: int, limit: int) -> dict[str, Path]:
    rankings = build_rankings(rows, days)
    paths: dict[str, Path] = {}
    for name, ranking_rows in rankings.items():
        path = output_dir / f"ranking_{name}_{days}d.csv"
        write_csv(path, [to_export_row(row, days) for row in ranking_rows[:limit]])
        paths[name] = path
    return paths, rankings


SNAPSHOT_TOP_N = 50


def snapshot_rankings(
    db_path: Path,
    rankings: dict[str, list[dict[str, Any]]],
    days: int,
    snapshot_date: str,
    top_n: int = SNAPSHOT_TOP_N,
) -> int:
    """把當日各排行前 N 名寫入 ranking_snapshots，供回測計算後續報酬。

    以資料日（非牆鐘）為鍵，同日重跑會冪等覆蓋。"""
    now = dt.datetime.now().isoformat(timespec="seconds")
    records = []
    for name, ranking_rows in rankings.items():
        for rank_no, row in enumerate(ranking_rows[:top_n], start=1):
            records.append(
                {
                    "snapshot_date": snapshot_date,
                    "days": days,
                    "ranking_name": name,
                    "rank_no": rank_no,
                    "stock_id": row["stock_id"],
                    "score": row.get("multifactor_score"),
                    "total_score": row.get("total_score"),
                    "close": row.get("close"),
                    "updated_at": now,
                }
            )
    if not records:
        return 0
    with connect_db(db_path) as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO ranking_snapshots (
                snapshot_date, days, ranking_name, rank_no, stock_id,
                score, total_score, close, updated_at
            )
            VALUES (:snapshot_date, :days, :ranking_name, :rank_no, :stock_id,
                    :score, :total_score, :close, :updated_at)
            """,
            records,
        )
        conn.commit()
    return len(records)


def export_watchlist_daily(
    output_dir: Path,
    raw_rows: list[dict[str, Any]],
    watchlist: list[str],
    days: int,
) -> Path:
    watchset = set(watchlist)
    rows = []
    for row in raw_rows:
        if row["stock_id"] not in watchset:
            continue
        rows.append(
            {
                "date": row["date"],
                "stock_id": row["stock_id"],
                "name": row["name"],
                "market": row["market"],
                "observed_days": "",
                "close": row["close"],
                "avg_price": round(row["avg_price"], 4) if row["avg_price"] is not None else None,
                "volume_lot": shares_to_lots(row["volume"]),
                "foreign_net_lot": shares_to_lots(row["foreign_net"]),
                "trust_net_lot": shares_to_lots(row["trust_net"]),
                "dealer_net_lot": shares_to_lots(row["dealer_net"]),
            }
        )
    path = output_dir / f"watchlist_daily_{days}d.csv"
    write_csv(path, rows)
    return path


def write_markdown_report(
    path: Path,
    rows: list[dict[str, Any]],
    days: int,
    dates: list[str],
    watchlist_rows: list[dict[str, Any]],
) -> None:
    top_score = sorted(rows, key=lambda row: row.get("total_score", row["chip_score"]), reverse=True)[:10]
    top_inst = sorted(rows, key=lambda row: row[f"{days}d_inst_net"], reverse=True)[:10]
    lines = [
        "# 台股籌碼掃描報告",
        "",
        f"- 產生時間：{dt.datetime.now().isoformat(timespec='seconds')}",
        f"- 交易日：{', '.join(dates)}",
        "",
        "## 自選股",
        "",
        f"| 股票 | 市場 | 收盤價 | {days}日均價 | 外資(張) | 投信(張) | 買超分點 | 分點均價 | 基礎分 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
    ]
    for row in watchlist_rows:
        exported = to_export_row(row, days)
        lines.append(
            f"| {row['stock_id']} {row['name']} | {row['market']} | {exported['close']} | "
            f"{exported[f'{days}d_avg_price']} | {exported[f'{days}d_foreign_net_lot']} | "
            f"{exported[f'{days}d_trust_net_lot']} | {exported.get('top_buy_branch_name') or ''} | "
            f"{exported.get('top_buy_branch_avg_price') or ''} | {exported['total_score']} |"
        )

    lines.extend(
        [
            "",
            "## 基礎分前 10",
            "",
            f"| 股票 | 市場 | 收盤價 | 外資(張) | 投信(張) | 連買 | 基礎分 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in top_score:
        exported = to_export_row(row, days)
        streak = f"外{row['foreign_buy_streak']} / 投{row['trust_buy_streak']}"
        lines.append(
            f"| {row['stock_id']} {row['name']} | {row['market']} | {exported['close']} | "
            f"{exported[f'{days}d_foreign_net_lot']} | {exported[f'{days}d_trust_net_lot']} | "
            f"{streak} | {exported['total_score']} |"
        )

    lines.extend(
        [
            "",
            "## 外資 + 投信買超前 10",
            "",
            f"| 股票 | 市場 | 收盤價 | 外資(張) | 投信(張) | 合計(張) |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in top_inst:
        exported = to_export_row(row, days)
        lines.append(
            f"| {row['stock_id']} {row['name']} | {row['market']} | {exported['close']} | "
            f"{exported[f'{days}d_foreign_net_lot']} | {exported[f'{days}d_trust_net_lot']} | "
            f"{exported[f'{days}d_inst_net_lot']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_scan(
    db_path: Path,
    output_dir: Path,
    days: int,
    limit: int,
    watchlist: list[str],
) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        dates = recent_dates(conn, days)
        raw_rows = load_window(conn, dates)
        branch_leaders = load_branch_leaders(conn, dates[-1], days) if dates else {}
        revenue_momentum = load_revenue_momentum(conn)
        history_stats = load_history_stats(conn, dates[-1]) if dates else {}
        profiles = load_profiles(conn)
        dispersion = load_shareholding_dispersion(conn)
        fundamentals = load_fundamentals(conn)
        branch_streaks = load_branch_streaks(conn, branch_leaders, dates)
        day_trader_hits = sum(
            1 for info in branch_leaders.values()
            if info.get("top_buy_branch_name") in load_day_trader_branches()
        )
        if branch_leaders:
            print(
                f"分點深化：連買/成本樣本 {len(branch_streaks)} 檔，隔日沖分點命中 {day_trader_hits} 檔",
                flush=True,
            )
    if len(dates) < days:
        raise RuntimeError(f"資料庫只有 {len(dates)} 個交易日，少於要求的 {days} 日。")
    rows = aggregate(raw_rows, days, dates[-1], branch_leaders, revenue_momentum, history_stats, profiles, dispersion, fundamentals, branch_streaks)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_path = output_dir / f"scan_all_{days}d.csv"
    write_csv(
        all_path,
        [
            to_export_row(row, days)
            for row in sorted(rows, key=lambda row: row.get("total_score", row["chip_score"]), reverse=True)
        ],
    )
    ranking_paths, rankings_by_name = export_rankings(output_dir, rows, days, limit)
    snapshot_count = snapshot_rankings(db_path, rankings_by_name, days, dates[-1])
    watchset = set(watchlist)
    watchlist_rows = [row for row in rows if row["stock_id"] in watchset]
    watchlist_rows.sort(key=lambda row: watchlist.index(row["stock_id"]) if row["stock_id"] in watchlist else 9999)
    watchlist_summary_path = output_dir / f"watchlist_summary_{days}d.csv"
    write_csv(watchlist_summary_path, [to_export_row(row, days) for row in watchlist_rows])
    watchlist_daily_path = export_watchlist_daily(output_dir, raw_rows, watchlist, days)
    report_path = output_dir / f"scan_report_{days}d.md"
    write_markdown_report(report_path, rows, days, dates, watchlist_rows)
    return {
        "dates": dates,
        "stock_count": len(rows),
        "snapshot_rows": snapshot_count,
        "all": all_path,
        "rankings": ranking_paths,
        "watchlist_summary": watchlist_summary_path,
        "watchlist_daily": watchlist_daily_path,
        "report": report_path,
    }


def parse_watchlist(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan stored TWSE official chip data.")
    parser.add_argument("--days", type=int, default=5, help="Recent trading days to scan.")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite database path.")
    parser.add_argument("--output-dir", default="reports", help="Report output directory.")
    parser.add_argument("--limit", type=int, default=50, help="Rows per ranking report.")
    parser.add_argument(
        "--watchlist",
        default=",".join(DEFAULT_WATCHLIST),
        help="Comma-separated stock ids for watchlist reports.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_scan(
        db_path=Path(args.db),
        output_dir=Path(args.output_dir),
        days=args.days,
        limit=args.limit,
        watchlist=parse_watchlist(args.watchlist),
    )
    print(f"scanned {result['stock_count']} stocks")
    print(f"dates: {', '.join(result['dates'])}")
    print(f"report: {result['report']}")
    print(f"watchlist summary: {result['watchlist_summary']}")


if __name__ == "__main__":
    main()

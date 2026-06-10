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
        (as_of_date, days),
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
    buy_avg = row.get("top_buy_branch_avg_price")
    sell_net = row.get("top_sell_branch_net_lot")
    sell_avg = row.get("top_sell_branch_avg_price")
    if close is None or buy_net is None or buy_avg is None:
        return 0.0

    score = 0.0
    volume_lot = (row[f"{row['days']}d_volume"] or 0) / 1000
    if volume_lot > 0:
        score += bounded(buy_net / volume_lot * 100, 0, 20) * 1.2
        if sell_net is not None:
            score -= bounded(abs(sell_net) / volume_lot * 100, 0, 20) * 0.5

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
    rsi = row.get("rsi14")
    if rsi is not None and rsi >= 75:
        parts.append("RSI過熱")
    volatility = row.get("volatility_pct")
    if volatility is not None and volatility > 65:
        parts.append("波動偏高")
    rev = revenue_reason(row)
    if rev:
        parts.append(rev)
    return "；".join(parts)


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
    buy_avg = row.get("top_buy_branch_avg_price")

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
) -> list[dict[str, Any]]:
    branch_leaders = branch_leaders or {}
    revenue_momentum = revenue_momentum or {}
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
        item["base_score"] = round(item["chip_score"] + item["margin_score"] + item["revenue_momentum_score"], 2)
        item["multifactor_score"] = round(
            item["base_score"]
            + item["momentum_score"]
            + item["valuation_score"]
            + item["volume_score"] * 0.5,
            2,
        )
        item["branch_status"] = branch_status(item)
        item["base_reason"] = base_reason(item)
        item.update(confluence_breakdown(item))
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
        "valuation_score": row.get("valuation_score", 0.0),
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


def export_rankings(output_dir: Path, rows: list[dict[str, Any]], days: int, limit: int) -> dict[str, Path]:
    rankings = {
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
        "chip_score": sorted(rows, key=lambda row: row["chip_score"], reverse=True),
        "selection_score": sorted(
            rows,
            key=lambda row: row.get("selection_score", 0.0),
            reverse=True,
        ),
        "foreign_buy": sorted(rows, key=lambda row: row[f"{days}d_foreign_net"], reverse=True),
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

    paths: dict[str, Path] = {}
    for name, ranking_rows in rankings.items():
        path = output_dir / f"ranking_{name}_{days}d.csv"
        write_csv(path, [to_export_row(row, days) for row in ranking_rows[:limit]])
        paths[name] = path
    return paths


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
        branch_leaders: dict[str, dict[str, Any]] = {}
        revenue_momentum = load_revenue_momentum(conn)
    if len(dates) < days:
        raise RuntimeError(f"資料庫只有 {len(dates)} 個交易日，少於要求的 {days} 日。")
    rows = aggregate(raw_rows, days, dates[-1], branch_leaders, revenue_momentum)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_path = output_dir / f"scan_all_{days}d.csv"
    write_csv(
        all_path,
        [
            to_export_row(row, days)
            for row in sorted(rows, key=lambda row: row.get("total_score", row["chip_score"]), reverse=True)
        ],
    )
    ranking_paths = export_rankings(output_dir, rows, days, limit)
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

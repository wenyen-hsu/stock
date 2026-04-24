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
            p.volume,
            p.turnover,
            p.avg_price,
            COALESCE(i.foreign_net, 0) AS foreign_net,
            COALESCE(i.trust_net, 0) AS trust_net,
            COALESCE(i.dealer_net, 0) AS dealer_net
        FROM daily_prices p
        JOIN stocks s
            ON s.stock_id = p.stock_id
        LEFT JOIN institutional_trades i
            ON i.date = p.date
           AND i.stock_id = p.stock_id
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
        "volume",
        "turnover",
        "avg_price",
        "foreign_net",
        "trust_net",
        "dealer_net",
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


def aggregate(
    rows: list[dict[str, Any]],
    days: int,
    latest_date: str,
    branch_leaders: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    branch_leaders = branch_leaders or {}
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
        turnover = sum(row["turnover"] or 0 for row in stock_rows)
        avg_price = turnover / volume if volume else None
        close = latest["close"]
        close_vs_avg_pct = None
        if close is not None and avg_price:
            close_vs_avg_pct = (close - avg_price) / avg_price * 100

        foreign_net = sum(row["foreign_net"] or 0 for row in stock_rows)
        trust_net = sum(row["trust_net"] or 0 for row in stock_rows)
        dealer_net = sum(row["dealer_net"] or 0 for row in stock_rows)
        item = {
            "days": days,
            "observed_days": len(stock_rows),
            "latest_date": latest["date"],
            "stock_id": stock_id,
            "name": latest["name"],
            "market": latest["market"],
            "close": close,
            f"{days}d_avg_price": avg_price,
            "close_vs_avg_pct": close_vs_avg_pct,
            f"{days}d_volume": volume,
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
        }
        item.update(branch_leaders.get(stock_id, {}))
        item["branch_score"] = branch_score_row(item)
        item["chip_score"] = score_row(item)
        item["total_score"] = round(item["chip_score"] + item["branch_score"], 2)
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
        f"{days}d_avg_price": round(avg_price, 4) if avg_price is not None else None,
        "close_vs_avg_pct": round(close_vs_avg_pct, 2) if close_vs_avg_pct is not None else None,
        f"{days}d_volume_lot": shares_to_lots(volume),
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
        "chip_score": sorted(rows, key=lambda row: row["chip_score"], reverse=True),
        "branch_score": sorted(
            [row for row in rows if row.get("top_buy_branch_name")],
            key=lambda row: row.get("branch_score", 0.0),
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
        f"| 股票 | 市場 | 收盤價 | {days}日均價 | 外資(張) | 投信(張) | 買超分點 | 分點均價 | 總分 |",
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
            "## 總分前 10",
            "",
            f"| 股票 | 市場 | 收盤價 | 外資(張) | 投信(張) | 連買 | 總分 |",
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
    if len(dates) < days:
        raise RuntimeError(f"資料庫只有 {len(dates)} 個交易日，少於要求的 {days} 日。")
    rows = aggregate(raw_rows, days, dates[-1], branch_leaders)
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

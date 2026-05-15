from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from stock_chip.official import connect_db
from stock_chip.scan import recent_dates


SCORE_INPUTS = {
    "price": "每日行情與成交量",
    "institutional": "法人買賣超",
    "margin": "融資融券",
    "revenue": "月營收",
}


def market_stock_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT stock_id
        FROM stocks
        WHERE market IN ('TWSE', 'TPEX')
        ORDER BY stock_id
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def distinct_ids(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> set[str]:
    rows = conn.execute(sql, params).fetchall()
    return {str(row[0]) for row in rows}


def score_input_coverage(db_path: Path, days: int) -> dict[str, object]:
    with connect_db(db_path) as conn:
        stock_ids = market_stock_ids(conn)
        stock_set = set(stock_ids)
        dates = recent_dates(conn, days)
        if not dates:
            return {
                "days": days,
                "dates": [],
                "total": len(stock_ids),
                "coverage": {},
                "ok": False,
                "message": "沒有交易日資料",
            }
        placeholders = ",".join("?" for _ in dates)
        price_ids = distinct_ids(
            conn,
            f"""
            SELECT DISTINCT stock_id
            FROM daily_prices
            WHERE date IN ({placeholders})
            """,
            tuple(dates),
        )
        institutional_ids = distinct_ids(
            conn,
            f"""
            SELECT DISTINCT stock_id
            FROM institutional_trades
            WHERE date IN ({placeholders})
            """,
            tuple(dates),
        )
        margin_ids = distinct_ids(
            conn,
            f"""
            SELECT DISTINCT stock_id
            FROM margin_trades
            WHERE date IN ({placeholders})
            """,
            tuple(dates),
        )
        revenue_ids = distinct_ids(conn, "SELECT DISTINCT stock_id FROM monthly_revenues")

    covered = {
        "price": price_ids & stock_set,
        "institutional": institutional_ids & stock_set,
        "margin": margin_ids & stock_set,
        "revenue": revenue_ids & stock_set,
    }
    coverage: dict[str, dict[str, object]] = {}
    for key, ids in covered.items():
        missing = [stock_id for stock_id in stock_ids if stock_id not in ids]
        coverage[key] = {
            "label": SCORE_INPUTS[key],
            "covered": len(ids),
            "missing": len(missing),
            "missing_ids": missing[:30],
        }
    ok = all(item["missing"] == 0 for item in coverage.values())
    message = "分數資料完整" if ok else "分數資料仍有缺漏"
    return {
        "days": days,
        "dates": dates,
        "total": len(stock_ids),
        "coverage": coverage,
        "ok": ok,
        "message": message,
    }


def format_report(result: dict[str, object]) -> str:
    lines = [
        f"檢查區間：最近 {result['days']} 個交易日",
        f"交易日：{', '.join(result.get('dates') or [])}",
        f"上市上櫃股票數：{result['total']}",
        str(result["message"]),
    ]
    coverage = result.get("coverage") or {}
    for key in ("price", "institutional", "margin", "revenue"):
        item = coverage.get(key) or {}
        missing_ids = item.get("missing_ids") or []
        suffix = f"，例：{', '.join(missing_ids)}" if missing_ids else ""
        lines.append(
            f"- {item.get('label', key)}：{item.get('covered', 0)} / {result['total']}，缺 {item.get('missing', 0)}{suffix}"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate score input coverage.")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite database path.")
    parser.add_argument("--days", type=int, default=20, help="Recent trading days used by score scan.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when any score input is missing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = score_input_coverage(Path(args.db), args.days)
    print(format_report(result))
    if args.strict and not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

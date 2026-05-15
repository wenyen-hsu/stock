from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from stock_chip.official import connect_db
from stock_chip.scan import DEFAULT_WATCHLIST, parse_watchlist


FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


@dataclass
class RevenueRow:
    revenue_month: str
    stock_id: str
    name: str
    market: str
    revenue: int | None
    prev_month_revenue: int | None
    last_year_revenue: int | None
    mom_pct: float | None
    yoy_pct: float | None
    cumulative_revenue: int | None
    last_year_cumulative_revenue: int | None
    cumulative_yoy_pct: float | None
    source: str


def month_start(value: dt.date) -> dt.date:
    return dt.date(value.year, value.month, 1)


def add_months(value: dt.date, months: int) -> dt.date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, 1)


def pct_change(current: int | None, base: int | None) -> float | None:
    if current is None or base in (None, 0):
        return None
    return round((current - base) / base * 100, 2)


def stock_lookup(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, dict[str, str]]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"""
        SELECT stock_id, name, market
        FROM stocks
        WHERE stock_id IN ({placeholders})
        """,
        stock_ids,
    ).fetchall()
    return {row[0]: {"name": row[1], "market": row[2]} for row in rows}


def fetch_finmind_revenue(stock_id: str, start_date: str, timeout_seconds: float = 20) -> list[dict[str, Any]]:
    resp = requests.get(
        FINMIND_URL,
        params={
            "dataset": "TaiwanStockMonthRevenue",
            "data_id": stock_id,
            "start_date": start_date,
        },
        timeout=timeout_seconds,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != 200:
        raise RuntimeError(payload.get("msg") or f"FinMind revenue failed for {stock_id}")
    return payload.get("data") or []


def normalize_finmind_rows(
    raw_rows: list[dict[str, Any]],
    stock_id: str,
    name: str,
    market: str,
    months: int,
) -> list[RevenueRow]:
    values: dict[str, int] = {}
    for raw in raw_rows:
        year = int(raw["revenue_year"])
        month = int(raw["revenue_month"])
        key = f"{year:04d}-{month:02d}"
        values[key] = int(raw["revenue"])

    ordered = sorted(values)
    if not ordered:
        return []
    selected = ordered[-months:]
    output: list[RevenueRow] = []
    cumulative_by_year: dict[int, int] = {}
    for key in ordered:
        year = int(key[:4])
        cumulative_by_year[year] = cumulative_by_year.get(year, 0) + values[key]
        if key not in selected:
            continue
        month_date = dt.date(int(key[:4]), int(key[5:7]), 1)
        prev_key = add_months(month_date, -1).strftime("%Y-%m")
        last_year_key = add_months(month_date, -12).strftime("%Y-%m")
        last_year = month_date.year - 1
        current_cum = sum(
            amount
            for month_key, amount in values.items()
            if int(month_key[:4]) == month_date.year and int(month_key[5:7]) <= month_date.month
        )
        last_year_cum = sum(
            amount
            for month_key, amount in values.items()
            if int(month_key[:4]) == last_year and int(month_key[5:7]) <= month_date.month
        )
        output.append(
            RevenueRow(
                revenue_month=key,
                stock_id=stock_id,
                name=name,
                market=market,
                revenue=values.get(key),
                prev_month_revenue=values.get(prev_key),
                last_year_revenue=values.get(last_year_key),
                mom_pct=pct_change(values.get(key), values.get(prev_key)),
                yoy_pct=pct_change(values.get(key), values.get(last_year_key)),
                cumulative_revenue=current_cum or None,
                last_year_cumulative_revenue=last_year_cum or None,
                cumulative_yoy_pct=pct_change(current_cum or None, last_year_cum or None),
                source="finmind",
            )
        )
    return output


def upsert_revenues(conn: sqlite3.Connection, rows: list[RevenueRow]) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO monthly_revenues (
            revenue_month, stock_id, name, market, revenue, prev_month_revenue,
            last_year_revenue, mom_pct, yoy_pct, cumulative_revenue,
            last_year_cumulative_revenue, cumulative_yoy_pct, source, updated_at
        )
        VALUES (
            :revenue_month, :stock_id, :name, :market, :revenue, :prev_month_revenue,
            :last_year_revenue, :mom_pct, :yoy_pct, :cumulative_revenue,
            :last_year_cumulative_revenue, :cumulative_yoy_pct, :source, :updated_at
        )
        ON CONFLICT(revenue_month, stock_id, source) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            revenue = excluded.revenue,
            prev_month_revenue = excluded.prev_month_revenue,
            last_year_revenue = excluded.last_year_revenue,
            mom_pct = excluded.mom_pct,
            yoy_pct = excluded.yoy_pct,
            cumulative_revenue = excluded.cumulative_revenue,
            last_year_cumulative_revenue = excluded.last_year_cumulative_revenue,
            cumulative_yoy_pct = excluded.cumulative_yoy_pct,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    conn.commit()


def run_revenue(
    db_path: Path,
    stock_ids: list[str],
    months: int,
    sleep_seconds: float,
    retries: int = 2,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    start = add_months(month_start(dt.date.today()), -(months + 14)).isoformat()
    row_count = 0
    failures: list[dict[str, str]] = []
    with connect_db(db_path) as conn:
        lookup = stock_lookup(conn, stock_ids)
        for idx, stock_id in enumerate(stock_ids):
            if idx:
                time.sleep(sleep_seconds)
            meta = lookup.get(stock_id, {"name": stock_id, "market": ""})
            last_error: Exception | None = None
            try:
                raw: list[dict[str, Any]] = []
                for attempt in range(retries + 1):
                    try:
                        raw = fetch_finmind_revenue(stock_id, start, timeout_seconds=timeout_seconds)
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt >= retries:
                            raise
                        delay = min(2 + attempt * 3, 8)
                        print(
                            f"[{idx + 1}/{len(stock_ids)}] {stock_id} retry {attempt + 1}/{retries}: {exc}",
                            flush=True,
                        )
                        time.sleep(delay)
                rows = normalize_finmind_rows(
                    raw,
                    stock_id=stock_id,
                    name=meta["name"],
                    market=meta["market"],
                    months=months,
                )
                upsert_revenues(conn, rows)
                row_count += len(rows)
                print(f"[{idx + 1}/{len(stock_ids)}] {stock_id} revenue rows: {len(rows)}", flush=True)
            except Exception as exc:
                error = str(last_error or exc)
                failures.append({"stock_id": stock_id, "error": error})
                print(f"[{idx + 1}/{len(stock_ids)}] {stock_id} failed: {error}", flush=True)
    return {
        "requested_stock_count": len(stock_ids),
        "row_count": row_count,
        "failures": failures,
        "start_date": start,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch monthly revenues for Taiwan stocks.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--months", type=int, default=24)
    parser.add_argument("--watchlist", default=",".join(DEFAULT_WATCHLIST))
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_revenue(
        db_path=Path(args.db),
        stock_ids=parse_watchlist(args.watchlist),
        months=args.months,
        sleep_seconds=args.sleep,
        retries=args.retries,
        timeout_seconds=args.timeout,
    )
    print(f"requested stocks: {result['requested_stock_count']}")
    print(f"revenue rows: {result['row_count']}")
    if result["failures"]:
        print(f"failures: {len(result['failures'])}")
        for failure in result["failures"][:10]:
            print(f"- {failure['stock_id']}: {failure['error']}")


if __name__ == "__main__":
    main()

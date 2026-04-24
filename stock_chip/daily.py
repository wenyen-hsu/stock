from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from stock_chip.branch import run_branch
from stock_chip.official import connect_db, print_update_result, update_recent
from stock_chip.scan import DEFAULT_WATCHLIST, parse_watchlist, run_scan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update TWSE data and generate chip scan reports.")
    parser.add_argument("--days", type=int, default=20, help="Recent trading days to update and scan.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite database path.")
    parser.add_argument("--output-dir", default="reports", help="Report output directory.")
    parser.add_argument("--limit", type=int, default=50, help="Rows per ranking report.")
    parser.add_argument(
        "--watchlist",
        default=",".join(DEFAULT_WATCHLIST),
        help="Comma-separated stock ids for watchlist reports.",
    )
    parser.add_argument(
        "--include-etf",
        action="store_true",
        help="Include non-stock securities such as ETFs.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-fetch dates even when complete data already exists locally.",
    )
    parser.add_argument(
        "--exclude-esb",
        action="store_true",
        help="Skip emerging-stock latest quote ingestion.",
    )
    parser.add_argument(
        "--with-branch",
        action="store_true",
        help="Also fetch HiStock top broker branch flows for the watchlist.",
    )
    parser.add_argument("--branch-top", type=int, default=10, help="Top N buy/sell branches per stock.")
    parser.add_argument(
        "--branch-sleep",
        type=float,
        default=1.0,
        help="Delay between third-party branch requests.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    output_dir = Path(args.output_dir)
    end_date = dt.date.fromisoformat(args.end_date)

    with connect_db(db_path) as conn:
        updated = update_recent(
            conn,
            days=args.days,
            end_date=end_date,
            stock_only=not args.include_etf,
            force_refresh=args.force_refresh,
            include_esb=not args.exclude_esb,
        )
    print_update_result(updated)

    watchlist = parse_watchlist(args.watchlist)
    result = run_scan(
        db_path=db_path,
        output_dir=output_dir,
        days=args.days,
        limit=args.limit,
        watchlist=watchlist,
    )
    print(f"scanned {result['stock_count']} stocks")
    print(f"dates: {', '.join(result['dates'])}")
    print(f"report: {result['report']}")
    print(f"watchlist summary: {result['watchlist_summary']}")

    if args.with_branch:
        branch_result = run_branch(
            db_path=db_path,
            output_dir=output_dir,
            days=args.days,
            top_n=args.branch_top,
            watchlist=watchlist,
            sleep_seconds=args.branch_sleep,
        )
        print(f"branch rows: {branch_result['row_count']}")
        print(f"branch report: {branch_result['report']}")


if __name__ == "__main__":
    main()

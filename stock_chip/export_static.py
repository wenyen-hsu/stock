from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from stock_chip.branch import branch_coverage
from stock_chip.gui import (
    DB_PATH,
    INDEX_HTML,
    REPORTS_DIR,
    as_float,
    connect_db,
    current_watchlist_ids,
    db_meta,
    market_payload,
    market_sentiment_payload,
    normalize_scan_row,
    now_text,
    ranking_path,
    read_csv,
    stock_detail,
)
from stock_chip.scan import recent_dates
from stock_chip.mops import load_mops_events
from stock_chip.us_news import load_cached_us_news


RANKINGS = [
    "total_score",
    "multifactor_score",
    "momentum_inst_buy",
    "high_52w_inst_buy",
    "value_dividend",
    "chip_score",
    "selection_score",
    "foreign_buy",
    "foreign_5d_revenue_growth",
    "inst_buy_volume",
    "volume_expansion",
    "revenue_volume_breakout",
    "margin_down_foreign_buy",
    "trust_buy",
    "inst_buy",
    "near_avg_with_inst_buy",
]


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    last_error: Exception | None = None
    for _ in range(5):
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2)
    if path.exists():
        raise last_error or RuntimeError(f"failed to remove {path}")


def rows_for_ranking(days: int, ranking: str) -> list[dict[str, object]]:
    return [normalize_scan_row(row, days) for row in read_csv(ranking_path(days, ranking))]


def scan_all_rows(days: int) -> list[dict[str, object]]:
    return [normalize_scan_row(row, days) for row in read_csv(REPORTS_DIR / f"scan_all_{days}d.csv")]


def collect_stock_ids(days_values: list[int], include_all: bool) -> list[str]:
    ids: set[str] = set(current_watchlist_ids())
    for days in days_values:
        rows = scan_all_rows(days) if include_all else []
        if include_all:
            ids.update(str(row["stock_id"]) for row in rows if row.get("stock_id"))
            continue
        for ranking in RANKINGS:
            ids.update(str(row["stock_id"]) for row in rows_for_ranking(days, ranking) if row.get("stock_id"))
    return sorted(ids)


def market_counts(rows: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        market = str(row.get("market") or "")
        counts[market] = counts.get(market, 0) + 1
    return counts


def export_static(out_dir: Path, days_values: list[int], include_all_details: bool) -> dict[str, object]:
    data_dir = out_dir / "data"
    ci_news_payload: dict[str, object] | None = None
    ci_news_path = data_dir / "ci_us_news.json"
    if ci_news_path.exists():
        try:
            ci_news_payload = json.loads(ci_news_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            ci_news_payload = None
    remove_tree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    html = INDEX_HTML.replace("window.STOCK_CHIP_STATIC = false;", "window.STOCK_CHIP_STATIC = true;")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    exported_ids = set(collect_stock_ids(days_values, include_all_details))
    watchlist = current_watchlist_ids()
    coverage_by_days: dict[str, object] = {}
    exported_by_days: dict[str, int] = {}
    missing_details: list[dict[str, object]] = []

    with connect_db(DB_PATH) as conn:
        for days in days_values:
            dates = recent_dates(conn, days)
            coverage = branch_coverage(conn, dates[-1], days) if dates else []
            coverage_by_days[str(days)] = {
                "branch_covered": sum(int(row.get("stocks_with_branch") or 0) for row in coverage),
                "rows": coverage,
            }
            write_json(data_dir / f"coverage_{days}d.json", {"coverage": coverage})
        write_json(data_dir / "us_news.json", {"rows": load_cached_us_news(conn, limit=200)})
        write_json(data_dir / "mops_events.json", load_mops_events(conn, limit=10000))
        if ci_news_payload is None:
            ci_news_payload = {"rows": load_cached_us_news(conn, limit=200)}
        write_json(data_dir / "ci_us_news.json", ci_news_payload)
    for product_code in ("TXF", "MXF", "TMF"):
        payload = market_payload(index_code="TAIEX", product_code=product_code, limit=5000)
        write_json(data_dir / f"market_{product_code}.json", payload)
        if product_code == "TXF":
            write_json(data_dir / "market.json", payload)
    write_json(data_dir / "market_sentiment.json", market_sentiment_payload(limit=5000))

    for days in days_values:
        ranking_dir = data_dir / "rankings" / f"{days}d"
        for ranking in RANKINGS:
            rows = rows_for_ranking(days, ranking)
            exported_ids.update(str(row["stock_id"]) for row in rows if row.get("stock_id"))
            write_json(ranking_dir / f"{ranking}.json", {"days": days, "ranking": ranking, "rows": rows})
        # 全市場列表級資料：搜尋、產業篩選與族群熱力圖涵蓋所有已入庫股票
        searchable = scan_all_rows(days)
        write_json(ranking_dir / "search_index.json", {"days": days, "rows": searchable})
        exported_by_days[str(days)] = len(searchable)

    for stock_id in sorted(exported_ids):
        for days in days_values:
            try:
                write_json(data_dir / "stocks" / stock_id / f"{days}d.json", stock_detail(stock_id, days))
            except Exception as exc:
                missing_details.append({"stock_id": stock_id, "days": days, "error": str(exc)})

    default_days = 20 if 20 in days_values else days_values[-1]
    meta = db_meta(default_days)
    search_rows = scan_all_rows(default_days)
    meta.update(
        {
            "exported_at": now_text(),
            "days": days_values,
            "watchlist": watchlist,
            "static_stock_count": len(exported_ids),
            "static_search_count_by_days": exported_by_days,
            "search_market_counts": market_counts(search_rows),
            "branch_coverage_by_days": coverage_by_days,
            "include_all_details": include_all_details,
            "missing_details": missing_details,
        }
    )
    write_json(data_dir / "meta.json", meta)
    return meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the local SQLite data as a static GitHub Pages site.")
    parser.add_argument("--out", default="docs", help="Output directory for GitHub Pages files.")
    parser.add_argument("--days", nargs="+", type=int, default=[5, 20], help="Trading-day windows to export.")
    parser.add_argument(
        "--include-all-details",
        action="store_true",
        help="Export detail JSON for every stock in scan_all instead of ranking rows plus watchlist.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    meta = export_static(Path(args.out), sorted(set(args.days)), args.include_all_details)
    print(
        f"Exported {meta['static_stock_count']} stocks to {Path(args.out).resolve()} "
        f"at {meta['exported_at']}"
    )


if __name__ == "__main__":
    main()

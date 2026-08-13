from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from stock_chip.branch import branch_coverage
from stock_chip.dividends import load_dividend_events
from stock_chip.etf import load_etf_flows
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
from stock_chip.official import shares_to_lots
from stock_chip.scan import recent_dates
from stock_chip.backtest import backtest_payload, build_daily_digest
from stock_chip.trend import build_trend_report
from stock_chip.weekly import build_weekly_report
from stock_chip.daytrade import build_daytrade_report
from stock_chip.health import collect_health
from stock_chip.mops import load_mops_events
from stock_chip.us_news import load_cached_us_news


RANKINGS = [
    "total_score",
    "multifactor_score",
    "momentum_inst_buy",
    "high_52w_inst_buy",
    "mispriced_value",
    "value_dividend",
    "big_holder_increase",
    "chip_score",
    "selection_score",
    "foreign_buy",
    "foreign_day_buy",
    "foreign_day_sell",
    "foreign_streak_buy",
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
            write_json(
                data_dir / f"digest_{days}d.json",
                build_daily_digest(DB_PATH, REPORTS_DIR / f"scan_all_{days}d.csv", days),
            )
            write_json(
                data_dir / f"trend_{days}d.json",
                build_trend_report(DB_PATH, REPORTS_DIR / f"scan_all_{days}d.csv", days),
            )
        write_json(data_dir / "us_news.json", {"rows": load_cached_us_news(conn, limit=200)})
        write_json(data_dir / "mops_events.json", load_mops_events(conn, limit=10000))
        write_json(data_dir / "dividends.json", load_dividend_events(conn, days=400))
        write_json(data_dir / "etf.json", load_etf_flows(conn))
        if ci_news_payload is None:
            ci_news_payload = {"rows": load_cached_us_news(conn, limit=200)}
        write_json(data_dir / "ci_us_news.json", ci_news_payload)
    # 當沖候選池：盤前名單，不是進出訊號（資料全是盤後的）。
    write_json(
        data_dir / "daytrade.json",
        build_daytrade_report(DB_PATH, REPORTS_DIR / f"scan_all_{max(days_values)}d.csv"),
    )

    # 週報：最近一個完整交易週的表現、資金流向與下週事件行事曆。
    # 讀 20 日掃描帶產業別與日均額；不新增任何抓取步驟。
    write_json(
        data_dir / "weekly.json",
        build_weekly_report(DB_PATH, REPORTS_DIR / f"scan_all_{max(days_values)}d.csv"),
    )

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
        if days == max(days_values):
            # 輕量搜尋建議索引（~150KB）：typeahead 不必下載 6MB 完整索引
            write_json(
                data_dir / "suggest.json",
                {
                    "rows": [
                        {
                            "i": row.get("stock_id"),
                            "n": row.get("name"),
                            "d": row.get("industry") or "",
                            "c": row.get("close"),
                            "m": row.get("multifactor_score"),
                            # 持股警示用：收盤 vs 20 日均價 %、當日外資/投信買賣超、單日量倍
                            "g": row.get("close_vs_avg_pct"),
                            "f": row.get("latest_foreign_net_lot"),
                            "t": row.get("latest_trust_net_lot"),
                            "v": row.get("volume_ratio_1d"),
                        }
                        for row in searchable
                    ]
                },
            )

    for stock_id in sorted(exported_ids):
        for days in days_values:
            try:
                write_json(data_dir / "stocks" / stock_id / f"{days}d.json", stock_detail(stock_id, days))
            except Exception as exc:
                missing_details.append({"stock_id": stock_id, "days": days, "error": str(exc)})

    # 全市場精簡明細：未匯出完整明細的股票仍有 240 日 K 線、20 日每日進出、
    # 融資券與 24 個月營收（每檔約 25KB；分點與新聞僅完整明細提供）
    with connect_db(DB_PATH) as conn:
        chart_dates = recent_dates(conn, 240)
        daily_dates = recent_dates(conn, max(days_values))
        lite_series: dict[str, dict[str, list[dict[str, object]]]] = {}

        def bucket(sid: str) -> dict[str, list[dict[str, object]]]:
            return lite_series.setdefault(sid, {"rows": [], "daily": [], "margin": [], "revenues": []})

        if chart_dates:
            placeholders = ",".join("?" for _ in chart_dates)
            for sid, date, open_, high, low, close in conn.execute(
                f"SELECT stock_id, date, open, high, low, close FROM daily_prices "
                f"WHERE date IN ({placeholders}) ORDER BY stock_id, date",
                chart_dates,
            ):
                if close is None:
                    continue
                bucket(sid)["rows"].append(
                    {"date": date, "open": open_, "high": high, "low": low, "close": close}
                )
        if daily_dates:
            placeholders = ",".join("?" for _ in daily_dates)
            for sid, date, close, avg_price, volume, pe, dy, pb, f_net, t_net, d_net in conn.execute(
                f"""
                SELECT p.stock_id, p.date, p.close, p.avg_price, p.volume, p.pe_ratio,
                       p.dividend_yield, p.pb_ratio,
                       COALESCE(i.foreign_net, 0), COALESCE(i.trust_net, 0), COALESCE(i.dealer_net, 0)
                FROM daily_prices p
                LEFT JOIN institutional_trades i
                    ON i.stock_id = p.stock_id AND i.date = p.date
                WHERE p.date IN ({placeholders})
                ORDER BY p.stock_id, p.date DESC
                """,
                daily_dates,
            ):
                bucket(sid)["daily"].append(
                    {
                        "date": date,
                        "close": close,
                        "avg_price": avg_price,
                        "volume_lot": shares_to_lots(volume),
                        "pe_ratio": pe,
                        "dividend_yield": dy,
                        "pb_ratio": pb,
                        "foreign_net_lot": shares_to_lots(f_net),
                        "trust_net_lot": shares_to_lots(t_net),
                        "dealer_net_lot": shares_to_lots(d_net),
                    }
                )
            for row in conn.execute(
                f"""
                SELECT stock_id, date, margin_buy, margin_sell, margin_cash_repay,
                       margin_prev_balance, margin_balance,
                       short_buy, short_sell, short_stock_repay,
                       short_prev_balance, short_balance, offset, note
                FROM margin_trades
                WHERE date IN ({placeholders})
                ORDER BY stock_id, date DESC
                """,
                daily_dates,
            ):
                bucket(row[0])["margin"].append(
                    {
                        "date": row[1],
                        "margin_buy_lot": row[2],
                        "margin_sell_lot": row[3],
                        "margin_cash_repay_lot": row[4],
                        "margin_prev_balance_lot": row[5],
                        "margin_balance_lot": row[6],
                        "margin_change_lot": (row[6] - row[5]) if row[6] is not None and row[5] is not None else None,
                        "short_buy_lot": row[7],
                        "short_sell_lot": row[8],
                        "short_stock_repay_lot": row[9],
                        "short_prev_balance_lot": row[10],
                        "short_balance_lot": row[11],
                        "short_change_lot": (row[11] - row[10]) if row[11] is not None and row[10] is not None else None,
                        "offset_lot": row[12],
                        "note": row[13],
                    }
                )
        revenue_counts: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT stock_id, revenue_month, revenue, prev_month_revenue, last_year_revenue,
                   mom_pct, yoy_pct, cumulative_revenue, last_year_cumulative_revenue,
                   cumulative_yoy_pct, source
            FROM monthly_revenues
            ORDER BY stock_id, revenue_month DESC
            """
        ):
            sid = row[0]
            if revenue_counts.get(sid, 0) >= 24:
                continue
            revenue_counts[sid] = revenue_counts.get(sid, 0) + 1
            bucket(sid)["revenues"].append(
                {
                    "revenue_month": row[1],
                    "revenue_million": round(row[2] / 1_000_000, 2) if row[2] is not None else None,
                    "prev_month_revenue_million": round(row[3] / 1_000_000, 2) if row[3] is not None else None,
                    "last_year_revenue_million": round(row[4] / 1_000_000, 2) if row[4] is not None else None,
                    "mom_pct": row[5],
                    "yoy_pct": row[6],
                    "cumulative_revenue_million": round(row[7] / 1_000_000, 2) if row[7] is not None else None,
                    "last_year_cumulative_revenue_million": round(row[8] / 1_000_000, 2) if row[8] is not None else None,
                    "cumulative_yoy_pct": row[9],
                    "source": row[10],
                }
            )
        lite_count = 0
        for sid, payload in lite_series.items():
            if sid in exported_ids:
                continue
            write_json(data_dir / "stocks" / sid / "chart_lite.json", {"stock_id": sid, **payload})
            lite_count += 1
        print(f"lite details exported: {lite_count}")

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
    write_json(data_dir / "backtest.json", backtest_payload(DB_PATH))
    write_json(data_dir / "health.json", collect_health(DB_PATH))
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

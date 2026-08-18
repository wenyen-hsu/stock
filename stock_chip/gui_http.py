"""Local HTTP handler for the stock chip GUI. Re-exported from stock_chip.gui."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from stock_chip.branch import branch_coverage, run_branch, run_branch_daily
from stock_chip.tdcc import refresh_dispersion
from stock_chip.financials import refresh_financials
from stock_chip.backtest import backtest_payload, build_daily_digest
from stock_chip.trend import build_trend_report
from stock_chip.health import collect_health
from stock_chip.market import PRODUCTS as FUTURES_PRODUCTS
from stock_chip.mops import load_mops_events, refresh_mops_events
from stock_chip.news import ensure_news_tables, load_cached_news, refresh_stock_news
from stock_chip.obsidian_news import export_obsidian_vault, obsidian_vault_status
from stock_chip.us_news import ensure_us_news_tables, import_static_us_news, load_cached_us_news, refresh_us_news
from stock_chip.official import (
    connect_db,
    fetch_tpex_institutional_all,
    fetch_tpex_margin_all,
    fetch_tpex_prices_all,
    fetch_twse_institutional_all,
    fetch_twse_margin_all,
    fetch_twse_prices_all,
    shares_to_lots,
    upsert_institutional,
    upsert_margin,
    upsert_prices,
)
from stock_chip.quality import score_input_coverage
from stock_chip.revenue import run_revenue
from stock_chip.scan import recent_dates, run_scan

from stock_chip.gui_data import (
    DB_PATH,
    INDEX_HTML,
    RANKING_LABELS,
    REPORTS_DIR,
    current_watchlist_ids,
    db_meta,
    filter_ci_us_news_payload,
    filtered_rows,
    load_ci_us_news_live,
    load_ci_us_news_preview,
    market_payload,
    market_sentiment_payload,
    normalize_scan_row,
    ranking_path,
    ranking_source_rows,
    read_csv,
    resolve_local_stock,
    set_watchlist_item,
    stock_detail,
    suggest_rows,
    watchlist_items,
)
from stock_chip.gui_jobs import (
    UPDATE_TASKS,
    cancel_running_update,
    ensure_stock_data,
    export_static_pages,
    publish_static_pages,
    refresh_stock_section,
    start_update_task,
    static_publish_status,
    update_task_state,
)

def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class GUIHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                body = INDEX_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/meta":
                days = int(params.get("days", ["20"])[0])
                json_response(self, db_meta(days))
                return
            if parsed.path == "/api/ranking":
                days = int(params.get("days", ["20"])[0])
                ranking = params.get("ranking", ["total_score"])[0]
                if ranking == "foreign_5d_revenue_growth":
                    days = 5
                market = params.get("market", [""])[0]
                q = params.get("q", [""])[0]
                limit = int(params.get("limit", ["50"])[0])
                min_volume = float(params.get("min_volume", ["0"])[0] or 0)
                industry = params.get("industry", [""])[0]
                rows = [normalize_scan_row(row, days) for row in ranking_source_rows(days, ranking, q)]
                json_response(
                    self,
                    {
                        "title": f"{days} 日排行：{RANKING_LABELS.get(ranking, ranking)}",
                        "rows": filtered_rows(rows, market, q, limit, min_volume, industry),
                    },
                )
                return
            if parsed.path == "/api/watchlist":
                days = int(params.get("days", ["20"])[0])
                market = params.get("market", [""])[0]
                q = params.get("q", [""])[0]
                limit = int(params.get("limit", ["50"])[0])
                min_volume = float(params.get("min_volume", ["0"])[0] or 0)
                watchlist = current_watchlist_ids()
                watch_order = {stock_id: idx for idx, stock_id in enumerate(watchlist)}
                source_rows = read_csv(REPORTS_DIR / f"scan_all_{days}d.csv") or read_csv(ranking_path(days, "total_score"))
                rows = [normalize_scan_row(row, days) for row in source_rows if row.get("stock_id") in watch_order]
                rows.sort(key=lambda row: watch_order.get(str(row.get("stock_id")), 9999))
                industry = params.get("industry", [""])[0]
                json_response(self, {"rows": filtered_rows(rows, market, q, limit, min_volume, industry)})
                return
            if parsed.path == "/api/scan-all":
                days = int(params.get("days", ["20"])[0])
                rows = [normalize_scan_row(row, days) for row in read_csv(REPORTS_DIR / f"scan_all_{days}d.csv")]
                json_response(self, {"days": days, "rows": rows})
                return
            if parsed.path == "/api/industries":
                days = int(params.get("days", ["20"])[0])
                rows = read_csv(REPORTS_DIR / f"scan_all_{days}d.csv")
                industries = sorted({(row.get("industry") or "").strip() for row in rows} - {""})
                json_response(self, {"industries": industries})
                return
            if parsed.path == "/api/watchlist-items":
                json_response(self, {"rows": watchlist_items()})
                return
            if parsed.path == "/api/stock/resolve":
                query = params.get("query", [""])[0]
                json_response(self, resolve_local_stock(query))
                return
            if parsed.path == "/api/stock":
                days = int(params.get("days", ["20"])[0])
                stock_id = params.get("stock_id", ["2376"])[0]
                json_response(self, stock_detail(stock_id, days))
                return
            if parsed.path == "/api/market":
                index_code = params.get("index", ["TAIEX"])[0]
                product_code = params.get("product", ["TXF"])[0]
                limit = int(params.get("limit", ["240"])[0])
                json_response(self, market_payload(index_code=index_code, product_code=product_code, limit=limit))
                return
            if parsed.path == "/api/market-sentiment":
                limit = int(params.get("limit", ["120"])[0])
                json_response(self, market_sentiment_payload(limit=limit))
                return
            if parsed.path == "/api/news":
                stock_id = params.get("stock_id", [""])[0].strip()
                limit = min(max(int(params.get("limit", ["20"])[0]), 1), 50)
                with connect_db(DB_PATH) as conn:
                    json_response(self, {"stock_id": stock_id, "rows": load_cached_news(conn, stock_id, limit=limit)})
                return
            if parsed.path == "/api/us-news":
                symbols = [
                    item.strip().upper()
                    for item in params.get("symbols", [""])[0].split(",")
                    if item.strip()
                ]
                industry = params.get("industry", [""])[0].strip()
                limit = min(max(int(params.get("limit", ["80"])[0]), 1), 200)
                with connect_db(DB_PATH) as conn:
                    json_response(
                        self,
                        {
                            "symbols": symbols,
                            "industry": industry,
                            "rows": load_cached_us_news(conn, symbols=symbols, industry=industry, limit=limit),
                        },
                    )
                return
            if parsed.path == "/api/ci-us-news":
                industry = params.get("industry", [""])[0].strip()
                source = params.get("source", [""])[0].strip()
                query = params.get("q", [""])[0].strip()
                limit = min(max(int(params.get("limit", ["200"])[0]), 1), 500)
                json_response(self, load_ci_us_news_preview(industry=industry, source=source, query=query, limit=limit))
                return
            if parsed.path == "/api/ci-us-news-live":
                industry = params.get("industry", [""])[0].strip()
                source = params.get("source", [""])[0].strip()
                query = params.get("q", [""])[0].strip()
                limit = min(max(int(params.get("limit", ["200"])[0]), 1), 500)
                json_response(self, load_ci_us_news_live(industry=industry, source=source, query=query, limit=limit))
                return
            if parsed.path == "/api/mops-events":
                start_date = params.get("start", [""])[0].strip()
                end_date = params.get("end", [""])[0].strip()
                stock_id = params.get("stock_id", [""])[0].strip()
                query = params.get("q", [""])[0].strip()
                limit = min(max(int(params.get("limit", ["500"])[0]), 1), 10000)
                with connect_db(DB_PATH) as conn:
                    json_response(
                        self,
                        load_mops_events(
                            conn,
                            start_date=start_date,
                            end_date=end_date,
                            stock_id=stock_id,
                            query=query,
                            limit=limit,
                        ),
                    )
                return
            if parsed.path == "/api/obsidian/status":
                json_response(self, obsidian_vault_status())
                return
            if parsed.path == "/api/coverage":
                days = int(params.get("days", ["20"])[0])
                with connect_db(DB_PATH) as conn:
                    dates = recent_dates(conn, days)
                    coverage = branch_coverage(conn, dates[-1], days) if dates else []
                json_response(self, {"coverage": coverage})
                return
            if parsed.path == "/api/backtest":
                json_response(self, backtest_payload(DB_PATH))
                return
            if parsed.path == "/api/digest":
                days = int(params.get("days", ["20"])[0])
                json_response(self, build_daily_digest(DB_PATH, REPORTS_DIR / f"scan_all_{days}d.csv", days))
                return
            if parsed.path == "/api/suggest":
                json_response(self, {"rows": suggest_rows()})
                return
            if parsed.path == "/api/trend":
                days = int(params.get("days", ["20"])[0])
                json_response(self, build_trend_report(DB_PATH, REPORTS_DIR / f"scan_all_{days}d.csv", days))
                return
            if parsed.path == "/api/etf":
                from stock_chip.etf import load_etf_flows

                with connect_db(DB_PATH) as conn:
                    json_response(self, load_etf_flows(conn))
                return
            if parsed.path == "/api/dividends":
                from stock_chip.dividends import load_dividend_events

                with connect_db(DB_PATH) as conn:
                    json_response(self, load_dividend_events(conn, days=400))
                return
            if parsed.path == "/api/daytrade":
                from stock_chip.daytrade import build_daytrade_report

                json_response(self, build_daytrade_report(DB_PATH, REPORTS_DIR / "scan_all_20d.csv"))
                return
            if parsed.path == "/api/weekly":
                from stock_chip.weekly import build_weekly_report

                json_response(self, build_weekly_report(DB_PATH, REPORTS_DIR / "scan_all_20d.csv"))
                return
            if parsed.path == "/api/health":
                json_response(self, collect_health(DB_PATH))
                return
            if parsed.path == "/api/update-tasks":
                json_response(self, update_task_state())
                return
            if parsed.path == "/api/static/status":
                json_response(self, static_publish_status())
                return
            json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/watchlist":
                payload = self.read_json_body()
                stock_id = str(payload.get("stock_id") or "")
                action = str(payload.get("action") or "add")
                json_response(self, set_watchlist_item(stock_id, action))
                return
            if parsed.path == "/api/update":
                payload = self.read_json_body()
                task_id = str(payload.get("task") or "")
                days = int(payload.get("days") or 20)
                if task_id not in {str(task["id"]) for task in UPDATE_TASKS}:
                    json_response(self, {"error": "unknown task"}, HTTPStatus.BAD_REQUEST)
                    return
                job = start_update_task(task_id, days)
                json_response(self, {"job": job})
                return
            if parsed.path == "/api/update/cancel":
                json_response(self, cancel_running_update())
                return
            if parsed.path == "/api/news/refresh":
                payload = self.read_json_body()
                stock_id = str(payload.get("stock_id") or "")
                limit = min(max(int(payload.get("limit") or 8), 1), 15)
                json_response(self, refresh_stock_news(DB_PATH, stock_id, limit=limit))
                return
            if parsed.path == "/api/us-news/refresh":
                payload = self.read_json_body()
                raw_symbols = payload.get("symbols") or []
                if isinstance(raw_symbols, str):
                    symbols = [item.strip() for item in raw_symbols.split(",") if item.strip()]
                elif isinstance(raw_symbols, list):
                    symbols = [str(item).strip() for item in raw_symbols if str(item).strip()]
                else:
                    symbols = []
                limit = min(max(int(payload.get("limit") or 5), 1), 25)
                use_ollama = bool(payload.get("use_ollama", True))
                include_symbol_news = bool(payload.get("include_symbol_news", True))
                model = str(payload.get("model") or "gemma4:e4b")
                json_response(
                    self,
                    refresh_us_news(
                        DB_PATH,
                        symbols=symbols,
                        limit=limit,
                        use_ollama=use_ollama,
                        model=model,
                        include_symbol_news=include_symbol_news,
                    ),
                )
                return
            if parsed.path == "/api/mops-events/refresh":
                payload = self.read_json_body()
                start_date_text = str(payload.get("start_date") or "").strip()
                end_date_text = str(payload.get("end_date") or "").strip()
                start_date = dt.date.fromisoformat(start_date_text) if start_date_text else None
                end_date = dt.date.fromisoformat(end_date_text) if end_date_text else None
                json_response(self, refresh_mops_events(DB_PATH, start_date=start_date, end_date=end_date))
                return
            if parsed.path == "/api/tdcc/refresh":
                payload = self.read_json_body()
                json_response(self, refresh_dispersion(DB_PATH, force=bool(payload.get("force"))))
                return
            if parsed.path == "/api/financials/refresh":
                payload = self.read_json_body()
                json_response(self, refresh_financials(DB_PATH, force=bool(payload.get("force"))))
                return
            if parsed.path == "/api/obsidian/export":
                result = export_obsidian_vault(DB_PATH)
                json_response(self, {"result": result, "status": obsidian_vault_status()})
                return
            if parsed.path == "/api/stock/refresh-section":
                payload = self.read_json_body()
                stock_id = str(payload.get("stock_id") or "")
                section = str(payload.get("section") or "")
                days = int(payload.get("days") or 20)
                json_response(self, refresh_stock_section(stock_id, section, days))
                return
            if parsed.path == "/api/stock/ensure":
                payload = self.read_json_body()
                query = str(payload.get("query") or "")
                days = int(payload.get("days") or 20)
                json_response(self, ensure_stock_data(query, days))
                return
            if parsed.path == "/api/static/export":
                json_response(self, export_static_pages())
                return
            if parsed.path == "/api/static/publish":
                json_response(self, publish_static_pages())
                return
            json_response(self, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local stock chip GUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), GUIHandler)
    print(f"GUI: http://{args.host}:{args.port}")
    server.serve_forever()


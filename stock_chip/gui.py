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


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "stock_chip.sqlite"
REPORTS_DIR = ROOT / "reports"
WATCHLIST = ["2376", "2382", "2324", "6196"]
UPDATE_JOBS: dict[str, dict[str, object]] = {}
UPDATE_LOCK = threading.RLock()
CI_US_NEWS_PAGES_URL = "https://wenyen-hsu.github.io/stock/data/ci_us_news.json"
CI_US_NEWS_SITE_URL = "https://wenyen-hsu.github.io/stock/"


INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def as_float(value: str | None) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def as_float_or_none(value: str | None) -> float | None:
    """Keep missing statistics as null so the UI shows blank instead of a misleading 0."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def ensure_gui_tables(conn: sqlite3.Connection) -> None:
    ensure_news_tables(conn)
    ensure_us_news_tables(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_watchlist (
            stock_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS update_job_history (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            current_step TEXT,
            error TEXT
        )
        """
    )
    count = conn.execute("SELECT COUNT(*) FROM user_watchlist").fetchone()[0]
    if count == 0:
        now = now_text()
        conn.executemany(
            "INSERT OR IGNORE INTO user_watchlist (stock_id, created_at) VALUES (?, ?)",
            [(stock_id, now) for stock_id in WATCHLIST],
        )
    conn.commit()


def current_watchlist_ids() -> list[str]:
    with connect_db(DB_PATH) as conn:
        ensure_gui_tables(conn)
        rows = conn.execute(
            """
            SELECT w.stock_id
            FROM user_watchlist w
            LEFT JOIN stocks s ON s.stock_id = w.stock_id
            ORDER BY w.rowid
            """
        ).fetchall()
    return [row[0] for row in rows] or WATCHLIST[:]


def watchlist_items() -> list[dict[str, object]]:
    with connect_db(DB_PATH) as conn:
        ensure_gui_tables(conn)
        rows = conn.execute(
            """
            SELECT w.stock_id, COALESCE(s.name, w.stock_id), COALESCE(s.market, ''), w.created_at
            FROM user_watchlist w
            LEFT JOIN stocks s ON s.stock_id = w.stock_id
            ORDER BY w.rowid
            """
        ).fetchall()
    return [
        {"stock_id": row[0], "name": row[1], "market": row[2], "created_at": row[3]}
        for row in rows
    ]


def set_watchlist_item(stock_id: str, action: str) -> dict[str, object]:
    stock_id = stock_id.strip()
    if not stock_id:
        raise ValueError("股票代號不可空白")
    with connect_db(DB_PATH) as conn:
        ensure_gui_tables(conn)
        stock = conn.execute(
            "SELECT stock_id, name, market FROM stocks WHERE stock_id = ?",
            (stock_id,),
        ).fetchone()
        if not stock:
            raise ValueError(f"找不到股票 {stock_id}")
        if action == "add":
            conn.execute(
                "INSERT OR IGNORE INTO user_watchlist (stock_id, created_at) VALUES (?, ?)",
                (stock_id, now_text()),
            )
        elif action == "remove":
            conn.execute("DELETE FROM user_watchlist WHERE stock_id = ?", (stock_id,))
        else:
            raise ValueError(f"未知自選股操作：{action}")
        conn.commit()
    return {
        "stock_id": stock[0],
        "name": stock[1],
        "market": stock[2],
        "in_watchlist": stock_id in current_watchlist_ids(),
        "watchlist": watchlist_items(),
    }


def normalize_scan_row(row: dict[str, str], days: int) -> dict[str, object]:
    return {
        "latest_date": row.get("latest_date"),
        "stock_id": row.get("stock_id"),
        "name": row.get("name"),
        "market": row.get("market"),
        "industry": row.get("industry") or "",
        "sub_industry": row.get("sub_industry") or "",
        "observed_days": int(as_float(row.get("observed_days"))),
        "close": as_float(row.get("close")),
        "pe_ratio": as_float_or_none(row.get("pe_ratio")),
        "dividend_yield": as_float_or_none(row.get("dividend_yield")),
        "pb_ratio": as_float_or_none(row.get("pb_ratio")),
        "pe_percentile": as_float_or_none(row.get("pe_percentile")),
        "pe_median_1y": as_float_or_none(row.get("pe_median_1y")),
        "pe_vs_median_pct": as_float_or_none(row.get("pe_vs_median_pct")),
        "eps_single_q": as_float_or_none(row.get("eps_single_q")),
        "eps_yoy_pct": as_float_or_none(row.get("eps_yoy_pct")),
        "operating_margin_yoy_pt": as_float_or_none(row.get("operating_margin_yoy_pt")),
        "mispriced_score": as_float_or_none(row.get("mispriced_score")),
        "mispriced_earnings_score": as_float_or_none(row.get("mispriced_earnings_score")),
        "mispriced_cheap_score": as_float_or_none(row.get("mispriced_cheap_score")),
        "mispriced_trap_penalty": as_float_or_none(row.get("mispriced_trap_penalty")),
        "mispriced_cyclical_peak": as_float_or_none(row.get("mispriced_cyclical_peak")),
        "eps_ttm_at_high": as_float_or_none(row.get("eps_ttm_at_high")),
        "mispriced_reason": row.get("mispriced_reason") or "",
        "period_return_pct": as_float_or_none(row.get("period_return_pct")),
        "rsi14": as_float_or_none(row.get("rsi14")),
        "volatility_pct": as_float_or_none(row.get("volatility_pct")),
        "close_vs_high_pct": as_float_or_none(row.get("close_vs_high_pct")),
        "close_vs_low_pct": as_float_or_none(row.get("close_vs_low_pct")),
        "avg_turnover_100m": as_float_or_none(row.get("avg_turnover_100m")),
        "momentum_score": as_float(row.get("momentum_score")),
        "long_momentum_score": as_float(row.get("long_momentum_score")),
        "history_days": as_float_or_none(row.get("history_days")),
        "return_6m_pct": as_float_or_none(row.get("return_6m_pct")),
        "return_12m_pct": as_float_or_none(row.get("return_12m_pct")),
        "rs_6m_pct": as_float_or_none(row.get("rs_6m_pct")),
        "rs_12m_pct": as_float_or_none(row.get("rs_12m_pct")),
        "close_vs_52w_high_pct": as_float_or_none(row.get("close_vs_52w_high_pct")),
        "close_vs_52w_low_pct": as_float_or_none(row.get("close_vs_52w_low_pct")),
        "close_vs_ma60_pct": as_float_or_none(row.get("close_vs_ma60_pct")),
        "close_vs_ma240_pct": as_float_or_none(row.get("close_vs_ma240_pct")),
        "valuation_score": as_float(row.get("valuation_score")),
        "disp_date": row.get("disp_date") or "",
        "big_holder_pct": as_float_or_none(row.get("big_holder_pct")),
        "big_holder_change_1w": as_float_or_none(row.get("big_holder_change_1w")),
        "big_holder_change_4w": as_float_or_none(row.get("big_holder_change_4w")),
        "holder_400_pct": as_float_or_none(row.get("holder_400_pct")),
        "retail_pct": as_float_or_none(row.get("retail_pct")),
        "retail_change_1w": as_float_or_none(row.get("retail_change_1w")),
        "dispersion_score": as_float(row.get("dispersion_score")),
        "fin_quarter": row.get("fin_quarter") or "",
        "gross_margin_pct": as_float_or_none(row.get("gross_margin_pct")),
        "operating_margin_pct": as_float_or_none(row.get("operating_margin_pct")),
        "net_margin_pct": as_float_or_none(row.get("net_margin_pct")),
        "gross_margin_streak": as_float_or_none(row.get("gross_margin_streak")),
        "eps_ttm": as_float_or_none(row.get("eps_ttm")),
        "pe_ttm": as_float_or_none(row.get("pe_ttm")),
        "fundamental_score": as_float(row.get("fundamental_score")),
        "multifactor_score": as_float(row.get("multifactor_score")),
        "avg_price": as_float(row.get(f"{days}d_avg_price")),
        "volume_lot": as_float(row.get(f"{days}d_volume_lot")),
        "foreign_net_lot": as_float(row.get(f"{days}d_foreign_net_lot")),
        "trust_net_lot": as_float(row.get(f"{days}d_trust_net_lot")),
        "inst_net_lot": as_float(row.get(f"{days}d_inst_net_lot")),
        "foreign_net_volume_pct": as_float(row.get("foreign_net_volume_pct")),
        "trust_net_volume_pct": as_float(row.get("trust_net_volume_pct")),
        "latest_volume_lot": as_float(row.get("latest_volume_lot")),
        "volume_avg_lot": as_float(row.get("volume_avg_lot")),
        "volume_5d_avg_lot": as_float(row.get("volume_5d_avg_lot")),
        "volume_ratio_1d": as_float_or_none(row.get("volume_ratio_1d")),
        "volume_ratio_5d": as_float_or_none(row.get("volume_ratio_5d")),
        "volume_score": as_float(row.get("volume_score")),
        "volume_signal": row.get("volume_signal") or "",
        "top_buy_branch_name": row.get("top_buy_branch_name") or "",
        "top_buy_branch_net_lot": as_float_or_none(row.get("top_buy_branch_net_lot")),
        "top_buy_branch_avg_price": as_float_or_none(row.get("top_buy_branch_avg_price")),
        "top_buy_branch_est_cost": as_float_or_none(row.get("top_buy_branch_est_cost")),
        "branch_buy_streak": as_float_or_none(row.get("branch_buy_streak")),
        "branch_streak_broker": row.get("branch_streak_broker") or "",
        "top_buy_is_day_trader": as_float(row.get("top_buy_is_day_trader")),
        "top_sell_branch_name": row.get("top_sell_branch_name") or "",
        "top_sell_branch_net_lot": as_float_or_none(row.get("top_sell_branch_net_lot")),
        "top_sell_branch_avg_price": as_float_or_none(row.get("top_sell_branch_avg_price")),
        "branch_score": as_float(row.get("branch_score")),
        "chip_score": as_float(row.get("chip_score")),
        "margin_score": as_float(row.get("margin_score")),
        "base_score": as_float(row.get("base_score")),
        "selection_score": as_float(row.get("selection_score")),
        "complete_chip_score": as_float(row.get("complete_chip_score")),
        "confluence_score": as_float(row.get("confluence_score")),
        "revenue_momentum_score": as_float(row.get("revenue_momentum_score")),
        "revenue_month": row.get("revenue_month") or "",
        "revenue_mom_pct": as_float_or_none(row.get("revenue_mom_pct")),
        "revenue_yoy_pct": as_float_or_none(row.get("revenue_yoy_pct")),
        "revenue_cumulative_yoy_pct": as_float_or_none(row.get("revenue_cumulative_yoy_pct")),
        "confluence_inst_score": as_float(row.get("confluence_inst_score")),
        "confluence_branch_score": as_float(row.get("confluence_branch_score")),
        "confluence_price_score": as_float(row.get("confluence_price_score")),
        "confluence_streak_score": as_float(row.get("confluence_streak_score")),
        "confluence_direction_score": as_float(row.get("confluence_direction_score")),
        "inst_net_volume_pct": as_float_or_none(row.get("inst_net_volume_pct")),
        "top_buy_branch_volume_pct": as_float_or_none(row.get("top_buy_branch_volume_pct")),
        "close_vs_top_buy_avg_pct": as_float_or_none(row.get("close_vs_top_buy_avg_pct")),
        "foreign_buy_streak": as_float(row.get("foreign_buy_streak")),
        "foreign_sell_streak": as_float(row.get("foreign_sell_streak")),
        "trust_buy_streak": as_float(row.get("trust_buy_streak")),
        "trust_sell_streak": as_float(row.get("trust_sell_streak")),
        # 單日法人買賣超與乖離：外資動向排行與持股警示都靠這三欄。
        # 它們在 scan_all CSV 早就有，先前漏在白名單外 → 線上恆為 null，
        # 連前端早就寫好的「最近一日外資」欄位也一直是空的。
        "latest_foreign_net_lot": as_float_or_none(row.get("latest_foreign_net_lot")),
        "latest_trust_net_lot": as_float_or_none(row.get("latest_trust_net_lot")),
        "close_vs_avg_pct": as_float_or_none(row.get("close_vs_avg_pct")),
        "margin_balance_change_lot": as_float_or_none(row.get("margin_balance_change_lot")),
        "short_balance_change_lot": as_float_or_none(row.get("short_balance_change_lot")),
        "branch_status": row.get("branch_status") or ("已取得" if row.get("top_buy_branch_name") else "未取得"),
        "selection_reason": row.get("selection_reason") or "",
        "base_reason": row.get("base_reason") or "",
        "total_score": as_float(row.get("total_score")),
    }


def ranking_path(days: int, ranking: str) -> Path:
    names = {
        "total_score": "ranking_total_score",
        "multifactor_score": "ranking_multifactor_score",
        "momentum_inst_buy": "ranking_momentum_inst_buy",
        "high_52w_inst_buy": "ranking_high_52w_inst_buy",
        "mispriced_value": "ranking_mispriced_value",
        "value_dividend": "ranking_value_dividend",
        "big_holder_increase": "ranking_big_holder_increase",
        "chip_score": "ranking_chip_score",
        "branch_score": "ranking_branch_score",
        "confluence_score": "ranking_confluence_score",
        "selection_score": "ranking_selection_score",
        "foreign_buy": "ranking_foreign_buy",
        "foreign_day_buy": "ranking_foreign_day_buy",
        "foreign_day_sell": "ranking_foreign_day_sell",
        "foreign_streak_buy": "ranking_foreign_streak_buy",
        "foreign_5d_revenue_growth": "ranking_foreign_5d_revenue_growth",
        "inst_buy_volume": "ranking_inst_buy_volume",
        "volume_expansion": "ranking_volume_expansion",
        "revenue_volume_breakout": "ranking_revenue_volume_breakout",
        "margin_down_foreign_buy": "ranking_margin_down_foreign_buy",
        "trust_buy": "ranking_trust_buy",
        "inst_buy": "ranking_inst_buy",
        "foreign_trust_same_buy": "ranking_foreign_trust_same_buy",
        "near_avg_with_inst_buy": "ranking_near_avg_with_inst_buy",
    }
    return REPORTS_DIR / f"{names.get(ranking, 'ranking_total_score')}_{days}d.csv"


RANKING_LABELS = {
    "total_score": "基礎選股分",
    "multifactor_score": "多因子綜合分",
    "momentum_inst_buy": "動能 + 法人買超",
    "high_52w_inst_buy": "52週新高 + 法人買超",
    "mispriced_value": "錯殺價值（跌深 + 獲利轉強）",
    "value_dividend": "低估值 + 高殖利率",
    "big_holder_increase": "大戶增持 + 法人買超",
    "chip_score": "法人籌碼分數",
    "selection_score": "基礎選股分",
    "foreign_buy": "外資買超",
    "foreign_day_buy": "外資今日買超",
    "foreign_day_sell": "外資今日賣超",
    "foreign_streak_buy": "外資連續買超（天天在買）",
    "foreign_5d_revenue_growth": "外資近5日買超 + 營收成長",
    "inst_buy_volume": "法人買超 + 占量",
    "volume_expansion": "成交量放大",
    "revenue_volume_breakout": "營收成長 + 量能",
    "margin_down_foreign_buy": "融資下降 + 外資買超",
    "trust_buy": "投信買超",
    "inst_buy": "外資 + 投信",
    "foreign_trust_same_buy": "外資投信同步買超",
    "near_avg_with_inst_buy": "接近均價且法人買超",
}


def filtered_rows(
    rows: list[dict[str, object]],
    market: str,
    q: str,
    limit: int,
    min_volume: float = 0.0,
    industry: str = "",
) -> list[dict[str, object]]:
    text = q.lower()
    output = []
    for row in rows:
        if market and row.get("market") != market:
            continue
        if industry and row.get("industry") != industry:
            continue
        if text and text not in str(row.get("stock_id", "")).lower() and text not in str(row.get("name", "")).lower():
            continue
        if min_volume > 0 and float(row.get("volume_lot") or 0) < min_volume:
            continue
        output.append(row)
        if len(output) >= limit:
            break
    return output


def ranking_source_rows(days: int, ranking: str, q: str) -> list[dict[str, str]]:
    text = q.strip()
    if text:
        all_rows = read_csv(REPORTS_DIR / f"scan_all_{days}d.csv")
        if all_rows:
            return all_rows
    return read_csv(ranking_path(days, ranking))


def db_meta(days: int) -> dict[str, object]:
    with connect_db(DB_PATH) as conn:
        latest = conn.execute("SELECT MAX(date) FROM trading_days").fetchone()[0]
        counts = dict(conn.execute("SELECT market, COUNT(*) FROM stocks GROUP BY market").fetchall())
        dates = recent_dates(conn, days)
        covered = 0
        if dates:
            covered = conn.execute(
                """
                SELECT COUNT(DISTINCT stock_id)
                FROM broker_branch_topn
                WHERE as_of_date = ?
                  AND window_days = ?
                  AND source = 'moneydj'
                """,
                (dates[-1], days),
            ).fetchone()[0]
    return {
        "latest_date": latest,
        "stock_count": counts.get("TWSE", 0) + counts.get("TPEX", 0),
        "twse_count": counts.get("TWSE", 0),
        "tpex_count": counts.get("TPEX", 0),
        "esb_count": counts.get("ESB", 0),
        "branch_covered": covered,
    }


def resolve_local_stock(query: str) -> dict[str, object]:
    text = query.strip()
    if not text:
        raise ValueError("請輸入股票代號或名稱")
    like = f"%{text}%"
    with connect_db(DB_PATH) as conn:
        exact = conn.execute(
            """
            SELECT stock_id, name, market
            FROM stocks
            WHERE stock_id = ? OR name = ?
            ORDER BY stock_id
            LIMIT 1
            """,
            (text, text),
        ).fetchone()
        row = exact or conn.execute(
            """
            SELECT stock_id, name, market
            FROM stocks
            WHERE stock_id LIKE ? OR name LIKE ?
            ORDER BY
                CASE
                    WHEN stock_id LIKE ? THEN 0
                    WHEN name LIKE ? THEN 1
                    ELSE 2
                END,
                stock_id
            LIMIT 1
            """,
            (like, like, f"{text}%", f"{text}%"),
        ).fetchone()
    if not row:
        raise ValueError(f"找不到股票 {text}")
    return {"stock_id": row[0], "name": row[1], "market": row[2]}


UPDATE_TASKS = [
    {
        "id": "all_data",
        "title": "一鍵更新全部資料",
        "description": "增量更新：官方行情、成交量、法人買賣超、融資融券、大盤指數與期貨多空只補缺漏交易日；營收先補 24 個月歷史，之後只補最新月份；分點改為個股頁單檔更新，不放入一鍵更新與評分。",
    },
    {
        "id": "official_scan",
        "title": "官方行情與排行",
        "description": "更新上市上櫃近 20 個交易日行情、成交量、法人買賣超、融資融券，並重算 20 日與 5 日排行。",
    },
    {
        "id": "market_data",
        "title": "大盤指數與期貨多空",
        "description": "補齊加權指數與大台 TXF、小台 MXF、微台 TMF 三大法人交易淨額與未平倉多空淨額；已下載日期會跳過，之後只補每日新資料。",
    },
    {
        "id": "candidate_revenue",
        "title": "候選股營收",
        "description": "更新 20 日基礎分前 300 檔加自選股的 24 個月營收，用於營收策略篩選。",
    },
    {
        "id": "all_market_revenue",
        "title": "全市場營收補齊",
        "description": "第一次補齊全部上市上櫃股票 24 個月營收；已有歷史資料後，一鍵更新只補最新應公布月份的近 3 個月資料。",
    },
    {
        "id": "quality_check",
        "title": "分數資料完整性檢查",
        "description": "檢查基礎分需要的行情成交量、法人買賣超、融資融券、月營收是否覆蓋全部上市上櫃股票。",
    },
    {
        "id": "watchlist_news",
        "title": "自選股新聞標題",
        "description": "更新自選股 Yahoo 股市 RSS 標題與連結；文章內文保留到個股頁手動抓取。",
    },
    {
        "id": "us_news_obsidian",
        "title": "本機抓取美股新聞",
        "description": "直接抓取美股 RSS，寫入本機 SQLite，使用 0 token 規則分類，並同步 Obsidian。",
    },
    {
        "id": "sync_ci_us_news",
        "title": "同步 GitHub 新聞到本機",
        "description": "讀取 docs/data/ci_us_news.json，去重匯入本機 SQLite，重新規則分類，並同步 Obsidian。",
    },
]


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_time(value: object) -> str:
    return str(value or "").replace("T", " ")[:19]


def candidate_revenue_ids(limit: int = 300) -> list[str]:
    rows = read_csv(REPORTS_DIR / "scan_all_20d.csv")
    if rows:
        rows = sorted(rows, key=lambda row: as_float(row.get("total_score")), reverse=True)
        ids = []
        for row in rows:
            stock_id = (row.get("stock_id") or "").strip()
            if stock_id and stock_id not in ids:
                ids.append(stock_id)
            if len(ids) >= limit:
                break
    else:
        ids = top_stock_ids_from_report(20, limit)
    for stock_id in current_watchlist_ids():
        if stock_id not in ids:
            ids.append(stock_id)
    return ids


def revenue_missing_stock_ids(stock_ids: list[str]) -> list[str]:
    if not stock_ids:
        return []
    with connect_db(DB_PATH) as conn:
        placeholders = ",".join("?" for _ in stock_ids)
        rows = conn.execute(
            f"""
            SELECT DISTINCT stock_id
            FROM monthly_revenues
            WHERE stock_id IN ({placeholders})
            """,
            stock_ids,
        ).fetchall()
    covered = {row[0] for row in rows}
    return [stock_id for stock_id in stock_ids if stock_id not in covered]


def expected_latest_revenue_month(today: dt.date | None = None) -> str:
    """Monthly revenue for the latest completed calendar month."""
    today = today or dt.date.today()
    first_day = today.replace(day=1)
    previous_month = first_day - dt.timedelta(days=1)
    return previous_month.strftime("%Y-%m")


def revenue_missing_month_stock_ids(stock_ids: list[str], revenue_month: str) -> list[str]:
    if not stock_ids:
        return []
    with connect_db(DB_PATH) as conn:
        placeholders = ",".join("?" for _ in stock_ids)
        rows = conn.execute(
            f"""
            SELECT DISTINCT stock_id
            FROM monthly_revenues
            WHERE stock_id IN ({placeholders})
              AND revenue_month = ?
            """,
            [*stock_ids, revenue_month],
        ).fetchall()
    covered = {row[0] for row in rows}
    return [stock_id for stock_id in stock_ids if stock_id not in covered]


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def all_market_stock_ids() -> list[str]:
    with connect_db(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT stock_id
            FROM stocks
            WHERE market IN ('TWSE', 'TPEX')
              AND name NOT LIKE '%-DR'
            ORDER BY stock_id
            """
        ).fetchall()
    return [row[0] for row in rows]


def revenue_coverage_status(candidate_limit: int = 300) -> dict[str, object]:
    candidate_ids = candidate_revenue_ids(candidate_limit)
    market_ids = all_market_stock_ids()
    with connect_db(DB_PATH) as conn:
        candidate_count = 0
        if candidate_ids:
            placeholders = ",".join("?" for _ in candidate_ids)
            candidate_count = conn.execute(
                f"SELECT COUNT(DISTINCT stock_id) FROM monthly_revenues WHERE stock_id IN ({placeholders})",
                candidate_ids,
            ).fetchone()[0]
        market_count = 0
        if market_ids:
            placeholders = ",".join("?" for _ in market_ids)
            market_count = conn.execute(
                f"SELECT COUNT(DISTINCT stock_id) FROM monthly_revenues WHERE stock_id IN ({placeholders})",
                market_ids,
            ).fetchone()[0]
    return {
        "candidate_total": len(candidate_ids),
        "candidate_count": candidate_count,
        "market_total": len(market_ids),
        "market_count": market_count,
    }


def failed_branch_ids(days: int = 20, limit: int = 100) -> list[str]:
    with connect_db(DB_PATH) as conn:
        dates = recent_dates(conn, days)
        if not dates:
            return []
        rows = conn.execute(
            """
            SELECT stock_id
            FROM branch_fetch_status
            WHERE trade_date = ?
              AND window_days = ?
              AND source = 'moneydj'
              AND status IN ('failed', 'empty')
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (dates[-1], days, limit),
        ).fetchall()
    ids: list[str] = []
    for row in rows:
        stock_id = str(row[0])
        if stock_id not in ids:
            ids.append(stock_id)
    return ids


def branch_coverage_summary(days: int = 20) -> dict[str, int]:
    with connect_db(DB_PATH) as conn:
        dates = recent_dates(conn, days)
        rows = branch_coverage(conn, dates[-1], days) if dates else []
    return {
        "covered": sum(int(row.get("stocks_with_branch") or 0) for row in rows),
        "attempted": sum(int(row.get("attempted") or 0) for row in rows),
        "empty": sum(int(row.get("empty_count") or 0) for row in rows),
        "failed": sum(int(row.get("failed_count") or 0) for row in rows),
        "not_attempted": sum(int(row.get("not_attempted") or 0) for row in rows),
        "total": sum(int(row.get("total_stocks") or 0) for row in rows),
        "no_branch": sum(int(row.get("no_branch") or row.get("remaining") or 0) for row in rows),
        "remaining": sum(int(row.get("not_attempted") or 0) for row in rows),
    }


def market_data_status() -> dict[str, object]:
    with connect_db(DB_PATH) as conn:
        latest_index = conn.execute(
            "SELECT MAX(date), MAX(updated_at), COUNT(*) FROM market_index_daily WHERE index_code = 'TAIEX'"
        ).fetchone()
        product_rows = conn.execute(
            """
            SELECT product_code, COUNT(DISTINCT date), MAX(date), MAX(updated_at)
            FROM futures_institution_oi
            WHERE product_code IN ('TXF', 'MXF', 'TMF')
            GROUP BY product_code
            """
        ).fetchall()
    products = {
        str(row[0]): {
            "date_count": int(row[1] or 0),
            "latest_date": normalize_time(row[2])[:10],
            "updated_at": normalize_time(row[3]),
        }
        for row in product_rows
    }
    latest_updated = max(
        [normalize_time(latest_index[1] if latest_index else ""), *[str(item["updated_at"]) for item in products.values()]],
        default="",
    )
    return {
        "latest_index_date": normalize_time(latest_index[0] if latest_index else "")[:10],
        "index_count": int(latest_index[2] or 0) if latest_index else 0,
        "products": products,
        "updated_at": latest_updated,
    }


def bound_float(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def moving_average(values: list[float], days: int) -> float | None:
    if len(values) < days:
        return None
    return sum(values[-days:]) / days


def sentiment_label(score: float) -> str:
    if score >= 70:
        return "偏多"
    if score >= 55:
        return "中性偏多"
    if score >= 45:
        return "中性"
    if score >= 30:
        return "中性偏空"
    return "偏空"


def sentiment_multiplier(score: float) -> float:
    if score >= 70:
        return 1.05
    if score >= 55:
        return 1.0
    if score >= 45:
        return 0.95
    if score >= 30:
        return 0.85
    return 0.75


def market_sentiment_payload(limit: int = 120) -> dict[str, object]:
    limit = min(max(int(limit or 120), 30), 5000)
    with connect_db(DB_PATH) as conn:
        index_rows_raw = conn.execute(
            """
            SELECT date, close
            FROM market_index_daily
            WHERE index_code = 'TAIEX'
            ORDER BY date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        futures_rows_raw = conn.execute(
            """
            SELECT date, product_code,
                   MAX(CASE WHEN institution = 'foreign' THEN trade_net END) AS foreign_trade_net,
                   MAX(CASE WHEN institution = 'foreign' THEN oi_net END) AS foreign_oi_net,
                   MAX(CASE WHEN institution = 'trust' THEN trade_net END) AS trust_trade_net,
                   MAX(CASE WHEN institution = 'trust' THEN oi_net END) AS trust_oi_net,
                   MAX(CASE WHEN institution = 'dealer' THEN trade_net END) AS dealer_trade_net,
                   MAX(CASE WHEN institution = 'dealer' THEN oi_net END) AS dealer_oi_net
            FROM futures_institution_oi
            WHERE product_code IN ('TXF', 'MXF', 'TMF')
            GROUP BY date, product_code
            ORDER BY date DESC
            LIMIT ?
            """,
            (limit * 3,),
        ).fetchall()
        margin_rows_raw = conn.execute(
            """
            SELECT date,
                   SUM(margin_balance) AS margin_balance,
                   SUM(short_balance) AS short_balance,
                   SUM(margin_balance - margin_prev_balance) AS margin_change,
                   SUM(short_balance - short_prev_balance) AS short_change
            FROM margin_trades
            GROUP BY date
            ORDER BY date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        updated_at = conn.execute(
            """
            SELECT MAX(updated_at)
            FROM (
                SELECT updated_at FROM market_index_daily
                UNION ALL
                SELECT updated_at FROM futures_institution_oi
                UNION ALL
                SELECT updated_at FROM margin_trades
            )
            """
        ).fetchone()[0]

    index_rows = [{"date": row[0], "close": row[1]} for row in reversed(index_rows_raw)]
    futures_rows = [
        {
            "date": row[0],
            "product_code": row[1],
            "foreign_trade_net": row[2] or 0,
            "foreign_oi_net": row[3] or 0,
            "trust_trade_net": row[4] or 0,
            "trust_oi_net": row[5] or 0,
            "dealer_trade_net": row[6] or 0,
            "dealer_oi_net": row[7] or 0,
        }
        for row in reversed(futures_rows_raw)
    ]
    margin_rows = [
        {
            "date": row[0],
            "margin_balance": row[1] or 0,
            "short_balance": row[2] or 0,
            "margin_change": row[3] or 0,
            "short_change": row[4] or 0,
        }
        for row in reversed(margin_rows_raw)
    ]

    reasons: list[str] = []
    parts = {
        "index_trend": 0.0,
        "foreign_futures": 0.0,
        "futures_trade": 0.0,
        "trust_futures": 0.0,
        "market_margin": 0.0,
        "dealer_hedge": 0.0,
    }
    latest_index = index_rows[-1] if index_rows else {}
    closes = [float(row["close"]) for row in index_rows if row.get("close") is not None]
    ma5 = moving_average(closes, 5)
    ma20 = moving_average(closes, 20)
    ma60 = moving_average(closes, 60)
    close = float(latest_index.get("close") or 0)
    if close and ma20:
        if close >= ma20:
            parts["index_trend"] += 5
            reasons.append("指數站上 MA20")
        else:
            parts["index_trend"] -= 6
            reasons.append("指數跌破 MA20")
    if close and ma20 and ma60:
        if close >= ma20 >= ma60:
            parts["index_trend"] += 7
            reasons.append("中期趨勢偏多")
        elif close < ma20 < ma60:
            parts["index_trend"] -= 6
            reasons.append("中期趨勢偏空")
    elif close and ma5 and ma20:
        if close >= ma5 >= ma20:
            parts["index_trend"] += 4
        elif close < ma5 < ma20:
            parts["index_trend"] -= 4
    parts["index_trend"] = round(bound_float(parts["index_trend"], -12, 12), 2)

    by_product: dict[str, list[dict[str, object]]] = {code: [] for code in FUTURES_PRODUCTS}
    for row in futures_rows:
        by_product.setdefault(str(row["product_code"]), []).append(row)
    weights = {"TXF": 1.0, "MXF": 0.25, "TMF": 0.08}
    weighted_foreign = 0.0
    weighted_foreign_abs = 0.0
    weighted_foreign_trade_5d = 0.0
    weighted_trade_abs = 0.0
    weighted_trust = 0.0
    weighted_trust_abs = 0.0
    weighted_dealer = 0.0
    weighted_dealer_abs = 0.0
    for code, rows in by_product.items():
        if not rows:
            continue
        weight = weights.get(code, 0.1)
        latest = rows[-1]
        foreign_values = [abs(float(item.get("foreign_oi_net") or 0)) for item in rows]
        trade_values = [abs(float(item.get("foreign_trade_net") or 0)) for item in rows]
        trust_values = [abs(float(item.get("trust_oi_net") or 0)) for item in rows]
        dealer_values = [abs(float(item.get("dealer_oi_net") or 0)) for item in rows]
        weighted_foreign += float(latest.get("foreign_oi_net") or 0) * weight
        weighted_foreign_abs += (max(foreign_values) or 1) * weight
        weighted_foreign_trade_5d += sum(float(item.get("foreign_trade_net") or 0) for item in rows[-5:]) * weight
        weighted_trade_abs += max(sum(trade_values[-5:]), 1) * weight
        weighted_trust += float(latest.get("trust_oi_net") or 0) * weight
        weighted_trust_abs += (max(trust_values) or 1) * weight
        weighted_dealer += float(latest.get("dealer_oi_net") or 0) * weight
        weighted_dealer_abs += (max(dealer_values) or 1) * weight

    if weighted_foreign_abs:
        parts["foreign_futures"] = round(bound_float(weighted_foreign / weighted_foreign_abs * 20, -20, 20), 2)
        reasons.append("外資期貨淨部位偏多" if parts["foreign_futures"] > 3 else "外資期貨淨部位偏空" if parts["foreign_futures"] < -3 else "外資期貨接近中性")
    if weighted_trade_abs:
        parts["futures_trade"] = round(bound_float(weighted_foreign_trade_5d / weighted_trade_abs * 10, -10, 10), 2)
        if parts["futures_trade"] < -3:
            reasons.append("外資近 5 日期貨交易偏空")
        elif parts["futures_trade"] > 3:
            reasons.append("外資近 5 日期貨交易偏多")
    if weighted_trust_abs:
        parts["trust_futures"] = round(bound_float(weighted_trust / weighted_trust_abs * 6, -6, 6), 2)
    if weighted_dealer_abs:
        dealer_raw = weighted_dealer / weighted_dealer_abs
        parts["dealer_hedge"] = round(bound_float(dealer_raw * 5, -5, 5), 2)
        if parts["dealer_hedge"] < -3:
            reasons.append("自營商避險空單偏重")

    if margin_rows:
        latest_margin = margin_rows[-1]
        recent_margin_change = sum(float(row.get("margin_change") or 0) for row in margin_rows[-5:])
        recent_short_change = sum(float(row.get("short_change") or 0) for row in margin_rows[-5:])
        latest_margin_balance = max(abs(float(latest_margin.get("margin_balance") or 0)), 1)
        margin_ratio = recent_margin_change / latest_margin_balance * 100
        market_margin_score = 0.0
        if close and len(closes) >= 5 and closes[-1] >= closes[-5] and recent_margin_change > 0:
            market_margin_score -= bound_float(margin_ratio * 2.5, 0, 8)
            reasons.append("指數上漲但融資增加，追價風險上升")
        elif recent_margin_change < 0:
            market_margin_score += bound_float(abs(margin_ratio) * 2.5, 0, 6)
            reasons.append("融資下降，籌碼較乾淨")
        if recent_short_change > 0 and close and len(closes) >= 5 and closes[-1] >= closes[-5]:
            market_margin_score += 2
            reasons.append("融券增加但指數未弱，短線有軋空壓力")
        parts["market_margin"] = round(bound_float(market_margin_score, -10, 10), 2)

    raw_score = 50 + sum(float(value or 0) for value in parts.values())
    score = round(bound_float(raw_score, 0, 100), 2)
    return {
        "score": score,
        "label": sentiment_label(score),
        "risk_multiplier": sentiment_multiplier(score),
        "parts": parts,
        "reasons": reasons[:8],
        "latest_date": latest_index.get("date") or "",
        "latest_close": latest_index.get("close"),
        "ma5": round(ma5, 2) if ma5 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "updated_at": normalize_time(updated_at),
    }


def market_payload(index_code: str = "TAIEX", product_code: str = "TXF", limit: int = 240) -> dict[str, object]:
    product_code = product_code.upper()
    if product_code not in FUTURES_PRODUCTS:
        product_code = "TXF"
    limit = min(max(int(limit or 240), 30), 5000)
    with connect_db(DB_PATH) as conn:
        index_rows = conn.execute(
            """
            SELECT date, open, high, low, close
            FROM market_index_daily
            WHERE index_code = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (index_code, limit),
        ).fetchall()
        futures_rows = conn.execute(
            """
            SELECT date,
                   MAX(CASE WHEN institution = 'foreign' THEN trade_net END) AS foreign_trade_net,
                   MAX(CASE WHEN institution = 'foreign' THEN oi_net END) AS foreign_oi_net,
                   MAX(CASE WHEN institution = 'trust' THEN trade_net END) AS trust_trade_net,
                   MAX(CASE WHEN institution = 'trust' THEN oi_net END) AS trust_oi_net,
                   MAX(CASE WHEN institution = 'dealer' THEN trade_net END) AS dealer_trade_net,
                   MAX(CASE WHEN institution = 'dealer' THEN oi_net END) AS dealer_oi_net
            FROM futures_institution_oi
            WHERE product_code = ?
            GROUP BY date
            ORDER BY date DESC
            LIMIT ?
            """,
            (product_code, limit),
        ).fetchall()
        updated_at = conn.execute(
            """
            SELECT MAX(updated_at)
            FROM (
                SELECT updated_at FROM market_index_daily WHERE index_code = ?
                UNION ALL
                SELECT updated_at FROM futures_institution_oi WHERE product_code = ?
            )
            """,
            (index_code, product_code),
        ).fetchone()[0]
    index_output = [
        {"date": row[0], "open": row[1], "high": row[2], "low": row[3], "close": row[4]}
        for row in reversed(index_rows)
    ]
    futures_output = [
        {
            "date": row[0],
            "foreign_trade_net": row[1],
            "foreign_oi_net": row[2],
            "trust_trade_net": row[3],
            "trust_oi_net": row[4],
            "dealer_trade_net": row[5],
            "dealer_oi_net": row[6],
        }
        for row in reversed(futures_rows)
    ]
    latest_index = index_output[-1] if index_output else {}
    latest_futures = futures_output[-1] if futures_output else {}
    return {
        "summary": {
            "latest_index_date": latest_index.get("date", ""),
            "latest_index_close": latest_index.get("close"),
            "product_code": product_code,
            "product_label": f"{FUTURES_PRODUCTS.get(product_code, product_code)} {product_code}",
            "foreign_oi_net": latest_futures.get("foreign_oi_net"),
            "trust_oi_net": latest_futures.get("trust_oi_net"),
            "dealer_oi_net": latest_futures.get("dealer_oi_net"),
        },
        "index_rows": index_output,
        "futures_rows": futures_output,
        "updated_at": normalize_time(updated_at),
    }


def data_update_times() -> dict[str, str]:
    with connect_db(DB_PATH) as conn:
        ensure_gui_tables(conn)
        watchlist = current_watchlist_ids()
        placeholders = ",".join("?" for _ in watchlist) if watchlist else "''"
        official = conn.execute("SELECT MAX(updated_at) FROM trading_days").fetchone()[0]
        candidate_ids = candidate_revenue_ids(300)
        candidate_placeholders = ",".join("?" for _ in candidate_ids) if candidate_ids else "''"
        candidate_revenue = conn.execute(
            f"SELECT MAX(updated_at) FROM monthly_revenues WHERE stock_id IN ({candidate_placeholders})",
            candidate_ids,
        ).fetchone()[0]
        all_market_revenue = conn.execute("SELECT MAX(updated_at) FROM monthly_revenues").fetchone()[0]
        candidate_revenue_job = conn.execute(
            "SELECT MAX(updated_at) FROM update_job_history WHERE task_id = 'candidate_revenue' AND status = 'done'"
        ).fetchone()[0]
        all_market_revenue_job = conn.execute(
            "SELECT MAX(updated_at) FROM update_job_history WHERE task_id = 'all_market_revenue' AND status = 'done'"
        ).fetchone()[0]
        watchlist_revenue = conn.execute(
            f"SELECT MAX(updated_at) FROM monthly_revenues WHERE stock_id IN ({placeholders})",
            watchlist,
        ).fetchone()[0]
        news = conn.execute(
            f"SELECT MAX(fetched_at) FROM stock_news WHERE stock_id IN ({placeholders})",
            watchlist,
        ).fetchone()[0]
        us_news = conn.execute("SELECT MAX(fetched_at) FROM us_stock_news").fetchone()[0]
        market_data = max(
            str(conn.execute("SELECT MAX(updated_at) FROM market_index_daily").fetchone()[0] or ""),
            str(conn.execute("SELECT MAX(updated_at) FROM futures_institution_oi").fetchone()[0] or ""),
        )
    expected_revenue_month = expected_latest_revenue_month()
    candidate_revenue_time = max(str(candidate_revenue or ""), str(watchlist_revenue or ""))
    if (
        candidate_ids
        and not revenue_missing_stock_ids(candidate_ids)
        and not revenue_missing_month_stock_ids(candidate_ids, expected_revenue_month)
    ):
        candidate_revenue_time = max(candidate_revenue_time, str(candidate_revenue_job or ""))
    market_ids = all_market_stock_ids()
    all_market_revenue_time = str(all_market_revenue or "")
    if (
        market_ids
        and not revenue_missing_stock_ids(market_ids)
        and not revenue_missing_month_stock_ids(market_ids, expected_revenue_month)
    ):
        all_market_revenue_time = max(all_market_revenue_time, str(all_market_revenue_job or ""))
    times = {
        "official_scan": normalize_time(official),
        "market_data": normalize_time(market_data),
        "candidate_revenue": normalize_time(candidate_revenue_time),
        "all_market_revenue": normalize_time(all_market_revenue_time),
        "quality_check": normalize_time(max(str(official or ""), str(all_market_revenue_time or ""))),
        "watchlist_news": normalize_time(news),
        "us_news_obsidian": normalize_time(us_news),
        "sync_ci_us_news": normalize_time(ci_us_news_status().get("generated_at", "")),
    }
    times["all_data"] = max((value for value in times.values() if value), default="")
    return times


def persist_job(job: dict[str, object]) -> None:
    with connect_db(DB_PATH) as conn:
        ensure_gui_tables(conn)
        conn.execute(
            """
            INSERT INTO update_job_history (
                id, task_id, title, status, created_at, started_at,
                finished_at, updated_at, current_step, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                updated_at = excluded.updated_at,
                current_step = excluded.current_step,
                error = excluded.error
            """,
            (
                job.get("id"),
                job.get("task_id"),
                job.get("title"),
                job.get("status"),
                job.get("created_at"),
                job.get("started_at"),
                job.get("finished_at"),
                job.get("updated_at"),
                job.get("current_step"),
                job.get("error"),
            ),
        )
        conn.commit()


def persisted_jobs(limit: int = 8) -> list[dict[str, object]]:
    with connect_db(DB_PATH) as conn:
        ensure_gui_tables(conn)
        rows = conn.execute(
            """
            SELECT id, task_id, title, status, created_at, started_at,
                   finished_at, updated_at, current_step, error
            FROM update_job_history
            ORDER BY updated_at DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    keys = [
        "id",
        "task_id",
        "title",
        "status",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
        "current_step",
        "error",
    ]
    return [dict(zip(keys, row, strict=True)) for row in rows]


def recent_jobs(limit: int = 8) -> list[dict[str, object]]:
    with UPDATE_LOCK:
        jobs = sorted(UPDATE_JOBS.values(), key=lambda job: str(job.get("created_at", "")), reverse=True)
        output = []
        for job in jobs[:limit]:
            item = dict(job)
            item.pop("log", None)
            output.append(item)
        return output


def running_job() -> dict[str, object] | None:
    with UPDATE_LOCK:
        for job in UPDATE_JOBS.values():
            if job.get("status") in {"queued", "running"}:
                item = dict(job)
                item.pop("log", None)
                return item
    return None


def parse_local_time(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace(" ", "T")[:19])
    except ValueError:
        return None


def branch_update_health() -> dict[str, object]:
    with connect_db(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*), MAX(updated_at)
            FROM branch_fetch_status
            WHERE window_days = 20
              AND source = 'moneydj'
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        distinct_count, latest_topn = conn.execute(
            """
            SELECT COUNT(DISTINCT stock_id), MAX(updated_at)
            FROM broker_branch_topn
            WHERE window_days = 20
              AND source = 'moneydj'
            """
        ).fetchone()
    latest_status = max((str(row[2] or "") for row in rows), default="")
    latest_write = normalize_time(max([latest_status, str(latest_topn or "")]))
    last_dt = parse_local_time(latest_write)
    stale_minutes = None
    if last_dt:
        stale_minutes = max(0, int((dt.datetime.now() - last_dt).total_seconds() // 60))
    summary = "、".join(f"{status}:{count}" for status, count, _updated in rows)
    if distinct_count:
        summary = f"{summary}，已抓 {distinct_count} 檔" if summary else f"已抓 {distinct_count} 檔"
    return {
        "last_write_at": latest_write,
        "stale_minutes": stale_minutes,
        "status_summary": summary,
        "possibly_stuck": stale_minutes is not None and stale_minutes >= 10,
    }


def update_task_state() -> dict[str, object]:
    memory_jobs = recent_jobs()
    persisted = persisted_jobs()
    by_id: dict[str, dict[str, object]] = {str(job["id"]): job for job in persisted}
    for job in memory_jobs:
        by_id[str(job["id"])] = job
    jobs = sorted(by_id.values(), key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)[:8]
    active_job = running_job()
    active_health = branch_update_health() if active_job and "區間分點" in str(active_job.get("current_step") or "") else {}
    if active_health:
        active_job["health"] = active_health
        active_job["possible_stuck"] = bool(active_health.get("possibly_stuck"))
    if active_job is None:
        for job in jobs:
            if job.get("status") in {"queued", "running"}:
                job["status"] = "interrupted"
                job["current_step"] = "上次執行未正常結束；目前沒有背景更新在跑。"
    latest = jobs[0] if jobs else None
    latest_by_task: dict[str, dict[str, object]] = {}
    for job in jobs:
        task_id = str(job.get("task_id") or "")
        if task_id and task_id not in latest_by_task:
            latest_by_task[task_id] = job
    data_times = data_update_times()
    ci_news = ci_us_news_status()
    latest_data_updated_at = max((value for value in data_times.values() if value), default="")
    all_data_progress = all_data_task_progress(active_job)
    tasks = []
    for task in UPDATE_TASKS:
        task_item = dict(task)
        latest_task_job = latest_by_task.get(str(task["id"]))
        task_item["last_job_status"] = latest_task_job.get("status") if latest_task_job else ""
        task_item["last_job_updated_at"] = latest_task_job.get("updated_at") if latest_task_job else ""
        task_item["last_data_updated_at"] = data_times.get(str(task["id"]), "")
        task_item["last_updated_at"] = max(
            str(task_item["last_job_updated_at"] or ""),
            str(task_item["last_data_updated_at"] or ""),
        )
        if str(task["id"]) == "sync_ci_us_news":
            task_item["extra_status"] = str(ci_news.get("sync_note") or "")
        if str(task["id"]) in {"watchlist_news", "us_news_obsidian", "sync_ci_us_news"}:
            task_item["warn_hours"] = 12
            task_item["bad_hours"] = 36
        if str(task["id"]) == "market_data":
            market_status = market_data_status()
            product_parts = []
            for code, label in FUTURES_PRODUCTS.items():
                item = (market_status.get("products") or {}).get(code) or {}
                product_parts.append(f"{label} {item.get('date_count', 0)} 日")
            task_item["extra_status"] = (
                f"加權指數 {market_status.get('index_count', 0)} 日"
                + ("；" + "、".join(product_parts) if product_parts else "")
            )
        if str(task["id"]) in {"candidate_revenue", "all_market_revenue"}:
            revenue_status = revenue_coverage_status(300)
            if str(task["id"]) == "candidate_revenue":
                missing_count = max(0, int(revenue_status["candidate_total"]) - int(revenue_status["candidate_count"]))
                expected_month = expected_latest_revenue_month()
                latest_missing = len(revenue_missing_month_stock_ids(candidate_revenue_ids(300), expected_month))
                task_item["extra_status"] = (
                    f"候選股已補 {revenue_status['candidate_count']} / {revenue_status['candidate_total']} 檔"
                    + (f"，待補 {missing_count} 檔" if missing_count else "")
                    + f"；{expected_month} 缺 {latest_missing} 檔"
                )
            else:
                missing_count = max(0, int(revenue_status["market_total"]) - int(revenue_status["market_count"]))
                expected_month = expected_latest_revenue_month()
                latest_missing = len(revenue_missing_month_stock_ids(all_market_stock_ids(), expected_month))
                task_item["extra_status"] = (
                    f"全市場已補 {revenue_status['market_count']} / {revenue_status['market_total']} 檔"
                    + (f"，待補 {missing_count} 檔" if missing_count else "")
                    + f"；{expected_month} 缺 {latest_missing} 檔"
                )
        if str(task["id"]) == "quality_check":
            quality = score_input_coverage(DB_PATH, 20)
            coverage = quality.get("coverage") or {}
            parts = []
            for key in ("price", "institutional", "margin", "revenue"):
                item = coverage.get(key) or {}
                text = f"{item.get('label', key)} {item.get('covered', 0)} / {quality.get('total', 0)}"
                if key == "margin" and item.get("missing"):
                    text += f"（未列信用交易 {item.get('missing', 0)}）"
                elif item.get("missing"):
                    text += f"（缺 {item.get('missing', 0)}）"
                parts.append(text)
            task_item["extra_status"] = "；".join(parts)
        if active_job:
            progress = all_data_progress.get(str(task["id"]))
            if progress:
                task_item.update(progress)
            elif str(active_job.get("task_id") or "") == str(task["id"]):
                task_item["run_state"] = "running"
                task_item["run_label"] = "執行中"
        tasks.append(task_item)
    return {
        "tasks": tasks,
        "running": active_job is not None,
        "latest_job": latest,
        "latest_data_updated_at": latest_data_updated_at,
        "jobs": jobs,
    }


def all_data_task_progress(active_job: dict[str, object] | None) -> dict[str, dict[str, str]]:
    if not active_job or active_job.get("task_id") != "all_data":
        return {}
    current = str(active_job.get("current_step") or "")
    ranges = [
        ("official_scan", "官方行情與排行", ["更新官方行情與 20 日排行", "重算 5 日排行"]),
        ("market_data", "大盤指數與期貨多空", ["補齊大盤指數與期貨三大法人多空"]),
        ("all_market_revenue", "全市場營收補齊", ["補齊全市場缺漏營收", "補最新月份營收", "全市場營收已補齊", "最新月份營收已補齊", "重算營收策略排行"]),
        ("watchlist_news", "自選股新聞標題", ["更新 Yahoo 股市 RSS 標題"]),
        ("sync_ci_us_news", "同步 GitHub 新聞到本機", ["匯入 docs/data/ci_us_news.json 並同步 Obsidian"]),
        ("us_news_obsidian", "本機抓取美股新聞", ["更新美股新聞並同步 Obsidian"]),
        ("quality_check", "分數資料完整性檢查", ["檢查基礎分資料完整性"]),
        ("static_export", "匯出 GitHub Pages 靜態資料", ["匯出 GitHub Pages 靜態資料"]),
    ]
    flat: list[tuple[str, str, str]] = []
    for task_id, title, labels in ranges:
        for label in labels:
            flat.append((task_id, title, label))
    current_index = next(
        (idx for idx, (_task_id, _title, label) in enumerate(flat) if current == label or current.startswith(label)),
        -1,
    )
    if current_index < 0 and current == "完成":
        current_index = len(flat)
    output: dict[str, dict[str, str]] = {"all_data": {"run_state": "running", "run_label": "執行中"}}
    for task_id, title, labels in ranges:
        step_indices = [idx for idx, (step_task_id, _title, _label) in enumerate(flat) if step_task_id == task_id]
        if current_index >= len(flat):
            state, label = "done", "已完成"
        elif current_index in step_indices:
            state, label = "running", "執行中"
        elif current_index > max(step_indices):
            state, label = "done", "已完成"
        else:
            state, label = "pending", "等待中"
        output[task_id] = {
            "run_state": state,
            "run_label": label,
            "run_group": title,
        }
    return output


def top_stock_ids_from_report(days: int, limit: int) -> list[str]:
    rows = read_csv(ranking_path(days, "total_score"))
    ids: list[str] = []
    for row in rows:
        stock_id = (row.get("stock_id") or "").strip()
        if stock_id and stock_id not in ids:
            ids.append(stock_id)
        if len(ids) >= limit:
            break
    return ids


def update_steps(task_id: str, days: int) -> tuple[str, list[tuple[str, list[str]]]]:
    py = sys.executable
    watchlist = ",".join(current_watchlist_ids())
    if task_id == "all_data":
        steps: list[tuple[str, list[str]]] = []
        for child_task in ("official_scan", "market_data", "all_market_revenue", "watchlist_news", "sync_ci_us_news", "us_news_obsidian"):
            _title, child_steps = update_steps(child_task, days)
            steps.extend(child_steps)
        steps.append(
            (
                "檢查基礎分資料完整性",
                [py, "-m", "stock_chip.quality", "--days", "20"],
            )
        )
        steps.append(
            (
                "匯出 GitHub Pages 靜態資料",
                [py, "-m", "stock_chip.export_static", "--out", "docs"],
            )
        )
        return ("一鍵更新全部資料", steps)
    if task_id == "official_scan":
        return (
            "官方行情與排行",
            [
                (
                    "更新官方行情與 20 日排行",
                    [py, "-m", "stock_chip.daily", "--days", "20", "--limit", "100", "--watchlist", watchlist, "--exclude-esb"],
                ),
                (
                    "重算 5 日排行",
                    [py, "-m", "stock_chip.scan", "--days", "5", "--limit", "100", "--watchlist", watchlist],
                ),
            ],
        )
    if task_id == "market_data":
        return (
            "大盤指數與期貨多空",
            [
                (
                    "補齊大盤指數與期貨三大法人多空",
                    [py, "-m", "stock_chip.market", "--years", "3", "--products", "TXF,MXF,TMF", "--sleep", "0.25"],
                )
            ],
        )
    if task_id == "candidate_revenue":
        all_candidate_ids = candidate_revenue_ids(300)
        expected_month = expected_latest_revenue_month()
        missing_ids = list(
            dict.fromkeys(
                revenue_missing_stock_ids(all_candidate_ids)
                + revenue_missing_month_stock_ids(all_candidate_ids, expected_month)
            )
        )
        candidate_ids = ",".join(missing_ids)
        steps: list[tuple[str, list[str]]] = []
        if candidate_ids:
            steps.append(
                (
                    f"補候選股缺漏營收或最新月份 {len(missing_ids)} 檔",
                    [
                        py,
                        "-m",
                        "stock_chip.revenue",
                        "--months",
                        "24",
                        "--watchlist",
                        candidate_ids,
                        "--sleep",
                        "0.15",
                        "--retries",
                        "2",
                        "--timeout",
                        "20",
                    ],
                )
            )
        else:
            steps.append(
                (
                    f"候選股營收與最新月份 {expected_month} 已補齊，略過抓取",
                    [py, "-c", "print('candidate revenue already covered')"],
                )
            )
        steps.extend(
            [
                (
                    "重算營收策略排行",
                    [py, "-m", "stock_chip.scan", "--days", "5", "--limit", "100", "--watchlist", watchlist],
                ),
                (
                    "重算 20 日排行",
                    [py, "-m", "stock_chip.scan", "--days", "20", "--limit", "100", "--watchlist", watchlist],
                )
            ]
        )
        return (
            "候選股營收",
            steps,
        )
    if task_id == "all_market_revenue":
        market_ids = all_market_stock_ids()
        all_missing_ids = revenue_missing_stock_ids(market_ids)
        expected_month = expected_latest_revenue_month()
        latest_missing_ids = [
            stock_id
            for stock_id in revenue_missing_month_stock_ids(market_ids, expected_month)
            if stock_id not in set(all_missing_ids)
        ]
        steps = []
        for batch_index, batch_ids in enumerate(chunked(all_missing_ids, 50), start=1):
            steps.append(
                (
                    f"補齊全市場缺漏營收 {len(all_missing_ids)} 檔 batch {batch_index}",
                    [
                        py,
                        "-m",
                        "stock_chip.revenue",
                        "--months",
                        "24",
                        "--watchlist",
                        ",".join(batch_ids),
                        "--sleep",
                        "0.05",
                        "--retries",
                        "1",
                        "--timeout",
                        "10",
                    ],
                )
            )
        for batch_index, batch_ids in enumerate(chunked(latest_missing_ids, 50), start=1):
            steps.append(
                (
                    f"補最新月份營收 {expected_month} 缺漏 {len(latest_missing_ids)} 檔 batch {batch_index}",
                    [
                        py,
                        "-m",
                        "stock_chip.revenue",
                        "--months",
                        "3",
                        "--watchlist",
                        ",".join(batch_ids),
                        "--sleep",
                        "0.05",
                        "--retries",
                        "1",
                        "--timeout",
                        "8",
                    ],
                )
            )
        if not latest_missing_ids:
            steps.append(
                (
                    f"最新月份營收已補齊，略過抓取 {expected_month}",
                    [py, "-c", f"print('latest revenue month already covered: {expected_month}')"],
                )
            )
        steps.extend(
            [
                (
                    "重算營收策略排行",
                    [py, "-m", "stock_chip.scan", "--days", "5", "--limit", "100", "--watchlist", watchlist],
                ),
                (
                    "重算 20 日排行",
                    [py, "-m", "stock_chip.scan", "--days", "20", "--limit", "100", "--watchlist", watchlist],
                )
            ]
        )
        return (
            "全市場營收補齊",
            steps,
        )
    if task_id == "quality_check":
        return (
            "分數資料完整性檢查",
            [
                (
                    "檢查基礎分資料完整性",
                    [py, "-m", "stock_chip.quality", "--days", "20"],
                )
            ],
        )
    if task_id == "watchlist_news":
        return (
            "自選股新聞標題",
            [
                (
                    "更新 Yahoo 股市 RSS 標題",
                    [
                        py,
                        "-m",
                        "stock_chip.news",
                        "--watchlist",
                        watchlist,
                        "--limit",
                        "5",
                        "--sleep",
                        "0.5",
                    ],
                )
            ],
        )
    if task_id == "us_news_obsidian":
        return (
            "本機抓取美股新聞",
            [
                (
                    "更新美股新聞並同步 Obsidian",
                    [
                        py,
                        "-m",
                        "stock_chip.us_news",
                        "--symbols",
                        "AAPL,MSFT,NVDA,TSLA",
                        "--limit",
                        "25",
                        "--market-only",
                        "--no-ollama",
                    ],
                )
            ],
        )
    if task_id == "sync_ci_us_news":
        return (
            "同步 GitHub 新聞到本機",
            [
                (
                    "下載 GitHub 最新新聞 JSON",
                    [
                        py,
                        "-c",
                        (
                            "from pathlib import Path; import requests; "
                            "url='https://raw.githubusercontent.com/wenyen-hsu/stock/main/docs/data/ci_us_news.json'; "
                            "resp=requests.get(url, timeout=30); resp.raise_for_status(); "
                            "path=Path('docs/data/ci_us_news.json'); path.parent.mkdir(parents=True, exist_ok=True); "
                            "path.write_text(resp.text, encoding='utf-8'); "
                            "print(f'downloaded {len(resp.text)} bytes from {url}')"
                        ),
                    ],
                ),
                (
                    "匯入 docs/data/ci_us_news.json 並同步 Obsidian",
                    [
                        py,
                        "-m",
                        "stock_chip.import_us_news_static",
                        "--json",
                        "docs/data/ci_us_news.json",
                        "--db",
                        str(DB_PATH),
                    ],
                )
            ],
        )
    raise ValueError(f"未知更新任務：{task_id}")


def append_job_log(job_id: str, text: str) -> None:
    updated_job: dict[str, object] | None = None
    with UPDATE_LOCK:
        job = UPDATE_JOBS.get(job_id)
        if not job:
            return
        log = str(job.get("log", ""))
        job["log"] = (log + text)[-20000:]
        job["updated_at"] = now_text()
        updated_job = dict(job)
    persist_job(updated_job)


def run_update_job(job_id: str, steps: list[tuple[str, list[str]]]) -> None:
    with UPDATE_LOCK:
        job = UPDATE_JOBS[job_id]
        job["status"] = "running"
        job["started_at"] = now_text()
        job["updated_at"] = job["started_at"]
        running_snapshot = dict(job)
    persist_job(running_snapshot)
    step_errors: list[str] = []
    try:
        for label, args in steps:
            with UPDATE_LOCK:
                job = UPDATE_JOBS[job_id]
                job["current_step"] = label
                job["updated_at"] = now_text()
                step_snapshot = dict(job)
            persist_job(step_snapshot)
            proc = subprocess.Popen(
                args,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            with UPDATE_LOCK:
                job = UPDATE_JOBS[job_id]
                job["current_process_pid"] = proc.pid
                job["step_started_at"] = now_text()
                pid_snapshot = dict(job)
            persist_job(pid_snapshot)
            recent_output = ""
            step_started = time.monotonic()
            assert proc.stdout is not None
            while True:
                if time.monotonic() - step_started > 60 * 60:
                    proc.kill()
                    remaining = proc.stdout.read()
                    if remaining:
                        recent_output = (recent_output + remaining)[-2000:]
                        append_job_log(job_id, remaining[-2000:])
                    proc.wait()
                    with UPDATE_LOCK:
                        job = UPDATE_JOBS[job_id]
                        job["current_process_pid"] = ""
                        timeout_snapshot = dict(job)
                    persist_job(timeout_snapshot)
                    message = f"{label} 逾時，已停止子程序"
                    if str(job.get("task_id") or "") == "all_data" and not label.startswith("更新官方行情"):
                        step_errors.append(message)
                        append_job_log(job_id, message + "\n")
                        break
                    raise RuntimeError(message)
                ready, _w, _x = select.select([proc.stdout], [], [], 1)
                if ready:
                    line = proc.stdout.readline()
                    if line:
                        recent_output = (recent_output + line)[-2000:]
                        append_job_log(job_id, line[-1000:])
                        continue
                if proc.poll() is not None:
                    remaining = proc.stdout.read()
                    if remaining:
                        recent_output = (recent_output + remaining)[-2000:]
                        append_job_log(job_id, remaining[-2000:])
                    break
            if step_errors and step_errors[-1].startswith(label):
                continue
            with UPDATE_LOCK:
                job = UPDATE_JOBS[job_id]
                job["current_process_pid"] = ""
            if proc.returncode != 0:
                message = f"{label} 失敗，exit code {proc.returncode}"
                if recent_output:
                    append_job_log(job_id, recent_output[-2000:])
                if str(job.get("task_id") or "") == "all_data" and not label.startswith("更新官方行情"):
                    step_errors.append(message)
                    append_job_log(job_id, message + "\n")
                    continue
                raise RuntimeError(message)
            with UPDATE_LOCK:
                job = UPDATE_JOBS[job_id]
                job["updated_at"] = now_text()
                step_done_snapshot = dict(job)
            persist_job(step_done_snapshot)
        with UPDATE_LOCK:
            job = UPDATE_JOBS[job_id]
            job["status"] = "failed" if step_errors else "done"
            job["finished_at"] = now_text()
            job["updated_at"] = job["finished_at"]
            job["current_step"] = "完成（部分失敗）" if step_errors else "完成"
            if step_errors:
                job["error"] = "；".join(step_errors[:5])
            done_snapshot = dict(job)
        persist_job(done_snapshot)
    except Exception as exc:
        with UPDATE_LOCK:
            job = UPDATE_JOBS[job_id]
            job["status"] = "failed"
            job["finished_at"] = now_text()
            job["updated_at"] = job["finished_at"]
            job["error"] = str(exc)
            failed_snapshot = dict(job)
        persist_job(failed_snapshot)


def start_update_task(task_id: str, days: int) -> dict[str, object]:
    current = running_job()
    if current:
        raise RuntimeError(f"已有任務執行中：{current.get('title')}")
    title, steps = update_steps(task_id, days)
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id,
        "task_id": task_id,
        "title": title,
        "status": "queued",
        "created_at": now_text(),
        "started_at": "",
        "finished_at": "",
        "updated_at": now_text(),
        "current_step": "排隊中",
        "error": "",
        "log": "",
    }
    with UPDATE_LOCK:
        UPDATE_JOBS[job_id] = job
    persist_job(job)
    thread = threading.Thread(target=run_update_job, args=(job_id, steps), daemon=True)
    thread.start()
    return dict(job)


def cancel_running_update() -> dict[str, object]:
    job = running_job()
    with UPDATE_LOCK:
        if not job:
            return {"cancelled": False, "message": "目前沒有執行中的更新。"}
        job_id = str(job.get("id") or "")
        pid = int(job.get("current_process_pid") or 0)
        live_job = UPDATE_JOBS.get(job_id)
        if live_job:
            live_job["current_step"] = "停止中"
            live_job["updated_at"] = now_text()
            snapshot = dict(live_job)
        else:
            snapshot = dict(job)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    persist_job(snapshot)
    return {"cancelled": True, "pid": pid, "job": snapshot}


def run_git(args: list[str], timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} 失敗")
    return proc.stdout.strip()


def pages_url_from_remote(remote_url: str) -> str:
    text = remote_url.strip()
    if not text:
        return ""
    owner_repo = ""
    if text.startswith("git@github.com:"):
        owner_repo = text.removeprefix("git@github.com:")
    elif "github.com/" in text:
        owner_repo = text.split("github.com/", 1)[1]
    owner_repo = owner_repo.removesuffix(".git").strip("/")
    parts = owner_repo.split("/")
    if len(parts) >= 2:
        return f"https://{parts[0]}.github.io/{parts[1]}/"
    return ""


def git_changed_count(paths: list[str]) -> int:
    output = run_git(["status", "--porcelain", "--", *paths])
    return len([line for line in output.splitlines() if line.strip()])


def merge_latest_remote_for_publish(branch: str) -> str:
    if not branch:
        raise RuntimeError("目前不在任何 Git branch，無法發布")
    run_git(["fetch", "origin", branch], timeout=10 * 60)
    remote_ref = f"origin/{branch}"
    ahead = subprocess.run(
        ["git", "merge-base", "--is-ancestor", remote_ref, "HEAD"],
        cwd=ROOT,
        check=False,
    )
    if ahead.returncode == 0:
        return "本機已包含遠端最新 commit。"
    merge = subprocess.run(
        ["git", "merge", "--no-edit", remote_ref],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10 * 60,
        check=False,
    )
    if merge.returncode == 0:
        return (merge.stdout or "").strip() or "已合併遠端最新 commit。"
    status = run_git(["status", "--porcelain"])
    unmerged = [line for line in status.splitlines() if line[:2] in {"UU", "AA", "DD", "DU", "UD", "UA", "AU"}]
    if unmerged == ["UU docs/data/us_news.json"]:
        run_git(["checkout", "--theirs", "docs/data/us_news.json"])
        run_git(["add", "docs/data/us_news.json"])
        run_git(["commit", "-m", "Merge latest CI news before static publish"], timeout=120)
        return "已自動採用 GitHub Action 最新新聞檔並完成合併。"
    detail = (merge.stderr or merge.stdout or "").strip()
    raise RuntimeError(detail or "合併遠端最新 commit 失敗")


def static_publish_status() -> dict[str, object]:
    meta_path = ROOT / "docs" / "data" / "meta.json"
    meta: dict[str, object] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    remote = run_git(["config", "--get", "remote.origin.url"]) if (ROOT / ".git").exists() else ""
    return {
        "git_repo": (ROOT / ".git").exists(),
        "branch": run_git(["branch", "--show-current"]) if (ROOT / ".git").exists() else "",
        "remote": remote,
        "pages_url": pages_url_from_remote(remote),
        "commit": run_git(["rev-parse", "--short", "HEAD"]) if (ROOT / ".git").exists() else "",
        "exported_at": meta.get("exported_at", ""),
        "latest_date": meta.get("latest_date", ""),
        "static_stock_count": meta.get("static_stock_count", 0),
        "ci_news": ci_us_news_status(),
        "docs_changed": git_changed_count(["docs"]) if (ROOT / ".git").exists() else 0,
        "reports_changed": git_changed_count(["reports"]) if (ROOT / ".git").exists() else 0,
    }


def ci_us_news_status() -> dict[str, object]:
    path = ROOT / "docs" / "data" / "ci_us_news.json"
    if not path.exists():
        path = ROOT / "docs" / "data" / "us_news.json"
    payload: dict[str, object] = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if path.exists() else ""
    generated_at = payload.get("generated_at", "") if isinstance(payload, dict) else ""
    generated_at = normalize_time(generated_at) or mtime
    local_latest = ""
    try:
        with connect_db(DB_PATH) as conn:
            local_latest = normalize_time(conn.execute("SELECT MAX(fetched_at) FROM us_stock_news").fetchone()[0])
    except Exception:
        local_latest = ""
    synced = bool(generated_at and local_latest and local_latest >= generated_at)
    if not path.exists():
        sync_note = "尚未產生 GitHub CI 新聞檔。"
    elif synced:
        sync_note = f"本機已同步到 GitHub 新聞；CI {generated_at}，本機 {local_latest}。"
    elif local_latest:
        sync_note = f"本機可能落後 GitHub 新聞；CI {generated_at}，本機 {local_latest}。"
    else:
        sync_note = f"本機尚未匯入 GitHub 新聞；CI {generated_at}。"
    return {
        "path": str(path),
        "exists": path.exists(),
        "generated_at": generated_at,
        "row_count": len(rows) if isinstance(rows, list) else 0,
        "fetched_count": payload.get("fetched_count", 0) if isinstance(payload, dict) else 0,
        "mtime": mtime,
        "local_latest": local_latest,
        "synced": synced,
        "sync_note": sync_note,
    }


def filter_ci_us_news_payload(
    payload: dict[str, object],
    industry: str = "",
    source: str = "",
    query: str = "",
    limit: int = 200,
) -> tuple[list[dict[str, object]], list[str]]:
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    q = query.strip().lower()
    filtered: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if industry and row.get("industry") != industry:
            continue
        if source and row.get("source") != source:
            continue
        if q:
            text = " ".join(str(row.get(key) or "") for key in ("title", "summary", "reason", "source", "industry")).lower()
            if q not in text:
                continue
        filtered.append(row)
        if len(filtered) >= limit:
            break
    sources = sorted({str(row.get("source") or "") for row in rows if isinstance(row, dict) and row.get("source")})
    return filtered, sources


def load_ci_us_news_preview(industry: str = "", source: str = "", query: str = "", limit: int = 200) -> dict[str, object]:
    path = ROOT / "docs" / "data" / "ci_us_news.json"
    fallback = False
    if not path.exists():
        path = ROOT / "docs" / "data" / "us_news.json"
        fallback = True
    payload: dict[str, object] = {}
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    filtered, sources = filter_ci_us_news_payload(payload, industry=industry, source=source, query=query, limit=limit)
    return {
        "path": str(path),
        "fallback": fallback,
        "generated_at": payload.get("generated_at", "") if isinstance(payload, dict) else "",
        "row_count": payload.get("row_count", len(rows)) if isinstance(payload, dict) else len(rows),
        "fetched_count": payload.get("fetched_count", 0) if isinstance(payload, dict) else 0,
        "rows": filtered,
        "sources": sources,
        "source_mode": "local",
        "source_url": str(path),
    }


def load_ci_us_news_live(industry: str = "", source: str = "", query: str = "", limit: int = 200) -> dict[str, object]:
    request = Request(
        CI_US_NEWS_PAGES_URL,
        headers={
            "User-Agent": "stock-chip-local-preview/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        payload = {}
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        rows = []
    filtered, sources = filter_ci_us_news_payload(payload, industry=industry, source=source, query=query, limit=limit)
    return {
        "path": CI_US_NEWS_PAGES_URL,
        "fallback": False,
        "generated_at": payload.get("generated_at", ""),
        "row_count": payload.get("row_count", len(rows)),
        "fetched_count": payload.get("fetched_count", 0),
        "rows": filtered,
        "sources": sources,
        "source_mode": "github_pages",
        "source_url": CI_US_NEWS_PAGES_URL,
        "site_url": CI_US_NEWS_SITE_URL,
    }


def export_static_pages() -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-m", "stock_chip.export_static", "--out", "docs"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10 * 60,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or "匯出靜態頁失敗")
    export_message = proc.stdout.strip() or "已匯出 docs/ 靜態資料。"
    return {
        "message": f"匯出成功；目前只有本機 docs/ 已更新，尚未發布到 GitHub Pages。\n{export_message}",
        "status": static_publish_status(),
    }


def publish_static_pages() -> dict[str, object]:
    branch = run_git(["branch", "--show-current"])
    export_result = export_static_pages()
    run_git(["add", "docs", "reports"])
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", "docs", "reports"],
        cwd=ROOT,
        check=False,
    )
    if staged.returncode == 0:
        status = static_publish_status()
        return {
            "message": f"沒有靜態資料變更需要發布。{export_result['message']}",
            "status": status,
            "published": False,
        }
    if staged.returncode not in {0, 1}:
        raise RuntimeError("檢查 staged 變更失敗")
    message = f"Update static stock data {now_text()}"
    if staged.returncode == 1:
        run_git(["commit", "-m", message], timeout=120)
    pre_sync = merge_latest_remote_for_publish(branch)
    try:
        run_git(["push", "origin", branch], timeout=10 * 60)
        retry_note = ""
    except RuntimeError as exc:
        if "fetch first" not in str(exc) and "non-fast-forward" not in str(exc):
            raise
        retry_note = merge_latest_remote_for_publish(branch)
        run_git(["push", "origin", branch], timeout=10 * 60)
    status = static_publish_status()
    notes = "\n".join(part for part in (pre_sync, retry_note) if part)
    return {
        "message": f"已發布到 GitHub Pages：{status.get('pages_url') or '-'}\n{notes}",
        "status": status,
        "published": True,
    }


def refresh_stock_section(stock_id: str, section: str, days: int) -> dict[str, object]:
    stock_id = stock_id.strip()
    if not stock_id:
        raise ValueError("缺少股票代號")
    with connect_db(DB_PATH) as conn:
        exists = conn.execute("SELECT 1 FROM stocks WHERE stock_id = ?", (stock_id,)).fetchone()
    if not exists:
        raise ValueError(f"找不到股票 {stock_id}")

    if section == "daily":
        result = ensure_stock_data(stock_id, days)
        return {"section": section, "stock_id": stock_id, **result}

    if section == "revenue":
        result = run_revenue(
            db_path=DB_PATH,
            stock_ids=[stock_id],
            months=24,
            sleep_seconds=0,
        )
        return {"section": section, "stock_id": stock_id, **result}

    if section == "branch_top":
        result = run_branch(
            db_path=DB_PATH,
            output_dir=REPORTS_DIR,
            days=days,
            top_n=10,
            watchlist=[stock_id],
            sleep_seconds=0,
            retry_sleeps=(2, 5),
        )
        return {"section": section, "stock_id": stock_id, **result}

    if section == "branch_all":
        top_result = run_branch(
            db_path=DB_PATH,
            output_dir=REPORTS_DIR,
            days=days,
            top_n=10,
            watchlist=[stock_id],
            sleep_seconds=0,
            retry_sleeps=(2, 5),
        )
        daily_result = run_branch_daily(
            db_path=DB_PATH,
            days=days,
            top_n=80,
            stock_ids=[stock_id],
            sleep_seconds=0.8,
            retry_sleeps=(2, 5),
            only_missing=False,
        )
        return {
            "section": section,
            "stock_id": stock_id,
            "branch_top": top_result,
            "branch_daily": daily_result,
        }

    if section == "branch_daily":
        result = run_branch_daily(
            db_path=DB_PATH,
            days=days,
            top_n=80,
            stock_ids=[stock_id],
            sleep_seconds=0.8,
            retry_sleeps=(2, 5),
            only_missing=False,
        )
        return {"section": section, "stock_id": stock_id, **result}

    raise ValueError(f"未知單檔更新項目：{section}")


def resolve_official_stock(query: str, date: dt.date) -> tuple[str, str, str]:
    text = query.strip()
    if not text:
        raise ValueError("請輸入股票代號或名稱")
    candidates = [
        *fetch_twse_prices_all(date, stock_only=True),
        *fetch_tpex_prices_all(date, stock_only=True),
    ]
    exact = [
        row for row in candidates
        if row.stock_id == text or row.name == text
    ]
    partial = [
        row for row in candidates
        if text in row.stock_id or text in row.name
    ]
    matches = exact or partial
    if not matches:
        raise ValueError(f"官方最新日找不到 {text}，請改用股票代號")
    first = matches[0]
    return first.stock_id, first.name, first.market


def ensure_stock_data(query: str, days: int) -> dict[str, object]:
    with connect_db(DB_PATH) as conn:
        dates = recent_dates(conn, days)
    if not dates:
        raise ValueError("資料庫沒有交易日，請先執行官方資料更新")
    target_id, target_name, target_market = resolve_official_stock(query, dt.date.fromisoformat(dates[-1]))

    price_rows = []
    inst_rows = []
    margin_rows = []
    for trade_date in dates:
        day = dt.date.fromisoformat(trade_date)
        if target_market == "TWSE":
            daily_prices = fetch_twse_prices_all(day, stock_only=True)
            daily_inst = fetch_twse_institutional_all(day, stock_only=True)
            daily_margin = fetch_twse_margin_all(day, stock_only=True)
        elif target_market == "TPEX":
            daily_prices = fetch_tpex_prices_all(day, stock_only=True)
            daily_inst = fetch_tpex_institutional_all(day, stock_only=True)
            daily_margin = fetch_tpex_margin_all(day, stock_only=True)
        else:
            daily_prices = [
                *fetch_twse_prices_all(day, stock_only=True),
                *fetch_tpex_prices_all(day, stock_only=True),
            ]
            daily_inst = [
                *fetch_twse_institutional_all(day, stock_only=True),
                *fetch_tpex_institutional_all(day, stock_only=True),
            ]
            daily_margin = [
                *fetch_twse_margin_all(day, stock_only=True),
                *fetch_tpex_margin_all(day, stock_only=True),
            ]
        price_rows.extend(row for row in daily_prices if row.stock_id == target_id)
        inst_rows.extend(row for row in daily_inst if row.stock_id == target_id)
        margin_rows.extend(row for row in daily_margin if row.stock_id == target_id)

    if not price_rows:
        raise ValueError(f"近 {days} 個交易日沒有 {target_id} {target_name} 的行情資料")
    with connect_db(DB_PATH) as conn:
        upsert_prices(conn, price_rows)
        upsert_institutional(conn, inst_rows)
        upsert_margin(conn, margin_rows)
        conn.commit()

    watchlist = current_watchlist_ids()
    if target_id not in watchlist:
        watchlist.append(target_id)
    run_scan(DB_PATH, REPORTS_DIR, days, 100, watchlist)
    if days != 20:
        try:
            run_scan(DB_PATH, REPORTS_DIR, 20, 100, watchlist)
        except Exception:
            pass
    if days != 5:
        try:
            run_scan(DB_PATH, REPORTS_DIR, 5, 100, watchlist)
        except Exception:
            pass
    return {
        "stock_id": target_id,
        "name": target_name,
        "market": target_market,
        "price_rows": len(price_rows),
        "institutional_rows": len(inst_rows),
        "margin_rows": len(margin_rows),
        "dates": dates,
    }


def classify_mops_event(title: str | None, detail: str | None, category: str | None = None) -> str:
    text = f"{category or ''} {title or ''} {detail or ''}"
    rules = [
        ("股東會", ("股東常會", "股東會", "股東臨時會", "停止過戶")),
        ("營收", ("營收", "合併營收", "自結營收")),
        ("財報", ("財務報告", "財報", "每股盈餘", "損益", "會計師")),
        ("除權息", ("除權", "除息", "配息", "配股", "股利")),
        ("法說會", ("法說會", "法人說明會", "業績發表會")),
        ("董事會", ("董事會", "審計委員會")),
        ("重大訊息", ("重大訊息", "重大訊息說明")),
    ]
    for label, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return label
    return category or "其他"


def load_stock_mops_events(conn: sqlite3.Connection, stock_id: str, days: int = 30, limit: int = 30) -> list[dict[str, object]]:
    cutoff = (dt.date.today() - dt.timedelta(days=max(1, days))).isoformat()
    payload = load_mops_events(conn, start_date=cutoff, stock_id=stock_id, limit=limit)
    rows: list[dict[str, object]] = []
    for row in payload.get("rows", []):
        item = dict(row)
        item["event_tag"] = classify_mops_event(
            str(item.get("title") or ""),
            str(item.get("detail") or ""),
            str(item.get("category") or ""),
        )
        rows.append(item)
    return rows


def stock_detail(stock_id: str, days: int) -> dict[str, object]:
    with connect_db(DB_PATH) as conn:
        dates = recent_dates(conn, days)
        if not dates:
            return {"stock": {"stock_id": stock_id, "name": stock_id}, "daily": [], "margin": [], "chart": [], "branch_top": [], "branch_daily": [], "revenues": [], "dispersion": [], "financials": [], "mops_events": []}
        latest_date = dates[-1]
        placeholders = ",".join("?" for _ in dates)
        stock_row = conn.execute(
            """
            SELECT s.stock_id, s.name, s.market, p.close, p.pe_ratio, p.dividend_yield, p.pb_ratio,
                   pr.industry_name, pr.business
            FROM stocks s
            LEFT JOIN daily_prices p
                ON p.stock_id = s.stock_id
               AND p.date = ?
            LEFT JOIN stock_profiles pr
                ON pr.stock_id = s.stock_id
            WHERE s.stock_id = ?
            """,
            (latest_date, stock_id),
        ).fetchone()
        if not stock_row:
            raise RuntimeError(f"找不到股票 {stock_id}")
        daily_raw = conn.execute(
            f"""
            SELECT
                p.date, p.close, p.avg_price, p.volume, p.pe_ratio, p.dividend_yield, p.pb_ratio,
                COALESCE(i.foreign_net, 0), COALESCE(i.trust_net, 0), COALESCE(i.dealer_net, 0)
            FROM daily_prices p
            LEFT JOIN institutional_trades i
                ON i.stock_id = p.stock_id
               AND i.date = p.date
            WHERE p.stock_id = ?
              AND p.date IN ({placeholders})
            ORDER BY p.date DESC
            """,
            [stock_id, *dates],
        ).fetchall()
        volume = sum(row[3] or 0 for row in daily_raw)
        turnover_row = conn.execute(
            f"""
            SELECT SUM(COALESCE(turnover, 0)), SUM(COALESCE(volume, 0))
            FROM daily_prices
            WHERE stock_id = ?
              AND date IN ({placeholders})
            """,
            [stock_id, *dates],
        ).fetchone()
        avg_price = None
        if turnover_row and turnover_row[1]:
            avg_price = turnover_row[0] / turnover_row[1]
        latest_volume = daily_raw[0][3] if daily_raw else None
        volume_avg = volume / len(daily_raw) if daily_raw else None
        recent5_daily = daily_raw[: min(5, len(daily_raw))]
        volume_5d_avg = (
            sum(row[3] or 0 for row in recent5_daily) / len(recent5_daily)
            if recent5_daily
            else None
        )
        volume_ratio_1d = latest_volume / volume_avg if latest_volume is not None and volume_avg else None
        volume_ratio_5d = volume_5d_avg / volume_avg if volume_5d_avg is not None and volume_avg else None
        foreign_net = sum(row[7] or 0 for row in daily_raw)
        trust_net = sum(row[8] or 0 for row in daily_raw)
        daily = [
            {
                "date": row[0],
                "close": row[1],
                "avg_price": row[2],
                "volume_lot": shares_to_lots(row[3]),
                "pe_ratio": row[4],
                "dividend_yield": row[5],
                "pb_ratio": row[6],
                "foreign_net_lot": shares_to_lots(row[7]),
                "trust_net_lot": shares_to_lots(row[8]),
                "dealer_net_lot": shares_to_lots(row[9]),
            }
            for row in daily_raw
        ]
        margin_raw = conn.execute(
            f"""
            SELECT
                date, margin_buy, margin_sell, margin_cash_repay,
                margin_prev_balance, margin_balance,
                short_buy, short_sell, short_stock_repay,
                short_prev_balance, short_balance, offset, note
            FROM margin_trades
            WHERE stock_id = ?
              AND date IN ({placeholders})
            ORDER BY date DESC
            """,
            [stock_id, *dates],
        ).fetchall()
        margin = [
            {
                "date": row[0],
                "margin_buy_lot": row[1],
                "margin_sell_lot": row[2],
                "margin_cash_repay_lot": row[3],
                "margin_prev_balance_lot": row[4],
                "margin_balance_lot": row[5],
                "margin_change_lot": (row[5] - row[4]) if row[5] is not None and row[4] is not None else None,
                "short_buy_lot": row[6],
                "short_sell_lot": row[7],
                "short_stock_repay_lot": row[8],
                "short_prev_balance_lot": row[9],
                "short_balance_lot": row[10],
                "short_change_lot": (row[10] - row[9]) if row[10] is not None and row[9] is not None else None,
                "offset_lot": row[11],
                "note": row[12],
            }
            for row in margin_raw
        ]
        margin_balance_change = None
        short_balance_change = None
        if margin:
            latest_margin = margin[0]
            oldest_margin = margin[-1]
            if latest_margin["margin_balance_lot"] is not None and oldest_margin["margin_prev_balance_lot"] is not None:
                margin_balance_change = latest_margin["margin_balance_lot"] - oldest_margin["margin_prev_balance_lot"]
            if latest_margin["short_balance_lot"] is not None and oldest_margin["short_prev_balance_lot"] is not None:
                short_balance_change = latest_margin["short_balance_lot"] - oldest_margin["short_prev_balance_lot"]
        chart_dates = recent_dates(conn, max(240, days))
        chart: list[dict[str, object]] = []
        if chart_dates:
            chart_placeholders = ",".join("?" for _ in chart_dates)
            chart_raw = conn.execute(
                f"""
                SELECT date, open, high, low, close, avg_price, volume, pe_ratio
                FROM daily_prices
                WHERE stock_id = ?
                  AND date IN ({chart_placeholders})
                ORDER BY date ASC
                """,
                [stock_id, *chart_dates],
            ).fetchall()
            closes: list[float | None] = [row[4] for row in chart_raw]
            for idx, row in enumerate(chart_raw):
                item: dict[str, object] = {
                    "date": row[0],
                    "open": row[1],
                    "high": row[2],
                    "low": row[3],
                    "close": row[4],
                    "avg_price": row[5],
                    "volume_lot": shares_to_lots(row[6]),
                    "pe_ratio": row[7],
                }
                for period in (5, 10, 20, 60):
                    window = closes[idx - period + 1 : idx + 1]
                    valid_window = [value for value in window if value is not None]
                    item[f"ma{period}"] = round(sum(valid_window) / period, 4) if len(valid_window) == period else None
                chart.append(item)
        branch_top_raw = conn.execute(
            """
            SELECT rank_no, broker_name, buy_lot, sell_lot, net_lot, avg_price
            FROM broker_branch_topn
            WHERE stock_id = ?
              AND as_of_date = ?
              AND window_days = ?
              AND rank_side = 'buy'
            ORDER BY rank_no
            LIMIT 10
            """,
            (stock_id, latest_date, days),
        ).fetchall()
        # MoneyDJ 區間表沒有分點均價；用每日明細 × 當日成交均價推估成本（≥3 天才算）
        est_cost_dates = recent_dates(conn, days)
        est_cost_by_broker: dict[str, float] = {}
        if est_cost_dates:
            est_placeholders = ",".join("?" for _ in est_cost_dates)
            est_rows = conn.execute(
                f"""
                SELECT d.broker_name,
                       SUM(d.net_lot * p.avg_price) / SUM(d.net_lot) AS est_cost,
                       COUNT(*) AS day_count
                FROM broker_branch_daily d
                JOIN daily_prices p
                  ON p.stock_id = d.stock_id
                 AND p.date = d.trade_date
                 AND p.avg_price IS NOT NULL
                WHERE d.stock_id = ?
                  AND d.net_lot > 0
                  AND d.trade_date IN ({est_placeholders})
                GROUP BY d.broker_name
                HAVING COUNT(*) >= 3
                """,
                [stock_id, *est_cost_dates],
            ).fetchall()
            est_cost_by_broker = {row[0]: round(row[1], 2) for row in est_rows if row[1]}
        branch_top = [
            {
                "rank_no": row[0],
                "broker_name": row[1],
                "buy_lot": row[2],
                "sell_lot": row[3],
                "net_lot": row[4],
                "avg_price": row[5],
                "est_cost": row[5] if row[5] is not None else est_cost_by_broker.get(row[1]),
            }
            for row in branch_top_raw
        ]
        branch_top_status_row = conn.execute(
            """
            SELECT status, row_count, attempts, error, updated_at
            FROM branch_fetch_status
            WHERE stock_id = ?
              AND trade_date = ?
              AND window_days = ?
              AND source = 'moneydj'
            """,
            (stock_id, latest_date, days),
        ).fetchone()
        if branch_top_status_row:
            branch_top_status = {
                "status": branch_top_status_row[0],
                "row_count": branch_top_status_row[1],
                "attempts": branch_top_status_row[2],
                "error": branch_top_status_row[3],
                "updated_at": normalize_time(branch_top_status_row[4]),
            }
        elif branch_top:
            branch_top_status = {
                "status": "success",
                "row_count": len(branch_top),
                "attempts": 0,
                "error": None,
                "updated_at": "",
            }
        else:
            branch_top_status = {
                "status": "missing",
                "row_count": 0,
                "attempts": 0,
                "error": None,
                "updated_at": "",
            }
        brokers = [row["broker_name"] for row in branch_top]
        branch_daily: list[dict[str, object]] = []
        if brokers:
            broker_placeholders = ",".join("?" for _ in brokers)
            daily_dates = recent_dates(conn, days)
            date_placeholders = ",".join("?" for _ in daily_dates)
            raw = conn.execute(
                f"""
                SELECT d.trade_date, d.broker_name, d.rank_side, d.rank_no,
                       d.buy_lot, d.sell_lot, d.net_lot, d.avg_price, p.avg_price
                FROM broker_branch_daily d
                LEFT JOIN daily_prices p
                  ON p.stock_id = d.stock_id AND p.date = d.trade_date
                WHERE d.stock_id = ?
                  AND d.broker_name IN ({broker_placeholders})
                  AND d.trade_date IN ({date_placeholders})
                ORDER BY d.trade_date DESC, d.broker_name
                """,
                [stock_id, *brokers, *daily_dates],
            ).fetchall()
            branch_daily = [
                {
                    "trade_date": row[0],
                    "broker_name": row[1],
                    "rank_side": row[2],
                    "rank_no": row[3],
                    "buy_lot": row[4],
                    "sell_lot": row[5],
                    "net_lot": row[6],
                    "avg_price": row[7],
                    "day_avg_price": round(row[8], 2) if row[8] is not None else None,
                }
                for row in raw
            ]
        daily_status_raw = []
        if brokers:
            daily_dates = recent_dates(conn, days)
            date_placeholders = ",".join("?" for _ in daily_dates)
            daily_status_raw = conn.execute(
                f"""
                SELECT trade_date, status, row_count, attempts, error
                FROM branch_fetch_status
                WHERE stock_id = ?
                  AND window_days = 1
                  AND source = 'moneydj'
                  AND trade_date IN ({date_placeholders})
                ORDER BY trade_date DESC
                """,
                [stock_id, *daily_dates],
            ).fetchall()
            existing_status_dates = {row[0] for row in daily_status_raw}
            missing_dates = [trade_date for trade_date in daily_dates if trade_date not in existing_status_dates]
            if missing_dates:
                row_count_by_date = dict(
                    conn.execute(
                        f"""
                        SELECT trade_date, COUNT(*)
                        FROM broker_branch_daily
                        WHERE stock_id = ?
                          AND source = 'moneydj'
                          AND trade_date IN ({date_placeholders})
                        GROUP BY trade_date
                        """,
                        [stock_id, *daily_dates],
                    ).fetchall()
                )
                daily_status_raw.extend(
                    (
                        trade_date,
                        "success" if row_count_by_date.get(trade_date, 0) > 0 else "missing",
                        row_count_by_date.get(trade_date, 0),
                        0,
                        None,
                    )
                    for trade_date in missing_dates
                )
                daily_status_raw = sorted(daily_status_raw, key=lambda row: row[0], reverse=True)
        revenue_raw = conn.execute(
            """
            SELECT revenue_month, revenue, prev_month_revenue, last_year_revenue,
                   mom_pct, yoy_pct, cumulative_revenue, last_year_cumulative_revenue,
                   cumulative_yoy_pct, source
            FROM monthly_revenues
            WHERE stock_id = ?
            ORDER BY revenue_month DESC
            LIMIT 24
            """,
            (stock_id,),
        ).fetchall()
        revenues = [
            {
                "revenue_month": row[0],
                "revenue_million": round(row[1] / 1_000_000, 2) if row[1] is not None else None,
                "prev_month_revenue_million": round(row[2] / 1_000_000, 2) if row[2] is not None else None,
                "last_year_revenue_million": round(row[3] / 1_000_000, 2) if row[3] is not None else None,
                "mom_pct": row[4],
                "yoy_pct": row[5],
                "cumulative_revenue_million": round(row[6] / 1_000_000, 2) if row[6] is not None else None,
                "last_year_cumulative_revenue_million": round(row[7] / 1_000_000, 2) if row[7] is not None else None,
                "cumulative_yoy_pct": row[8],
                "source": row[9],
            }
            for row in revenue_raw
        ]
        sub_industry = ""
        try:
            sub_row = conn.execute(
                "SELECT sub_industry FROM stock_profiles WHERE stock_id = ?", (stock_id,)
            ).fetchone()
            sub_industry = (sub_row[0] or "") if sub_row else ""
        except sqlite3.OperationalError:
            sub_industry = ""
        dispersion = load_stock_dispersion(conn, stock_id)
        financials = load_stock_financials(conn, stock_id)
        news = load_cached_news(conn, stock_id)
        mops_events = load_stock_mops_events(conn, stock_id)
    selection = {}
    for row in read_csv(REPORTS_DIR / f"scan_all_{days}d.csv"):
        if row.get("stock_id") == stock_id:
            selection = normalize_scan_row(row, days)
            break
    return {
        "stock": {
            "stock_id": stock_row[0],
            "name": stock_row[1],
            "market": stock_row[2],
            "latest_date": latest_date,
            "close": stock_row[3],
            "pe_ratio": stock_row[4],
            "dividend_yield": stock_row[5],
            "pb_ratio": stock_row[6],
            "industry": stock_row[7] or "",
            "sub_industry": sub_industry,
            "business": stock_row[8] or "",
            "avg_price": round(avg_price, 4) if avg_price is not None else None,
            "foreign_net_lot": shares_to_lots(foreign_net),
            "trust_net_lot": shares_to_lots(trust_net),
            "volume_lot": shares_to_lots(volume),
            "latest_volume_lot": shares_to_lots(latest_volume),
            "volume_avg_lot": shares_to_lots(volume_avg),
            "volume_5d_avg_lot": shares_to_lots(volume_5d_avg),
            "volume_ratio_1d": round(volume_ratio_1d, 2) if volume_ratio_1d is not None else None,
            "volume_ratio_5d": round(volume_ratio_5d, 2) if volume_ratio_5d is not None else None,
            "margin_balance_change_lot": margin_balance_change,
            "short_balance_change_lot": short_balance_change,
            "in_watchlist": stock_row[0] in current_watchlist_ids(),
        },
        "selection": selection,
        "daily": daily,
        "margin": margin,
        "chart": chart,
        "branch_top": branch_top,
        "branch_top_status": branch_top_status,
        "branch_daily": branch_daily,
        "branch_daily_status": [
            {
                "trade_date": row[0],
                "status": row[1],
                "row_count": row[2],
                "attempts": row[3],
                "error": row[4],
            }
            for row in daily_status_raw
        ],
        "revenues": revenues,
        "dispersion": dispersion,
        "financials": financials,
        "news": news,
        "mops_events": mops_events,
    }


def load_stock_dispersion(conn: sqlite3.Connection, stock_id: str, weeks: int = 26) -> list[dict[str, object]]:
    """TDCC 股權分散週序列：千張大戶 / 400張以上 / 散戶比率與總股東數。"""
    try:
        rows = conn.execute(
            """
            SELECT data_date,
                   SUM(CASE WHEN level = 15 THEN share_pct END) AS big_pct,
                   SUM(CASE WHEN level BETWEEN 12 AND 15 THEN share_pct END) AS b400_pct,
                   SUM(CASE WHEN level <= 9 THEN share_pct END) AS retail_pct,
                   MAX(CASE WHEN level = 17 THEN holder_count END) AS holders
            FROM shareholding_dispersion
            WHERE stock_id = ?
              AND level != 16
            GROUP BY data_date
            ORDER BY data_date DESC
            LIMIT ?
            """,
            (stock_id, weeks),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "data_date": row[0],
            "big_holder_pct": round(row[1], 2) if row[1] is not None else None,
            "holder_400_pct": round(row[2], 2) if row[2] is not None else None,
            "retail_pct": round(row[3], 2) if row[3] is not None else None,
            "total_holders": row[4],
        }
        for row in rows
    ]


def load_stock_financials(conn: sqlite3.Connection, stock_id: str, quarters: int = 8) -> list[dict[str, object]]:
    """季度財報單季值（自累計差分），新到舊，供個股頁小表。"""
    from stock_chip.financials import load_financial_rows, single_quarter_values

    cumulative = load_financial_rows(conn, stock_id, limit=quarters + 4)
    rows = single_quarter_values(cumulative)[-quarters:]
    return [
        {
            "year_quarter": row["year_quarter"],
            "revenue_million": round(row["revenue"] / 1000, 1) if row.get("revenue") is not None else None,
            "gross_margin_pct": row.get("gross_margin_pct"),
            "operating_margin_pct": row.get("operating_margin_pct"),
            "net_margin_pct": row.get("net_margin_pct"),
            "eps": row.get("eps"),
        }
        for row in reversed(rows)
    ]


_SUGGEST_CACHE: dict[str, object] = {"mtime": None, "rows": []}


def suggest_rows(days: int = 20) -> list[dict[str, object]]:
    """搜尋 typeahead 用的輕量索引（代號/名稱/產業/收盤/多因子分）。"""
    path = REPORTS_DIR / f"scan_all_{days}d.csv"
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    if _SUGGEST_CACHE["mtime"] == mtime:
        return _SUGGEST_CACHE["rows"]  # type: ignore[return-value]
    rows = [
        {
            "i": row.get("stock_id"),
            "n": row.get("name"),
            "d": row.get("industry") or "",
            "c": as_float_or_none(row.get("close")),
            "m": as_float_or_none(row.get("multifactor_score")),
        }
        for row in read_csv(path)
    ]
    _SUGGEST_CACHE["mtime"] = mtime
    _SUGGEST_CACHE["rows"] = rows
    return rows


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
            if parsed.path == "/api/weekly":
                # 本機 GUI 也要能看週報。/api/etf 與 /api/dividends 當初只加了靜態
                # 路由、漏了這個 handler，結果本機模式一直落到 404，別重蹈。
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


if __name__ == "__main__":
    main()

"""GUI update jobs, publish, and per-stock refresh. Re-exported from stock_chip.gui."""
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
    REPORTS_DIR,
    ROOT,
    UPDATE_JOBS,
    UPDATE_LOCK,
    all_market_stock_ids,
    as_float,
    branch_coverage_summary,
    candidate_revenue_ids,
    chunked,
    ci_us_news_status,
    current_watchlist_ids,
    data_update_times,
    ensure_gui_tables,
    expected_latest_revenue_month,
    failed_branch_ids,
    market_data_status,
    now_text,
    normalize_time,
    ranking_path,
    read_csv,
    resolve_local_stock,
    revenue_coverage_status,
    revenue_missing_month_stock_ids,
    revenue_missing_stock_ids,
    stock_detail,
)

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


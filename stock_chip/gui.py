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


INDEX_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>台股籌碼觀察</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f6f7;
      --ink: #17212b;
      --muted: #607080;
      --line: #d8e0e6;
      --panel: #fff;
      --head: #102a2a;
      --accent: #0f766e;
      --accent-dark: #115e59;
      --danger: #b42318;
      --good: #067647;
      --warn: #b54708;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      padding: 18px 28px 14px;
      background: var(--head);
      color: #fff;
    }
    h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
    .subhead { display: flex; gap: 14px; flex-wrap: wrap; color: #c7d7d5; font-size: 14px; }
    main { padding: 18px 28px 32px; }
    label { display: block; color: var(--muted); font-size: 12px; font-weight: 700; margin-bottom: 4px; }
    select, input {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 0 10px;
      font-size: 14px;
    }
    button {
      height: 38px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    button:hover { background: var(--accent-dark); }
    button:disabled {
      background: #cbd5dc;
      color: #52616f;
      cursor: not-allowed;
    }
    button:disabled:hover { background: #cbd5dc; }
    button.secondary,
    a.secondary {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 0 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font-size: 14px;
      font-weight: 800;
      text-decoration: none;
    }
    button.secondary:hover,
    a.secondary:hover { background: #eef6f5; }
    .toolbar {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
    }
    .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
    .tab {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
      padding: 9px 12px;
      height: auto;
    }
    .tab.active { background: var(--accent); border-color: var(--accent); color: #fff; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .metric .label { color: var(--muted); font-size: 13px; }
    .metric .value { font-size: 24px; font-weight: 850; margin-top: 4px; }
    .sentiment-strip {
      display: grid;
      grid-template-columns: minmax(180px, 0.8fr) minmax(260px, 1.2fr) minmax(260px, 1.5fr);
      gap: 12px;
      padding: 14px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
    }
    .sentiment-score {
      font-size: 36px;
      font-weight: 900;
      line-height: 1;
      margin: 6px 0;
    }
    .sentiment-label { font-weight: 850; }
    .sentiment-detail {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
      margin-top: 6px;
    }
    .sentiment-parts {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
      gap: 8px;
    }
    .sentiment-part {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: white;
      cursor: help;
    }
    .sentiment-part .part-label { color: var(--muted); font-size: 12px; font-weight: 750; }
    .sentiment-part .part-value { font-size: 18px; font-weight: 900; margin-top: 2px; }
    .assist-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 10px;
      padding: 14px;
    }
    .assist-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfdff;
    }
    .assist-label { color: var(--muted); font-size: 12px; font-weight: 800; }
    .assist-value { color: var(--ink); font-size: 22px; font-weight: 900; margin-top: 4px; }
    .assist-note { color: var(--muted); font-size: 12px; line-height: 1.45; margin-top: 6px; }
    .assist-reason {
      border-top: 1px solid var(--line);
      padding: 12px 14px;
      color: #344054;
      font-size: 13px;
      line-height: 1.6;
    }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(110px, 1fr));
      gap: 10px;
      padding: 14px;
    }
    .status-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfdff;
    }
    .status-card.good { border-color: #bfe3d0; background: #f3fbf6; }
    .status-card.warn { border-color: #f6c76d; background: #fffbf0; }
    .status-card.bad { border-color: #efc4c0; background: #fff7f6; }
    .status-name { color: var(--muted); font-size: 12px; font-weight: 800; }
    .status-value { margin-top: 4px; color: var(--ink); font-size: 16px; font-weight: 900; }
    .status-detail { margin-top: 5px; color: var(--muted); font-size: 12px; line-height: 1.45; }
    .judgement-box {
      border-top: 1px solid var(--line);
      padding: 13px 14px;
      color: #26384d;
      font-size: 14px;
      line-height: 1.65;
      background: #fbfdff;
    }
    .column-controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 12px 14px;
      border-top: 1px solid var(--line);
      background: #fbfdff;
    }
    .column-controls .toggle { height: 28px; font-size: 12px; }
    .tooltip-anchor { position: relative; }
    .tooltip-anchor::after {
      content: attr(data-tip);
      position: absolute;
      left: 0;
      top: calc(100% + 8px);
      z-index: 20;
      width: min(320px, 72vw);
      padding: 10px 12px;
      border: 1px solid #cfd9e1;
      border-radius: 8px;
      background: #102a43;
      color: #f8fafc;
      box-shadow: 0 10px 24px rgba(15, 23, 42, .18);
      font-size: 12px;
      font-weight: 700;
      line-height: 1.55;
      white-space: normal;
      pointer-events: none;
      opacity: 0;
      transform: translateY(-4px);
      transition: opacity .12s ease, transform .12s ease;
    }
    .tooltip-anchor:hover::after,
    .tooltip-anchor:focus-within::after {
      opacity: 1;
      transform: translateY(0);
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      margin-bottom: 14px;
    }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }
    .panel-title { font-weight: 850; }
    .panel-body { padding: 14px; }
    .table-wrap {
      position: relative;
      z-index: 1;
      overflow: auto;
      max-height: 64vh;
      background: var(--panel);
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; white-space: nowrap; }
    th, td { border-bottom: 1px solid #edf1f4; padding: 8px 10px; text-align: right; }
    th { position: sticky; top: 0; background: #f8fafb; color: #344054; z-index: 1; font-size: 12px; }
    th.sortable { cursor: pointer; user-select: none; }
    th.sortable:hover { background: #eef6f5; color: var(--accent-dark); }
    th.sortable .sort-mark { margin-left: 4px; color: var(--accent-dark); }
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(3), td:nth-child(3) { text-align: left; }
    tr.clickable { cursor: pointer; }
    tr.clickable:hover td { background: #eef8f6; }
    .pos { color: var(--good); font-weight: 800; }
    .neg { color: var(--danger); font-weight: 800; }
    .warn { color: var(--warn); font-weight: 800; }
    .muted { color: var(--muted); }
    .layout-2 { display: grid; grid-template-columns: 1.2fr .8fr; gap: 14px; }
    .detail-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
    .detail-title { font-size: 24px; font-weight: 900; }
    .chart-toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .toggle {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      height: 30px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
    }
    .toggle input { width: auto; height: auto; margin: 0; }
    .series-option .legend-swatch, .ma-option .legend-swatch { width: 20px; height: 4px; }
    .chart-range {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .chart-range select {
      width: auto;
      height: 30px;
      padding: 0 26px 0 8px;
      font-size: 12px;
      font-weight: 800;
    }
    .price-chart-wrap,
    .revenue-chart-wrap,
    .market-chart-wrap {
      height: 330px;
      min-height: 260px;
      position: relative;
      overflow: hidden;
      contain: layout paint;
    }
    .revenue-chart-wrap { height: 300px; }
    .margin-chart-wrap { height: 300px; }
    .market-chart-wrap { height: 320px; }
    .price-chart-wrap canvas { cursor: zoom-in; }
    #price-chart,
    #revenue-chart,
    #margin-chart,
    #market-index-chart,
    #market-futures-chart {
      width: 100% !important;
      height: 100% !important;
      max-width: 100%;
      max-height: 100%;
      display: block;
    }
    .legend {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }
    .legend-item { display: inline-flex; align-items: center; gap: 6px; }
    .legend-swatch { width: 18px; height: 3px; border-radius: 999px; display: inline-block; }
    .update-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(240px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .job-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fff;
    }
    .job-card.primary-job {
      border-color: #9ccdc8;
      background: #f6fbfa;
    }
    .job-card.stale-warn {
      border-color: #f6c76d;
      background: #fffbf0;
    }
    .job-card.stale-bad {
      border-color: #efb3ad;
      background: #fff7f6;
    }
    .job-title { font-weight: 850; margin-bottom: 6px; }
    .job-desc { color: var(--muted); font-size: 13px; line-height: 1.5; min-height: 38px; }
    .job-time { color: var(--muted); font-size: 12px; margin-top: 8px; line-height: 1.5; }
    .job-extra { color: #344054; font-size: 12px; margin-top: 8px; line-height: 1.5; }
    .job-card button { margin-top: 12px; min-width: 120px; }
    .publish-box {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fff;
      margin-bottom: 14px;
    }
    .publish-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .publish-actions button { min-width: 140px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      background: #eef6f5;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 800;
    }
    .status-pill.running { background: #fff4e5; color: var(--warn); }
    .status-pill.failed { background: #fff1f0; color: var(--danger); }
    .status-pill.done { background: #ecfdf3; color: var(--good); }
    .status-pill.warn { background: #fff4e5; color: var(--warn); }
    .status-pill.bad { background: #fff1f0; color: var(--danger); }
    .job-meta {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 10px;
    }
    .log-box { max-height: 360px; white-space: pre-wrap; }
    .broker-list { display: flex; gap: 8px; flex-wrap: wrap; }
    .broker-chip {
      height: 32px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font-weight: 750;
    }
    .broker-chip.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .news-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .panel-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .panel-actions button { min-width: 96px; }
    .inline-action {
      height: 30px;
      padding: 0 10px;
      margin-left: 8px;
      vertical-align: middle;
    }
    .obsidian-status {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: flex-start;
      margin: 4px 0 16px;
      padding: 12px 14px;
      border: 1px solid #d8e2ea;
      border-radius: 8px;
      background: #f6f8fa;
    }
    .obsidian-kicker {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      margin-bottom: 4px;
    }
    .obsidian-text { color: #344054; font-size: 13px; line-height: 1.55; }
    .news-section-title {
      margin: 16px 0 10px;
      color: var(--ink);
      font-size: 15px;
      font-weight: 850;
    }
    .news-list { display: grid; gap: 10px; }
    .news-item {
      padding: 14px;
      border: 1px solid #dce5ec;
      border-radius: 8px;
      background: #fbfdff;
    }
    .news-item:nth-child(even) { background: #f7fafc; }
    .event-alert {
      display: grid;
      gap: 10px;
    }
    .event-alert-summary {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }
    .event-item {
      padding: 12px 14px;
      border: 1px solid #dce5ec;
      border-radius: 8px;
      background: #fbfdff;
    }
    .event-item.important {
      border-color: #f2c9c5;
      background: #fff8f7;
    }
    .event-line {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      flex-wrap: wrap;
    }
    .event-title {
      color: var(--ink);
      font-size: 15px;
      font-weight: 850;
      line-height: 1.45;
    }
    .event-meta {
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .event-text {
      margin-top: 8px;
      color: #344054;
      font-size: 13px;
      line-height: 1.55;
    }
    .event-tag {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid #cfdbe5;
      background: #f7fafc;
      color: #344054;
      font-size: 12px;
      font-weight: 850;
      white-space: nowrap;
    }
    .event-tag.hot { border-color: #f5b7b1; background: #fff1f0; color: var(--danger); }
    .event-tag.good { border-color: #abefc6; background: #ecfdf3; color: var(--good); }
    .event-open { color: var(--accent-dark); font-size: 13px; font-weight: 850; text-decoration: none; }
    .calendar-grid {
      display: grid;
      grid-template-columns: repeat(7, minmax(110px, 1fr));
      gap: 8px;
      margin-bottom: 14px;
    }
    .calendar-day {
      min-height: 102px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfdff;
      padding: 8px;
      cursor: pointer;
    }
    .calendar-day:hover { border-color: #9fcfc8; background: #f1faf8; }
    .calendar-day.selected { border-color: var(--accent); background: #eaf8f5; box-shadow: inset 0 0 0 1px var(--accent); }
    .calendar-day.empty-day {
      background: #f3f6f8;
      color: #8b9bab;
    }
    .calendar-date {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 850;
      margin-bottom: 6px;
    }
    .calendar-count {
      font-size: 22px;
      font-weight: 900;
      color: var(--accent-dark);
    }
    .calendar-hint { color: var(--muted); font-size: 12px; line-height: 1.45; }
    .calendar-window-bar {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      margin: 8px 0 12px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
    }
    .calendar-window-bar .window-label { color: var(--ink); }
    .calendar-nav {
      width: 38px;
      min-width: 38px;
      padding: 0;
      font-size: 20px;
      line-height: 1;
    }
    .mops-summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .mops-summary-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfdff;
      padding: 10px;
    }
    .mops-summary-card .label { color: var(--muted); font-size: 12px; font-weight: 800; }
    .mops-summary-card .value { margin-top: 4px; font-size: 20px; font-weight: 900; }
    .quick-filter-bar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 8px 0 12px;
    }
    .quick-filter {
      height: 30px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      font-weight: 800;
      cursor: pointer;
    }
    .quick-filter.active { background: var(--accent); color: #fff; border-color: var(--accent); }
    .event-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfdff;
      padding: 12px;
      margin-bottom: 10px;
      cursor: pointer;
    }
    .event-card:hover { border-color: #9fcfc8; background: #f1faf8; }
    .event-title { font-size: 15px; font-weight: 900; line-height: 1.45; }
    .event-meta { color: var(--muted); font-size: 12px; margin-top: 6px; }
    .event-detail-text {
      white-space: pre-wrap;
      color: #26384d;
      font-size: 14px;
      line-height: 1.7;
    }
    .news-title {
      display: block;
      color: var(--ink);
      font-weight: 850;
      text-decoration: none;
      line-height: 1.45;
    }
    .news-title:hover { color: var(--accent-dark); text-decoration: underline; }
    .news-open {
      display: inline-flex;
      margin-top: 4px;
      color: var(--accent-dark);
      font-size: 12px;
      font-weight: 800;
      text-decoration: none;
    }
    .news-open:hover { text-decoration: underline; }
    .news-meta { color: var(--muted); font-size: 12px; margin: 5px 0 7px; }
    .news-text { color: #344054; font-size: 13px; line-height: 1.65; white-space: pre-wrap; }
    .news-match { color: #475467; font-size: 12px; line-height: 1.5; margin-top: 6px; }
    .tag-list { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
    .tag {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: #344054;
      background: #f8fafb;
      font-size: 12px;
      font-weight: 700;
    }
    .tag.good { color: var(--good); border-color: #bfe3d0; background: #f3fbf6; }
    .tag.bad { color: var(--danger); border-color: #efc4c0; background: #fff6f5; }
    .tag.ai { color: var(--accent-dark); border-color: #b8d9d5; background: #f0faf8; }
    pre {
      margin: 0;
      padding: 14px;
      background: #0b1220;
      color: #dbe7ff;
      overflow: auto;
      border-radius: 8px;
      font-size: 13px;
    }
    pre a { color: #93c5fd; font-weight: 800; }
    pre a:hover { color: #bfdbfe; }
    .empty { padding: 20px; color: var(--muted); }
    .sources {
      margin-top: 18px;
      padding: 14px 0 0;
      border-top: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.7;
    }
    .sources strong { color: #344054; }
    .sources a { color: var(--accent-dark); text-decoration: none; font-weight: 700; }
    .sources a:hover { text-decoration: underline; }
    .source-audit {
      margin-bottom: 16px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f8fafb;
    }
    .source-audit h3 {
      margin: 0 0 10px;
      font-size: 18px;
    }
    .source-audit-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .source-audit-card {
      padding: 12px;
      border: 1px solid #d6e0e8;
      border-radius: 8px;
      background: #fff;
    }
    .source-audit-card strong {
      display: block;
      margin-bottom: 6px;
      color: #1d2939;
    }
    .source-audit-card ul {
      margin: 0;
      padding-left: 18px;
      color: #475467;
      font-size: 13px;
      line-height: 1.65;
    }
    .source-audit-card a { color: var(--accent-dark); font-weight: 800; text-decoration: none; }
    .source-audit-card a:hover { text-decoration: underline; }
    @media (max-width: 1000px) {
      main { padding: 14px; }
      .toolbar { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .metrics, .layout-2, .update-grid, .assist-grid, .status-grid, .source-audit-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>台股籌碼觀察</h1>
    <div class="subhead" id="meta">讀取中...</div>
  </header>
  <main>
    <section class="toolbar">
      <div>
        <label for="days">區間</label>
        <select id="days">
          <option value="5">5 日</option>
          <option value="20" selected>20 日</option>
        </select>
      </div>
      <div>
        <label for="ranking">排行</label>
        <select id="ranking">
          <option value="total_score">基礎選股分</option>
          <option value="chip_score">法人籌碼分數</option>
          <option value="foreign_buy">外資買超</option>
          <option value="foreign_5d_revenue_growth">外資近5日買超 + 營收成長</option>
          <option value="inst_buy_volume">法人買超 + 占量</option>
          <option value="volume_expansion">成交量放大</option>
          <option value="revenue_volume_breakout">營收成長 + 量能</option>
          <option value="margin_down_foreign_buy">融資下降 + 外資買超</option>
          <option value="trust_buy">投信買超</option>
          <option value="inst_buy">外資 + 投信</option>
          <option value="near_avg_with_inst_buy">接近均價且法人買超</option>
        </select>
      </div>
      <div>
        <label for="market">市場</label>
        <select id="market">
          <option value="">全部</option>
          <option value="TWSE">上市</option>
          <option value="TPEX">上櫃</option>
        </select>
      </div>
      <div>
        <label for="query">搜尋</label>
        <input id="query" placeholder="代號或名稱" />
      </div>
      <div>
        <label for="min-volume">最低成交量(張)</label>
        <input id="min-volume" type="number" min="0" step="100" placeholder="不限" />
      </div>
      <div>
        <label for="limit">筆數</label>
        <select id="limit">
          <option value="20">20</option>
          <option value="50" selected>50</option>
          <option value="100">100</option>
        </select>
      </div>
      <button id="refresh">重新整理</button>
    </section>

    <section class="tabs">
      <button class="tab active" data-tab="ranking">排行</button>
      <button class="tab" data-tab="watchlist">自選股</button>
      <button class="tab" data-tab="detail">個股</button>
      <button class="tab" data-tab="market">大盤指數</button>
      <button class="tab" data-tab="mops-events">重大事件</button>
      <button class="tab" data-tab="us-news">美股新聞</button>
      <button class="tab" data-tab="ci-us-news">GitHub 新聞預覽</button>
      <button class="tab" data-tab="coverage">資料狀態</button>
    </section>

    <section class="metrics" id="metrics"></section>

    <section id="ranking-view" class="panel">
      <div class="panel-head">
        <div class="panel-title" id="ranking-title">排行</div>
        <div class="muted" id="ranking-note"></div>
      </div>
      <div id="ranking-sentiment"></div>
      <div class="column-controls" id="ranking-columns"></div>
      <div class="table-wrap" id="ranking-table"></div>
    </section>

    <section id="detail-view" style="display:none;">
      <div class="detail-head">
        <div>
          <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
            <button class="secondary" id="back-detail" style="display:none;">返回</button>
            <div class="detail-title" id="detail-title">個股明細</div>
          </div>
          <div class="muted" id="detail-subtitle"></div>
        </div>
        <div style="display:flex; gap:8px; align-items:end;">
          <div style="width:180px">
            <label for="detail-stock">股票</label>
            <input id="detail-stock" value="2376" />
          </div>
          <button id="load-detail">載入</button>
          <button class="secondary" id="watchlist-toggle">加入自選</button>
        </div>
      </div>
      <section class="metrics" id="detail-metrics"></section>
      <section class="panel" id="detail-status-panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">資料品質與判斷摘要</div>
            <div class="muted">區分已更新、缺資料與來源空回應，避免把缺資料誤判成 0。</div>
          </div>
        </div>
        <div id="detail-data-status"></div>
        <div class="judgement-box" id="detail-judgement"></div>
      </section>
      <section class="panel" id="selection-panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">選股輔助</div>
            <div class="muted">共振分拆解：法人方向、分點集中、成本位置、連買續航與綜合理由</div>
          </div>
        </div>
        <div id="selection-assist"></div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">近期重大事件</div>
            <div class="muted" id="stock-events-note">顯示 MOPS 已公告事件；日期為公告發布日，非一定是實際事件日。</div>
          </div>
          <button class="secondary" id="open-mops-for-stock">查看事件頁</button>
        </div>
        <div class="panel-body" id="stock-events-list"></div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">股價走勢</div>
          <div class="chart-toolbar">
            <span>蠟燭圖</span>
            <label class="toggle ma-option" data-ma="5"><span class="legend-swatch"></span><input type="checkbox" class="ma-toggle" value="5" checked /> MA5</label>
            <label class="toggle ma-option" data-ma="10"><span class="legend-swatch"></span><input type="checkbox" class="ma-toggle" value="10" checked /> MA10</label>
            <label class="toggle ma-option" data-ma="20"><span class="legend-swatch"></span><input type="checkbox" class="ma-toggle" value="20" checked /> MA20</label>
            <label class="toggle ma-option" data-ma="60"><span class="legend-swatch"></span><input type="checkbox" class="ma-toggle" value="60" /> MA60</label>
            <label class="chart-range">顯示
              <select id="chart-range">
                <option value="20" selected>20日</option>
                <option value="60">60日</option>
                <option value="120">120日</option>
                <option value="all">全部</option>
              </select>
            </label>
          </div>
        </div>
        <div class="panel-body">
          <div class="price-chart-wrap"><canvas id="price-chart"></canvas></div>
          <div class="legend" id="price-legend"></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">個股新聞</div>
            <div class="muted" id="news-note">只讀本機快取；按下按鈕才抓取 Yahoo 股市新聞</div>
          </div>
          <div class="news-actions">
            <button class="secondary" id="refresh-news">抓取新聞</button>
          </div>
        </div>
        <div class="panel-body" id="news-list"></div>
      </section>
      <section class="layout-2">
        <div class="panel">
          <div class="panel-head">
            <div><div class="panel-title">每日進出</div><div class="muted">成交量為全市場成交張數；三大法人欄位為買賣超</div></div>
            <div class="panel-actions"><button class="secondary single-refresh" data-section="daily">更新每日</button></div>
          </div>
          <div class="table-wrap" id="daily-table"></div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <div><div class="panel-title">24 個月營收</div><div class="muted" id="revenue-note"></div></div>
            <div class="panel-actions"><button class="secondary single-refresh" data-section="revenue">更新營收</button></div>
          </div>
          <div class="panel-body">
            <div class="revenue-chart-wrap"><canvas id="revenue-chart"></canvas></div>
            <div class="legend" id="revenue-legend"></div>
          </div>
          <div class="table-wrap" id="revenue-table"></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">融資融券餘額</div>
            <div class="muted" id="margin-note">取自 TWSE / TPEx 信用交易公開資料，單位為張</div>
          </div>
          <div class="panel-actions"><button class="secondary single-refresh" data-section="daily">更新融資融券</button></div>
        </div>
        <div class="panel-body">
          <div class="revenue-chart-wrap margin-chart-wrap"><canvas id="margin-chart"></canvas></div>
          <div class="legend" id="margin-legend"></div>
        </div>
        <div class="table-wrap" id="margin-table"></div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div><div class="panel-title">區間合計買超前十分點</div><div class="muted" id="branch-top-note">點分點看每日明細</div></div>
          <div class="panel-actions">
            <button class="single-refresh" data-section="branch_all">更新分點資訊</button>
            <button class="secondary single-refresh" data-section="branch_top">只更新排行</button>
          </div>
        </div>
        <div class="table-wrap" id="branch-top-table"></div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title" id="broker-title">分點每日明細</div>
          <div class="panel-actions">
            <div class="broker-list" id="broker-list"></div>
            <button class="secondary single-refresh" data-section="branch_daily">只更新每日明細</button>
          </div>
        </div>
        <div class="table-wrap" id="broker-daily-table"></div>
      </section>
    </section>

    <section id="market-view" style="display:none;">
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">大盤指數與三大法人期貨多空</div>
            <div class="muted" id="market-note">加權指數取自 TWSE；大台、小台、微台三大法人未平倉多空取自 TAIFEX。</div>
          </div>
          <button class="secondary" id="refresh-market-view">重新讀取</button>
        </div>
        <div class="panel-body">
          <section class="controls" style="padding:0; margin-bottom:12px;">
            <div>
              <label for="market-index-code">指數</label>
              <select id="market-index-code">
                <option value="TAIEX" selected>加權指數</option>
              </select>
            </div>
            <div>
              <label for="market-product">期貨商品</label>
              <select id="market-product">
                <option value="TXF" selected>大台 TXF</option>
                <option value="MXF">小台 MXF</option>
                <option value="TMF">微台 TMF</option>
              </select>
            </div>
            <div>
              <label for="market-range">顯示天數</label>
              <select id="market-range">
                <option value="60">60日</option>
                <option value="120" selected>120日</option>
                <option value="240">240日</option>
                <option value="all">全部</option>
              </select>
            </div>
          </section>
          <section class="metrics" id="market-metrics"></section>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">大盤多空分</div>
            <div class="muted">用加權指數趨勢、期貨三大法人與全市場融資融券估算目前市場風險。</div>
          </div>
        </div>
        <div id="market-sentiment"></div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">加權指數走勢</div>
          <div class="muted" id="market-index-note"></div>
        </div>
        <div class="panel-body">
          <div class="market-chart-wrap"><canvas id="market-index-chart"></canvas></div>
          <div class="legend" id="market-index-legend"></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">三大法人期貨未平倉多空淨額</div>
          <div class="muted" id="market-futures-note"></div>
        </div>
        <div class="panel-body">
          <div class="market-chart-wrap"><canvas id="market-futures-chart"></canvas></div>
          <div class="legend" id="market-futures-legend"></div>
        </div>
        <div class="table-wrap" id="market-futures-table"></div>
      </section>
    </section>

    <section id="mops-events-view" style="display:none;">
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">MOPS 重大事件月曆</div>
            <div class="muted" id="mops-note">來源：MOPS 公開資訊觀測站；抓取近期重大訊息與公告，點事件可看內文。</div>
          </div>
          <button class="secondary" id="refresh-mops-events">抓取 MOPS 事件</button>
        </div>
        <div class="panel-body">
          <section class="controls" style="padding:0; margin-bottom:12px;">
            <div>
              <label for="mops-start">開始日</label>
              <input id="mops-start" type="date" />
            </div>
            <div>
              <label for="mops-end">結束日</label>
              <input id="mops-end" type="date" />
            </div>
            <div>
              <label for="mops-stock">股票代號</label>
              <input id="mops-stock" placeholder="選填，例如 2330" />
            </div>
            <div>
              <label for="mops-query">關鍵字</label>
              <input id="mops-query" placeholder="主旨、公司、內文" />
            </div>
            <button id="reload-mops-events" style="align-self:end;">重新整理</button>
          </section>
          <div id="mops-calendar"></div>
          <div class="mops-summary" id="mops-summary"></div>
          <div class="news-section-title" id="mops-list-title">事件列表</div>
          <div class="quick-filter-bar" id="mops-quick-filters"></div>
          <div id="mops-event-list"></div>
        </div>
      </section>
      <section class="panel" id="mops-event-detail-panel" style="display:none;">
        <div class="panel-head">
          <div>
            <div class="panel-title" id="mops-detail-title">事件明細</div>
            <div class="muted" id="mops-detail-meta"></div>
          </div>
          <a class="secondary" id="mops-source-link" href="https://mopsov.twse.com.tw/mops/web/t05st02" target="_blank" rel="noreferrer">開啟 MOPS</a>
        </div>
        <div class="panel-body">
          <div class="event-detail-text" id="mops-detail-text"></div>
        </div>
      </section>
    </section>

    <section id="us-news-view" style="display:none;">
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">美股新聞</div>
            <div class="muted" id="us-news-note">來源：Yahoo Finance 個股 RSS、CNBC 與 MarketWatch 市場 RSS；預設用本機規則快速分類，實際 LLM token 為 0。</div>
          </div>
          <div class="news-actions">
            <button class="secondary" id="refresh-us-news">抓取美股新聞</button>
          </div>
        </div>
        <div class="panel-body">
          <section class="controls" style="padding:0; margin-bottom:12px;">
            <div>
              <label for="us-news-mode">抓取模式</label>
              <select id="us-news-mode">
                <option value="market" selected>市場新聞</option>
                <option value="symbols">指定個股 + 市場新聞</option>
              </select>
            </div>
            <div>
              <label for="us-symbols">美股代號（選填）</label>
              <input id="us-symbols" value="" placeholder="AAPL,NVDA,TSLA" />
            </div>
            <div>
              <label for="us-limit">每個 RSS 最多幾則</label>
              <select id="us-limit">
                <option value="5" selected>5</option>
                <option value="8">8</option>
                <option value="15">15</option>
                <option value="25">25</option>
              </select>
            </div>
            <label class="toggle" style="align-self:end; min-height:42px;">
              <input type="checkbox" id="us-use-ollama" /> Ollama 分類
            </label>
          </section>
          <section class="controls" style="padding:0; margin-bottom:12px;">
            <div>
              <label for="us-industry">產業篩選</label>
              <select id="us-industry">
                <option value="">全部</option>
                <option value="半導體 / AI">半導體 / AI</option>
                <option value="AI / 伺服器">AI / 伺服器</option>
                <option value="軟體雲端">軟體雲端</option>
                <option value="電動車">電動車</option>
                <option value="金融">金融</option>
                <option value="能源">能源</option>
                <option value="生技醫療">生技醫療</option>
                <option value="消費零售">消費零售</option>
                <option value="原物料">原物料</option>
                <option value="總體經濟">總體經濟</option>
                <option value="其他">其他</option>
              </select>
            </div>
            <div style="align-self:end;">
              <div class="muted">先抓來源 RSS，再依標題與摘要關鍵字分類；產業選單只篩選已抓結果。</div>
            </div>
          </section>
          <div class="obsidian-status">
            <div>
              <div class="obsidian-kicker">Obsidian Vault 同步狀態</div>
              <div class="obsidian-text" id="obsidian-note">讀取 Obsidian 同步狀態...</div>
            </div>
          </div>
          <div class="news-section-title">新聞列表</div>
          <div id="us-news-list"></div>
        </div>
      </section>
    </section>

    <section id="ci-us-news-view" style="display:none;">
      <section class="panel">
        <div class="panel-head">
          <div>
            <div class="panel-title">GitHub 新聞預覽</div>
            <div class="muted" id="ci-us-news-note">讀取 GitHub Action 自動抓取的新聞；這裡只預覽，還不會寫入本機 SQLite 或 Obsidian。</div>
          </div>
        </div>
        <div class="panel-body">
          <section class="controls" style="padding:0; margin-bottom:12px;">
            <div>
              <label for="ci-us-mode">預覽來源</label>
              <select id="ci-us-mode">
                <option value="live" selected>GitHub Pages 最新</option>
                <option value="local">本機快取</option>
              </select>
            </div>
            <div>
              <label for="ci-us-industry">產業篩選</label>
              <select id="ci-us-industry">
                <option value="">全部</option>
                <option value="半導體 / AI">半導體 / AI</option>
                <option value="AI / 伺服器">AI / 伺服器</option>
                <option value="軟體雲端">軟體雲端</option>
                <option value="電動車">電動車</option>
                <option value="金融">金融</option>
                <option value="能源">能源</option>
                <option value="生技醫療">生技醫療</option>
                <option value="消費零售">消費零售</option>
                <option value="原物料">原物料</option>
                <option value="總體經濟">總體經濟</option>
                <option value="其他">其他</option>
              </select>
            </div>
            <div>
              <label for="ci-us-source">來源篩選</label>
              <select id="ci-us-source">
                <option value="">全部</option>
              </select>
            </div>
            <div>
              <label for="ci-us-query">搜尋</label>
              <input id="ci-us-query" placeholder="標題、摘要、來源" />
            </div>
            <div style="align-self:end; display:flex; gap:8px; flex-wrap:wrap;">
              <a class="secondary" id="ci-us-site-link" href="https://wenyen-hsu.github.io/stock/" target="_blank" rel="noreferrer">開啟 GitHub Pages</a>
              <a class="secondary" id="ci-us-json-link" href="https://wenyen-hsu.github.io/stock/data/ci_us_news.json" target="_blank" rel="noreferrer">查看 JSON</a>
            </div>
          </section>
          <div class="news-section-title" id="ci-us-news-title">GitHub Action 抓取結果</div>
          <div id="ci-us-news-list"></div>
        </div>
      </section>
    </section>

    <section id="coverage-view" class="panel" style="display:none;">
      <div class="panel-head">
        <div class="panel-title">更新中心</div>
        <div class="muted">背景執行固定任務；分點資料改在個股頁單檔更新</div>
      </div>
      <div class="panel-body">
        <div class="source-audit">
          <h3>資料來源速查</h3>
          <div class="source-audit-grid">
            <div class="source-audit-card">
              <strong>目前已接入</strong>
              <ul>
                <li><a href="https://www.twse.com.tw/" target="_blank" rel="noreferrer">TWSE</a> / <a href="https://www.tpex.org.tw/" target="_blank" rel="noreferrer">TPEx</a>：行情、成交量、三大法人、融資融券、上市上櫃清單。</li>
                <li><a href="https://www.taifex.com.tw/" target="_blank" rel="noreferrer">TAIFEX</a>：大台、小台、微台三大法人期貨多空與未平倉。</li>
                <li><a href="https://finmindtrade.com/" target="_blank" rel="noreferrer">FinMind</a>：目前用於月營收；分點資料尚未正式接入。</li>
                <li><a href="https://histock.tw/" target="_blank" rel="noreferrer">HiStock</a>：目前分點排行來源，但全市場覆蓋不完整。</li>
                <li>Yahoo 股市、Yahoo Finance、CNBC、MarketWatch：台股與美股新聞標題、連結、摘要。</li>
              </ul>
            </div>
            <div class="source-audit-card">
              <strong>已確認但未接入</strong>
              <ul>
                <li>Stooq：適合全球指數與海外股價歷史備援；對台股法人、分點、營收幫助有限。</li>
                <li>MOPS 公開資訊觀測站：可補重大訊息、法說會、財報公告與官方月營收追溯。</li>
                <li>Goodinfo / CMoney / Wantgoo：資料豐富但偏網頁或商業服務，穩定性與授權風險較高。</li>
              </ul>
            </div>
            <div class="source-audit-card">
              <strong>下一步建議</strong>
              <ul>
                <li>優先測試 FinMind TaiwanStockTradingDailyReport，評估能否取代 HiStock 分點資料。</li>
                <li>若 FinMind 分點可用，改成本機儲存每日分點明細，再自行計算 5 / 20 日排行與均價。</li>
                <li>保留 HiStock 當 fallback，並在覆蓋狀態清楚區分「來源空回應」與「尚未嘗試」。</li>
              </ul>
            </div>
          </div>
        </div>
        <div class="update-grid" id="update-tasks"></div>
        <div class="publish-box" id="static-publish-box">
          <div class="job-meta">
            <div>
              <div class="panel-title">GitHub Pages 發布</div>
              <div class="muted" id="static-publish-note">讀取發布狀態...</div>
            </div>
            <span class="status-pill" id="static-publish-status">讀取中</span>
          </div>
          <div class="publish-actions">
            <button class="secondary" id="static-export">匯出靜態頁</button>
            <button id="static-publish">發布到 GitHub Pages</button>
          </div>
          <pre class="log-box" id="static-publish-log">尚未執行。</pre>
        </div>
        <div class="layout-2">
          <div>
            <div class="panel-title" style="margin-bottom:8px;">分點更新方式</div>
            <div class="empty">
              全市場分點已從一鍵更新移除。需要分點資料時，請進入個股頁按「更新分點資訊」；分點資料只作個股進階確認，不參與排行分數。
            </div>
          </div>
          <div>
            <div class="job-meta">
              <div class="panel-title">最近任務</div>
              <span class="status-pill" id="job-status">尚未執行</span>
            </div>
            <div class="publish-actions" id="job-actions" style="display:none;">
              <button class="secondary" id="cancel-update">停止目前更新</button>
            </div>
            <pre class="log-box" id="job-log">尚未執行更新任務。</pre>
          </div>
        </div>
      </div>
    </section>

    <section class="sources">
      <strong>資料來源與計算方式：</strong>
      每日行情、成交均價、外資、投信、自營商買賣超取自
      <a href="https://www.twse.com.tw/" target="_blank" rel="noreferrer">TWSE 臺灣證券交易所</a>
      與 <a href="https://www.tpex.org.tw/" target="_blank" rel="noreferrer">TPEx 櫃買中心</a>
      公開資料；融資融券取自 TWSE / TPEx 信用交易公開資料；區間外資、投信、均價、排行分數由本機 SQLite 依最近交易日重新彙總。
      大盤加權指數取自 TWSE 指數歷史資料；大台、小台、微台三大法人期貨交易與未平倉多空取自
      <a href="https://www.taifex.com.tw/" target="_blank" rel="noreferrer">TAIFEX 臺灣期貨交易所</a>
      三大法人查詢公開頁。
      月營收 24 個月歷史目前使用
      <a href="https://finmindtrade.com/" target="_blank" rel="noreferrer">FinMind</a>
      免費 API，月增率與年增率由本機依月營收與去年同期計算。
      分點區間排行與可取得的分點日明細目前解析
      <a href="https://histock.tw/" target="_blank" rel="noreferrer">HiStock</a>
      公開頁面；若批次抓取遇到空回應或該分點未進入單日前排行，個股頁會顯示抓取狀態。
      個股新聞採手動按鈕觸發，來源為
      <a href="https://tw.stock.yahoo.com/" target="_blank" rel="noreferrer">Yahoo 股市</a>
      RSS 與公開文章頁，本機只保留標題、連結與內文摘錄。
      美股新聞採手動按鈕觸發，來源為
      <a href="https://finance.yahoo.com/" target="_blank" rel="noreferrer">Yahoo Finance</a>、
      <a href="https://www.cnbc.com/" target="_blank" rel="noreferrer">CNBC</a>、
      <a href="https://www.marketwatch.com/" target="_blank" rel="noreferrer">MarketWatch</a>
      RSS；GitHub 新聞預覽讀取 GitHub Action 產出的靜態新聞 JSON，不寫入本機資料庫；產業分類預設由本機關鍵字規則快速判斷，可選擇 Ollama 模型輔助分類並記錄 token 數。
    </section>
  </main>

  <script>
    window.STOCK_CHIP_STATIC = false;
  </script>
  <script>
    const state = { tab: "ranking", previousTab: "ranking", detail: null, broker: "", lastDataUpdatedAt: "", chartRange: 20, rankingRows: [], rankingSort: null, usNewsRows: [], ciUsNewsRows: [], ciUsNewsMeta: {}, mopsEvents: [], mopsSelectedDate: "", mopsVisibleCount: 50, mopsQuickFilter: "", mopsDateInitialized: false, mopsDateMode: "future30", mopsCalendarStart: "", columnGroup: "core", market: null, marketSentiment: null };
    const STATIC_MODE = window.STOCK_CHIP_STATIC === true;
    const staticCache = {};
    const chartColors = {
      up: "#b42318",
      down: "#087f5b",
      flat: "#667085",
      ma5: "#b42318",
      ma10: "#175cd3",
      ma20: "#f59e0b",
      ma60: "#7a5af8",
      revenueCurrent: "#0f766e",
      revenueLastYear: "#94a3b8",
      revenueYoy: "#b42318",
      margin: "#175cd3",
      short: "#f59e0b",
      index: "#0f766e",
      foreignOi: "#b42318",
      trustOi: "#175cd3",
      dealerOi: "#f59e0b"
    };
    const fmt = (v) => {
      if (v === null || v === undefined || v === "") return "";
      const n = Number(v);
      if (!Number.isFinite(n)) return String(v);
      return Math.abs(n) >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(Math.round(n * 100) / 100);
    };
    const cls = (v) => Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "";
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    function linkifyUrls(text) {
      return esc(text).replace(/(https?:[/][/][^\\s]+)/g, url => `<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`);
    }
    function parseLocalTime(value) {
      if (!value) return null;
      const text = String(value).replace(" ", "T");
      const date = new Date(text);
      return Number.isNaN(date.getTime()) ? null : date;
    }
    function freshness(value, warnHours = 24, badHours = 48) {
      const date = parseLocalTime(value);
      if (!date) return { klass: "bad", label: "尚無資料", detail: "" };
      const hours = Math.max(0, (Date.now() - date.getTime()) / 36e5);
      if (hours >= badHours) return { klass: "bad", label: "過期", detail: `${Math.round(hours)} 小時前` };
      if (hours >= warnHours) return { klass: "warn", label: "偏舊", detail: `${Math.round(hours)} 小時前` };
      return { klass: "done", label: "新鮮", detail: `${Math.round(hours)} 小時前` };
    }
    function staticWatchlistIds() {
      try {
        const saved = JSON.parse(localStorage.getItem("stockChipWatchlist") || "null");
        if (Array.isArray(saved)) return saved.map(String);
      } catch (_err) {}
      return (state.staticMeta?.watchlist || ["2376", "2382", "2324", "6196"]).map(String);
    }
    function saveStaticWatchlist(ids) {
      localStorage.setItem("stockChipWatchlist", JSON.stringify([...new Set(ids.map(String))]));
    }
    async function staticData(path) {
      if (staticCache[path]) return staticCache[path];
      const res = await fetch(path);
      if (!res.ok) throw new Error(`靜態資料不存在：${path}`);
      staticCache[path] = await res.json();
      return staticCache[path];
    }
    function staticFilterRows(rows, market, q, limit, minVolume = 0) {
      const text = String(q || "").trim().toLowerCase();
      const max = Number(limit || 50);
      const volumeFloor = Number(minVolume || 0);
      const output = [];
      for (const row of rows || []) {
        if (market && row.market !== market) continue;
        if (text && !String(row.stock_id || "").toLowerCase().includes(text) && !String(row.name || "").toLowerCase().includes(text)) continue;
        if (volumeFloor > 0 && Number(row.volume_lot || 0) < volumeFloor) continue;
        output.push(row);
        if (output.length >= max) break;
      }
      return output;
    }
    async function staticGetJSON(url) {
      const parsed = new URL(url, location.origin);
      const days = parsed.searchParams.get("days") || document.querySelector("#days")?.value || "20";
      if (parsed.pathname === "/api/meta") {
        const meta = await staticData("data/meta.json");
        state.staticMeta = meta;
        const branch = meta.branch_coverage_by_days?.[days] || {};
        return {...meta, branch_covered: branch.branch_covered || 0};
      }
      if (parsed.pathname === "/api/ranking") {
        const ranking = parsed.searchParams.get("ranking") || "total_score";
        const q = parsed.searchParams.get("q") || "";
        const source = q
          ? await staticData(`data/rankings/${days}d/search_index.json`)
          : await staticData(`data/rankings/${days}d/${ranking}.json`);
        return {
          title: `${days} 日排行：${ranking}`,
          rows: staticFilterRows(source.rows || source, parsed.searchParams.get("market") || "", q, parsed.searchParams.get("limit") || 50, parsed.searchParams.get("min_volume") || 0),
        };
      }
      if (parsed.pathname === "/api/watchlist") {
        const ids = staticWatchlistIds();
        const source = await staticData(`data/rankings/${days}d/search_index.json`);
        const order = new Map(ids.map((id, idx) => [id, idx]));
        const rows = (source.rows || source).filter(row => order.has(String(row.stock_id)))
          .sort((a, b) => order.get(String(a.stock_id)) - order.get(String(b.stock_id)));
        return {rows: staticFilterRows(rows, parsed.searchParams.get("market") || "", parsed.searchParams.get("q") || "", parsed.searchParams.get("limit") || 50, parsed.searchParams.get("min_volume") || 0)};
      }
      if (parsed.pathname === "/api/stock") {
        const stockId = parsed.searchParams.get("stock_id") || "2376";
        const data = await staticData(`data/stocks/${stockId}/${days}d.json`);
        data.stock.in_watchlist = staticWatchlistIds().includes(String(data.stock.stock_id));
        return data;
      }
      if (parsed.pathname === "/api/market") {
        const product = (parsed.searchParams.get("product") || "TXF").toUpperCase();
        try {
          return await staticData(`data/market_${product}.json`);
        } catch (_err) {
          try {
            return await staticData("data/market.json");
          } catch (__err) {
            return {summary:{}, index_rows:[], futures_rows:[], updated_at:""};
          }
        }
      }
      if (parsed.pathname === "/api/market-sentiment") {
        try {
          return await staticData("data/market_sentiment.json");
        } catch (_err) {
          return {score: 50, label: "尚無資料", risk_multiplier: 0.95, parts: {}, reasons: ["靜態資料未包含大盤多空分"]};
        }
      }
      if (parsed.pathname === "/api/coverage") {
        return staticData(`data/coverage_${days}d.json`);
      }
      if (parsed.pathname === "/api/update-tasks") {
        const meta = await staticData("data/meta.json");
        return {running: false, latest_job: null, latest_data_updated_at: meta.exported_at || "", tasks: []};
      }
      if (parsed.pathname === "/api/us-news") {
        const data = await staticData("data/us_news.json");
        const industry = parsed.searchParams.get("industry") || "";
        const limit = Number(parsed.searchParams.get("limit") || 80);
        const rows = (data.rows || []).filter(row => !industry || row.industry === industry).slice(0, limit);
        return {rows};
      }
      if (parsed.pathname === "/api/ci-us-news" || parsed.pathname === "/api/ci-us-news-live") {
        let data;
        try {
          data = await staticData("data/ci_us_news.json");
        } catch (_err) {
          data = await staticData("data/us_news.json");
        }
        const industry = parsed.searchParams.get("industry") || "";
        const source = parsed.searchParams.get("source") || "";
        const q = String(parsed.searchParams.get("q") || "").trim().toLowerCase();
        const limit = Number(parsed.searchParams.get("limit") || 200);
        const rows = (data.rows || []).filter(row => {
          if (industry && row.industry !== industry) return false;
          if (source && row.source !== source) return false;
          if (q) {
            const text = [row.title, row.summary, row.reason, row.source, row.industry].join(" ").toLowerCase();
            if (!text.includes(q)) return false;
          }
          return true;
        }).slice(0, limit);
        return {
          generated_at: data.generated_at || "",
          row_count: data.row_count || (data.rows || []).length,
          fetched_count: data.fetched_count || 0,
          rows,
          sources: [...new Set((data.rows || []).map(row => row.source).filter(Boolean))].sort(),
          source_mode: parsed.pathname === "/api/ci-us-news-live" ? "github_pages" : "local",
          source_url: parsed.pathname === "/api/ci-us-news-live" ? "https://wenyen-hsu.github.io/stock/data/ci_us_news.json" : "data/ci_us_news.json",
        };
      }
      if (parsed.pathname === "/api/mops-events") {
        let data;
        try {
          data = await staticData("data/mops_events.json");
        } catch (_err) {
          data = {rows: []};
        }
        const start = parsed.searchParams.get("start") || "";
        const end = parsed.searchParams.get("end") || "";
        const stock = parsed.searchParams.get("stock_id") || "";
        const q = String(parsed.searchParams.get("q") || "").trim().toLowerCase();
        const limit = Number(parsed.searchParams.get("limit") || 500);
        const rows = (data.rows || []).filter(row => {
          if (start && row.event_date < start) return false;
          if (end && row.event_date > end) return false;
          if (stock && row.stock_id !== stock) return false;
          if (q) {
            const text = [row.title, row.detail, row.company_name, row.stock_id, row.category].join(" ").toLowerCase();
            if (!text.includes(q)) return false;
          }
          return true;
        }).slice(0, limit);
        return {rows, row_count: rows.length};
      }
      throw new Error(`靜態版不支援：${parsed.pathname}`);
    }
    async function getJSON(url) {
      if (STATIC_MODE && String(url).startsWith("/api/")) return staticGetJSON(url);
      const res = await fetch(url);
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }
    async function postJSON(url, payload) {
      if (STATIC_MODE && url === "/api/watchlist") {
        const stockId = String(payload?.stock_id || "");
        const ids = staticWatchlistIds();
        const exists = ids.includes(stockId);
        const next = payload?.action === "remove" ? ids.filter(id => id !== stockId) : exists ? ids : [...ids, stockId];
        saveStaticWatchlist(next);
        return {stock_id: stockId, in_watchlist: next.includes(stockId), watchlist: next};
      }
      if (STATIC_MODE) throw new Error("靜態版無法執行更新，請由本機後端匯出後重新部署。");
      const res = await fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload || {})
      });
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }
    function params() {
      const ranking = document.querySelector("#ranking").value;
      const daysSelect = document.querySelector("#days");
      if (ranking === "foreign_5d_revenue_growth" && daysSelect.value !== "5") {
        daysSelect.value = "5";
      }
      return new URLSearchParams({
        days: daysSelect.value,
        ranking,
        market: document.querySelector("#market").value,
        q: document.querySelector("#query").value.trim(),
        min_volume: document.querySelector("#min-volume").value.trim(),
        limit: document.querySelector("#limit").value
      });
    }
    function enforceRankingDays() {
      const ranking = document.querySelector("#ranking").value;
      const daysSelect = document.querySelector("#days");
      if (ranking === "foreign_5d_revenue_growth" && daysSelect.value !== "5") {
        daysSelect.value = "5";
      }
    }
    function renderMetrics(target, rows) {
      target.innerHTML = rows.map(([label, value, klass]) =>
        `<div class="metric"><div class="label">${esc(label)}</div><div class="value ${klass || ""}">${esc(value)}</div></div>`
      ).join("");
    }
    function sortRows(rows, columns, sort) {
      if (!sort?.key) return rows;
      const col = columns.find(c => c.key === sort.key) || {};
      const textSort = col.sortType === "text";
      const direction = sort.dir === "asc" ? 1 : -1;
      return [...rows].sort((a, b) => {
        const av = a[sort.key];
        const bv = b[sort.key];
        if (!textSort) {
          const an = Number(av);
          const bn = Number(bv);
          if (Number.isFinite(an) && Number.isFinite(bn)) return (an - bn) * direction;
        }
        return String(av ?? "").localeCompare(String(bv ?? ""), "zh-Hant-TW", { numeric: true }) * direction;
      });
    }
    function renderTable(target, rows, columns, options = {}) {
      if (!rows.length) {
        target.innerHTML = `<div class="empty">沒有資料。請先執行更新或調整篩選。</div>`;
        return;
      }
      const sort = options.sort || {};
      const head = columns.map(c => {
        const sortable = options.onSort ? " sortable" : "";
        const mark = sort.key === c.key ? `<span class="sort-mark">${sort.dir === "asc" ? "▲" : "▼"}</span>` : "";
        return `<th class="${sortable}" data-key="${esc(c.key)}">${esc(c.label)}${mark}</th>`;
      }).join("");
      const body = rows.map(row => {
        const clickable = options.onClick ? " clickable" : "";
        const attr = options.rowId ? ` data-id="${esc(options.rowId(row))}"` : "";
        return `<tr class="${clickable}"${attr}>${columns.map(c => {
          const raw = row[c.key];
          const value = c.format ? c.format(raw, row) : fmt(raw);
          const klass = c.signed ? cls(raw) : "";
          return `<td class="${klass}">${esc(value)}</td>`;
        }).join("")}</tr>`;
      }).join("");
      target.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
      if (options.onClick) {
        target.querySelectorAll("tr[data-id]").forEach(tr => tr.addEventListener("click", () => options.onClick(tr.dataset.id)));
      }
      if (options.onSort) {
        target.querySelectorAll("th.sortable[data-key]").forEach(th => th.addEventListener("click", () => options.onSort(th.dataset.key)));
      }
    }
    function selectedMaPeriods() {
      return [...document.querySelectorAll(".ma-toggle:checked")].map(input => input.value);
    }
    function syncSeriesSwatches() {
      document.querySelectorAll(".ma-option").forEach(label => {
        const swatch = label.querySelector(".legend-swatch");
        const color = chartColors[`ma${label.dataset.ma}`] || "#607080";
        if (swatch) swatch.style.background = color;
      });
    }
    function chartRows() {
      const rows = state.detail?.chart || [];
      if (!rows.length) return [];
      const range = state.chartRange === "all" ? rows.length : Number(state.chartRange || 20);
      return rows.slice(-Math.min(rows.length, Math.max(5, range)));
    }
    function setChartRange(value) {
      state.chartRange = value === "all" ? "all" : Number(value || 20);
      const select = document.querySelector("#chart-range");
      if (select) select.value = String(state.chartRange);
      renderPriceChart();
    }
    function zoomChart(delta) {
      const rows = state.detail?.chart || [];
      if (!rows.length) return;
      const current = state.chartRange === "all" ? rows.length : Number(state.chartRange || 20);
      const next = delta < 0 ? Math.max(5, Math.round(current * 0.75)) : Math.min(rows.length, Math.round(current * 1.35));
      state.chartRange = next >= rows.length ? "all" : next;
      const select = document.querySelector("#chart-range");
      if (select) select.value = ["20", "60", "120", "all"].includes(String(state.chartRange)) ? String(state.chartRange) : "all";
      renderPriceChart();
    }
    function renderPriceChart() {
      const canvas = document.querySelector("#price-chart");
      const legend = document.querySelector("#price-legend");
      const allRows = state.detail?.chart || [];
      const rows = chartRows();
      if (!canvas || !legend) return;
      const wrap = canvas.parentElement;
      const dpr = window.devicePixelRatio || 1;
      canvas.style.width = `${wrap.clientWidth}px`;
      canvas.style.height = `${wrap.clientHeight}px`;
      canvas.width = Math.floor(wrap.clientWidth * dpr);
      canvas.height = Math.floor(wrap.clientHeight * dpr);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, wrap.clientWidth, wrap.clientHeight);
      if (!rows.length) {
        legend.innerHTML = `<span class="muted">沒有股價資料。</span>`;
        return;
      }
      const maSeries = selectedMaPeriods().map(period => ({ key: `ma${period}`, label: `MA${period}`, color: chartColors[`ma${period}`] }));
      const values = [];
      rows.forEach(row => {
        ["open", "high", "low", "close"].forEach(key => {
          const n = Number(row[key]);
          if (Number.isFinite(n) && n > 0) values.push(n);
        });
        maSeries.forEach(s => {
          const n = Number(row[s.key]);
          if (Number.isFinite(n) && n > 0) values.push(n);
        });
      });
      if (!values.length) {
        legend.innerHTML = `<span class="muted">沒有可繪製的價格。</span>`;
        return;
      }
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      const pad = { top: 12, right: 54, bottom: 72, left: 48 };
      let min = Math.min(...values);
      let max = Math.max(...values);
      const span = max - min || max * 0.04 || 1;
      min -= span * 0.12;
      max += span * 0.12;
      const x = idx => pad.left + (rows.length <= 1 ? 0 : idx * (w - pad.left - pad.right) / (rows.length - 1));
      const y = value => pad.top + (max - value) * (h - pad.top - pad.bottom) / (max - min);
      ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      ctx.lineWidth = 1;
      ctx.strokeStyle = "#edf1f4";
      ctx.fillStyle = "#607080";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 4; i++) {
        const gy = pad.top + i * (h - pad.top - pad.bottom) / 4;
        const value = max - i * (max - min) / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, gy);
        ctx.lineTo(w - pad.right, gy);
        ctx.stroke();
        ctx.fillText(fmt(value), pad.left - 8, gy);
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const tickStep = Math.max(1, Math.ceil(rows.length / 6));
      rows.forEach((row, idx) => {
        if (idx % tickStep !== 0 && idx !== rows.length - 1) return;
        ctx.fillText(String(row.date).slice(5), x(idx), h - pad.bottom + 10);
      });
      const volumeMax = Math.max(...rows.map(row => Number(row.volume_lot || 0)), 0);
      const volumeTop = h - 44;
      const volumeBottom = h - 18;
      const slot = rows.length <= 1 ? (w - pad.left - pad.right) : (w - pad.left - pad.right) / rows.length;
      const candleWidth = Math.max(4, Math.min(14, slot * 0.58));
      rows.forEach((row, idx) => {
        const open = Number(row.open);
        const high = Number(row.high);
        const low = Number(row.low);
        const close = Number(row.close);
        if (![open, high, low, close].every(Number.isFinite)) return;
        const cx = x(idx);
        const color = close > open ? chartColors.up : close < open ? chartColors.down : chartColors.flat;
        const top = y(Math.max(open, close));
        const bottom = y(Math.min(open, close));
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1.5;
        if (volumeMax > 0) {
          const volume = Number(row.volume_lot || 0);
          const vh = (volume / volumeMax) * (volumeBottom - volumeTop);
          ctx.globalAlpha = 0.22;
          ctx.fillRect(cx - candleWidth / 2, volumeBottom - vh, candleWidth, Math.max(1, vh));
          ctx.globalAlpha = 1;
        }
        ctx.beginPath();
        ctx.moveTo(cx, y(high));
        ctx.lineTo(cx, y(low));
        ctx.stroke();
        const bodyHeight = Math.max(2, bottom - top);
        ctx.fillRect(cx - candleWidth / 2, top, candleWidth, bodyHeight);
      });
      function drawLine(s) {
        ctx.beginPath();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 1.8;
        let started = false;
        rows.forEach((row, idx) => {
          const n = Number(row[s.key]);
          if (!Number.isFinite(n) || n <= 0) {
            started = false;
            return;
          }
          if (!started) {
            ctx.moveTo(x(idx), y(n));
            started = true;
          } else {
            ctx.lineTo(x(idx), y(n));
          }
        });
        ctx.stroke();
      }
      maSeries.forEach(drawLine);
      const latest = rows[rows.length - 1];
      const candleLegend = [
        `<span class="legend-item"><span class="legend-swatch" style="background:${chartColors.up}"></span>上漲K</span>`,
        `<span class="legend-item"><span class="legend-swatch" style="background:${chartColors.down}"></span>下跌K</span>`,
        `<span class="legend-item">收盤 ${esc(fmt(latest?.close))}</span>`,
        `<span class="legend-item">量 ${esc(fmt(latest?.volume_lot))} 張</span>`
      ].join("");
      legend.innerHTML = candleLegend + maSeries.map(s => {
        const value = latest?.[s.key];
        return `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${esc(s.label)} ${esc(fmt(value))}</span>`;
      }).join("") + `<span class="muted">顯示 ${rows.length} / ${allRows.length} 日，滾輪可縮放</span>`;
    }
    function renderRevenueChart() {
      const canvas = document.querySelector("#revenue-chart");
      const legend = document.querySelector("#revenue-legend");
      const rows = [...(state.detail?.revenues || [])].reverse().slice(-12);
      if (!canvas || !legend) return;
      const wrap = canvas.parentElement;
      const dpr = window.devicePixelRatio || 1;
      canvas.style.width = `${wrap.clientWidth}px`;
      canvas.style.height = `${wrap.clientHeight}px`;
      canvas.width = Math.floor(wrap.clientWidth * dpr);
      canvas.height = Math.floor(wrap.clientHeight * dpr);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, wrap.clientWidth, wrap.clientHeight);
      if (!rows.length) {
        legend.innerHTML = `<span class="muted">沒有營收資料。</span>`;
        return;
      }
      const values = [];
      rows.forEach(row => {
        const current = Number(row.revenue_million);
        const lastYear = Number(row.last_year_revenue_million);
        if (Number.isFinite(current) && current > 0) values.push(current);
        if (Number.isFinite(lastYear) && lastYear > 0) values.push(lastYear);
      });
      if (!values.length) {
        legend.innerHTML = `<span class="muted">沒有可繪製的營收。</span>`;
        return;
      }
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      const pad = { top: 14, right: 20, bottom: 42, left: 64 };
      const max = Math.max(...values) * 1.16;
      const y = value => pad.top + (max - value) * (h - pad.top - pad.bottom) / max;
      const baseY = h - pad.bottom;
      ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      ctx.lineWidth = 1;
      ctx.strokeStyle = "#edf1f4";
      ctx.fillStyle = "#607080";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 4; i++) {
        const gy = pad.top + i * (h - pad.top - pad.bottom) / 4;
        const value = max - i * max / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, gy);
        ctx.lineTo(w - pad.right, gy);
        ctx.stroke();
        ctx.fillText(fmt(value), pad.left - 8, gy);
      }
      const plotW = w - pad.left - pad.right;
      const groupW = plotW / rows.length;
      const barW = Math.max(5, Math.min(18, groupW * 0.26));
      const yoyValues = rows.map(row => Number(row.yoy_pct)).filter(Number.isFinite);
      let yoyMin = yoyValues.length ? Math.min(...yoyValues) : 0;
      let yoyMax = yoyValues.length ? Math.max(...yoyValues) : 0;
      const yoySpan = yoyMax - yoyMin || Math.max(Math.abs(yoyMax), 10);
      yoyMin -= yoySpan * 0.18;
      yoyMax += yoySpan * 0.18;
      const yPct = value => pad.top + (yoyMax - value) * (h - pad.top - pad.bottom) / (yoyMax - yoyMin || 1);
      rows.forEach((row, idx) => {
        const center = pad.left + groupW * idx + groupW / 2;
        const lastYear = Number(row.last_year_revenue_million);
        const current = Number(row.revenue_million);
        [
          { value: lastYear, x: center - barW * 0.65, color: chartColors.revenueLastYear },
          { value: current, x: center + barW * 0.65, color: chartColors.revenueCurrent }
        ].forEach(bar => {
          if (!Number.isFinite(bar.value) || bar.value <= 0) return;
          const top = y(bar.value);
          ctx.fillStyle = bar.color;
          ctx.fillRect(bar.x - barW / 2, top, barW, Math.max(1, baseY - top));
        });
        ctx.fillStyle = "#607080";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(String(row.revenue_month || "").slice(2), center, baseY + 10);
      });
      if (yoyValues.length) {
        ctx.strokeStyle = chartColors.revenueYoy;
        ctx.lineWidth = 2;
        ctx.beginPath();
        let started = false;
        rows.forEach((row, idx) => {
          const yoy = Number(row.yoy_pct);
          if (!Number.isFinite(yoy)) {
            started = false;
            return;
          }
          const center = pad.left + groupW * idx + groupW / 2;
          const py = yPct(yoy);
          if (!started) {
            ctx.moveTo(center, py);
            started = true;
          } else {
            ctx.lineTo(center, py);
          }
        });
        ctx.stroke();
        rows.forEach((row, idx) => {
          const yoy = Number(row.yoy_pct);
          if (!Number.isFinite(yoy)) return;
          const center = pad.left + groupW * idx + groupW / 2;
          ctx.fillStyle = chartColors.revenueYoy;
          ctx.beginPath();
          ctx.arc(center, yPct(yoy), 2.6, 0, Math.PI * 2);
          ctx.fill();
        });
      }
      const latest = rows[rows.length - 1];
      legend.innerHTML = [
        `<span class="legend-item"><span class="legend-swatch" style="background:${chartColors.revenueCurrent}"></span>本期 ${esc(fmt(latest?.revenue_million))} 百萬</span>`,
        `<span class="legend-item"><span class="legend-swatch" style="background:${chartColors.revenueLastYear}"></span>去年同期 ${esc(fmt(latest?.last_year_revenue_million))} 百萬</span>`,
        `<span class="legend-item"><span class="legend-swatch" style="background:${chartColors.revenueYoy}"></span>YoY 線</span>`,
        `<span class="${cls(latest?.yoy_pct)}">年增 ${esc(fmt(latest?.yoy_pct))}%</span>`
      ].join("");
    }
    function renderMarginChart() {
      const canvas = document.querySelector("#margin-chart");
      const legend = document.querySelector("#margin-legend");
      const rows = [...(state.detail?.margin || [])].reverse();
      if (!canvas || !legend) return;
      const wrap = canvas.parentElement;
      const dpr = window.devicePixelRatio || 1;
      canvas.style.width = `${wrap.clientWidth}px`;
      canvas.style.height = `${wrap.clientHeight}px`;
      canvas.width = Math.floor(wrap.clientWidth * dpr);
      canvas.height = Math.floor(wrap.clientHeight * dpr);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, wrap.clientWidth, wrap.clientHeight);
      if (!rows.length) {
        legend.innerHTML = `<span class="muted">沒有融資融券資料。</span>`;
        return;
      }
      const series = [
        { key: "margin_balance_lot", label: "融資餘額", color: chartColors.margin },
        { key: "short_balance_lot", label: "融券餘額", color: chartColors.short }
      ];
      const values = [];
      rows.forEach(row => series.forEach(s => {
        const n = Number(row[s.key]);
        if (Number.isFinite(n)) values.push(n);
      }));
      if (!values.length) {
        legend.innerHTML = `<span class="muted">沒有可繪製的融資融券餘額。</span>`;
        return;
      }
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      const pad = { top: 14, right: 28, bottom: 38, left: 62 };
      let min = Math.min(...values);
      let max = Math.max(...values);
      const span = max - min || Math.max(max * 0.08, 1);
      min -= span * 0.12;
      max += span * 0.12;
      const x = idx => pad.left + (rows.length <= 1 ? 0 : idx * (w - pad.left - pad.right) / (rows.length - 1));
      const y = value => pad.top + (max - value) * (h - pad.top - pad.bottom) / (max - min);
      ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      ctx.lineWidth = 1;
      ctx.strokeStyle = "#edf1f4";
      ctx.fillStyle = "#607080";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 4; i++) {
        const gy = pad.top + i * (h - pad.top - pad.bottom) / 4;
        const value = max - i * (max - min) / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, gy);
        ctx.lineTo(w - pad.right, gy);
        ctx.stroke();
        ctx.fillText(fmt(value), pad.left - 8, gy);
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const tickStep = Math.max(1, Math.ceil(rows.length / 5));
      rows.forEach((row, idx) => {
        if (idx % tickStep !== 0 && idx !== rows.length - 1) return;
        ctx.fillText(String(row.date || "").slice(5), x(idx), h - pad.bottom + 10);
      });
      series.forEach(s => {
        ctx.beginPath();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        let started = false;
        rows.forEach((row, idx) => {
          const n = Number(row[s.key]);
          if (!Number.isFinite(n)) {
            started = false;
            return;
          }
          if (!started) {
            ctx.moveTo(x(idx), y(n));
            started = true;
          } else {
            ctx.lineTo(x(idx), y(n));
          }
        });
        ctx.stroke();
      });
      const latest = rows[rows.length - 1];
      legend.innerHTML = series.map(s => `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${esc(s.label)} ${esc(fmt(latest?.[s.key]))}</span>`).join("");
    }
    function drawLineChart(canvas, legend, rows, series, options = {}) {
      if (!canvas || !legend) return;
      const wrap = canvas.parentElement;
      const dpr = window.devicePixelRatio || 1;
      canvas.style.width = `${wrap.clientWidth}px`;
      canvas.style.height = `${wrap.clientHeight}px`;
      canvas.width = Math.floor(wrap.clientWidth * dpr);
      canvas.height = Math.floor(wrap.clientHeight * dpr);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, wrap.clientWidth, wrap.clientHeight);
      if (!rows.length) {
        legend.innerHTML = `<span class="muted">${esc(options.empty || "沒有資料。")}</span>`;
        return;
      }
      const values = [];
      rows.forEach(row => series.forEach(s => {
        const n = Number(row[s.key]);
        if (Number.isFinite(n)) values.push(n);
      }));
      if (!values.length) {
        legend.innerHTML = `<span class="muted">${esc(options.empty || "沒有可繪製資料。")}</span>`;
        return;
      }
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      const pad = { top: 14, right: 28, bottom: 42, left: 72 };
      let min = Math.min(...values);
      let max = Math.max(...values);
      if (options.zeroLine) {
        min = Math.min(min, 0);
        max = Math.max(max, 0);
      }
      const span = max - min || Math.max(Math.abs(max), 1);
      min -= span * 0.12;
      max += span * 0.12;
      const x = idx => pad.left + (rows.length <= 1 ? 0 : idx * (w - pad.left - pad.right) / (rows.length - 1));
      const y = value => pad.top + (max - value) * (h - pad.top - pad.bottom) / (max - min);
      ctx.font = "12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
      ctx.lineWidth = 1;
      ctx.strokeStyle = "#edf1f4";
      ctx.fillStyle = "#607080";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 4; i++) {
        const gy = pad.top + i * (h - pad.top - pad.bottom) / 4;
        const value = max - i * (max - min) / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, gy);
        ctx.lineTo(w - pad.right, gy);
        ctx.stroke();
        ctx.fillText(fmt(value), pad.left - 8, gy);
      }
      if (options.zeroLine && min < 0 && max > 0) {
        const zy = y(0);
        ctx.strokeStyle = "#b8c4cc";
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(pad.left, zy);
        ctx.lineTo(w - pad.right, zy);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const tickStep = Math.max(1, Math.ceil(rows.length / 6));
      rows.forEach((row, idx) => {
        if (idx % tickStep !== 0 && idx !== rows.length - 1) return;
        ctx.fillText(String(row.date || "").slice(5), x(idx), h - pad.bottom + 10);
      });
      series.forEach(s => {
        ctx.beginPath();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        let started = false;
        rows.forEach((row, idx) => {
          const n = Number(row[s.key]);
          if (!Number.isFinite(n)) {
            started = false;
            return;
          }
          if (!started) {
            ctx.moveTo(x(idx), y(n));
            started = true;
          } else {
            ctx.lineTo(x(idx), y(n));
          }
        });
        ctx.stroke();
      });
      const latest = rows[rows.length - 1] || {};
      legend.innerHTML = series.map(s => `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${esc(s.label)} ${esc(fmt(latest?.[s.key]))}</span>`).join("");
    }
    function marketRowsInRange(rows) {
      const range = document.querySelector("#market-range")?.value || "120";
      if (range === "all") return rows || [];
      const count = Math.max(10, Number(range || 120));
      return (rows || []).slice(-count);
    }
    function sentimentClass(score) {
      const n = Number(score);
      if (n >= 55) return "pos";
      if (n < 45) return "neg";
      return "";
    }
    function renderSentiment(target, data, compact = false) {
      if (!target) return;
      if (!data) {
        target.innerHTML = `<div class="empty">讀取大盤多空分中...</div>`;
        return;
      }
      const parts = data.parts || {};
      const partItems = [
        ["指數趨勢", parts.index_trend, "加權指數相對 MA5、MA20、MA60 的位置。分數越高代表大盤技術趨勢越偏多，跌破均線會扣分。"],
        ["外資期貨", parts.foreign_futures, "外資在大台、小台、微台的未平倉多空淨額，依商品大小加權。正分代表外資期貨部位偏多，負分代表偏空。"],
        ["期貨交易", parts.futures_trade, "外資近 5 日期貨交易淨額的方向。正分代表近期偏買多或回補空單，負分代表近期偏放空或減多。"],
        ["投信期貨", parts.trust_futures, "投信在台指期的未平倉多空淨額。此項權重較小，用來觀察投信是否與市場方向一致。"],
        ["融資融券", parts.market_margin, "全市場融資與融券變化。指數上漲但融資快速增加會扣分；融資下降或融券增加且指數不弱，通常加分。"],
        ["自營避險", parts.dealer_hedge, "自營商期貨未平倉多空淨額，主要視為避險壓力。負分偏向避險空單較重，可能代表市場風險升高。"],
      ];
      const reasons = (data.reasons || []).slice(0, compact ? 3 : 6).map(esc).join("；");
      target.innerHTML = `
        <div class="sentiment-strip">
          <div>
            <div class="muted tooltip-anchor" data-tip="用加權指數趨勢、外資/投信/自營商期貨部位與全市場融資融券估算市場多空環境。50 附近為中性，高於 55 偏多，低於 45 偏空。" title="用加權指數趨勢、期貨三大法人與全市場融資融券估算市場多空環境。">大盤多空分</div>
            <div class="sentiment-score ${sentimentClass(data.score)}">${esc(fmt(data.score))}</div>
            <div class="sentiment-label ${sentimentClass(data.score)}">${esc(data.label || "")}</div>
          </div>
          <div>
            <div class="muted tooltip-anchor" data-tip="用大盤多空分換算成排名調整係數。偏多時不扣分或小幅加分；偏空時降低風險調整分。原始基礎分不會被改寫。" title="用大盤多空分換算成排名調整係數。">風險調整</div>
            <div class="sentiment-score">${esc(fmt((Number(data.risk_multiplier || 1) * 100)))}%</div>
            <div class="sentiment-detail">排行表的「風險調整分」= 基礎分 × 此係數；原始基礎分不改。</div>
          </div>
          <div>
            <div class="sentiment-parts">
              ${partItems.map(([label, value, tip]) => `
                <div class="sentiment-part tooltip-anchor" data-tip="${esc(tip)}" title="${esc(tip)}">
                  <div class="part-label">${esc(label)}</div>
                  <div class="part-value ${cls(value)}">${esc(fmt(value))}</div>
                </div>
              `).join("")}
            </div>
            <div class="sentiment-detail">${reasons || "尚無足夠資料"}</div>
          </div>
        </div>`;
    }
    async function loadMarketSentiment() {
      const data = await getJSON("/api/market-sentiment");
      state.marketSentiment = data;
      renderSentiment(document.querySelector("#ranking-sentiment"), data, true);
      renderSentiment(document.querySelector("#market-sentiment"), data, false);
      if (state.rankingRows?.length) {
        applyRiskAdjustedScores();
        renderRankingTable();
      }
      return data;
    }
    function applyRiskAdjustedScores() {
      const sentiment = state.marketSentiment || {};
      const multiplier = Number(sentiment.risk_multiplier || 1);
      const score = Number(sentiment.score);
      state.rankingRows = (state.rankingRows || []).map(row => ({
        ...row,
        market_sentiment_score: Number.isFinite(score) ? score : null,
        risk_adjusted_score: Number.isFinite(Number(row.total_score))
          ? Math.round(Number(row.total_score) * multiplier * 100) / 100
          : null,
      }));
    }
    function renderMarketView(data) {
      state.market = data || {};
      const indexRows = marketRowsInRange(data?.index_rows || []);
      const futuresRows = marketRowsInRange(data?.futures_rows || []);
      const summary = data?.summary || {};
      renderMetrics(document.querySelector("#market-metrics"), [
        ["指數最新日", summary.latest_index_date || ""],
        ["收盤", fmt(summary.latest_index_close)],
        ["商品", summary.product_label || ""],
        ["外資未平倉淨額", fmt(summary.foreign_oi_net), cls(summary.foreign_oi_net)]
      ]);
      document.querySelector("#market-note").textContent = data?.updated_at
        ? `本機資料最後更新 ${data.updated_at}；期貨商品可切換大台、小台、微台。`
        : "尚無大盤資料，請到資料狀態執行「大盤指數與期貨多空」。";
      document.querySelector("#market-index-note").textContent = indexRows.length ? `顯示 ${indexRows.length} 筆` : "尚無指數資料";
      document.querySelector("#market-futures-note").textContent = futuresRows.length ? `顯示 ${futuresRows.length} 筆，單位：口` : "尚無期貨多空資料";
      drawLineChart(
        document.querySelector("#market-index-chart"),
        document.querySelector("#market-index-legend"),
        indexRows,
        [{ key: "close", label: "收盤指數", color: chartColors.index }],
        { empty: "沒有加權指數資料。" }
      );
      drawLineChart(
        document.querySelector("#market-futures-chart"),
        document.querySelector("#market-futures-legend"),
        futuresRows,
        [
          { key: "foreign_oi_net", label: "外資未平倉淨額", color: chartColors.foreignOi },
          { key: "trust_oi_net", label: "投信未平倉淨額", color: chartColors.trustOi },
          { key: "dealer_oi_net", label: "自營商未平倉淨額", color: chartColors.dealerOi }
        ],
        { empty: "沒有期貨多空資料。", zeroLine: true }
      );
      renderTable(document.querySelector("#market-futures-table"), [...futuresRows].reverse(), [
        {key:"date", label:"日期"},
        {key:"foreign_trade_net", label:"外資交易淨額", signed:true},
        {key:"foreign_oi_net", label:"外資未平倉淨額", signed:true},
        {key:"trust_trade_net", label:"投信交易淨額", signed:true},
        {key:"trust_oi_net", label:"投信未平倉淨額", signed:true},
        {key:"dealer_trade_net", label:"自營商交易淨額", signed:true},
        {key:"dealer_oi_net", label:"自營商未平倉淨額", signed:true}
      ]);
    }
    async function loadMarket() {
      const indexCode = document.querySelector("#market-index-code")?.value || "TAIEX";
      const product = document.querySelector("#market-product")?.value || "TXF";
      const range = document.querySelector("#market-range")?.value || "120";
      const limit = range === "all" ? 5000 : Math.max(120, Number(range || 120));
      const data = await getJSON(`/api/market?index=${encodeURIComponent(indexCode)}&product=${encodeURIComponent(product)}&limit=${limit}`);
      renderMarketView(data);
      await loadMarketSentiment();
    }
    function stockEventTag(row) {
      const text = `${row?.event_tag || ""} ${row?.title || ""} ${row?.detail || ""}`;
      if (/股東常會|股東會|股東臨時會|停止過戶/.test(text)) return "股東會";
      if (/營收|合併營收|自結營收/.test(text)) return "營收";
      if (/財務報告|財報|每股盈餘|損益|會計師/.test(text)) return "財報";
      if (/除權|除息|配息|配股|股利/.test(text)) return "除權息";
      if (/法說會|法人說明會|業績發表會/.test(text)) return "法說會";
      if (/董事會|審計委員會/.test(text)) return "董事會";
      if (/重大訊息|重大訊息說明/.test(text)) return "重大訊息";
      return row?.event_tag || "其他";
    }
    function renderStockEvents() {
      const target = document.querySelector("#stock-events-list");
      const note = document.querySelector("#stock-events-note");
      if (!target || !note) return;
      const rows = state.detail?.mops_events || [];
      const stock = state.detail?.stock?.stock_id || document.querySelector("#detail-stock")?.value.trim() || "";
      if (!rows.length) {
        note.textContent = "近 30 日無已抓取 MOPS 公告；這不代表未來沒有實際事件，只代表目前快取沒有該股公告。";
        target.innerHTML = `<div class="empty">近 30 日無已抓取重大事件。</div>`;
        return;
      }
      const tags = Array.from(new Set(rows.map(stockEventTag)));
      const latest = rows.map(row => row.fetched_at).filter(Boolean).sort().at(-1) || "";
      note.textContent = `${STATIC_MODE ? "靜態快取" : "本機快取"} ${rows.length} 件，分類：${tags.join("、")}；最後抓取 ${latest || "-"}`;
      target.innerHTML = `
        <div class="event-alert">
          <div class="event-alert-summary">
            <span class="status-pill ${rows.length ? "warn" : ""}">${esc(rows.length)} 件公告</span>
            <span>日期為公告發布日；後續可再解析內文中的實際事件日。</span>
          </div>
          ${rows.slice(0, 8).map(row => {
            const tag = stockEventTag(row);
            const important = ["股東會", "除權息", "財報", "重大訊息"].includes(tag);
            const text = (row.detail || "").replace(/\\s+/g, " ").slice(0, 150);
            return `<article class="event-item ${important ? "important" : ""}">
              <div class="event-line">
                <div>
                  <span class="event-tag ${tag === "營收" ? "good" : important ? "hot" : ""}">${esc(tag)}</span>
                  <span class="event-meta">${esc(row.event_date || "")} ${esc(row.event_time || "")}</span>
                </div>
                <a class="event-open" href="${esc(row.source_url || "https://mopsov.twse.com.tw/mops/web/t05st02")}" target="_blank" rel="noreferrer">開啟 MOPS</a>
              </div>
              <div class="event-title">${esc(row.title || "未命名事件")}</div>
              <div class="event-meta">${esc(row.category || "MOPS")} · ${esc(row.company_name || stock)}</div>
              ${text ? `<div class="event-text">${esc(text)}${(row.detail || "").length > 150 ? "..." : ""}</div>` : ""}
            </article>`;
          }).join("")}
          ${rows.length > 8 ? `<div class="muted">尚有 ${esc(fmt(rows.length - 8))} 件，請到重大事件頁查看完整列表。</div>` : ""}
        </div>`;
    }
    function renderNews() {
      const target = document.querySelector("#news-list");
      const note = document.querySelector("#news-note");
      const rows = state.detail?.news || [];
      if (!target || !note) return;
      const latestFetch = rows.map(row => row.fetched_at).filter(Boolean).sort().at(-1);
      note.textContent = latestFetch
        ? `${STATIC_MODE ? "靜態快取" : "本機快取"} ${rows.length} 則，最後抓取 ${latestFetch}`
        : STATIC_MODE ? "本次匯出尚無新聞快取" : "尚未抓取；按下按鈕才會連到 Yahoo 股市抓標題與內文摘錄";
      if (!rows.length) {
        target.innerHTML = `<div class="empty">${STATIC_MODE ? "本次匯出尚無新聞快取。" : "尚無新聞快取。需要時按「抓取新聞」。"}</div>`;
        return;
      }
      target.innerHTML = `<div class="news-list">${rows.map(row => {
        const text = row.content_excerpt || row.summary || "";
        return `<article class="news-item">
          <a class="news-title" href="${esc(row.url)}" target="_self" rel="noreferrer">${esc(row.title)}</a>
          <a class="news-open" href="${esc(row.url)}" target="_self" rel="noreferrer">開啟原文</a>
          <div class="news-meta">${esc(row.source || "Yahoo股市")} · ${esc(row.published_at || "")} · 抓取 ${esc(row.fetched_at || "")}</div>
          <div class="news-text">${esc(text)}</div>
        </article>`;
      }).join("")}</div>`;
    }
    async function refreshNews() {
      const stock = state.detail?.stock?.stock_id || document.querySelector("#detail-stock").value.trim();
      const btn = document.querySelector("#refresh-news");
      if (!stock || !btn) return;
      btn.disabled = true;
      btn.textContent = "抓取中";
      document.querySelector("#news-note").textContent = "正在抓取 Yahoo 股市 RSS 與文章頁...";
      try {
        const result = await postJSON("/api/news/refresh", { stock_id: stock, limit: 8 });
        if (state.detail?.stock?.stock_id === result.stock_id) {
          state.detail.news = result.rows || [];
          renderNews();
        }
      } catch (err) {
        document.querySelector("#news-note").textContent = `抓取失敗：${err.message}`;
      } finally {
        btn.disabled = false;
        btn.textContent = "抓取新聞";
      }
    }
    function renderUSNews(rows) {
      const target = document.querySelector("#us-news-list");
      const note = document.querySelector("#us-news-note");
      if (!target || !note) return;
      const latestFetch = rows.map(row => row.fetched_at).filter(Boolean).sort().at(-1);
      const aiCount = rows.filter(row => String(row.classified_by || "").startsWith("ollama:")).length;
      const tokenTotal = rows.reduce((sum, row) => sum + Number(row.classification_total_tokens || 0), 0);
      note.textContent = latestFetch
        ? `本機快取 ${rows.length} 則，最後抓取 ${latestFetch}，Ollama 分類 ${aiCount} 則，分類 token ${fmt(tokenTotal)}`
        : "尚無快取；按「抓取美股新聞」後會抓 Yahoo Finance、CNBC、MarketWatch RSS，預設用 0 token 規則分類";
      if (!rows.length) {
        target.innerHTML = `<div class="empty">${STATIC_MODE ? "靜態版目前未匯出美股新聞。" : "尚無美股新聞快取。"}</div>`;
        return;
      }
      target.innerHTML = `<div class="news-list">${rows.map(row => {
        const sentimentClass = row.sentiment === "偏多" ? "good" : row.sentiment === "偏空" ? "bad" : "";
        const classifierClass = String(row.classified_by || "").startsWith("ollama:") ? "ai" : "";
        return `<article class="news-item">
          <a class="news-title" href="${esc(row.url)}" target="_self" rel="noreferrer">${esc(row.title)}</a>
          <a class="news-open" href="${esc(row.url)}" target="_self" rel="noreferrer">開啟原文</a>
          <div class="news-meta">${esc(row.symbol || "MARKET")} · ${esc(row.source || "")} · ${esc(row.published_at || "")} · 抓取 ${esc(row.fetched_at || "")}</div>
          <div class="tag-list">
            <span class="tag">${esc(row.industry || "其他")}</span>
            <span class="tag">${esc(row.event_type || "一般新聞")}</span>
            <span class="tag ${sentimentClass}">${esc(row.sentiment || "中性")}</span>
            <span class="tag ${classifierClass}">${esc(row.classified_by || "rules")}</span>
            <span class="tag">信心 ${esc(fmt(row.confidence || 0))}</span>
            <span class="tag">token ${esc(fmt(row.classification_total_tokens || 0))}</span>
          </div>
          ${row.matched_keywords ? `<div class="news-match">命中詞：${esc(row.matched_keywords)}</div>` : ""}
          <div class="news-text">${esc(row.reason || row.summary || "")}</div>
        </article>`;
      }).join("")}</div>`;
    }
    function renderCIUSNews(data) {
      const target = document.querySelector("#ci-us-news-list");
      const note = document.querySelector("#ci-us-news-note");
      const sourceSelect = document.querySelector("#ci-us-source");
      if (!target || !note) return;
      const rows = data?.rows || [];
      state.ciUsNewsRows = rows;
      state.ciUsNewsMeta = data || {};
      if (sourceSelect && !sourceSelect.dataset.loaded) {
        const current = sourceSelect.value;
        sourceSelect.innerHTML = `<option value="">全部</option>${(data?.sources || []).map(source => `<option value="${esc(source)}">${esc(source)}</option>`).join("")}`;
        sourceSelect.value = current;
        sourceSelect.dataset.loaded = "1";
      }
      const generated = data?.generated_at || "";
      const fallback = data?.fallback ? "，目前以正式新聞檔暫代，等 GitHub Action 下一次成功後會改讀 CI 檔" : "";
      const sourceLabel = data?.source_mode === "github_pages" ? "GitHub Pages 最新" : "本機快取";
      note.textContent = generated
        ? `${sourceLabel}：GitHub Action 新聞 ${fmt(data?.row_count || rows.length)} 則，最後抓取 ${generated}${fallback}`
        : `${sourceLabel}：尚未產生 GitHub Action 新聞檔${fallback}`;
      const title = document.querySelector("#ci-us-news-title");
      if (title) title.textContent = data?.source_mode === "github_pages" ? "GitHub Pages 最新抓取結果" : "本機快取抓取結果";
      if (!rows.length) {
        target.innerHTML = `<div class="empty">目前沒有可預覽的 GitHub 新聞。</div>`;
        return;
      }
      target.innerHTML = `<div class="news-list">${rows.map(row => {
        const sentimentClass = row.sentiment === "偏多" ? "good" : row.sentiment === "偏空" ? "bad" : "";
        return `<article class="news-item">
          <a class="news-title" href="${esc(row.url)}" target="_self" rel="noreferrer">${esc(row.title)}</a>
          <a class="news-open" href="${esc(row.url)}" target="_self" rel="noreferrer">開啟原文</a>
          <div class="news-meta">${esc(row.symbol || "MARKET")} · ${esc(row.source || "")} · ${esc(row.published_at || "")} · 抓取 ${esc(row.fetched_at || "")}</div>
          <div class="tag-list">
            <span class="tag">${esc(row.industry || "其他")}</span>
            <span class="tag">${esc(row.event_type || "一般新聞")}</span>
            <span class="tag ${sentimentClass}">${esc(row.sentiment || "中性")}</span>
            <span class="tag">${esc(row.classified_by || "rules")}</span>
            <span class="tag">信心 ${esc(fmt(row.confidence || 0))}</span>
          </div>
          ${row.matched_keywords ? `<div class="news-match">命中詞：${esc(row.matched_keywords)}</div>` : ""}
          <div class="news-text">${esc(row.summary || row.reason || "")}</div>
        </article>`;
      }).join("")}</div>`;
    }
    async function loadCIUSNews() {
      const industry = document.querySelector("#ci-us-industry")?.value || "";
      const source = document.querySelector("#ci-us-source")?.value || "";
      const q = document.querySelector("#ci-us-query")?.value || "";
      const mode = document.querySelector("#ci-us-mode")?.value || "live";
      const endpoint = mode === "local" ? "/api/ci-us-news" : "/api/ci-us-news-live";
      const data = await getJSON(`${endpoint}?industry=${encodeURIComponent(industry)}&source=${encodeURIComponent(source)}&q=${encodeURIComponent(q)}&limit=200`);
      renderCIUSNews(data);
    }
    function isoDate(offset = 0) {
      const day = new Date();
      day.setDate(day.getDate() + offset);
      return localDateString(day);
    }
    function localDateString(day) {
      const year = day.getFullYear();
      const month = String(day.getMonth() + 1).padStart(2, "0");
      const date = String(day.getDate()).padStart(2, "0");
      return `${year}-${month}-${date}`;
    }
    function parseLocalDate(value) {
      if (!value) return null;
      const [year, month, day] = value.split("-").map(Number);
      if (!year || !month || !day) return null;
      return new Date(year, month - 1, day);
    }
    function addLocalDays(day, offset) {
      const next = new Date(day);
      next.setDate(next.getDate() + offset);
      return next;
    }
    function addLocalMonths(day, offset) {
      const next = new Date(day);
      next.setMonth(next.getMonth() + offset);
      return next;
    }
    function setMopsRange(mode, shouldLoad = true) {
      const start = document.querySelector("#mops-start");
      const end = document.querySelector("#mops-end");
      state.mopsDateMode = mode;
      state.mopsDateInitialized = true;
      if (mode === "future30") {
        if (start) start.value = isoDate(0);
        if (end) end.value = isoDate(30);
      } else if (mode === "recent30") {
        if (start) start.value = isoDate(-30);
        if (end) end.value = isoDate(0);
      } else if (mode === "all") {
        if (start) start.value = "";
        if (end) end.value = "";
      }
      state.mopsSelectedDate = "";
      state.mopsVisibleCount = 50;
      state.mopsQuickFilter = "";
      state.mopsCalendarStart = start?.value || "";
      if (shouldLoad) loadMopsEvents();
    }
    function initMopsDates() {
      if (!state.mopsDateInitialized) setMopsRange("future30", false);
    }
    function renderMopsEventDetail(row) {
      const panel = document.querySelector("#mops-event-detail-panel");
      if (!panel || !row) return;
      panel.style.display = "";
      document.querySelector("#mops-detail-title").textContent = row.title || "事件明細";
      document.querySelector("#mops-detail-meta").textContent =
        `${row.event_date || ""} ${row.event_time || ""} · ${row.stock_id || ""} ${row.company_name || ""} · ${row.category || ""}`;
      document.querySelector("#mops-detail-text").textContent = row.detail || "此事件未解析到內文，請開啟 MOPS 原頁確認。";
      const link = document.querySelector("#mops-source-link");
      if (link) link.href = row.source_url || "https://mopsov.twse.com.tw/mops/web/t05st02";
      panel.scrollIntoView({behavior: "smooth", block: "nearest"});
    }
    function renderMopsCalendar(rows) {
      const target = document.querySelector("#mops-calendar");
      if (!target) return;
      const startValue = document.querySelector("#mops-start")?.value || "";
      const endValue = document.querySelector("#mops-end")?.value || "";
      const byDate = new Map();
      rows.forEach(row => {
        if (!byDate.has(row.event_date)) byDate.set(row.event_date, []);
        byDate.get(row.event_date).push(row);
      });
      const dates = [...byDate.keys()].sort();
      const fallbackEnd = dates.at(-1) || isoDate(0);
      const fallbackStart = dates.length ? dates[0] : fallbackEnd;
      const rangeStartText = startValue || fallbackStart;
      const rangeEndText = endValue || fallbackEnd;
      let start = parseLocalDate(rangeStartText);
      let end = parseLocalDate(rangeEndText);
      if (!start || !end || start > end) {
        target.innerHTML = `<div class="empty">請選擇有效日期區間。</div>`;
        return;
      }
      let windowStart = parseLocalDate(state.mopsCalendarStart || rangeStartText) || start;
      if (windowStart < start) windowStart = start;
      if (windowStart > end) windowStart = start;
      let windowEnd = addLocalDays(windowStart, 29);
      if (windowEnd > end) windowEnd = end;
      const days = [];
      for (let d = new Date(windowStart); d <= windowEnd && days.length < 30; d.setDate(d.getDate() + 1)) {
        days.push(localDateString(d));
      }
      const maxCount = Math.max(1, ...days.map(date => (byDate.get(date) || []).length));
      if (!days.length) {
        target.innerHTML = `<div class="empty">請選擇有效日期區間。</div>`;
        return;
      }
      state.mopsCalendarStart = localDateString(windowStart);
      target.innerHTML = `
        <div class="calendar-window-bar">
          <button class="secondary calendar-nav" data-mops-calendar-step="-1" title="上個月" aria-label="上個月">&lsaquo;</button>
          <span class="window-label">月曆顯示 ${esc(localDateString(windowStart))} ~ ${esc(localDateString(windowEnd))}；查詢區間 ${esc(rangeStartText)} ~ ${esc(rangeEndText)}</span>
          <button class="secondary calendar-nav" data-mops-calendar-step="1" title="下個月" aria-label="下個月">&rsaquo;</button>
        </div>
        <div class="calendar-grid">${days.map(date => {
        const items = byDate.get(date) || [];
        const heat = items.length ? 0.12 + Math.min(0.72, items.length / maxCount * 0.58) : 0;
        const style = items.length ? `style="background: rgba(15, 118, 110, ${heat});"` : "";
        const selected = state.mopsSelectedDate === date ? "selected" : "";
        return `<div class="calendar-day ${items.length ? "" : "empty-day"} ${selected}" data-date="${esc(date)}" ${style}>
          <div class="calendar-date"><span>${esc(date.slice(5))}</span><span>${esc(fmt(items.length))} 件</span></div>
          <div class="calendar-count">${esc(fmt(items.length))}</div>
          <div class="calendar-hint">${items.length ? "點日期查看列表" : "無事件"}</div>
        </div>`;
      }).join("")}</div>`;
    }
    function mopsRowsForSelectedDate() {
      let rows = state.mopsEvents || [];
      if (state.mopsSelectedDate) rows = rows.filter(row => row.event_date === state.mopsSelectedDate);
      if (state.mopsQuickFilter) {
        const q = state.mopsQuickFilter;
        rows = rows.filter(row => [row.title, row.detail, row.company_name, row.category].join(" ").includes(q));
      }
      return rows;
    }
    function renderMopsSummary(rows) {
      const target = document.querySelector("#mops-summary");
      if (!target) return;
      const categories = {};
      const companies = {};
      rows.forEach(row => {
        categories[row.category || "未分類"] = (categories[row.category || "未分類"] || 0) + 1;
        const name = `${row.stock_id || ""} ${row.company_name || ""}`.trim() || "未知公司";
        companies[name] = (companies[name] || 0) + 1;
      });
      const topCompany = Object.entries(companies).sort((a, b) => b[1] - a[1])[0];
      target.innerHTML = `
        <div class="mops-summary-card"><div class="label">目前日期</div><div class="value">${esc(state.mopsSelectedDate || "全部")}</div></div>
        <div class="mops-summary-card"><div class="label">符合事件</div><div class="value">${esc(fmt(rows.length))}</div></div>
        <div class="mops-summary-card"><div class="label">重大訊息</div><div class="value">${esc(fmt(categories["重大訊息"] || 0))}</div></div>
        <div class="mops-summary-card"><div class="label">公告</div><div class="value">${esc(fmt(categories["公告"] || 0))}</div></div>
        <div class="mops-summary-card"><div class="label">最多事件公司</div><div class="value">${esc(topCompany ? `${topCompany[0]} ${topCompany[1]}件` : "-")}</div></div>
      `;
    }
    function renderMopsQuickFilters(rows) {
      const target = document.querySelector("#mops-quick-filters");
      if (!target) return;
      const filters = ["董事會", "股東會", "營收", "法說", "取得", "處分", "除權", "除息", "停牌", "復牌"];
      target.innerHTML = `<button class="quick-filter ${state.mopsQuickFilter ? "" : "active"}" data-filter="">全部</button>` +
        filters.map(filter => {
          const count = rows.filter(row => [row.title, row.detail].join(" ").includes(filter)).length;
          return `<button class="quick-filter ${state.mopsQuickFilter === filter ? "active" : ""}" data-filter="${esc(filter)}">${esc(filter)} ${esc(fmt(count))}</button>`;
        }).join("");
    }
    function renderMopsEventList() {
      const list = document.querySelector("#mops-event-list");
      const title = document.querySelector("#mops-list-title");
      if (!list) return;
      const rows = mopsRowsForSelectedDate();
      renderMopsSummary(rows);
      renderMopsQuickFilters(state.mopsSelectedDate ? (state.mopsEvents || []).filter(row => row.event_date === state.mopsSelectedDate) : state.mopsEvents || []);
      if (title) title.textContent = state.mopsSelectedDate ? `${state.mopsSelectedDate} 事件列表` : "全部日期事件列表";
      if (!rows.length) {
        list.innerHTML = `<div class="empty">目前查無事件。可調整日期、股票代號或關鍵字。</div>`;
        return;
      }
      const visible = rows.slice(0, state.mopsVisibleCount || 50);
      list.innerHTML = visible.map(row => `<article class="event-card" data-event-id="${esc(row.event_id)}">
        <div class="event-title">${esc(row.title || "")}</div>
        <div class="event-meta">${esc(row.event_date || "")} ${esc(row.event_time || "")} · ${esc(row.stock_id || "")} ${esc(row.company_name || "")} · ${esc(row.category || "")}</div>
        <div class="news-text">${esc(row.detail || "").slice(0, 160)}${(row.detail || "").length > 160 ? "..." : ""}</div>
      </article>`).join("") + (rows.length > visible.length
        ? `<button class="secondary" id="mops-load-more" style="margin-top:8px;">載入更多（${esc(fmt(visible.length))} / ${esc(fmt(rows.length))}）</button>`
        : "");
    }
    function renderMopsEvents(rows) {
      state.mopsEvents = rows || [];
      state.mopsVisibleCount = 50;
      const note = document.querySelector("#mops-note");
      const latestFetch = state.mopsEvents.map(row => row.fetched_at).filter(Boolean).sort().at(-1);
      const start = document.querySelector("#mops-start")?.value || "";
      const end = document.querySelector("#mops-end")?.value || "";
      const rangeText = start || end ? `，目前區間 ${start || "最早"} ~ ${end || "最新"}` : "，目前顯示全部快取";
      if (note) note.textContent = latestFetch
        ? `${STATIC_MODE ? "靜態快取" : "本機快取"} ${fmt(state.mopsEvents.length)} 件${rangeText}，最後抓取 ${latestFetch}`
        : "尚無 MOPS 事件快取；按「抓取 MOPS 事件」取得近期重大訊息。";
      renderMopsCalendar(state.mopsEvents);
      renderMopsEventList();
    }
    async function loadMopsEvents() {
      initMopsDates();
      state.mopsSelectedDate = "";
      state.mopsVisibleCount = 50;
      state.mopsQuickFilter = "";
      const start = document.querySelector("#mops-start")?.value || "";
      const end = document.querySelector("#mops-end")?.value || "";
      const stock = document.querySelector("#mops-stock")?.value.trim() || "";
      const q = document.querySelector("#mops-query")?.value.trim() || "";
      const data = await getJSON(`/api/mops-events?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&stock_id=${encodeURIComponent(stock)}&q=${encodeURIComponent(q)}&limit=5000`);
      renderMopsEvents(data.rows || []);
    }
    async function refreshMopsEvents() {
      const btn = document.querySelector("#refresh-mops-events");
      if (!btn) return;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "抓取中";
      const startDate = document.querySelector("#mops-start")?.value || "";
      const endDate = document.querySelector("#mops-end")?.value || "";
      try {
        const result = await postJSON("/api/mops-events/refresh", {start_date: startDate, end_date: endDate});
        const note = document.querySelector("#mops-note");
        if (note) note.textContent = `MOPS 抓取完成：${fmt(result.row_count || 0)} 件，更新 ${fmt(result.changed || 0)} 筆，清除 ${fmt(result.pruned || 0)} 筆半年前資料，失敗 ${fmt((result.failed || []).length)} 日`;
        state.mopsSelectedDate = "";
        state.mopsVisibleCount = 50;
        state.mopsQuickFilter = "";
        state.mopsCalendarStart = startDate || "";
        await loadMopsEvents();
      } catch (err) {
        const note = document.querySelector("#mops-note");
        if (note) note.textContent = `MOPS 抓取失敗：${err.message}`;
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }
    function renderObsidianStatus(data) {
      const note = document.querySelector("#obsidian-note");
      if (!note) return;
      if (!data?.exists) {
        note.innerHTML = "尚未建立 vault；抓取美股新聞後會自動建立 <strong>obsidian/news-vault</strong>。";
        return;
      }
      note.innerHTML = `
        <strong>${esc(data.vault_dir || "obsidian/news-vault")}</strong>
        <span>raw ${esc(fmt(data.raw_count || 0))} 則（保留近 ${esc(fmt(data.retention_days || 3))} 日）</span>
        <span>daily ${esc(fmt(data.daily_count || 0))} 檔</span>
        <span>產業 ${esc(fmt(data.industry_count || 0))} 檔</span>
        <span>最後同步 ${esc(data.updated_at || "-")}</span>
      `;
    }
    async function loadObsidianStatus() {
      if (STATIC_MODE) {
        renderObsidianStatus({exists:false});
        return;
      }
      try {
        renderObsidianStatus(await getJSON("/api/obsidian/status"));
      } catch (err) {
        const note = document.querySelector("#obsidian-note");
        if (note) note.textContent = `Obsidian 狀態讀取失敗：${err.message}`;
      }
    }
    async function loadUSNews() {
      const industry = document.querySelector("#us-industry")?.value || "";
      const [data] = await Promise.all([
        getJSON(`/api/us-news?industry=${encodeURIComponent(industry)}&limit=80`),
        loadObsidianStatus()
      ]);
      state.usNewsRows = data.rows || [];
      renderUSNews(state.usNewsRows);
    }
    async function refreshUSNews() {
      const btn = document.querySelector("#refresh-us-news");
      if (!btn) return;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "抓取中";
      document.querySelector("#us-news-note").textContent = document.querySelector("#us-use-ollama").checked
        ? "正在抓取 RSS 並執行 Ollama 分類，第一次可能需要較久..."
        : "正在抓取 RSS 並執行本機規則快速分類，分類 token 為 0...";
      try {
        const mode = document.querySelector("#us-news-mode").value;
        const symbols = mode === "symbols"
          ? document.querySelector("#us-symbols").value.split(",").map(item => item.trim()).filter(Boolean)
          : [];
        const limit = Number(document.querySelector("#us-limit").value || 5);
        const useOllama = document.querySelector("#us-use-ollama").checked;
        const result = await postJSON("/api/us-news/refresh", { symbols, limit, use_ollama: useOllama, include_symbol_news: mode === "symbols" });
        state.usNewsRows = result.rows || [];
        renderUSNews(state.usNewsRows);
        if (result.obsidian) renderObsidianStatus(result.obsidian);
      } catch (err) {
        document.querySelector("#us-news-note").textContent = `抓取失敗：${err.message}`;
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    }
    async function refreshSingleSection(section, btn) {
      const stock = state.detail?.stock?.stock_id || document.querySelector("#detail-stock").value.trim();
      const days = Number(document.querySelector("#days").value || 20);
      if (!stock || !btn) return;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "更新中";
      try {
        await postJSON("/api/stock/refresh-section", { stock_id: stock, section, days });
        await loadDetail();
      } catch (err) {
        btn.textContent = "更新失敗";
        const target = section === "daily" ? "#daily-table" : section === "revenue" ? "#revenue-table" : section === "branch_top" || section === "branch_all" ? "#branch-top-table" : "#broker-daily-table";
        document.querySelector(target).innerHTML = `<div class="empty">單檔更新失敗：${esc(err.message)}</div>`;
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1800);
        return;
      }
      btn.disabled = false;
      btn.textContent = original;
    }
    const rankingCols = [
      {key:"stock_id", label:"代號", sortType:"text", format: v => v, groups:["core","chip","foreign","revenue","margin","volume","valuation"]},
      {key:"name", label:"名稱", sortType:"text", groups:["core","chip","foreign","revenue","margin","volume","valuation"]},
      {key:"market", label:"市場", sortType:"text", groups:["core","valuation"]},
      {key:"close", label:"收盤", groups:["core","chip","foreign","revenue","margin","volume","valuation"]},
      {key:"observed_days", label:"資料日", groups:["core"]},
      {key:"pe_ratio", label:"本益比", groups:["core","valuation"]},
      {key:"dividend_yield", label:"殖利率%", groups:["valuation"]},
      {key:"pb_ratio", label:"股淨比", groups:["valuation"]},
      {key:"total_score", label:"基礎分", signed:true, groups:["core"]},
      {key:"market_sentiment_score", label:"大盤分", signed:true, groups:["core"]},
      {key:"risk_adjusted_score", label:"風險調整分", signed:true, groups:["core"]},
      {key:"chip_score", label:"法人分", signed:true, groups:["core","chip"]},
      {key:"revenue_momentum_score", label:"營收分", signed:true, groups:["core","revenue"]},
      {key:"margin_score", label:"融資分", signed:true, groups:["core","margin"]},
      {key:"volume_lot", label:"成交量", groups:["core","chip","foreign","volume"]},
      {key:"latest_volume_lot", label:"最新日量", groups:["volume"]},
      {key:"volume_avg_lot", label:"區間均量", groups:["volume"]},
      {key:"volume_5d_avg_lot", label:"5日均量", groups:["volume"]},
      {key:"volume_ratio_1d", label:"單日量倍", groups:["volume"]},
      {key:"volume_ratio_5d", label:"5日量倍", groups:["volume"]},
      {key:"volume_score", label:"量能分", signed:true, groups:["volume"]},
      {key:"volume_signal", label:"量能", sortType:"text", groups:["volume"]},
      {key:"foreign_net_lot", label:"外資", signed:true, groups:["chip","foreign"]},
      {key:"trust_net_lot", label:"投信", signed:true, groups:["chip"]},
      {key:"inst_net_lot", label:"外資+投信", signed:true, groups:["chip"]},
      {key:"foreign_net_volume_pct", label:"外資占量%", signed:true, groups:["chip","foreign"]},
      {key:"latest_foreign_net_lot", label:"最近一日外資", signed:true, groups:["foreign"]},
      {key:"foreign_buy_streak", label:"外資連買", groups:["foreign"]},
      {key:"foreign_sell_streak", label:"外資連賣", groups:["foreign"]},
      {key:"margin_balance_change_lot", label:"融資增減", signed:true, groups:["margin"]},
      {key:"short_balance_change_lot", label:"融券增減", signed:true, groups:["margin"]},
      {key:"avg_price", label:"均價", groups:["core"]},
      {key:"revenue_month", label:"營收月", sortType:"text", groups:["revenue"]},
      {key:"revenue_mom_pct", label:"月增%", signed:true, groups:["revenue"]},
      {key:"revenue_yoy_pct", label:"年增%", signed:true, groups:["revenue"]},
      {key:"base_reason", label:"理由", sortType:"text", groups:["core"]}
    ];
    const columnGroups = [
      {key:"core", label:"核心", tip:"核心：用全市場較穩定可取得的資料做快速排序，包含收盤、資料日、基礎分、法人分、營收分、融資分、成交量與均價。"},
      {key:"chip", label:"法人籌碼", tip:"法人籌碼：觀察外資、投信買賣超與外資占成交量比例。占量越高，代表法人買賣超相對當期成交量越集中。"},
      {key:"foreign", label:"外資籌碼", tip:"外資籌碼：只看外資相關資料，包含區間外資買賣超、外資占成交量比例、最近一日外資、外資連買與連賣。適合判斷外資是否持續偏多或短線轉向。"},
      {key:"revenue", label:"營收", tip:"營收：顯示最新營收月份、營收分、月增率與年增率，用來判斷基本面動能是否同步轉強。"},
      {key:"margin", label:"融資券", tip:"融資券：觀察融資餘額與融券餘額增減。融資快速增加可能代表散戶追價，融資下降且法人買超通常較乾淨。"},
      {key:"volume", label:"量能", tip:"量能：比較最新日成交量與區間平均量、5日均量與區間均量。單日量倍高代表當天成交明顯放大，5日量倍高代表最近一段時間量能持續升溫。"},
      {key:"valuation", label:"估值", tip:"估值：使用交易所每日行情揭露的本益比、殖利率與股價淨值比。空值通常代表該來源未揭露、EPS 為負或資料不可計算；目前只顯示與排序，不納入基礎分。"}
    ];
    function visibleRankingCols() {
      return rankingCols.filter(col => (col.groups || ["core"]).includes(state.columnGroup));
    }
    function renderColumnControls() {
      const target = document.querySelector("#ranking-columns");
      if (!target) return;
      target.innerHTML = columnGroups.map(group => `
        <label class="toggle tooltip-anchor" data-tip="${esc(group.tip)}" title="${esc(group.tip)}">
          <input type="radio" name="column-group" value="${esc(group.key)}" ${state.columnGroup === group.key ? "checked" : ""} />
          ${esc(group.label)}
        </label>
      `).join("");
      target.querySelectorAll("input[name='column-group']").forEach(input => {
        input.addEventListener("change", () => {
          state.columnGroup = input.value;
          renderRankingTable();
        });
      });
    }
    async function loadMeta() {
      const meta = await getJSON("/api/meta?days=" + document.querySelector("#days").value);
      renderMetrics(document.querySelector("#metrics"), [
        ["最新交易日", meta.latest_date || ""],
        ["股票數", fmt(meta.stock_count)],
        ["上市 / 上櫃", `${fmt(meta.twse_count)} / ${fmt(meta.tpex_count)}`],
        ["分點資料", "個股頁單檔更新"]
      ]);
      document.querySelector("#meta").innerHTML = STATIC_MODE
        ? `<span>靜態資料：GitHub Pages</span><span>最新日：${esc(meta.latest_date || "")}</span><span>匯出：${esc(meta.exported_at || "")}</span>`
        : `<span>資料庫：data/stock_chip.sqlite</span><span>最新日：${esc(meta.latest_date || "")}</span>`;
    }
    async function loadRanking(watchlist = false) {
      const data = await getJSON((watchlist ? "/api/watchlist?" : "/api/ranking?") + params().toString());
      state.rankingRows = data.rows;
      if (!state.marketSentiment) {
        try { await loadMarketSentiment(); } catch (_err) {}
      }
      applyRiskAdjustedScores();
      document.querySelector("#ranking-title").textContent = watchlist ? "自選股摘要" : data.title;
      const q = document.querySelector("#query").value.trim();
      const note = document.querySelector("#ranking-note");
      note.innerHTML = `${esc(data.rows.length)} 筆，點股票可進個股頁`;
      if (!watchlist && q && !data.rows.length && !STATIC_MODE) {
        note.innerHTML += ` <button class="secondary inline-action" id="expand-stock">補進資料庫</button>`;
        setTimeout(() => {
          document.querySelector("#expand-stock")?.addEventListener("click", expandStockFromSearch);
        }, 0);
      } else if (!watchlist && q && !data.rows.length && STATIC_MODE) {
        note.innerHTML += "，靜態資料未包含此股票";
      }
      renderRankingTable();
    }
    function renderRankingTable() {
      renderColumnControls();
      const cols = visibleRankingCols();
      const rows = sortRows(state.rankingRows || [], cols, state.rankingSort);
      renderTable(document.querySelector("#ranking-table"), rows, cols, {
        rowId: row => row.stock_id,
        onClick: stock => openDetail(stock),
        sort: state.rankingSort,
        onSort: key => {
          const same = state.rankingSort?.key === key;
          state.rankingSort = { key, dir: same && state.rankingSort.dir === "desc" ? "asc" : "desc" };
          renderRankingTable();
        }
      });
    }
    function renderSelectionAssist(selection) {
      const target = document.querySelector("#selection-assist");
      if (!target) return;
      if (!selection) {
        target.innerHTML = `<div class="empty">目前沒有足夠資料可計算選股輔助。</div>`;
        return;
      }
      const cards = [
        ["基礎選股分", selection.total_score, "法人 + 融資融券 + 營收"],
        ["法人分", selection.chip_score, `法人占量 ${fmt(selection.inst_net_volume_pct)}%`],
        ["融資分", selection.margin_score, `融資 ${fmt(selection.margin_balance_change_lot)} / 融券 ${fmt(selection.short_balance_change_lot)}`],
        ["量能", selection.volume_signal || "無資料", `單日 ${fmt(selection.volume_ratio_1d)} 倍 / 5日 ${fmt(selection.volume_ratio_5d)} 倍`],
        ["量能分", selection.volume_score, "獨立指標，尚未併入基礎分"],
        ["營收動能", selection.revenue_momentum_score, `${esc(selection.revenue_month || "無月份")} 年增 ${fmt(selection.revenue_yoy_pct)}%`],
      ];
      target.innerHTML = `
        <div class="assist-grid">
          ${cards.map(([label, value, note]) => `
            <div class="assist-card">
              <div class="assist-label">${esc(label)}</div>
              <div class="assist-value ${cls(value)}">${esc(typeof value === "number" ? fmt(value) : value)}</div>
              <div class="assist-note">${esc(note)}</div>
            </div>
          `).join("")}
        </div>
        <div class="assist-reason">${esc(selection.base_reason || selection.selection_reason || "")}</div>
      `;
    }
    function renderDataQuality(data) {
      const target = document.querySelector("#detail-data-status");
      const judgement = document.querySelector("#detail-judgement");
      if (!target || !judgement) return;
      const days = Number(document.querySelector("#days").value || 20);
      const branchStatus = data.branch_top_status || {};
      const statusText = {success:"已取得", empty:"來源空回應", failed:"抓取失敗", missing:"尚未抓取"}[branchStatus.status] || branchStatus.status || "未知";
      const items = [
        {
          name: "每日行情",
          ok: (data.daily || []).length >= Math.min(days, 5),
          warn: (data.daily || []).length > 0,
          value: `${fmt((data.daily || []).length)} / ${days} 日`,
          detail: data.stock?.latest_date || "-"
        },
        {
          name: "法人買賣超",
          ok: (data.daily || []).some(row => Number(row.foreign_net_lot || 0) !== 0 || Number(row.trust_net_lot || 0) !== 0 || Number(row.dealer_net_lot || 0) !== 0),
          warn: (data.daily || []).length > 0,
          value: "每日進出",
          detail: "0 可能是真 0，也可能是來源未提供"
        },
        {
          name: "24月營收",
          ok: (data.revenues || []).length >= 12,
          warn: (data.revenues || []).length > 0,
          value: `${fmt((data.revenues || []).length)} 個月`,
          detail: data.revenues?.[0]?.revenue_month || "-"
        },
        {
          name: "融資融券",
          ok: (data.margin || []).length >= Math.min(days, 5),
          warn: (data.margin || []).length > 0,
          value: `${fmt((data.margin || []).length)} / ${days} 日`,
          detail: data.margin?.[0]?.date || "-"
        },
        {
          name: "分點排行",
          ok: branchStatus.status === "success" && (data.branch_top || []).length > 0,
          warn: ["empty", "missing"].includes(branchStatus.status),
          value: statusText,
          detail: branchStatus.updated_at || branchStatus.error || "-"
        },
        {
          name: "新聞",
          ok: (data.news || []).length > 0,
          warn: false,
          value: `${fmt((data.news || []).length)} 則`,
          detail: (data.news || []).map(row => row.fetched_at).filter(Boolean).sort().at(-1) || "手動抓取"
        }
      ];
      target.innerHTML = `<div class="status-grid">${items.map(item => {
        const klass = item.ok ? "good" : item.warn ? "warn" : "bad";
        return `<div class="status-card ${klass}">
          <div class="status-name">${esc(item.name)}</div>
          <div class="status-value">${esc(item.ok ? "可用" : item.warn ? "部分" : "缺資料")}</div>
          <div class="status-detail">${esc(item.value)} · ${esc(item.detail)}</div>
        </div>`;
      }).join("")}</div>`;
      const s = data.selection || {};
      const notes = [];
      if (Number(s.chip_score || 0) > 55) notes.push("法人籌碼偏強");
      else if (Number(s.chip_score || 0) < 35) notes.push("法人籌碼偏弱");
      if (Number(s.revenue_yoy_pct || 0) > 0 && Number(s.revenue_mom_pct || 0) > 0) notes.push("營收月增與年增同步為正");
      else if ((data.revenues || []).length) notes.push("營收動能需要再確認");
      if (Number(data.stock?.margin_balance_change_lot || 0) > 0 && Number(data.stock?.foreign_net_lot || 0) > 0) notes.push("外資買超但融資也增加，需留意追價風險");
      if (branchStatus.status !== "success") notes.push("分點資料改為個股頁手動更新，不影響排行分數");
      if (!notes.length) notes.push("目前資料不足，先以基礎行情、法人與營收做初步觀察");
      judgement.textContent = `初步判斷：${notes.join("；")}。這不是買賣建議，主要用來提醒目前資料完整度與矛盾點。`;
    }
    async function expandStockFromSearch() {
      const q = document.querySelector("#query").value.trim();
      const btn = document.querySelector("#expand-stock");
      if (!q || !btn) return;
      btn.disabled = true;
      btn.textContent = "補資料中";
      try {
        const days = Number(document.querySelector("#days").value || 20);
        const result = await postJSON("/api/stock/ensure", { query: q, days });
        document.querySelector("#query").value = result.stock_id;
        await loadMeta();
        await loadRanking(false);
        await openDetail(result.stock_id);
      } catch (err) {
        btn.textContent = "補資料失敗";
        document.querySelector("#ranking-table").innerHTML = `<div class="empty">補進資料庫失敗：${esc(err.message)}</div>`;
      }
    }
    async function openStockFromQuery() {
      const q = document.querySelector("#query").value.trim();
      if (!q) return false;
      try {
        const data = await getJSON(`/api/stock/resolve?query=${encodeURIComponent(q)}`);
        if (data.stock_id) {
          await openDetail(data.stock_id);
          return true;
        }
      } catch (err) {
        if (!STATIC_MODE) {
          try {
            const days = Number(document.querySelector("#days").value || 20);
            const result = await postJSON("/api/stock/ensure", { query: q, days });
            document.querySelector("#query").value = result.stock_id;
            await loadMeta();
            await openDetail(result.stock_id);
            return true;
          } catch (ensureErr) {
            document.querySelector("#detail-title").textContent = "找不到股票";
            document.querySelector("#detail-subtitle").textContent = ensureErr.message;
          }
        }
      }
      return false;
    }
    function updateWatchlistButton() {
      const btn = document.querySelector("#watchlist-toggle");
      const stock = state.detail?.stock;
      if (!btn || !stock) return;
      btn.textContent = stock.in_watchlist ? "移除自選" : "加入自選";
      btn.dataset.action = stock.in_watchlist ? "remove" : "add";
      btn.disabled = false;
    }
    async function toggleWatchlist() {
      const stock = document.querySelector("#detail-stock").value.trim();
      const btn = document.querySelector("#watchlist-toggle");
      if (!stock || !btn) return;
      btn.disabled = true;
      try {
        const result = await postJSON("/api/watchlist", { stock_id: stock, action: btn.dataset.action || "add" });
        if (state.detail?.stock?.stock_id === result.stock_id) {
          state.detail.stock.in_watchlist = result.in_watchlist;
          updateWatchlistButton();
        }
        if (state.tab === "watchlist") await loadRanking(true);
      } catch (err) {
        btn.textContent = err.message;
        setTimeout(updateWatchlistButton, 1500);
      }
    }
    async function loadCoverage() {
      const days = document.querySelector("#days").value;
      const [data, updateState] = await Promise.all([
        getJSON(`/api/coverage?days=${days}`),
        getJSON("/api/update-tasks")
      ]);
      state.lastDataUpdatedAt = updateState.latest_data_updated_at || "";
      renderUpdateTasks(updateState.tasks, updateState.running);
      renderJob(updateState.latest_job);
      await loadStaticPublishStatus();
    }
    function renderUpdateTasks(tasks, running) {
      if (STATIC_MODE) {
        document.querySelector("#update-tasks").innerHTML = `<div class="empty">靜態版不執行更新任務；資料由本機匯出後部署。</div>`;
        return;
      }
      document.querySelector("#update-tasks").innerHTML = tasks.map(task => `
        ${(() => {
          const stale = freshness(task.last_data_updated_at, task.warn_hours || 24, task.bad_hours || 48);
          const staleClass = stale.klass === "warn" ? "stale-warn" : stale.klass === "bad" ? "stale-bad" : "";
          const runState = task.run_state || "";
          const runLabel = task.run_label || "";
          const runClass = runState === "done" ? "done" : runState === "running" ? "running" : runState === "pending" ? "warn" : "";
          const buttonLabel = running
            ? (runLabel || "等待中")
            : task.id === "all_data" ? "一鍵更新"
              : task.id === "all_market_revenue" ? "補齊全市場"
              : "開始更新";
          const extra = task.extra_status ? `<div class="job-extra">${esc(task.extra_status)}</div>` : "";
          return `<div class="job-card ${task.id === "all_data" ? "primary-job" : ""} ${staleClass}">
          <div class="job-title">${esc(task.title)}</div>
          <div class="job-desc">${esc(task.description)}</div>
          ${runLabel ? `<div class="job-time"><span class="status-pill ${runClass}">${esc(runLabel)}</span></div>` : ""}
          <div class="job-time">最後更新：${esc(task.last_updated_at || "尚未更新")}</div>
          <div class="job-time">資料更新：${esc(task.last_data_updated_at || "尚無資料")}</div>
          <div class="job-time"><span class="status-pill ${stale.klass}">${esc(stale.label)}</span> ${esc(stale.detail)}</div>
          ${extra}
          <button class="${task.id === "all_data" ? "" : "secondary"}" data-task="${esc(task.id)}" ${running ? "disabled" : ""}>${esc(buttonLabel)}</button>
        </div>`;
        })()}
      `).join("");
      document.querySelectorAll("[data-task]").forEach(btn => btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "啟動中";
        try {
          const days = Number(document.querySelector("#days").value || 20);
          await postJSON("/api/update", { task: btn.dataset.task, days });
          await loadCoverage();
          startJobPolling();
        } catch (err) {
          document.querySelector("#job-log").textContent = err.message;
        }
      }));
    }
    function renderJob(job) {
      const status = document.querySelector("#job-status");
      const log = document.querySelector("#job-log");
      const actions = document.querySelector("#job-actions");
      if (!job) {
        status.className = "status-pill";
        status.textContent = `尚未執行 · 最後更新 ${state.lastDataUpdatedAt || "-"}`;
        if (actions) actions.style.display = "none";
        log.textContent = state.lastDataUpdatedAt
          ? `最後資料更新：${state.lastDataUpdatedAt}`
          : "尚未執行更新任務。";
        return;
      }
      const statusText = {queued:"排隊中", running:"執行中", done:"完成", failed:"失敗", interrupted:"未正常結束"}[job.status] || job.status;
      status.className = `status-pill ${job.status === "done" ? "done" : job.status === "failed" ? "failed" : job.possible_stuck ? "bad" : job.status === "running" ? "running" : job.status === "interrupted" ? "warn" : ""}`;
      status.textContent = `${statusText} · ${job.title || ""} · 最後更新 ${job.updated_at || "-"}`;
      if (actions) actions.style.display = job.status === "running" ? "" : "none";
      const health = job.health || {};
      log.textContent = [
        job.current_step ? `目前步驟：${job.current_step}` : "",
        job.started_at ? `開始：${job.started_at}` : "",
        job.updated_at ? `最後更新：${job.updated_at}` : "",
        job.current_process_pid ? `子程序 PID：${job.current_process_pid}` : "",
        health.last_write_at ? `分點最後寫入：${health.last_write_at}` : "",
        health.stale_minutes !== undefined ? `距離最後寫入：約 ${health.stale_minutes} 分鐘` : "",
        health.status_summary ? `分點進度：${health.status_summary}` : "",
        job.possible_stuck ? "狀態判斷：可能卡住，建議停止後重新分批更新。" : "",
        job.finished_at ? `結束：${job.finished_at}` : "",
        job.error ? `錯誤：${job.error}` : "",
      ].filter(Boolean).join("\\n");
    }
    async function cancelCurrentUpdate() {
      const btn = document.querySelector("#cancel-update");
      if (!btn) return;
      btn.disabled = true;
      btn.textContent = "停止中";
      try {
        await postJSON("/api/update/cancel", {});
        startJobPolling();
      } catch (err) {
        document.querySelector("#job-log").textContent = `停止失敗：${err.message}`;
      } finally {
        btn.disabled = false;
        btn.textContent = "停止目前更新";
      }
    }
    function renderStaticPublishStatus(data, message = "") {
      const status = document.querySelector("#static-publish-status");
      const note = document.querySelector("#static-publish-note");
      const log = document.querySelector("#static-publish-log");
      if (!status || !note || !log) return;
      const dirty = Number(data?.docs_changed || 0) + Number(data?.reports_changed || 0);
      status.className = `status-pill ${dirty ? "running" : "done"}`;
      status.textContent = dirty ? "已匯出，待發布" : "已同步";
      note.innerHTML = [
        data?.pages_url ? `<a href="${esc(data.pages_url)}" target="_blank" rel="noreferrer">${esc(data.pages_url)}</a>` : "",
        data?.exported_at ? `靜態資料匯出：${esc(data.exported_at)}` : "",
        data?.ci_news?.generated_at ? `CI 新聞：${esc(data.ci_news.generated_at)}（${esc(fmt(data.ci_news.row_count || 0))} 則）` : "",
        data?.commit ? `目前 commit：${esc(data.commit)}` : "",
      ].filter(Boolean).join(" · ") || "尚無發布資訊";
      log.innerHTML = linkifyUrls(message || [
        `分支：${data?.branch || "-"}`,
        `docs 變更：${fmt(data?.docs_changed || 0)} 個檔案`,
        `reports 變更：${fmt(data?.reports_changed || 0)} 個檔案`,
        `Pages：${data?.pages_url || "-"}`,
      ].join("\\n"));
    }
    async function loadStaticPublishStatus() {
      if (STATIC_MODE) return;
      try {
        renderStaticPublishStatus(await getJSON("/api/static/status"));
      } catch (err) {
        const status = document.querySelector("#static-publish-status");
        const log = document.querySelector("#static-publish-log");
        if (status) {
          status.className = "status-pill failed";
          status.textContent = "讀取失敗";
        }
        if (log) log.innerHTML = linkifyUrls(err.message);
      }
    }
    async function runStaticAction(action, btn) {
      if (!btn) return;
      const original = btn.textContent;
      document.querySelectorAll("#static-export, #static-publish").forEach(item => item.disabled = true);
      btn.textContent = action === "publish" ? "發布中" : "匯出中";
      renderStaticPublishStatus({}, action === "publish" ? "正在匯出、提交並推送..." : "正在重新匯出 docs/ 靜態資料...");
      try {
        const result = await postJSON(action === "publish" ? "/api/static/publish" : "/api/static/export", {});
        renderStaticPublishStatus(result.status, result.message || "完成");
      } catch (err) {
        const status = document.querySelector("#static-publish-status");
        const log = document.querySelector("#static-publish-log");
        if (status) {
          status.className = "status-pill failed";
          status.textContent = "失敗";
        }
        if (log) log.innerHTML = linkifyUrls(err.message);
      } finally {
        btn.textContent = original;
        document.querySelectorAll("#static-export, #static-publish").forEach(item => item.disabled = false);
      }
    }
    function startJobPolling() {
      clearInterval(window.__jobPoll);
      window.__jobPoll = setInterval(async () => {
        if (state.tab !== "coverage") {
          clearInterval(window.__jobPoll);
          return;
        }
        const updateState = await getJSON("/api/update-tasks");
        renderUpdateTasks(updateState.tasks, updateState.running);
        renderJob(updateState.latest_job);
        if (!updateState.running) {
          clearInterval(window.__jobPoll);
          await loadMeta();
        }
      }, 2500);
    }
    function setTab(tab) {
      if (tab !== "detail") state.previousTab = tab;
      state.tab = tab;
      document.querySelectorAll(".tab").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === tab));
      reload();
    }
    async function openDetail(stock) {
      document.querySelector("#detail-stock").value = stock;
      if (state.tab !== "detail") state.previousTab = state.tab;
      state.tab = "detail";
      document.querySelectorAll(".tab").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === "detail"));
      await reload();
    }
    async function goBackFromDetail() {
      state.tab = state.previousTab || "ranking";
      document.querySelectorAll(".tab").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === state.tab));
      await reload();
    }
    async function loadDetail() {
      const stock = document.querySelector("#detail-stock").value.trim() || "2376";
      const days = document.querySelector("#days").value;
      const data = await getJSON(`/api/stock?days=${days}&stock_id=${encodeURIComponent(stock)}`);
      state.detail = data;
      state.broker = data.branch_top[0]?.broker_name || "";
      const rangeSelect = document.querySelector("#chart-range");
      state.chartRange = rangeSelect?.value === "all" ? "all" : Number(rangeSelect?.value || 20);
      document.querySelector("#detail-title").textContent = `${data.stock.stock_id} ${data.stock.name}`;
      document.querySelector("#detail-subtitle").textContent = `${data.stock.market} · 最新日 ${data.stock.latest_date || ""}`;
      const backBtn = document.querySelector("#back-detail");
      backBtn.style.display = state.previousTab ? "" : "none";
      backBtn.textContent = `返回${state.previousTab === "watchlist" ? "自選股" : state.previousTab === "coverage" ? "資料狀態" : "排行"}`;
      updateWatchlistButton();
      renderMetrics(document.querySelector("#detail-metrics"), [
        ["收盤", fmt(data.stock.close)],
        [`${days}日均價`, fmt(data.stock.avg_price)],
        ["本益比", fmt(data.stock.pe_ratio)],
        ["殖利率", data.stock.dividend_yield != null ? `${fmt(data.stock.dividend_yield)}%` : "-"],
        ["股淨比", fmt(data.stock.pb_ratio)],
        ["單日量倍", fmt(data.stock.volume_ratio_1d)],
        ["5日量倍", fmt(data.stock.volume_ratio_5d)],
        [`${days}日外資`, fmt(data.stock.foreign_net_lot), cls(data.stock.foreign_net_lot)],
        [`${days}日投信`, fmt(data.stock.trust_net_lot), cls(data.stock.trust_net_lot)],
        [`${days}日融資`, fmt(data.stock.margin_balance_change_lot), cls(data.stock.margin_balance_change_lot)],
        [`${days}日融券`, fmt(data.stock.short_balance_change_lot), cls(data.stock.short_balance_change_lot)]
      ]);
      renderDataQuality(data);
      renderSelectionAssist(data.selection);
      renderPriceChart();
      renderRevenueChart();
      renderMarginChart();
      renderStockEvents();
      renderNews();
      renderTable(document.querySelector("#daily-table"), data.daily, [
        {key:"date", label:"日期"},
        {key:"close", label:"收盤"},
        {key:"avg_price", label:"均價"},
        {key:"volume_lot", label:"成交量(張)"},
        {key:"pe_ratio", label:"本益比"},
        {key:"dividend_yield", label:"殖利率%"},
        {key:"pb_ratio", label:"股淨比"},
        {key:"foreign_net_lot", label:"外資買賣超", signed:true},
        {key:"trust_net_lot", label:"投信買賣超", signed:true},
        {key:"dealer_net_lot", label:"自營買賣超", signed:true}
      ]);
      document.querySelector("#revenue-note").textContent = data.revenues.length ? `最新 ${data.revenues[0].revenue_month}` : "沒有資料時可單檔更新";
      renderTable(document.querySelector("#revenue-table"), data.revenues, [
        {key:"revenue_month", label:"月份"},
        {key:"revenue_million", label:"營收(百萬)"},
        {key:"mom_pct", label:"月增%", signed:true},
        {key:"yoy_pct", label:"年增%", signed:true},
        {key:"last_year_revenue_million", label:"去年同期"}
      ]);
      const marginLatest = data.margin?.[0];
      document.querySelector("#margin-note").textContent = marginLatest
        ? `最新 ${marginLatest.date}，融資餘額 ${fmt(marginLatest.margin_balance_lot)} 張，融券餘額 ${fmt(marginLatest.short_balance_lot)} 張`
        : "沒有資料時可按右側更新，或先執行官方行情與排行。";
      renderTable(document.querySelector("#margin-table"), data.margin || [], [
        {key:"date", label:"日期"},
        {key:"margin_buy_lot", label:"資買"},
        {key:"margin_sell_lot", label:"資賣"},
        {key:"margin_cash_repay_lot", label:"現償"},
        {key:"margin_balance_lot", label:"融資餘額"},
        {key:"margin_change_lot", label:"資增減", signed:true},
        {key:"short_sell_lot", label:"券賣"},
        {key:"short_buy_lot", label:"券買"},
        {key:"short_stock_repay_lot", label:"券償"},
        {key:"short_balance_lot", label:"融券餘額"},
        {key:"short_change_lot", label:"券增減", signed:true},
        {key:"offset_lot", label:"資券互抵"}
      ]);
      const branchTopStatus = data.branch_top_status;
      const branchTopNote = document.querySelector("#branch-top-note");
      if (!data.branch_top.length && branchTopStatus) {
        const statusText = {success:"成功", empty:"空回應", failed:"抓取失敗", missing:"尚未抓取"}[branchTopStatus.status] || branchTopStatus.status;
        const detail = branchTopStatus.error ? `；原因：${branchTopStatus.error}` : "";
        if (branchTopNote) branchTopNote.textContent = `狀態：${statusText}，可單檔更新`;
        document.querySelector("#branch-top-table").innerHTML =
          `<div class="empty">區間分點排行狀態：${esc(statusText)}，最後更新：${esc(branchTopStatus.updated_at || "-")}${esc(detail)}</div>`;
      } else {
        if (branchTopNote) branchTopNote.textContent = `這是 ${days} 日區間合計；點分點看最近 ${days} 個交易日明細`;
        renderTable(document.querySelector("#branch-top-table"), data.branch_top, [
          {key:"rank_no", label:"排名"},
          {key:"broker_name", label:"分點"},
          {key:"buy_lot", label:"買進"},
          {key:"sell_lot", label:"賣出"},
          {key:"net_lot", label:`${days}日買超`, signed:true},
          {key:"avg_price", label:"均價"}
        ], {
          rowId: row => row.broker_name,
          onClick: broker => {
            state.broker = broker;
            renderBrokerDaily();
          }
        });
      }
      renderBrokerDaily();
    }
    function renderBrokerDaily() {
      const data = state.detail;
      const brokers = data?.branch_top || [];
      document.querySelector("#broker-list").innerHTML = brokers.map(row =>
        `<button class="broker-chip ${row.broker_name === state.broker ? "active" : ""}" data-broker="${esc(row.broker_name)}">${esc(row.broker_name)}</button>`
      ).join("");
      document.querySelectorAll(".broker-chip").forEach(btn => btn.addEventListener("click", () => {
        state.broker = btn.dataset.broker;
        renderBrokerDaily();
      }));
      const days = document.querySelector("#days").value;
      document.querySelector("#broker-title").textContent = state.broker ? `${state.broker} 最近 ${days} 個交易日` : `分點最近 ${days} 個交易日`;
      const rows = (data?.branch_daily || []).filter(row => row.broker_name === state.broker);
      const statuses = data?.branch_daily_status || [];
      const success = statuses.filter(row => row.status === "success").length;
      const failed = statuses.filter(row => row.status !== "success");
      if (!rows.length) {
        const message = failed.length
          ? `尚缺 ${failed.length} 個交易日分點日資料；狀態：${failed.map(row => `${row.trade_date} ${row.status}`).join("、")}`
          : success
            ? `單日分點頁已抓取，但此分點最近 ${days} 個交易日未進入 HiStock 可解析的單日前排行。`
            : `尚未抓取分點最近 ${days} 個交易日資料。`;
        document.querySelector("#broker-daily-table").innerHTML = `<div class="empty">${esc(message)}</div>`;
        return;
      }
      renderTable(document.querySelector("#broker-daily-table"), rows, [
        {key:"trade_date", label:"日期"},
        {key:"rank_side", label:"方向", format: v => v === "buy" ? "買超" : "賣超"},
        {key:"rank_no", label:"排名", format: v => Number(v) ? v : ""},
        {key:"buy_lot", label:"買進"},
        {key:"sell_lot", label:"賣出"},
        {key:"net_lot", label:"買賣超", signed:true},
        {key:"avg_price", label:"均價"}
      ]);
    }
    async function reload() {
      await loadMeta();
      const showTwStockOverview = state.tab === "ranking" || state.tab === "watchlist" || state.tab === "detail";
      document.querySelector(".toolbar").style.display = showTwStockOverview ? "" : "none";
      document.querySelector("#metrics").style.display = showTwStockOverview ? "" : "none";
      document.querySelector("#ranking-view").style.display = state.tab === "ranking" || state.tab === "watchlist" ? "" : "none";
      document.querySelector("#detail-view").style.display = state.tab === "detail" ? "" : "none";
      document.querySelector("#market-view").style.display = state.tab === "market" ? "" : "none";
      document.querySelector("#mops-events-view").style.display = state.tab === "mops-events" ? "" : "none";
      document.querySelector("#us-news-view").style.display = state.tab === "us-news" ? "" : "none";
      document.querySelector("#ci-us-news-view").style.display = state.tab === "ci-us-news" ? "" : "none";
      document.querySelector("#coverage-view").style.display = state.tab === "coverage" ? "" : "none";
      if (state.tab === "ranking") await loadRanking(false);
      if (state.tab === "watchlist") await loadRanking(true);
      if (state.tab === "detail") await loadDetail();
      if (state.tab === "market") await loadMarket();
      if (state.tab === "mops-events") await loadMopsEvents();
      if (state.tab === "us-news") await loadUSNews();
      if (state.tab === "ci-us-news") await loadCIUSNews();
      if (state.tab === "coverage") {
        await loadCoverage();
        startJobPolling();
      }
    }
    document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
    document.querySelector("#ranking").addEventListener("change", () => {
      enforceRankingDays();
      reload();
    });
    document.querySelector("#days").addEventListener("change", () => {
      enforceRankingDays();
      reload();
    });
    ["market","limit"].forEach(id => document.querySelector("#" + id).addEventListener("change", reload));
    document.querySelector("#min-volume").addEventListener("input", () => {
      clearTimeout(window.__vol);
      window.__vol = setTimeout(reload, 250);
    });
    document.querySelector("#query").addEventListener("input", () => {
      clearTimeout(window.__q);
      window.__q = setTimeout(() => {
        if (state.tab === "detail") openStockFromQuery();
        else reload();
      }, 300);
    });
    document.querySelector("#query").addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      if (state.tab === "detail") openStockFromQuery();
    });
    document.querySelector("#refresh").addEventListener("click", reload);
    document.querySelector("#back-detail").addEventListener("click", goBackFromDetail);
    document.querySelector("#load-detail").addEventListener("click", loadDetail);
    document.querySelector("#watchlist-toggle").addEventListener("click", toggleWatchlist);
    document.querySelector("#open-mops-for-stock").addEventListener("click", async () => {
      const stock = state.detail?.stock?.stock_id || document.querySelector("#detail-stock").value.trim();
      document.querySelector("#mops-stock").value = stock || "";
      state.mopsSelectedDate = "";
      state.mopsQuickFilter = "";
      state.mopsVisibleCount = 50;
      await setTab("mops-events");
    });
    document.querySelector("#refresh-news").addEventListener("click", refreshNews);
    document.querySelector("#refresh-market-view").addEventListener("click", loadMarket);
    ["market-index-code","market-product","market-range"].forEach(id => document.querySelector("#" + id).addEventListener("change", loadMarket));
    document.querySelector("#refresh-mops-events").addEventListener("click", refreshMopsEvents);
    document.querySelector("#reload-mops-events").addEventListener("click", loadMopsEvents);
    ["mops-start","mops-end","mops-stock","mops-query"].forEach(id => document.querySelector("#" + id).addEventListener("input", () => {
      if (id === "mops-start" || id === "mops-end") {
        state.mopsDateMode = "custom";
        state.mopsDateInitialized = true;
        state.mopsCalendarStart = document.querySelector("#mops-start")?.value || "";
        state.mopsSelectedDate = "";
        state.mopsVisibleCount = 50;
        state.mopsQuickFilter = "";
      }
      clearTimeout(window.__mopsQ);
      window.__mopsQ = setTimeout(loadMopsEvents, 250);
    }));
    document.querySelector("#mops-events-view").addEventListener("click", event => {
      const step = event.target.closest("[data-mops-calendar-step]");
      if (step) {
        const startInput = document.querySelector("#mops-start");
        const endInput = document.querySelector("#mops-end");
        const currentStart = parseLocalDate(startInput?.value || state.mopsCalendarStart || isoDate(0));
        const currentEnd = parseLocalDate(endInput?.value || startInput?.value || isoDate(0));
        const direction = Number(step.dataset.mopsCalendarStep || 0);
        if (currentStart && currentEnd && direction) {
          const nextStart = addLocalMonths(currentStart, direction);
          const nextEnd = addLocalMonths(currentEnd, direction);
          if (startInput) startInput.value = localDateString(nextStart);
          if (endInput) endInput.value = localDateString(nextEnd);
          state.mopsDateMode = "custom";
          state.mopsDateInitialized = true;
          state.mopsCalendarStart = localDateString(nextStart);
          state.mopsSelectedDate = "";
          state.mopsVisibleCount = 50;
          state.mopsQuickFilter = "";
          loadMopsEvents();
        }
        return;
      }
      const more = event.target.closest("#mops-load-more");
      if (more) {
        state.mopsVisibleCount = (state.mopsVisibleCount || 50) + 50;
        renderMopsEventList();
        return;
      }
      const filter = event.target.closest("[data-filter]");
      if (filter) {
        state.mopsQuickFilter = filter.dataset.filter || "";
        state.mopsVisibleCount = 50;
        renderMopsEventList();
        return;
      }
      const item = event.target.closest("[data-event-id]");
      if (!item) return;
      const row = (state.mopsEvents || []).find(entry => entry.event_id === item.dataset.eventId);
      if (row) renderMopsEventDetail(row);
    });
    document.querySelector("#mops-calendar").addEventListener("click", event => {
      const day = event.target.closest("[data-date]");
      if (!day) return;
      const date = day.dataset.date || "";
      state.mopsSelectedDate = state.mopsSelectedDate === date ? "" : date;
      state.mopsVisibleCount = 50;
      state.mopsQuickFilter = "";
      renderMopsCalendar(state.mopsEvents || []);
      renderMopsEventList();
    });
    document.querySelector("#refresh-us-news").addEventListener("click", refreshUSNews);
    document.querySelector("#us-industry").addEventListener("change", loadUSNews);
    document.querySelector("#ci-us-mode").addEventListener("change", () => {
      const sourceSelect = document.querySelector("#ci-us-source");
      if (sourceSelect) {
        sourceSelect.dataset.loaded = "";
        sourceSelect.innerHTML = `<option value="">全部</option>`;
      }
      loadCIUSNews();
    });
    document.querySelector("#ci-us-industry").addEventListener("change", loadCIUSNews);
    document.querySelector("#ci-us-source").addEventListener("change", loadCIUSNews);
    document.querySelector("#ci-us-query").addEventListener("input", () => {
      clearTimeout(window.__ciNewsQ);
      window.__ciNewsQ = setTimeout(loadCIUSNews, 250);
    });
    document.querySelector("#static-export").addEventListener("click", event => runStaticAction("export", event.target));
    document.querySelector("#static-publish").addEventListener("click", event => runStaticAction("publish", event.target));
    document.querySelector("#cancel-update").addEventListener("click", cancelCurrentUpdate);
    document.querySelector("#detail-stock").addEventListener("change", loadDetail);
    document.querySelectorAll(".ma-toggle").forEach(input => input.addEventListener("change", renderPriceChart));
    document.querySelectorAll(".single-refresh").forEach(btn => {
      btn.addEventListener("click", () => refreshSingleSection(btn.dataset.section, btn));
    });
    if (STATIC_MODE) {
      document.querySelectorAll(".single-refresh, #refresh-news, #refresh-us-news, #refresh-mops-events, #us-use-ollama").forEach(btn => btn.style.display = "none");
      document.querySelector("#static-publish-box").style.display = "none";
      document.querySelector("#watchlist-toggle").title = "靜態版自選股儲存在此瀏覽器";
    }
    document.querySelector("#chart-range").addEventListener("change", event => setChartRange(event.target.value));
    document.querySelector("#price-chart").addEventListener("wheel", event => {
      event.preventDefault();
      zoomChart(event.deltaY);
    }, { passive: false });
    syncSeriesSwatches();
    window.addEventListener("resize", () => {
      clearTimeout(window.__chartResize);
      window.__chartResize = setTimeout(() => {
        renderPriceChart();
        renderRevenueChart();
        renderMarginChart();
      }, 120);
    });
    reload().catch(err => {
      document.querySelector("#ranking-table").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    });
  </script>
</body>
</html>
"""


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
        "observed_days": int(as_float(row.get("observed_days"))),
        "close": as_float(row.get("close")),
        "pe_ratio": as_float(row.get("pe_ratio")),
        "dividend_yield": as_float(row.get("dividend_yield")),
        "pb_ratio": as_float(row.get("pb_ratio")),
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
        "volume_ratio_1d": as_float(row.get("volume_ratio_1d")),
        "volume_ratio_5d": as_float(row.get("volume_ratio_5d")),
        "volume_score": as_float(row.get("volume_score")),
        "volume_signal": row.get("volume_signal") or "",
        "top_buy_branch_name": row.get("top_buy_branch_name") or "",
        "top_buy_branch_net_lot": as_float(row.get("top_buy_branch_net_lot")),
        "top_buy_branch_avg_price": as_float(row.get("top_buy_branch_avg_price")),
        "top_sell_branch_name": row.get("top_sell_branch_name") or "",
        "top_sell_branch_net_lot": as_float(row.get("top_sell_branch_net_lot")),
        "top_sell_branch_avg_price": as_float(row.get("top_sell_branch_avg_price")),
        "branch_score": as_float(row.get("branch_score")),
        "chip_score": as_float(row.get("chip_score")),
        "margin_score": as_float(row.get("margin_score")),
        "base_score": as_float(row.get("base_score")),
        "selection_score": as_float(row.get("selection_score")),
        "complete_chip_score": as_float(row.get("complete_chip_score")),
        "confluence_score": as_float(row.get("confluence_score")),
        "revenue_momentum_score": as_float(row.get("revenue_momentum_score")),
        "revenue_month": row.get("revenue_month") or "",
        "revenue_mom_pct": as_float(row.get("revenue_mom_pct")),
        "revenue_yoy_pct": as_float(row.get("revenue_yoy_pct")),
        "revenue_cumulative_yoy_pct": as_float(row.get("revenue_cumulative_yoy_pct")),
        "confluence_inst_score": as_float(row.get("confluence_inst_score")),
        "confluence_branch_score": as_float(row.get("confluence_branch_score")),
        "confluence_price_score": as_float(row.get("confluence_price_score")),
        "confluence_streak_score": as_float(row.get("confluence_streak_score")),
        "confluence_direction_score": as_float(row.get("confluence_direction_score")),
        "inst_net_volume_pct": as_float(row.get("inst_net_volume_pct")),
        "top_buy_branch_volume_pct": as_float(row.get("top_buy_branch_volume_pct")),
        "close_vs_top_buy_avg_pct": as_float(row.get("close_vs_top_buy_avg_pct")),
        "foreign_buy_streak": as_float(row.get("foreign_buy_streak")),
        "foreign_sell_streak": as_float(row.get("foreign_sell_streak")),
        "trust_buy_streak": as_float(row.get("trust_buy_streak")),
        "trust_sell_streak": as_float(row.get("trust_sell_streak")),
        "margin_balance_change_lot": as_float(row.get("margin_balance_change_lot")),
        "short_balance_change_lot": as_float(row.get("short_balance_change_lot")),
        "branch_status": row.get("branch_status") or ("已取得" if row.get("top_buy_branch_name") else "未取得"),
        "selection_reason": row.get("selection_reason") or "",
        "base_reason": row.get("base_reason") or "",
        "total_score": as_float(row.get("total_score")),
    }


def ranking_path(days: int, ranking: str) -> Path:
    names = {
        "total_score": "ranking_total_score",
        "chip_score": "ranking_chip_score",
        "branch_score": "ranking_branch_score",
        "confluence_score": "ranking_confluence_score",
        "selection_score": "ranking_selection_score",
        "foreign_buy": "ranking_foreign_buy",
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
    "chip_score": "法人籌碼分數",
    "selection_score": "基礎選股分",
    "foreign_buy": "外資買超",
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
) -> list[dict[str, object]]:
    text = q.lower()
    output = []
    for row in rows:
        if market and row.get("market") != market:
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
                  AND source = 'histock'
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
              AND source = 'histock'
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
              AND source = 'histock'
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        distinct_count, latest_topn = conn.execute(
            """
            SELECT COUNT(DISTINCT stock_id), MAX(updated_at)
            FROM broker_branch_topn
            WHERE window_days = 20
              AND source = 'histock'
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
    return {
        "message": f"已發布到 GitHub Pages：{status.get('pages_url') or '-'}\n{pre_sync}{('\\n' + retry_note) if retry_note else ''}",
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
            return {"stock": {"stock_id": stock_id, "name": stock_id}, "daily": [], "margin": [], "chart": [], "branch_top": [], "branch_daily": [], "revenues": [], "mops_events": []}
        latest_date = dates[-1]
        placeholders = ",".join("?" for _ in dates)
        stock_row = conn.execute(
            """
            SELECT s.stock_id, s.name, s.market, p.close, p.pe_ratio, p.dividend_yield, p.pb_ratio
            FROM stocks s
            LEFT JOIN daily_prices p
                ON p.stock_id = s.stock_id
               AND p.date = ?
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
                SELECT date, open, high, low, close, avg_price, volume
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
        branch_top = [
            {
                "rank_no": row[0],
                "broker_name": row[1],
                "buy_lot": row[2],
                "sell_lot": row[3],
                "net_lot": row[4],
                "avg_price": row[5],
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
              AND source = 'histock'
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
                SELECT trade_date, broker_name, rank_side, rank_no, buy_lot, sell_lot, net_lot, avg_price
                FROM broker_branch_daily
                WHERE stock_id = ?
                  AND broker_name IN ({broker_placeholders})
                  AND trade_date IN ({date_placeholders})
                ORDER BY trade_date DESC, broker_name
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
                  AND source = 'histock'
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
                          AND source = 'histock'
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
        "news": news,
        "mops_events": mops_events,
    }


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
                rows = [normalize_scan_row(row, days) for row in ranking_source_rows(days, ranking, q)]
                json_response(
                    self,
                    {
                        "title": f"{days} 日排行：{RANKING_LABELS.get(ranking, ranking)}",
                        "rows": filtered_rows(rows, market, q, limit, min_volume),
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
                json_response(self, {"rows": filtered_rows(rows, market, q, limit, min_volume)})
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

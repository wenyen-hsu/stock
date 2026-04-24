from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
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

from stock_chip.branch import branch_coverage, run_branch, run_branch_daily
from stock_chip.news import ensure_news_tables, load_cached_news, refresh_stock_news
from stock_chip.official import (
    connect_db,
    fetch_tpex_institutional_all,
    fetch_tpex_prices_all,
    fetch_twse_institutional_all,
    fetch_twse_prices_all,
    shares_to_lots,
    upsert_institutional,
    upsert_prices,
)
from stock_chip.revenue import run_revenue
from stock_chip.scan import recent_dates, run_scan


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "stock_chip.sqlite"
REPORTS_DIR = ROOT / "reports"
WATCHLIST = ["2376", "2382", "2324", "6196"]
UPDATE_JOBS: dict[str, dict[str, object]] = {}
UPDATE_LOCK = threading.Lock()


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
    button.secondary {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink);
    }
    button.secondary:hover { background: #eef6f5; }
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
    .table-wrap { overflow: auto; max-height: 64vh; }
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
    .price-chart-wrap {
      height: 330px;
      min-height: 260px;
      position: relative;
    }
    .price-chart-wrap canvas { cursor: zoom-in; }
    #price-chart { width: 100%; height: 100%; display: block; }
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
    .job-title { font-weight: 850; margin-bottom: 6px; }
    .job-desc { color: var(--muted); font-size: 13px; line-height: 1.5; min-height: 38px; }
    .job-time { color: var(--muted); font-size: 12px; margin-top: 8px; line-height: 1.5; }
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
    .news-list { display: grid; gap: 12px; }
    .news-item {
      border-bottom: 1px solid #edf1f4;
      padding-bottom: 12px;
    }
    .news-item:last-child { border-bottom: 0; padding-bottom: 0; }
    .news-title {
      color: var(--ink);
      font-weight: 850;
      text-decoration: none;
      line-height: 1.45;
    }
    .news-title:hover { color: var(--accent-dark); text-decoration: underline; }
    .news-meta { color: var(--muted); font-size: 12px; margin: 5px 0 7px; }
    .news-text { color: #344054; font-size: 13px; line-height: 1.65; white-space: pre-wrap; }
    pre {
      margin: 0;
      padding: 14px;
      background: #0b1220;
      color: #dbe7ff;
      overflow: auto;
      border-radius: 8px;
      font-size: 13px;
    }
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
    @media (max-width: 1000px) {
      main { padding: 14px; }
      .toolbar { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
      .metrics, .layout-2, .update-grid { grid-template-columns: 1fr; }
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
          <option value="total_score">總分</option>
          <option value="chip_score">法人籌碼分數</option>
          <option value="branch_score">分點分數</option>
          <option value="foreign_buy">外資買超</option>
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
      <button class="tab" data-tab="coverage">資料狀態</button>
    </section>

    <section class="metrics" id="metrics"></section>

    <section id="ranking-view" class="panel">
      <div class="panel-head">
        <div class="panel-title" id="ranking-title">排行</div>
        <div class="muted" id="ranking-note"></div>
      </div>
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
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title">股價走勢</div>
          <div class="chart-toolbar">
            <span>線圖</span>
            <label class="toggle series-option" data-series="close"><span class="legend-swatch"></span><input type="checkbox" id="close-toggle" checked /> 收盤</label>
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
          <div class="table-wrap" id="revenue-table"></div>
        </div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div><div class="panel-title">區間合計買超前十分點</div><div class="muted" id="branch-top-note">點分點看最近 10 日</div></div>
          <div class="panel-actions"><button class="secondary single-refresh" data-section="branch_top">更新分點排行</button></div>
        </div>
        <div class="table-wrap" id="branch-top-table"></div>
      </section>
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title" id="broker-title">分點最近 10 日</div>
          <div class="panel-actions">
            <div class="broker-list" id="broker-list"></div>
            <button class="secondary single-refresh" data-section="branch_daily">更新近10日</button>
          </div>
        </div>
        <div class="table-wrap" id="broker-daily-table"></div>
      </section>
    </section>

    <section id="coverage-view" class="panel" style="display:none;">
      <div class="panel-head">
        <div class="panel-title">更新中心</div>
        <div class="muted">背景執行固定任務；分點任務會刻意放慢</div>
      </div>
      <div class="panel-body">
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
            <div class="panel-title" style="margin-bottom:8px;">已抓區間分點排行</div>
            <div id="coverage-table"></div>
          </div>
          <div>
            <div class="job-meta">
              <div class="panel-title">最近任務</div>
              <span class="status-pill" id="job-status">尚未執行</span>
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
      公開資料；區間外資、投信、均價、排行分數由本機 SQLite 依最近交易日重新彙總。
      月營收 24 個月歷史目前使用
      <a href="https://finmindtrade.com/" target="_blank" rel="noreferrer">FinMind</a>
      免費 API，月增率與年增率由本機依月營收與去年同期計算。
      分點區間排行與可取得的分點日明細目前解析
      <a href="https://histock.tw/" target="_blank" rel="noreferrer">HiStock</a>
      公開頁面；若批次抓取遇到空回應或該分點未進入單日前排行，個股頁會顯示抓取狀態。
      個股新聞採手動按鈕觸發，來源為
      <a href="https://tw.stock.yahoo.com/" target="_blank" rel="noreferrer">Yahoo 股市</a>
      RSS 與公開文章頁，本機只保留標題、連結與內文摘錄。
    </section>
  </main>

  <script>
    window.STOCK_CHIP_STATIC = false;
  </script>
  <script>
    const state = { tab: "ranking", previousTab: "ranking", detail: null, broker: "", lastDataUpdatedAt: "", chartRange: 20, rankingRows: [], rankingSort: null };
    const STATIC_MODE = window.STOCK_CHIP_STATIC === true;
    const staticCache = {};
    const chartColors = { close: "#0f766e", ma5: "#b42318", ma10: "#175cd3", ma20: "#f59e0b", ma60: "#7a5af8" };
    const fmt = (v) => {
      if (v === null || v === undefined || v === "") return "";
      const n = Number(v);
      if (!Number.isFinite(n)) return String(v);
      return Math.abs(n) >= 1000 ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(Math.round(n * 100) / 100);
    };
    const cls = (v) => Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "";
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
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
    function staticFilterRows(rows, market, q, limit) {
      const text = String(q || "").trim().toLowerCase();
      const max = Number(limit || 50);
      const output = [];
      for (const row of rows || []) {
        if (market && row.market !== market) continue;
        if (text && !String(row.stock_id || "").toLowerCase().includes(text) && !String(row.name || "").toLowerCase().includes(text)) continue;
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
          rows: staticFilterRows(source.rows || source, parsed.searchParams.get("market") || "", q, parsed.searchParams.get("limit") || 50),
        };
      }
      if (parsed.pathname === "/api/watchlist") {
        const ids = staticWatchlistIds();
        const source = await staticData(`data/rankings/${days}d/search_index.json`);
        const order = new Map(ids.map((id, idx) => [id, idx]));
        const rows = (source.rows || source).filter(row => order.has(String(row.stock_id)))
          .sort((a, b) => order.get(String(a.stock_id)) - order.get(String(b.stock_id)));
        return {rows: staticFilterRows(rows, parsed.searchParams.get("market") || "", parsed.searchParams.get("q") || "", parsed.searchParams.get("limit") || 50)};
      }
      if (parsed.pathname === "/api/stock") {
        const stockId = parsed.searchParams.get("stock_id") || "2376";
        const data = await staticData(`data/stocks/${stockId}/${days}d.json`);
        data.stock.in_watchlist = staticWatchlistIds().includes(String(data.stock.stock_id));
        return data;
      }
      if (parsed.pathname === "/api/coverage") {
        return staticData(`data/coverage_${days}d.json`);
      }
      if (parsed.pathname === "/api/update-tasks") {
        const meta = await staticData("data/meta.json");
        return {running: false, latest_job: null, latest_data_updated_at: meta.exported_at || "", tasks: []};
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
      return new URLSearchParams({
        days: document.querySelector("#days").value,
        ranking: document.querySelector("#ranking").value,
        market: document.querySelector("#market").value,
        q: document.querySelector("#query").value.trim(),
        limit: document.querySelector("#limit").value
      });
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
      document.querySelectorAll(".series-option").forEach(label => {
        const swatch = label.querySelector(".legend-swatch");
        const color = chartColors[label.dataset.series] || "#607080";
        if (swatch) swatch.style.background = color;
      });
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
      canvas.width = Math.floor(wrap.clientWidth * dpr);
      canvas.height = Math.floor(wrap.clientHeight * dpr);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, wrap.clientWidth, wrap.clientHeight);
      if (!rows.length) {
        legend.innerHTML = `<span class="muted">沒有股價資料。</span>`;
        return;
      }
      const series = [];
      if (document.querySelector("#close-toggle")?.checked) {
        series.push({ key: "close", label: "收盤", color: chartColors.close });
      }
      selectedMaPeriods().forEach(period => series.push({ key: `ma${period}`, label: `MA${period}`, color: chartColors[`ma${period}`] }));
      const values = [];
      rows.forEach(row => series.forEach(s => {
        const n = Number(row[s.key]);
        if (Number.isFinite(n) && n > 0) values.push(n);
      }));
      if (!values.length) {
        legend.innerHTML = `<span class="muted">沒有可繪製的價格。</span>`;
        return;
      }
      const w = wrap.clientWidth;
      const h = wrap.clientHeight;
      const pad = { top: 12, right: 54, bottom: 34, left: 48 };
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
      function drawLine(s) {
        ctx.beginPath();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.key === "close" ? 2.5 : 1.8;
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
      series.forEach(drawLine);
      const latest = rows[rows.length - 1];
      legend.innerHTML = series.map(s => {
        const value = latest?.[s.key];
        return `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${esc(s.label)} ${esc(fmt(value))}</span>`;
      }).join("") + `<span class="muted">顯示 ${rows.length} / ${allRows.length} 日，滾輪可縮放</span>`;
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
          <a class="news-title" href="${esc(row.url)}" target="_blank" rel="noreferrer">${esc(row.title)}</a>
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
        const target = section === "daily" ? "#daily-table" : section === "revenue" ? "#revenue-table" : section === "branch_top" ? "#branch-top-table" : "#broker-daily-table";
        document.querySelector(target).innerHTML = `<div class="empty">單檔更新失敗：${esc(err.message)}</div>`;
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1800);
        return;
      }
      btn.disabled = false;
      btn.textContent = original;
    }
    const rankingCols = [
      {key:"stock_id", label:"代號", sortType:"text", format: v => v},
      {key:"name", label:"名稱", sortType:"text"},
      {key:"market", label:"市場", sortType:"text"},
      {key:"close", label:"收盤"},
      {key:"observed_days", label:"資料日"},
      {key:"total_score", label:"總分", signed:true},
      {key:"chip_score", label:"法人分", signed:true},
      {key:"branch_score", label:"分點分", signed:true},
      {key:"foreign_net_lot", label:"外資", signed:true},
      {key:"trust_net_lot", label:"投信", signed:true},
      {key:"avg_price", label:"均價"},
      {key:"top_buy_branch_name", label:"買超分點", sortType:"text"},
      {key:"top_buy_branch_net_lot", label:"買超", signed:true},
      {key:"top_buy_branch_avg_price", label:"買超均價"}
    ];
    async function loadMeta() {
      const meta = await getJSON("/api/meta?days=" + document.querySelector("#days").value);
      renderMetrics(document.querySelector("#metrics"), [
        ["最新交易日", meta.latest_date || ""],
        ["股票數", fmt(meta.stock_count)],
        ["上市 / 上櫃", `${fmt(meta.twse_count)} / ${fmt(meta.tpex_count)}`],
        ["已抓區間分點排行", `${fmt(meta.branch_covered)} 檔`]
      ]);
      document.querySelector("#meta").innerHTML = STATIC_MODE
        ? `<span>靜態資料：GitHub Pages</span><span>最新日：${esc(meta.latest_date || "")}</span><span>匯出：${esc(meta.exported_at || "")}</span>`
        : `<span>資料庫：data/stock_chip.sqlite</span><span>最新日：${esc(meta.latest_date || "")}</span>`;
    }
    async function loadRanking(watchlist = false) {
      const data = await getJSON((watchlist ? "/api/watchlist?" : "/api/ranking?") + params().toString());
      state.rankingRows = data.rows;
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
      const rows = sortRows(state.rankingRows || [], rankingCols, state.rankingSort);
      renderTable(document.querySelector("#ranking-table"), rows, rankingCols, {
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
      renderTable(document.querySelector("#coverage-table"), data.coverage, [
        {key:"market", label:"市場"},
        {key:"stocks_with_branch", label:"已抓排行"},
        {key:"attempted", label:"已嘗試"},
        {key:"empty_count", label:"空回應"},
        {key:"failed_count", label:"失敗"},
        {key:"total_stocks", label:"總數"},
        {key:"remaining", label:"剩餘"}
      ]);
      await loadStaticPublishStatus();
    }
    function renderUpdateTasks(tasks, running) {
      if (STATIC_MODE) {
        document.querySelector("#update-tasks").innerHTML = `<div class="empty">靜態版不執行更新任務；資料由本機匯出後部署。</div>`;
        return;
      }
      document.querySelector("#update-tasks").innerHTML = tasks.map(task => `
        <div class="job-card">
          <div class="job-title">${esc(task.title)}</div>
          <div class="job-desc">${esc(task.description)}</div>
          <div class="job-time">最後更新：${esc(task.last_updated_at || "尚未更新")}</div>
          <div class="job-time">資料更新：${esc(task.last_data_updated_at || "尚無資料")}</div>
          <button data-task="${esc(task.id)}" ${running ? "disabled" : ""}>${running ? "執行中" : "開始更新"}</button>
        </div>
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
      if (!job) {
        status.className = "status-pill";
        status.textContent = `尚未執行 · 最後更新 ${state.lastDataUpdatedAt || "-"}`;
        log.textContent = state.lastDataUpdatedAt
          ? `最後資料更新：${state.lastDataUpdatedAt}`
          : "尚未執行更新任務。";
        return;
      }
      const statusText = {queued:"排隊中", running:"執行中", done:"完成", failed:"失敗"}[job.status] || job.status;
      status.className = `status-pill ${job.status === "done" ? "done" : job.status === "failed" ? "failed" : job.status === "running" ? "running" : ""}`;
      status.textContent = `${statusText} · ${job.title || ""} · 最後更新 ${job.updated_at || "-"}`;
      log.textContent = [
        job.current_step ? `目前步驟：${job.current_step}` : "",
        job.started_at ? `開始：${job.started_at}` : "",
        job.updated_at ? `最後更新：${job.updated_at}` : "",
        job.finished_at ? `結束：${job.finished_at}` : "",
        job.error ? `錯誤：${job.error}` : "",
      ].filter(Boolean).join("\\n");
    }
    function renderStaticPublishStatus(data, message = "") {
      const status = document.querySelector("#static-publish-status");
      const note = document.querySelector("#static-publish-note");
      const log = document.querySelector("#static-publish-log");
      if (!status || !note || !log) return;
      const dirty = Number(data?.docs_changed || 0) + Number(data?.reports_changed || 0);
      status.className = `status-pill ${dirty ? "running" : "done"}`;
      status.textContent = dirty ? "有未發布變更" : "已同步";
      note.innerHTML = [
        data?.pages_url ? `<a href="${esc(data.pages_url)}" target="_blank" rel="noreferrer">${esc(data.pages_url)}</a>` : "",
        data?.exported_at ? `靜態資料匯出：${esc(data.exported_at)}` : "",
        data?.commit ? `目前 commit：${esc(data.commit)}` : "",
      ].filter(Boolean).join(" · ") || "尚無發布資訊";
      log.textContent = message || [
        `分支：${data?.branch || "-"}`,
        `docs 變更：${fmt(data?.docs_changed || 0)} 個檔案`,
        `reports 變更：${fmt(data?.reports_changed || 0)} 個檔案`,
        `Pages：${data?.pages_url || "-"}`,
      ].join("\\n");
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
        if (log) log.textContent = err.message;
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
        if (log) log.textContent = err.message;
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
        [`${days}日外資`, fmt(data.stock.foreign_net_lot), cls(data.stock.foreign_net_lot)],
        [`${days}日投信`, fmt(data.stock.trust_net_lot), cls(data.stock.trust_net_lot)]
      ]);
      renderPriceChart();
      renderNews();
      renderTable(document.querySelector("#daily-table"), data.daily, [
        {key:"date", label:"日期"},
        {key:"close", label:"收盤"},
        {key:"avg_price", label:"均價"},
        {key:"volume_lot", label:"成交量(張)"},
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
      const branchTopStatus = data.branch_top_status;
      const branchTopNote = document.querySelector("#branch-top-note");
      if (!data.branch_top.length && branchTopStatus) {
        const statusText = {success:"成功", empty:"空回應", failed:"抓取失敗", missing:"尚未抓取"}[branchTopStatus.status] || branchTopStatus.status;
        const detail = branchTopStatus.error ? `；原因：${branchTopStatus.error}` : "";
        if (branchTopNote) branchTopNote.textContent = `狀態：${statusText}，可單檔更新`;
        document.querySelector("#branch-top-table").innerHTML =
          `<div class="empty">區間分點排行狀態：${esc(statusText)}，最後更新：${esc(branchTopStatus.updated_at || "-")}${esc(detail)}</div>`;
      } else {
        if (branchTopNote) branchTopNote.textContent = "點分點看最近 10 日";
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
      document.querySelector("#broker-title").textContent = state.broker ? `${state.broker} 最近 10 日` : "分點最近 10 日";
      const rows = (data?.branch_daily || []).filter(row => row.broker_name === state.broker);
      const statuses = data?.branch_daily_status || [];
      const success = statuses.filter(row => row.status === "success").length;
      const failed = statuses.filter(row => row.status !== "success");
      if (!rows.length) {
        const message = failed.length
          ? `尚缺 ${failed.length} 個交易日分點日資料；狀態：${failed.map(row => `${row.trade_date} ${row.status}`).join("、")}`
          : success
            ? "單日分點頁已抓取，但此分點最近 10 日未進入 HiStock 可解析的單日前排行。"
            : "尚未抓取分點最近 10 日資料。";
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
      document.querySelector("#ranking-view").style.display = state.tab === "ranking" || state.tab === "watchlist" ? "" : "none";
      document.querySelector("#detail-view").style.display = state.tab === "detail" ? "" : "none";
      document.querySelector("#coverage-view").style.display = state.tab === "coverage" ? "" : "none";
      if (state.tab === "ranking") await loadRanking(false);
      if (state.tab === "watchlist") await loadRanking(true);
      if (state.tab === "detail") await loadDetail();
      if (state.tab === "coverage") {
        await loadCoverage();
        startJobPolling();
      }
    }
    document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
    ["days","ranking","market","limit"].forEach(id => document.querySelector("#" + id).addEventListener("change", reload));
    document.querySelector("#query").addEventListener("input", () => { clearTimeout(window.__q); window.__q = setTimeout(reload, 250); });
    document.querySelector("#refresh").addEventListener("click", reload);
    document.querySelector("#back-detail").addEventListener("click", goBackFromDetail);
    document.querySelector("#load-detail").addEventListener("click", loadDetail);
    document.querySelector("#watchlist-toggle").addEventListener("click", toggleWatchlist);
    document.querySelector("#refresh-news").addEventListener("click", refreshNews);
    document.querySelector("#static-export").addEventListener("click", event => runStaticAction("export", event.target));
    document.querySelector("#static-publish").addEventListener("click", event => runStaticAction("publish", event.target));
    document.querySelector("#detail-stock").addEventListener("change", loadDetail);
    document.querySelectorAll(".ma-toggle").forEach(input => input.addEventListener("change", renderPriceChart));
    document.querySelector("#close-toggle").addEventListener("change", renderPriceChart);
    document.querySelectorAll(".single-refresh").forEach(btn => {
      btn.addEventListener("click", () => refreshSingleSection(btn.dataset.section, btn));
    });
    if (STATIC_MODE) {
      document.querySelectorAll(".single-refresh, #refresh-news").forEach(btn => btn.style.display = "none");
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
      window.__chartResize = setTimeout(renderPriceChart, 120);
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
        "avg_price": as_float(row.get(f"{days}d_avg_price")),
        "foreign_net_lot": as_float(row.get(f"{days}d_foreign_net_lot")),
        "trust_net_lot": as_float(row.get(f"{days}d_trust_net_lot")),
        "inst_net_lot": as_float(row.get(f"{days}d_inst_net_lot")),
        "top_buy_branch_name": row.get("top_buy_branch_name") or "",
        "top_buy_branch_net_lot": as_float(row.get("top_buy_branch_net_lot")),
        "top_buy_branch_avg_price": as_float(row.get("top_buy_branch_avg_price")),
        "top_sell_branch_name": row.get("top_sell_branch_name") or "",
        "top_sell_branch_net_lot": as_float(row.get("top_sell_branch_net_lot")),
        "top_sell_branch_avg_price": as_float(row.get("top_sell_branch_avg_price")),
        "branch_score": as_float(row.get("branch_score")),
        "chip_score": as_float(row.get("chip_score")),
        "total_score": as_float(row.get("total_score")),
    }


def ranking_path(days: int, ranking: str) -> Path:
    names = {
        "total_score": "ranking_total_score",
        "chip_score": "ranking_chip_score",
        "branch_score": "ranking_branch_score",
        "foreign_buy": "ranking_foreign_buy",
        "trust_buy": "ranking_trust_buy",
        "inst_buy": "ranking_inst_buy",
        "foreign_trust_same_buy": "ranking_foreign_trust_same_buy",
        "near_avg_with_inst_buy": "ranking_near_avg_with_inst_buy",
    }
    return REPORTS_DIR / f"{names.get(ranking, 'ranking_total_score')}_{days}d.csv"


def filtered_rows(rows: list[dict[str, object]], market: str, q: str, limit: int) -> list[dict[str, object]]:
    text = q.lower()
    output = []
    for row in rows:
        if market and row.get("market") != market:
            continue
        if text and text not in str(row.get("stock_id", "")).lower() and text not in str(row.get("name", "")).lower():
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


UPDATE_TASKS = [
    {
        "id": "official_scan",
        "title": "官方行情與排行",
        "description": "更新上市上櫃近 20 個交易日行情、法人買賣超，並重算 20 日與 5 日排行。",
    },
    {
        "id": "watchlist_revenue",
        "title": "自選股營收",
        "description": "更新自選股近 24 個月營收與去年同期，用於個股頁營收表。",
    },
    {
        "id": "watchlist_branch_daily",
        "title": "自選股近 10 日分點",
        "description": "補自選股最近 10 個交易日的 HiStock 單日分點頁，只抓缺漏資料。",
    },
    {
        "id": "watchlist_news",
        "title": "自選股新聞標題",
        "description": "更新自選股 Yahoo 股市 RSS 標題與連結；文章內文保留到個股頁手動抓取。",
    },
    {
        "id": "top100_branch",
        "title": "排行前 100 區間分點",
        "description": "依目前 20 日總分排行取前 100 檔，更新區間買超前十分點，再重算排行。",
    },
]


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_time(value: object) -> str:
    return str(value or "").replace("T", " ")[:19]


def data_update_times() -> dict[str, str]:
    with connect_db(DB_PATH) as conn:
        ensure_gui_tables(conn)
        watchlist = current_watchlist_ids()
        placeholders = ",".join("?" for _ in watchlist) if watchlist else "''"
        official = conn.execute("SELECT MAX(updated_at) FROM trading_days").fetchone()[0]
        revenue = conn.execute(
            f"SELECT MAX(updated_at) FROM monthly_revenues WHERE stock_id IN ({placeholders})",
            watchlist,
        ).fetchone()[0]
        branch_daily = conn.execute(
            f"""
            SELECT MAX(updated_at)
            FROM branch_fetch_status
            WHERE stock_id IN ({placeholders})
              AND window_days = 1
            """,
            watchlist,
        ).fetchone()[0]
        news = conn.execute(
            f"SELECT MAX(fetched_at) FROM stock_news WHERE stock_id IN ({placeholders})",
            watchlist,
        ).fetchone()[0]
        top100_branch = conn.execute(
            """
            SELECT MAX(updated_at)
            FROM broker_branch_topn
            WHERE window_days = 20
              AND source = 'histock'
            """
        ).fetchone()[0]
    return {
        "official_scan": normalize_time(official),
        "watchlist_revenue": normalize_time(revenue),
        "watchlist_branch_daily": normalize_time(branch_daily),
        "watchlist_news": normalize_time(news),
        "top100_branch": normalize_time(top100_branch),
    }


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


def update_task_state() -> dict[str, object]:
    memory_jobs = recent_jobs()
    persisted = persisted_jobs()
    by_id: dict[str, dict[str, object]] = {str(job["id"]): job for job in persisted}
    for job in memory_jobs:
        by_id[str(job["id"])] = job
    jobs = sorted(by_id.values(), key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)[:8]
    latest = jobs[0] if jobs else None
    latest_by_task: dict[str, dict[str, object]] = {}
    for job in jobs:
        task_id = str(job.get("task_id") or "")
        if task_id and task_id not in latest_by_task:
            latest_by_task[task_id] = job
    data_times = data_update_times()
    latest_data_updated_at = max((value for value in data_times.values() if value), default="")
    tasks = []
    for task in UPDATE_TASKS:
        task_item = dict(task)
        latest_task_job = latest_by_task.get(str(task["id"]))
        task_item["last_job_status"] = latest_task_job.get("status") if latest_task_job else ""
        task_item["last_job_updated_at"] = latest_task_job.get("updated_at") if latest_task_job else ""
        task_item["last_data_updated_at"] = data_times.get(str(task["id"]), "")
        task_item["last_updated_at"] = task_item["last_job_updated_at"] or task_item["last_data_updated_at"]
        tasks.append(task_item)
    return {
        "tasks": tasks,
        "running": running_job() is not None,
        "latest_job": latest,
        "latest_data_updated_at": latest_data_updated_at,
        "jobs": jobs,
    }


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
    if task_id == "watchlist_revenue":
        return (
            "自選股營收",
            [
                (
                    "更新 24 個月營收",
                    [py, "-m", "stock_chip.revenue", "--months", "24", "--watchlist", watchlist, "--sleep", "0.4"],
                )
            ],
        )
    if task_id == "watchlist_branch_daily":
        return (
            "自選股近 10 日分點",
            [
                (
                    "補自選股最近 10 日分點",
                    [
                        py,
                        "-m",
                        "stock_chip.branch",
                        "--days",
                        "10",
                        "--top",
                        "80",
                        "--daily",
                        "--only-missing",
                        "--watchlist",
                        watchlist,
                        "--sleep",
                        "1.2",
                        "--retry-sleeps",
                        "8,20,45",
                    ],
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
    if task_id == "top100_branch":
        ids = top_stock_ids_from_report(20, 100)
        if not ids:
            ids = current_watchlist_ids()
        branch_watchlist = ",".join(ids)
        return (
            "排行前 100 區間分點",
            [
                (
                    "更新前 100 檔 20 日區間分點",
                    [
                        py,
                        "-m",
                        "stock_chip.branch",
                        "--days",
                        "20",
                        "--top",
                        "10",
                        "--watchlist",
                        branch_watchlist,
                        "--sleep",
                        "1.5",
                        "--retry-sleeps",
                        "8,20,45",
                    ],
                ),
                (
                    "重算 20 日排行",
                    [py, "-m", "stock_chip.scan", "--days", "20", "--limit", "100", "--watchlist", watchlist],
                ),
                (
                    "重算 5 日排行",
                    [py, "-m", "stock_chip.scan", "--days", "5", "--limit", "100", "--watchlist", watchlist],
                ),
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
    try:
        for label, args in steps:
            with UPDATE_LOCK:
                job = UPDATE_JOBS[job_id]
                job["current_step"] = label
                job["updated_at"] = now_text()
                step_snapshot = dict(job)
            persist_job(step_snapshot)
            proc = subprocess.run(
                args,
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60 * 60,
                check=False,
            )
            if proc.returncode != 0:
                if proc.stderr:
                    append_job_log(job_id, proc.stderr[-2000:])
                raise RuntimeError(f"{label} 失敗，exit code {proc.returncode}")
            with UPDATE_LOCK:
                job = UPDATE_JOBS[job_id]
                job["updated_at"] = now_text()
                step_done_snapshot = dict(job)
            persist_job(step_done_snapshot)
        with UPDATE_LOCK:
            job = UPDATE_JOBS[job_id]
            job["status"] = "done"
            job["finished_at"] = now_text()
            job["updated_at"] = job["finished_at"]
            job["current_step"] = "完成"
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
    return {
        "message": proc.stdout.strip() or "已匯出 docs/ 靜態資料。",
        "status": static_publish_status(),
    }


def publish_static_pages() -> dict[str, object]:
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
    run_git(["commit", "-m", message], timeout=120)
    run_git(["push", "origin", run_git(["branch", "--show-current"])], timeout=10 * 60)
    status = static_publish_status()
    return {
        "message": f"已發布到 GitHub Pages：{status.get('pages_url') or '-'}",
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

    if section == "branch_daily":
        result = run_branch_daily(
            db_path=DB_PATH,
            days=10,
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
    for trade_date in dates:
        day = dt.date.fromisoformat(trade_date)
        if target_market == "TWSE":
            daily_prices = fetch_twse_prices_all(day, stock_only=True)
            daily_inst = fetch_twse_institutional_all(day, stock_only=True)
        elif target_market == "TPEX":
            daily_prices = fetch_tpex_prices_all(day, stock_only=True)
            daily_inst = fetch_tpex_institutional_all(day, stock_only=True)
        else:
            daily_prices = [
                *fetch_twse_prices_all(day, stock_only=True),
                *fetch_tpex_prices_all(day, stock_only=True),
            ]
            daily_inst = [
                *fetch_twse_institutional_all(day, stock_only=True),
                *fetch_tpex_institutional_all(day, stock_only=True),
            ]
        price_rows.extend(row for row in daily_prices if row.stock_id == target_id)
        inst_rows.extend(row for row in daily_inst if row.stock_id == target_id)

    if not price_rows:
        raise ValueError(f"近 {days} 個交易日沒有 {target_id} {target_name} 的行情資料")
    with connect_db(DB_PATH) as conn:
        upsert_prices(conn, price_rows)
        upsert_institutional(conn, inst_rows)
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
        "dates": dates,
    }


def stock_detail(stock_id: str, days: int) -> dict[str, object]:
    with connect_db(DB_PATH) as conn:
        dates = recent_dates(conn, days)
        if not dates:
            return {"stock": {"stock_id": stock_id, "name": stock_id}, "daily": [], "chart": [], "branch_top": [], "branch_daily": [], "revenues": []}
        latest_date = dates[-1]
        placeholders = ",".join("?" for _ in dates)
        stock_row = conn.execute(
            """
            SELECT s.stock_id, s.name, s.market, p.close
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
                p.date, p.close, p.avg_price, p.volume,
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
        foreign_net = sum(row[4] or 0 for row in daily_raw)
        trust_net = sum(row[5] or 0 for row in daily_raw)
        daily = [
            {
                "date": row[0],
                "close": row[1],
                "avg_price": row[2],
                "volume_lot": shares_to_lots(row[3]),
                "foreign_net_lot": shares_to_lots(row[4]),
                "trust_net_lot": shares_to_lots(row[5]),
                "dealer_net_lot": shares_to_lots(row[6]),
            }
            for row in daily_raw
        ]
        chart_dates = recent_dates(conn, max(240, days))
        chart: list[dict[str, object]] = []
        if chart_dates:
            chart_placeholders = ",".join("?" for _ in chart_dates)
            chart_raw = conn.execute(
                f"""
                SELECT date, close, avg_price, volume
                FROM daily_prices
                WHERE stock_id = ?
                  AND date IN ({chart_placeholders})
                ORDER BY date ASC
                """,
                [stock_id, *chart_dates],
            ).fetchall()
            closes: list[float | None] = [row[1] for row in chart_raw]
            for idx, row in enumerate(chart_raw):
                item: dict[str, object] = {
                    "date": row[0],
                    "close": row[1],
                    "avg_price": row[2],
                    "volume_lot": shares_to_lots(row[3]),
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
            daily_dates = recent_dates(conn, 10)
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
            daily_dates = recent_dates(conn, 10)
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
    return {
        "stock": {
            "stock_id": stock_row[0],
            "name": stock_row[1],
            "market": stock_row[2],
            "latest_date": latest_date,
            "close": stock_row[3],
            "avg_price": round(avg_price, 4) if avg_price is not None else None,
            "foreign_net_lot": shares_to_lots(foreign_net),
            "trust_net_lot": shares_to_lots(trust_net),
            "volume_lot": shares_to_lots(volume),
            "in_watchlist": stock_row[0] in current_watchlist_ids(),
        },
        "daily": daily,
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
    }


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
                market = params.get("market", [""])[0]
                q = params.get("q", [""])[0]
                limit = int(params.get("limit", ["50"])[0])
                rows = [normalize_scan_row(row, days) for row in ranking_source_rows(days, ranking, q)]
                json_response(
                    self,
                    {"title": f"{days} 日排行：{ranking}", "rows": filtered_rows(rows, market, q, limit)},
                )
                return
            if parsed.path == "/api/watchlist":
                days = int(params.get("days", ["20"])[0])
                market = params.get("market", [""])[0]
                q = params.get("q", [""])[0]
                limit = int(params.get("limit", ["50"])[0])
                watchlist = current_watchlist_ids()
                watch_order = {stock_id: idx for idx, stock_id in enumerate(watchlist)}
                source_rows = read_csv(REPORTS_DIR / f"scan_all_{days}d.csv") or read_csv(ranking_path(days, "total_score"))
                rows = [normalize_scan_row(row, days) for row in source_rows if row.get("stock_id") in watch_order]
                rows.sort(key=lambda row: watch_order.get(str(row.get("stock_id")), 9999))
                json_response(self, {"rows": filtered_rows(rows, market, q, limit)})
                return
            if parsed.path == "/api/watchlist-items":
                json_response(self, {"rows": watchlist_items()})
                return
            if parsed.path == "/api/stock":
                days = int(params.get("days", ["20"])[0])
                stock_id = params.get("stock_id", ["2376"])[0]
                json_response(self, stock_detail(stock_id, days))
                return
            if parsed.path == "/api/news":
                stock_id = params.get("stock_id", [""])[0].strip()
                limit = min(max(int(params.get("limit", ["20"])[0]), 1), 50)
                with connect_db(DB_PATH) as conn:
                    json_response(self, {"stock_id": stock_id, "rows": load_cached_news(conn, stock_id, limit=limit)})
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
            if parsed.path == "/api/news/refresh":
                payload = self.read_json_body()
                stock_id = str(payload.get("stock_id") or "")
                limit = min(max(int(payload.get("limit") or 8), 1), 15)
                json_response(self, refresh_stock_news(DB_PATH, stock_id, limit=limit))
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

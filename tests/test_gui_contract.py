"""整理計畫第 0 階段：凍結 GUI 路由、分頁與靜態 JSON 路徑。

產品碼不變。搬家時這些斷言必須繼續綠。解析失敗要當成測試失敗，不能靜默通過。
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from stock_chip import gui
from stock_chip.gui import GUIHandler, INDEX_HTML


FROZEN_GET = {
    "/",
    "/api/meta",
    "/api/ranking",
    "/api/watchlist",
    "/api/scan-all",
    "/api/industries",
    "/api/watchlist-items",
    "/api/stock/resolve",
    "/api/stock",
    "/api/market",
    "/api/market-sentiment",
    "/api/news",
    "/api/us-news",
    "/api/ci-us-news",
    "/api/ci-us-news-live",
    "/api/mops-events",
    "/api/obsidian/status",
    "/api/coverage",
    "/api/backtest",
    "/api/digest",
    "/api/suggest",
    "/api/trend",
    "/api/etf",
    "/api/dividends",
    "/api/daytrade",
    "/api/weekly",
    "/api/health",
    "/api/update-tasks",
    "/api/static/status",
}

FROZEN_POST = {
    "/api/watchlist",
    "/api/update",
    "/api/update/cancel",
    "/api/news/refresh",
    "/api/us-news/refresh",
    "/api/mops-events/refresh",
    "/api/tdcc/refresh",
    "/api/financials/refresh",
    "/api/obsidian/export",
    "/api/stock/refresh-section",
    "/api/stock/ensure",
    "/api/static/export",
    "/api/static/publish",
}

FROZEN_TABS = {
    "ranking",
    "trend",
    "weekly",
    "daytrade",
    "watchlist",
    "portfolio",
    "sector",
    "detail",
    "market",
    "mops-events",
    "us-news",
    "ci-us-news",
    "coverage",
}

STATIC_JSON_MARKERS = [
    "data/meta.json",
    "data/rankings/",
    "search_index.json",
    "chart_lite.json",
    "data/weekly.json",
    "data/daytrade.json",
    "data/trend_",
    "data/market.json",
    "data/market_sentiment.json",
    "data/backtest.json",
    "data/us_news.json",
    "data/ci_us_news.json",
    "data/mops_events.json",
    "data/health.json",
    "data/suggest.json",
    "data/digest_",
    "data/coverage_",
    "data/dividends.json",
    "data/etf.json",
]

GUI_EXPORT_NAMES = {
    "INDEX_HTML",
    "DB_PATH",
    "REPORTS_DIR",
    "as_float",
    "connect_db",
    "current_watchlist_ids",
    "db_meta",
    "market_payload",
    "market_sentiment_payload",
    "normalize_scan_row",
    "now_text",
    "ranking_path",
    "read_csv",
    "stock_detail",
}

PATH_COMPARE_RE = re.compile(r'parsed\.path\s*==\s*"([^"]+)"')
TAB_RE = re.compile(r'data-tab="([^"]+)"')


def _handler_paths(method_name: str) -> set[str]:
    source = inspect.getsource(getattr(GUIHandler, method_name))
    paths = set(PATH_COMPARE_RE.findall(source))
    assert paths, f"解析不到 {method_name} 的路徑——正則失效，不是契約通過"
    return paths


def test_get_routes_match_frozen_list():
    assert _handler_paths("do_GET") == FROZEN_GET


def test_post_routes_match_frozen_list():
    assert _handler_paths("do_POST") == FROZEN_POST


def test_local_get_uses_export_static_loaders():
    source = inspect.getsource(GUIHandler.do_GET)
    required = {
        "/api/etf": "load_etf_flows",
        "/api/dividends": "load_dividend_events",
        "/api/daytrade": "build_daytrade_report",
    }
    for path, loader in required.items():
        assert path in source, f"do_GET 缺少 {path}"
        assert loader in source, f"{path} 必須呼叫 {loader}，不要另寫一套計算"


def test_index_html_keeps_static_mode_flag():
    assert "window.STOCK_CHIP_STATIC = false;" in INDEX_HTML


def test_index_html_keeps_all_data_tabs():
    found = set(TAB_RE.findall(INDEX_HTML))
    assert found, "解析不到 data-tab——正則失效"
    missing = FROZEN_TABS - found
    assert not missing, f"INDEX_HTML 缺少分頁：{sorted(missing)}"


def test_index_html_keeps_static_json_paths():
    missing = [marker for marker in STATIC_JSON_MARKERS if marker not in INDEX_HTML]
    assert not missing, f"INDEX_HTML 缺少靜態 JSON 路徑：{missing}"


def test_export_static_still_imports_gui_names():
    source = Path("stock_chip/export_static.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "stock_chip.gui":
            imported.update(alias.name for alias in node.names)
    assert imported, "export_static.py 不再從 stock_chip.gui import——契約被改掉了"
    missing = GUI_EXPORT_NAMES - imported
    assert not missing, f"export_static 少 import：{sorted(missing)}"


def test_gui_still_exports_contract_names():
    missing = [name for name in sorted(GUI_EXPORT_NAMES) if not hasattr(gui, name)]
    assert not missing, f"stock_chip.gui 缺少名稱：{missing}"

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

from stock_chip.official import connect_db
from stock_chip.revenue import expected_revenue_month


# 來源健康檢查：逐來源比對「最新資料日」與期望值（以 trading_days 為準，
# 週末與假日不誤報），異常時可自動開/更新 GitHub Issue。
# 動機：HiStock 曾悄悄改版導致抓了數週空資料才被發現。
ISSUE_TITLE = "資料來源異常"
ISSUE_LABEL = "data-health"


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def trading_days_back(conn: sqlite3.Connection, latest: str, back: int) -> str:
    """最新交易日往回第 back 個交易日（back=0 即最新）。"""
    rows = conn.execute(
        "SELECT date FROM trading_days WHERE date <= ? ORDER BY date DESC LIMIT ?",
        (latest, back + 1),
    ).fetchall()
    return rows[-1][0] if rows else latest


def latest_published_quarter(today: dt.date | None = None) -> str:
    from stock_chip.financials import published_quarters

    quarters = published_quarters(today, count=1)
    year, season = quarters[0]
    return f"{year}Q{season}"


def check_source(
    conn: sqlite3.Connection,
    key: str,
    label: str,
    table: str,
    date_column: str,
    expected: str,
    min_rows: int = 1,
    warn_only: bool = False,
    date_filter_sql: str = "",
) -> dict[str, Any]:
    if not table_exists(conn, table):
        return {"key": key, "label": label, "latest": None, "expected": expected,
                "rows_at_latest": 0, "status": "not_deployed", "note": "資料表尚未建立"}
    latest_row = conn.execute(f"SELECT MAX({date_column}) FROM {table} {date_filter_sql}").fetchone()
    latest = latest_row[0] if latest_row else None
    if latest is None:
        return {"key": key, "label": label, "latest": None, "expected": expected,
                "rows_at_latest": 0,
                "status": "warn" if warn_only else "fail", "note": "資料表為空"}
    # 檢核基準：最近一個「列數達門檻」的日期。公布期剛開始時（如月營收，
    # 少數公司提前公告新月份）最大日期只有涓滴資料，不應觸發告警。
    healthy_row = conn.execute(
        f"""
        SELECT {date_column}, COUNT(*) FROM {table} {date_filter_sql}
        GROUP BY {date_column}
        HAVING COUNT(*) >= ?
        ORDER BY {date_column} DESC
        LIMIT 1
        """,
        (min_rows,),
    ).fetchone()
    healthy_latest = healthy_row[0] if healthy_row else None
    rows_at_healthy = healthy_row[1] if healthy_row else 0
    ok = healthy_latest is not None and healthy_latest >= expected
    note = ""
    if healthy_latest is None:
        note = f"沒有任何日期的列數達門檻 {min_rows}"
    elif healthy_latest < expected:
        note = f"達門檻的最新日 {healthy_latest} 落後期望 {expected}"
    elif latest > healthy_latest:
        rows_at_max = conn.execute(
            f"SELECT COUNT(*) FROM {table} {date_filter_sql} {'AND' if date_filter_sql else 'WHERE'} {date_column} = ?",
            (latest,),
        ).fetchone()[0]
        note = f"{latest} 公布中（{rows_at_max} 列），以 {healthy_latest} 檢核"
    return {
        "key": key,
        "label": label,
        "latest": healthy_latest or latest,
        "expected": expected,
        "rows_at_latest": rows_at_healthy,
        "status": "ok" if ok else ("warn" if warn_only else "fail"),
        "note": note,
    }


def collect_health(db_path: Path) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        latest_row = conn.execute("SELECT MAX(date) FROM trading_days").fetchone()
        latest_trading = latest_row[0] if latest_row and latest_row[0] else None
        if latest_trading is None:
            return {
                "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
                "latest_trading_day": None,
                "sources": [],
                "ok": False,
                "note": "trading_days 為空，資料庫尚未初始化",
            }
        one_back = trading_days_back(conn, latest_trading, 1)
        two_back = trading_days_back(conn, latest_trading, 2)
        two_weeks_ago = (dt.date.fromisoformat(latest_trading) - dt.timedelta(days=14)).isoformat()
        three_days_ago = (dt.date.fromisoformat(latest_trading) - dt.timedelta(days=3)).isoformat()

        sources = [
            check_source(conn, "daily_prices", "日行情", "daily_prices", "date",
                         expected=latest_trading, min_rows=800),
            check_source(conn, "institutional_trades", "三大法人", "institutional_trades", "date",
                         expected=latest_trading, min_rows=800),
            check_source(conn, "margin_trades", "融資融券", "margin_trades", "date",
                         expected=latest_trading, min_rows=500),
            check_source(conn, "market_index_daily", "大盤指數", "market_index_daily", "date",
                         expected=one_back,
                         date_filter_sql="WHERE index_code = 'TAIEX'"),
            check_source(conn, "monthly_revenues", "月營收", "monthly_revenues", "revenue_month",
                         expected=expected_revenue_month(), min_rows=100),
            check_source(conn, "broker_branch_topn", "分點排行", "broker_branch_topn", "as_of_date",
                         expected=one_back, min_rows=100),
            check_source(conn, "broker_branch_daily", "分點每日明細", "broker_branch_daily", "trade_date",
                         expected=two_back, warn_only=True),
            check_source(conn, "mops_events", "MOPS 重大事件", "mops_events", "event_date",
                         expected=three_days_ago, warn_only=True),
            check_source(conn, "shareholding_dispersion", "TDCC 股權分散", "shareholding_dispersion", "data_date",
                         expected=two_weeks_ago, min_rows=10000),
            check_source(conn, "dividend_events", "除權息事件", "dividend_events", "ex_date",
                         expected=two_weeks_ago, warn_only=True),
            check_source(conn, "etf_universe", "ETF 資金流", "etf_universe", "data_date",
                         expected=one_back, min_rows=100, warn_only=True),
            check_source(conn, "quarterly_financials", "季度財報", "quarterly_financials", "year_quarter",
                         expected=latest_published_quarter(), min_rows=300),
            check_source(conn, "ranking_snapshots", "排行快照", "ranking_snapshots", "snapshot_date",
                         expected=one_back, warn_only=True),
        ]
    failures = [source for source in sources if source["status"] == "fail"]
    return {
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "latest_trading_day": latest_trading,
        "sources": sources,
        "ok": not failures,
        "fail_count": len(failures),
        "warn_count": sum(1 for source in sources if source["status"] == "warn"),
    }


def health_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"資料健康檢查於 {payload['checked_at']}（最新交易日 {payload.get('latest_trading_day')}）",
        "",
        "| 來源 | 最新 | 期望 | 最新日列數 | 狀態 | 備註 |",
        "| --- | --- | --- | ---: | --- | --- |",
    ]
    for source in payload.get("sources", []):
        lines.append(
            f"| {source['label']} | {source['latest'] or '-'} | {source['expected']} | "
            f"{source['rows_at_latest']} | {source['status']} | {source['note'] or ''} |"
        )
    return "\n".join(lines)


def github_api(method: str, url: str, token: str, body: dict | None = None) -> Any:
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "stock-chip-health",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def ensure_github_issue(payload: dict[str, Any]) -> str:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return "skipped: no GITHUB_TOKEN/GITHUB_REPOSITORY"
    base = f"https://api.github.com/repos/{repo}"
    issues = github_api("GET", f"{base}/issues?state=open&labels={ISSUE_LABEL}&per_page=20", token)
    existing = next((issue for issue in issues or [] if issue.get("title") == ISSUE_TITLE), None)
    body = health_markdown(payload)
    if payload.get("ok"):
        if existing:
            github_api(
                "POST",
                f"{base}/issues/{existing['number']}/comments",
                token,
                {"body": "所有來源已恢復正常，自動關閉。\n\n" + body},
            )
            github_api("PATCH", f"{base}/issues/{existing['number']}", token, {"state": "closed"})
            return f"closed issue #{existing['number']} (recovered)"
        return "ok: no issue needed"
    if existing:
        github_api("POST", f"{base}/issues/{existing['number']}/comments", token, {"body": body})
        return f"commented on issue #{existing['number']}"
    created = github_api(
        "POST",
        f"{base}/issues",
        token,
        {"title": ISSUE_TITLE, "body": body, "labels": [ISSUE_LABEL]},
    )
    return f"created issue #{created.get('number')}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check data-source freshness against expectations.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--json-out", default="")
    parser.add_argument(
        "--github-issue",
        action="store_true",
        help="異常時開/更新 GitHub Issue（需 GITHUB_TOKEN；此模式下永遠以 0 退出）。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = collect_health(Path(args.db))
    print(health_markdown(payload))
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.github_issue:
        try:
            print(ensure_github_issue(payload))
        except Exception as exc:
            print(f"issue update failed: {exc}")
        return
    if not payload.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

from stock_chip.official import connect_db, unresolved_fetch_failures
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


def weekdays_between(start: dt.date, end: dt.date) -> int:
    """start（不含）到 end（含）之間的平日數。"""
    count = 0
    cursor = start + dt.timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            count += 1
        cursor += dt.timedelta(days=1)
    return count


def check_freshness(latest_trading: str, today: dt.date | None = None) -> dict[str, Any]:
    """資料新鮮度：最新交易日與真實日曆的落差。

    其他檢查都以 trading_days 為期望基準，若「最新交易日整天沒進站」，
    行情與 trading_days 會一起停住而比不出異常——2026-07-30 全天資料
    未入庫卻通過所有檢查即為此盲點。此處改以系統日期為外部基準。

    台股假日無法從資料推知，故用平日數分級：落後 1 個平日內為正常
    （當日盤後尚未更新），2 個平日 warn，4 個以上 fail（連假最長約
    9 天，屆時 note 會說明可能為假日）。
    """
    today = today or dt.date.today()
    gap = weekdays_between(dt.date.fromisoformat(latest_trading), today)
    if gap <= 1:
        status, note = "ok", ""
    elif gap <= 3:
        status = "warn"
        note = f"最新交易日落後 {gap} 個平日（可能為連假，或當日資料未入庫）"
    else:
        status = "fail"
        note = f"最新交易日落後 {gap} 個平日，資料很可能中斷"
    return {
        "key": "data_freshness",
        "label": "資料新鮮度",
        "latest": latest_trading,
        "expected": today.isoformat(),
        "rows_at_latest": gap,
        "status": status,
        "note": note,
    }


def check_fetch_failures(conn: sqlite3.Connection) -> dict[str, Any]:
    """抓取端自己回報的失敗，是這裡唯一不必靠日曆猜的訊號。

    其他檢查都在問「資料多久沒動了」，而那個問法分不出「今天是假日」與
    「今天抓失敗了」——2026-08-18 TWSE 逾時整天沒進站，freshness 因為
    「落後一天算正常」而放行，就是這個盲點。抓取端其實分得出來（official.py
    會標「!! 新於已入庫資料」、daytrade.py 知道哪個來源掛了），這裡直接讀那個結論。

    處置股尤其只能靠這條：它沒有「當日應有幾筆」的概念，安靜的一天筆數本來
    就不會動，但它是當沖候選池的硬排除條件，漏掉會讓做不了當沖的股票進榜。
    """
    if not table_exists(conn, "fetch_failures"):
        return {"key": "fetch_failures", "label": "抓取失敗紀錄", "latest": None,
                "expected": "0 筆", "rows_at_latest": 0,
                "status": "not_deployed", "note": "資料表尚未建立"}
    rows = unresolved_fetch_failures(conn)
    if not rows:
        return {"key": "fetch_failures", "label": "抓取失敗紀錄", "latest": None,
                "expected": "0 筆", "rows_at_latest": 0, "status": "ok", "note": ""}
    detail = "；".join(f"{r['source']}@{r['target_date']}：{r['error'][:80]}" for r in rows[:4])
    if len(rows) > 4:
        detail += f"（另有 {len(rows) - 4} 筆）"
    return {
        "key": "fetch_failures",
        "label": "抓取失敗紀錄",
        "latest": max(r["target_date"] for r in rows),
        "expected": "0 筆",
        "rows_at_latest": len(rows),
        "status": "fail",
        "note": detail,
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
            check_freshness(latest_trading),
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
            # two_back 而非 one_back：分點抓取自 2026-08-06 起是獨立 workflow
            # （commit 9712b17d），它在主管線之後才跑、要兩小時。所以健檢執行的
            # 當下，分點資料必然落後兩個交易日；期望 one_back 等於每個交易日
            # 都必定失敗一次，再由分點跑完後的重新匯出把 issue 關掉。
            # 實測 #7(8/14)、#15(8/19)、#19(8/27) 三次健檢 issue，唯一的 fail
            # 都是這一項，且都在 1~1.5 小時後自動關閉——拆分前的 #3(7/24) 則正常。
            # 這種每晚固定開關的告警只會訓練人忽略它，比沒有告警更糟。
            # 放寬一天仍保有偵測力：分點真的斷一天就會落到 three_back 而觸發。
            check_source(conn, "broker_branch_topn", "分點排行", "broker_branch_topn", "as_of_date",
                         expected=two_back, min_rows=100),
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
            # 抓取端自己回報的失敗。放在這裡而不是靠日期落差判斷，是因為
            # 「多久沒更新」分不出假日與抓失敗——2026-08-18 就是這樣被放行的。
            check_fetch_failures(conn),
            # 期貨多空餵市場情緒（gui_data 有 5 處讀它），與大盤指數同一步驟抓，
            # 但先前只有大盤指數被盯著，期貨那半靜靜停掉不會有人知道。
            check_source(conn, "futures_institution_oi", "期貨多空", "futures_institution_oi", "date",
                         expected=one_back),
            # ETF 成分股。etf_universe 有檢查而 holdings 沒有，但頁面上的
            # 持股明細讀的是 holdings——來源改版時空的是這半邊。
            check_source(conn, "etf_holdings", "ETF 成分股", "etf_holdings", "data_date",
                         expected=two_weeks_ago, min_rows=100, warn_only=True),
            # TWSE 找不到路徑時回「HTTP 200 + 404 HTML」，抓取失敗被 continue-on-error
            # 吞掉後，當沖榜只是安靜地少掉整個上市市場。這裡用列數當外部證據：
            # 實測單日約 1,200 檔，設 500 是為了容忍冷門日，不是容忍抓取失敗。
            check_source(conn, "day_trade_stats", "當沖統計", "day_trade_stats", "date",
                         expected=latest_trading, min_rows=500),
            # Yahoo RSS 被限速時回空 feed 而非錯誤，步驟仍會顯示成功；
            # 用「當日寫入列數」當外部證據，靜默失效才看得見。
            check_source(conn, "stock_news", "個股新聞", "stock_news", "substr(fetched_at, 1, 10)",
                         expected=one_back, min_rows=200, warn_only=True),
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

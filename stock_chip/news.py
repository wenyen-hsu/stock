from __future__ import annotations

import datetime as dt
import html
import re
import sqlite3
import argparse
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup


YAHOO_RSS_URL = "https://tw.stock.yahoo.com/rss"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}
ARTICLE_EXCERPT_LIMIT = 1200


@dataclass(frozen=True)
class NewsItem:
    stock_id: str
    title: str
    url: str
    source: str
    published_at: str
    summary: str
    content_excerpt: str
    fetched_at: str


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_published_at(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo:
        parsed = parsed.astimezone(dt.timezone(dt.timedelta(hours=8)))
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def ensure_news_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_news (
            stock_id TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            source TEXT,
            published_at TEXT,
            summary TEXT,
            content_excerpt TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (stock_id, url)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_news_stock ON stock_news(stock_id, published_at DESC)")
    conn.commit()


def extract_article_excerpt(url: str, timeout: float = 12) -> str:
    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header"]):
        tag.decompose()

    container = soup.find("article") or soup.select_one("[data-test-locator='articleBody']") or soup.body
    if not container:
        return ""

    ignored = {
        "加入為 Google 偏好來源",
        "將 Yahoo 設為首選來源，在 Google 上查看更多我們的精彩報導",
    }
    parts: list[str] = []
    for node in container.find_all(["p", "h2", "li"]):
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) < 18 or text in ignored:
            continue
        if text not in parts:
            parts.append(text)
        joined = " ".join(parts)
        if len(joined) >= ARTICLE_EXCERPT_LIMIT:
            return joined[:ARTICLE_EXCERPT_LIMIT].rstrip() + "..."
    return " ".join(parts)[:ARTICLE_EXCERPT_LIMIT]


def fetch_yahoo_news(stock_id: str, limit: int = 8, fetch_content: bool = True) -> list[NewsItem]:
    response = requests.get(
        YAHOO_RSS_URL,
        params={"s": stock_id},
        headers=REQUEST_HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    fetched_at = now_text()
    rows: list[NewsItem] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title"))
        url = clean_text(item.findtext("link"))
        if not title or not url or url in seen:
            continue
        seen.add(url)
        summary = clean_text(item.findtext("description"))
        excerpt = extract_article_excerpt(url) if fetch_content else ""
        rows.append(
            NewsItem(
                stock_id=stock_id,
                title=title,
                url=url,
                source="Yahoo股市",
                published_at=normalize_published_at(item.findtext("pubDate")),
                summary=summary,
                content_excerpt=excerpt,
                fetched_at=fetched_at,
            )
        )
        if len(rows) >= limit:
            break
    return rows


def upsert_news(conn: sqlite3.Connection, rows: list[NewsItem]) -> None:
    ensure_news_tables(conn)
    conn.executemany(
        """
        INSERT INTO stock_news (
            stock_id, title, url, source, published_at, summary, content_excerpt, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_id, url) DO UPDATE SET
            title = excluded.title,
            source = excluded.source,
            published_at = excluded.published_at,
            summary = excluded.summary,
            content_excerpt = excluded.content_excerpt,
            fetched_at = excluded.fetched_at
        """,
        [
            (
                row.stock_id,
                row.title,
                row.url,
                row.source,
                row.published_at,
                row.summary,
                row.content_excerpt,
                row.fetched_at,
            )
            for row in rows
        ],
    )
    conn.commit()


def load_cached_news(conn: sqlite3.Connection, stock_id: str, limit: int = 20) -> list[dict[str, str]]:
    ensure_news_tables(conn)
    raw = conn.execute(
        """
        SELECT stock_id, title, url, source, published_at, summary, content_excerpt, fetched_at
        FROM stock_news
        WHERE stock_id = ?
        ORDER BY published_at DESC, fetched_at DESC
        LIMIT ?
        """,
        (stock_id, limit),
    ).fetchall()
    return [
        {
            "stock_id": row[0],
            "title": row[1],
            "url": row[2],
            "source": row[3] or "",
            "published_at": row[4] or "",
            "summary": row[5] or "",
            "content_excerpt": row[6] or "",
            "fetched_at": row[7] or "",
        }
        for row in raw
    ]


def refresh_stock_news(db_path: Path, stock_id: str, limit: int = 8) -> dict[str, object]:
    stock_id = stock_id.strip()
    if not stock_id:
        raise ValueError("缺少股票代號")
    rows = fetch_yahoo_news(stock_id, limit=limit, fetch_content=True)
    with sqlite3.connect(db_path) as conn:
        upsert_news(conn, rows)
        cached = load_cached_news(conn, stock_id)
    return {
        "stock_id": stock_id,
        "source": "Yahoo股市 RSS",
        "fetched_count": len(rows),
        "fetched_at": rows[0].fetched_at if rows else now_text(),
        "rows": cached,
    }


def refresh_many_stock_news(
    db_path: Path,
    stock_ids: list[str],
    limit: int = 5,
    fetch_content: bool = False,
    sleep_seconds: float = 0.4,
    empty_streak_abort: int = 0,
) -> dict[str, object]:
    """逐檔抓 RSS 並寫入快取。

    empty_streak_abort > 0 時，連續這麼多檔都回 0 則且先前曾抓到東西，
    視為被 Yahoo 節流（RSS 被限速時回的是空 feed 而非錯誤，會靜默地
    只讓名單後段沒資料），中止剩餘請求並在回傳標記 aborted。
    """
    output: list[dict[str, object]] = []
    total = 0
    empty_streak = 0
    got_any = False
    aborted = ""
    targets = [item.strip() for item in stock_ids if item.strip()]
    # 整批共用一條連線：先前每檔都 connect 一次，數百檔規模下是白付的連線與
    # lock 成本。仍逐檔 commit（在 upsert_news 內），所以被限速中止時已抓到的
    # 部分不會遺失。
    conn = sqlite3.connect(db_path)
    for index, stock_id in enumerate(targets):
        try:
            rows = fetch_yahoo_news(stock_id, limit=limit, fetch_content=fetch_content)
            upsert_news(conn, rows)
            total += len(rows)
            output.append({"stock_id": stock_id, "status": "success", "fetched_count": len(rows), "error": ""})
            if rows:
                got_any = True
                empty_streak = 0
            else:
                empty_streak += 1
        except Exception as exc:
            output.append({"stock_id": stock_id, "status": "failed", "fetched_count": 0, "error": str(exc)})
            empty_streak += 1
        if empty_streak_abort > 0 and got_any and empty_streak >= empty_streak_abort:
            aborted = f"連續 {empty_streak} 檔無資料，疑似被限速，中止剩餘 {len(targets) - index - 1} 檔"
            break
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    conn.close()
    return {
        "stock_count": len(output),
        "requested_count": len(targets),
        "fetched_count": total,
        "empty_count": sum(1 for row in output if row["status"] == "success" and not row["fetched_count"]),
        "failed_count": sum(1 for row in output if row["status"] != "success"),
        "fetch_content": fetch_content,
        "aborted": aborted,
        "fetched_at": now_text(),
        "rows": output,
    }


def db_watchlist_ids(db_path: Path) -> list[str]:
    """使用者自選股（GUI 寫入的 user_watchlist；表不存在時回空清單）。"""
    try:
        with sqlite3.connect(db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_watchlist'"
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute("SELECT stock_id FROM user_watchlist ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]).strip() for row in rows if str(row[0] or "").strip()]


def resolve_targets(args: argparse.Namespace) -> list[str]:
    """自選股 → 各排行前 N 聯集 → 總分前 N，去重後依序截斷到 --max-requests。

    順序即優先序：被 --max-requests 砍掉的一定是最後面的低優先標的，
    所以縮小預算只會削尾巴，不會讓熱門股掉出名單。
    """
    merged: list[str] = []
    seen: set[str] = set()

    def extend(ids: list[str]) -> None:
        for stock_id in ids:
            stock_id = str(stock_id).strip()
            if stock_id and stock_id not in seen:
                seen.add(stock_id)
                merged.append(stock_id)

    extend([item for item in (args.watchlist or "").split(",")])
    if args.include_db_watchlist:
        extend(db_watchlist_ids(Path(args.db)))
    if args.from_rankings > 0 or args.from_scan > 0:
        from stock_chip.revenue import top_scan_stock_ids, union_ranking_stock_ids

        if args.from_rankings > 0:
            extend(union_ranking_stock_ids(Path(args.reports_dir), args.days, args.from_rankings))
        if args.from_scan > 0:
            extend(top_scan_stock_ids(Path(args.scan_report), args.from_scan))
    if args.max_requests > 0:
        merged = merged[: args.max_requests]
    return merged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Yahoo stock news into the local SQLite cache.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--watchlist", default="", help="Comma-separated stock ids.")
    parser.add_argument("--include-db-watchlist", action="store_true", help="也納入 GUI 自選股清單。")
    parser.add_argument("--days", type=int, default=20, help="讀取哪個視窗的排行報表（決定 ranking_*_{days}d.csv）。")
    parser.add_argument("--from-rankings", type=int, default=0, help="納入每個排行榜前 N 名的交錯聯集。")
    parser.add_argument("--from-scan", type=int, default=0, help="納入籌碼總分前 N 名。")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--scan-report", default="reports/scan_all_20d.csv")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--sleep", type=float, default=0.4)
    parser.add_argument("--max-requests", type=int, default=0, help="本次最多抓幾檔（0 = 不限）；用來把步驟時間箝制住。")
    parser.add_argument(
        "--empty-streak",
        type=int,
        default=40,
        help="連續這麼多檔回空就中止（疑似被限速）；0 = 不中止。",
    )
    parser.add_argument(
        "--fail-tolerance",
        type=float,
        default=0.0,
        help="允許的失敗比例；超過才以 1 退出。預設 0（任何失敗都算失敗，維持既有單機行為）。",
    )
    parser.add_argument("--content", action="store_true", help="Also fetch article excerpts. Default only fetches RSS titles.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    targets = resolve_targets(args)
    if not targets:
        raise SystemExit("沒有可抓取的股票：請給 --watchlist 或 --from-rankings/--from-scan")
    print(
        f"[news] 抓取名單 {len(targets)} 檔"
        f"（排行聯集前 {args.from_rankings}／總分前 {args.from_scan}／自選股"
        f"{'／上限 ' + str(args.max_requests) if args.max_requests > 0 else ''}）"
    )
    result = refresh_many_stock_news(
        Path(args.db),
        targets,
        limit=args.limit,
        fetch_content=args.content,
        sleep_seconds=args.sleep,
        empty_streak_abort=args.empty_streak,
    )
    print(
        f"Fetched {result['fetched_count']} news items for {result['stock_count']} stocks "
        f"（無新聞 {result['empty_count']} 檔、失敗 {result['failed_count']} 檔）"
        f" at {result['fetched_at']}"
    )
    failed = [row for row in result["rows"] if row["status"] != "success"]
    for row in failed[:20]:
        print(f"{row['stock_id']} failed: {row['error']}")
    if len(failed) > 20:
        print(f"...另有 {len(failed) - 20} 檔失敗未列出")
    if result["aborted"]:
        print(f"[news] {result['aborted']}")
        raise SystemExit(1)
    # 大量抓取時零星逾時是常態，整步驟不應為此失敗；比例超標才視為來源異常。
    if failed and (not args.fail_tolerance or len(failed) / max(1, result["stock_count"]) > args.fail_tolerance):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

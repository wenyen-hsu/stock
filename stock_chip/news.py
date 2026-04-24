from __future__ import annotations

import datetime as dt
import html
import re
import sqlite3
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

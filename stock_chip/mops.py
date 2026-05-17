from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import sqlite3
import time
import warnings
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "stock_chip.sqlite"
DEFAULT_KEEP_DAYS = 183
MOPS_HOST = "mopsov.twse.com.tw"
MOPS_URL = "https://mopsov.twse.com.tw"
MOPS_FALLBACK_URL = "https://163.29.17.81"
MOPS_LIST_PATH = "/mops/web/t05st02"
MOPS_AJAX_PATH = "/mops/web/ajax_t05st02"


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def ensure_mops_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mops_events (
            event_id TEXT PRIMARY KEY,
            event_date TEXT NOT NULL,
            event_time TEXT,
            stock_id TEXT,
            company_name TEXT,
            title TEXT NOT NULL,
            detail TEXT,
            category TEXT,
            source TEXT,
            source_url TEXT,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mops_events_date ON mops_events(event_date DESC, event_time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mops_events_stock ON mops_events(stock_id, event_date DESC)")
    conn.commit()


def roc_date_to_iso(value: str) -> str:
    text = clean_text(value)
    match = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", text)
    if not match:
        return text
    year, month, day = (int(part) for part in match.groups())
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


def iso_to_roc_parts(day: dt.date) -> dict[str, str]:
    return {"year": str(day.year - 1911), "month": f"{day.month:02d}", "day": f"{day.day:02d}"}


def event_id_for(row: dict[str, Any]) -> str:
    key = "|".join(
        clean_text(row.get(part))
        for part in ("event_date", "event_time", "stock_id", "company_name", "title", "category")
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:18]


def _session_headers(base_url: str) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        "Referer": f"{base_url}{MOPS_LIST_PATH}",
    }
    if "163.29.17.81" in base_url:
        headers["Host"] = MOPS_HOST
    return headers


def _hidden_detail(row: Any) -> str:
    values: list[str] = []
    for item in row.select("input[type=hidden], input[name^=h]"):
        name = clean_text(item.get("name"))
        value = clean_text(item.get("value"))
        if not name.startswith("h") or not value:
            continue
        values.append(value)
    if not values:
        return ""
    # MOPS embeds the detail text as one hidden field. It is usually the longest value.
    return max(values, key=len)


def _parse_tables(html: str, fetched_at: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        header_cells = [clean_text(cell.get_text(" ")) for cell in table.select("tr th")]
        if "公司代號" not in header_cells or "公司名稱" not in header_cells or "主旨" not in header_cells:
            continue
        category = "重大訊息" if "發言日期" in header_cells else "公告"
        for tr in table.select("tr"):
            cells = tr.find_all("td")
            if len(cells) < 5:
                continue
            date_text = clean_text(cells[0].get_text(" "))
            stock_id = clean_text(cells[2].get_text(" "))
            title = clean_text(cells[4].get_text(" "))
            if not re.match(r"^\d{2,3}/\d{1,2}/\d{1,2}$", date_text) or not title:
                continue
            row = {
                "event_date": roc_date_to_iso(date_text),
                "event_time": clean_text(cells[1].get_text(" ")),
                "stock_id": stock_id,
                "company_name": clean_text(cells[3].get_text(" ")),
                "title": title,
                "detail": _hidden_detail(tr),
                "category": category,
                "source": "MOPS 公開資訊觀測站",
                "source_url": f"https://{MOPS_HOST}{MOPS_LIST_PATH}",
                "fetched_at": fetched_at,
            }
            row["event_id"] = event_id_for(row)
            rows.append(row)
    return rows


def fetch_mops_events_for_date(day: dt.date, timeout: int = 25) -> list[dict[str, Any]]:
    parts = iso_to_roc_parts(day)
    payload = {
        "step": "1",
        "step00": "0",
        "firstin": "ture",
        "off": "1",
        "TYPEK": "all",
        **parts,
    }
    last_error: Exception | None = None
    warnings.simplefilter("ignore", InsecureRequestWarning)
    for base_url in (MOPS_URL, MOPS_FALLBACK_URL):
        try:
            session = requests.Session()
            headers = _session_headers(base_url)
            session.get(f"{base_url}{MOPS_LIST_PATH}", headers=headers, timeout=timeout, verify=False)
            response = session.post(
                f"{base_url}{MOPS_AJAX_PATH}",
                data=payload,
                headers=headers,
                timeout=timeout,
                verify=False,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            return _parse_tables(response.text, now_text())
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise RuntimeError(f"MOPS fetch failed for {day}: {last_error}") from last_error
    return []


def upsert_mops_events(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    ensure_mops_tables(conn)
    before = conn.total_changes
    for row in rows:
        conn.execute(
            """
            INSERT INTO mops_events (
                event_id, event_date, event_time, stock_id, company_name, title,
                detail, category, source, source_url, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                detail=excluded.detail,
                category=excluded.category,
                source=excluded.source,
                source_url=excluded.source_url,
                fetched_at=excluded.fetched_at
            """,
            (
                row.get("event_id"),
                row.get("event_date"),
                row.get("event_time"),
                row.get("stock_id"),
                row.get("company_name"),
                row.get("title"),
                row.get("detail"),
                row.get("category"),
                row.get("source"),
                row.get("source_url"),
                row.get("fetched_at"),
            ),
        )
    conn.commit()
    return conn.total_changes - before


def prune_mops_events(conn: sqlite3.Connection, keep_days: int = DEFAULT_KEEP_DAYS, today: dt.date | None = None) -> int:
    ensure_mops_tables(conn)
    cutoff = (today or dt.date.today()) - dt.timedelta(days=max(1, int(keep_days)))
    before = conn.total_changes
    conn.execute("DELETE FROM mops_events WHERE event_date < ?", (cutoff.isoformat(),))
    conn.commit()
    return conn.total_changes - before


def refresh_mops_events(
    db_path: Path = DB_PATH,
    days: int = 14,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    sleep_seconds: float = 0.25,
    keep_days: int = DEFAULT_KEEP_DAYS,
) -> dict[str, Any]:
    end = end_date or dt.date.today()
    if start_date:
        if start_date > end:
            start_date, end = end, start_date
        date_list: list[dt.date] = []
        day = start_date
        while day <= end and len(date_list) < 60:
            date_list.append(day)
            day += dt.timedelta(days=1)
    else:
        days = max(1, min(int(days), 60))
        date_list = [end - dt.timedelta(days=offset) for offset in range(days)]
    all_rows: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for day in date_list:
        try:
            rows = fetch_mops_events_for_date(day)
            all_rows.extend(rows)
        except Exception as exc:
            failed.append({"date": day.isoformat(), "error": str(exc)})
        if sleep_seconds:
            time.sleep(sleep_seconds)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        pruned = prune_mops_events(conn, keep_days=keep_days)
        changed = upsert_mops_events(conn, all_rows)
    return {
        "rows": all_rows,
        "row_count": len(all_rows),
        "changed": changed,
        "pruned": pruned,
        "keep_days": keep_days,
        "failed": failed,
        "days": len(date_list),
        "start_date": date_list[0].isoformat() if date_list else "",
        "end_date": end.isoformat(),
        "updated_at": now_text(),
    }


def load_mops_events(
    conn: sqlite3.Connection,
    start_date: str = "",
    end_date: str = "",
    stock_id: str = "",
    query: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    ensure_mops_tables(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if start_date:
        clauses.append("event_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("event_date <= ?")
        params.append(end_date)
    if stock_id:
        clauses.append("stock_id = ?")
        params.append(stock_id)
    if query:
        like = f"%{query}%"
        clauses.append("(title LIKE ? OR detail LIKE ? OR company_name LIKE ? OR stock_id LIKE ?)")
        params.extend([like, like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(int(limit), 10000))
    cursor = conn.execute(
        f"""
        SELECT event_id, event_date, event_time, stock_id, company_name, title,
               detail, category, source, source_url, fetched_at
        FROM mops_events
        {where}
        ORDER BY event_date DESC, event_time DESC, stock_id
        LIMIT ?
        """,
        [*params, limit],
    )
    columns = [item[0] for item in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return {"rows": rows, "row_count": len(rows)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch MOPS major events into the local SQLite database.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--start-date", default="", help="YYYY-MM-DD; when set, fetches start through end.")
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD; defaults to today.")
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start_date = dt.date.fromisoformat(args.start_date) if args.start_date else None
    end_date = dt.date.fromisoformat(args.end_date) if args.end_date else None
    result = refresh_mops_events(
        Path(args.db),
        days=args.days,
        start_date=start_date,
        end_date=end_date,
        sleep_seconds=args.sleep,
        keep_days=args.keep_days,
    )
    print(
        f"MOPS events: {result['row_count']} rows, changed {result['changed']}, "
        f"pruned {result['pruned']}, failed {len(result['failed'])}, updated {result['updated_at']}"
    )


if __name__ == "__main__":
    main()

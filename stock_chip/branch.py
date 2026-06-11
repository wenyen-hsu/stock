from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from stock_chip.official import connect_db
from stock_chip.scan import DEFAULT_WATCHLIST, parse_watchlist, recent_dates


HISTOCK_BRANCH_URL = "https://histock.tw/stock/branch.aspx"
HISTOCK_BROKER_TRACE_URL = "https://histock.tw/stock/brokertrace.aspx"
DEFAULT_RETRY_SLEEPS = (5.0, 15.0, 30.0)
REQUEST_TIMEOUT_SECONDS = 12
BROKER_ID_OVERRIDES = {
    "摩根大通": "8440",
    "美林": "1440",
    "台灣摩根士丹利": "1470",
    "美商高盛": "1480",
    "新加坡商瑞銀": "1650",
    "港商野村": "1560",
    "香港上海匯豐": "8960",
    "港商麥格理": "1360",
    "法銀巴黎": "8900",
    "花旗環球": "1590",
    "凱基": "9200",
    "凱基-台北": "9268",
    "凱基-士林": "9238",
    "元大": "9800",
    "富邦": "9600",
    "富邦-三重": "9677",
    "國泰": "8880",
    "大和國泰": "8890",
    "統一": "5850",
    "國票": "7790",
    "國票-天祥": "779V",
    "國票-敦北法人": "7790",
    "兆豐": "7000",
    "群益金鼎": "9100",
    "永豐金": "9A00",
    "永豐金-匯立": "9A81",
}


@dataclass
class BranchRow:
    as_of_date: str
    from_date: str
    to_date: str
    window_days: int
    stock_id: str
    name: str
    rank_side: str
    rank_no: int
    broker_name: str
    broker_id: str | None
    buy_lot: float | None
    sell_lot: float | None
    net_lot: float | None
    avg_price: float | None
    source: str
    source_url: str


@dataclass
class FetchStatus:
    trade_date: str
    stock_id: str
    name: str
    window_days: int
    source: str
    status: str
    row_count: int
    attempts: int
    error: str | None
    source_url: str | None


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compact_date(value: str) -> str:
    return value.replace("-", "")


def normalize_date(value: str) -> str:
    text = value.strip().replace("/", "-")
    return text


def broker_id_from_cell(cell: Any) -> str | None:
    link = cell.find("a", href=True)
    if not link:
        return None
    match = re.search(r"bno=([^&]+)", link["href"])
    return match.group(1) if match else None


def fetch_histock_branch(
    stock_id: str,
    stock_name: str,
    from_date: str,
    to_date: str,
    window_days: int,
    top_n: int,
) -> list[BranchRow]:
    params = {
        "no": stock_id,
        "from": compact_date(from_date),
        "to": compact_date(to_date),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 stock-chip-branch/0.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(HISTOCK_BRANCH_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    source_url = resp.url
    soup = BeautifulSoup(resp.text, "html.parser")

    parsed: list[BranchRow] = []
    sell_rank = 0
    buy_rank = 0
    for tr in soup.find_all("tr"):
        td_cells = tr.find_all("td")
        cells = [cell.get_text(strip=True) for cell in td_cells]
        if len(td_cells) != 10:
            continue
        sell_broker, sell_buy, sell_sell, sell_net, sell_avg = cells[:5]
        buy_broker, buy_buy, buy_sell, buy_net, buy_avg = cells[5:]
        sell_broker_id = broker_id_from_cell(td_cells[0])
        buy_broker_id = broker_id_from_cell(td_cells[5])
        if sell_broker and sell_rank < top_n:
            sell_rank += 1
            parsed.append(
                BranchRow(
                    as_of_date=to_date,
                    from_date=from_date,
                    to_date=to_date,
                    window_days=window_days,
                    stock_id=stock_id,
                    name=stock_name,
                    rank_side="sell",
                    rank_no=sell_rank,
                    broker_name=sell_broker,
                    broker_id=sell_broker_id,
                    buy_lot=parse_number(sell_buy),
                    sell_lot=parse_number(sell_sell),
                    net_lot=parse_number(sell_net),
                    avg_price=parse_number(sell_avg),
                    source="histock",
                    source_url=source_url,
                )
            )
        if buy_broker and buy_rank < top_n:
            buy_rank += 1
            parsed.append(
                BranchRow(
                    as_of_date=to_date,
                    from_date=from_date,
                    to_date=to_date,
                    window_days=window_days,
                    stock_id=stock_id,
                    name=stock_name,
                    rank_side="buy",
                    rank_no=buy_rank,
                    broker_name=buy_broker,
                    broker_id=buy_broker_id,
                    buy_lot=parse_number(buy_buy),
                    sell_lot=parse_number(buy_sell),
                    net_lot=parse_number(buy_net),
                    avg_price=parse_number(buy_avg),
                    source="histock",
                    source_url=source_url,
                )
            )
        if sell_rank >= top_n and buy_rank >= top_n:
            break
    return parsed


def weighted_avg_price(
    buy_lot: float | None,
    buy_avg: float | None,
    sell_lot: float | None,
    sell_avg: float | None,
) -> float | None:
    buy_value = (buy_lot or 0) * (buy_avg or 0)
    sell_value = (sell_lot or 0) * (sell_avg or 0)
    lots = (buy_lot or 0) + (sell_lot or 0)
    if lots <= 0:
        return buy_avg or sell_avg
    return round((buy_value + sell_value) / lots, 4)


def fetch_histock_broker_trace(
    stock_id: str,
    stock_name: str,
    broker_name: str,
    broker_id: str,
    wanted_dates: set[str],
) -> list[BranchRow]:
    params = {"bno": broker_id, "no": stock_id}
    headers = {
        "User-Agent": "Mozilla/5.0 stock-chip-branch/0.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(HISTOCK_BROKER_TRACE_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    rows: list[BranchRow] = []
    for tr in soup.find_all("tr"):
        cells = [cell.get_text(strip=True) for cell in tr.find_all("td")]
        if len(cells) < 7:
            continue
        trade_date = normalize_date(cells[0])
        if trade_date not in wanted_dates:
            continue
        buy_lot = parse_number(cells[1])
        buy_avg = parse_number(cells[2])
        sell_lot = parse_number(cells[3])
        sell_avg = parse_number(cells[4])
        net_lot = parse_number(cells[6])
        rows.append(
            BranchRow(
                as_of_date=trade_date,
                from_date=trade_date,
                to_date=trade_date,
                window_days=1,
                stock_id=stock_id,
                name=stock_name,
                rank_side="buy" if (net_lot or 0) >= 0 else "sell",
                rank_no=0,
                broker_name=broker_name,
                broker_id=broker_id,
                buy_lot=buy_lot,
                sell_lot=sell_lot,
                net_lot=net_lot,
                avg_price=weighted_avg_price(buy_lot, buy_avg, sell_lot, sell_avg),
                source="histock",
                source_url=resp.url,
            )
        )
    return rows


def fetch_histock_branch_with_retry(
    stock_id: str,
    stock_name: str,
    from_date: str,
    to_date: str,
    window_days: int,
    top_n: int,
    retry_sleeps: tuple[float, ...] = DEFAULT_RETRY_SLEEPS,
) -> tuple[list[BranchRow], FetchStatus]:
    attempts = 0
    last_error: str | None = None
    source_url: str | None = None
    total_attempts = 1 + len(retry_sleeps)

    for attempt_index in range(total_attempts):
        if attempt_index:
            time.sleep(retry_sleeps[attempt_index - 1])
        attempts += 1
        try:
            rows = fetch_histock_branch(
                stock_id=stock_id,
                stock_name=stock_name,
                from_date=from_date,
                to_date=to_date,
                window_days=window_days,
                top_n=top_n,
            )
            if rows:
                source_url = rows[0].source_url
                return rows, FetchStatus(
                    trade_date=to_date,
                    stock_id=stock_id,
                    name=stock_name,
                    window_days=window_days,
                    source="histock",
                    status="success",
                    row_count=len(rows),
                    attempts=attempts,
                    error=None,
                    source_url=source_url,
                )
            last_error = "empty parseable branch table"
        except Exception as exc:
            last_error = str(exc)

    return [], FetchStatus(
        trade_date=to_date,
        stock_id=stock_id,
        name=stock_name,
        window_days=window_days,
        source="histock",
        status="empty" if last_error == "empty parseable branch table" else "failed",
        row_count=0,
        attempts=attempts,
        error=last_error,
        source_url=source_url,
    )


def stock_names(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, str]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"SELECT stock_id, name FROM stocks WHERE stock_id IN ({placeholders})",
        stock_ids,
    ).fetchall()
    names = {row[0]: row[1] for row in rows}
    for stock_id in stock_ids:
        names.setdefault(stock_id, stock_id)
    return names


def select_stock_ids(
    conn: sqlite3.Connection,
    markets: list[str],
    limit: int | None,
    offset: int,
) -> list[str]:
    placeholders = ",".join("?" for _ in markets)
    params: list[Any] = [*markets]
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = conn.execute(
        f"""
        SELECT stock_id
        FROM stocks
        WHERE market IN ({placeholders})
        ORDER BY market, stock_id
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [row[0] for row in rows]


def existing_branch_stock_ids(conn: sqlite3.Connection, as_of_date: str, days: int) -> set[str]:
    branch_rows = conn.execute(
        """
        SELECT DISTINCT stock_id
        FROM broker_branch_topn
        WHERE as_of_date = ?
          AND window_days = ?
          AND source = 'histock'
        """,
        (as_of_date, days),
    ).fetchall()
    status_rows = conn.execute(
        """
        SELECT DISTINCT stock_id
        FROM branch_fetch_status
        WHERE trade_date = ?
          AND window_days = ?
          AND source = 'histock'
          AND status IN ('success', 'empty')
        """,
        (as_of_date, days),
    ).fetchall()
    return {row[0] for row in branch_rows} | {row[0] for row in status_rows}


def branch_coverage(conn: sqlite3.Connection, as_of_date: str, days: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            s.market,
            COUNT(DISTINCT s.stock_id) AS total_stocks,
            COUNT(DISTINCT b.stock_id) AS stocks_with_branch,
            COUNT(DISTINCT CASE
                WHEN b.stock_id IS NOT NULL OR fs.stock_id IS NOT NULL THEN s.stock_id
            END) AS attempted,
            COUNT(DISTINCT CASE WHEN fs.status = 'empty' THEN fs.stock_id END) AS empty_count,
            COUNT(DISTINCT CASE WHEN fs.status = 'failed' THEN fs.stock_id END) AS failed_count
        FROM stocks s
        LEFT JOIN broker_branch_topn b
            ON b.stock_id = s.stock_id
           AND b.as_of_date = ?
           AND b.window_days = ?
           AND b.source = 'histock'
        LEFT JOIN branch_fetch_status fs
            ON fs.stock_id = s.stock_id
           AND fs.trade_date = ?
           AND fs.window_days = ?
           AND fs.source = 'histock'
        WHERE s.market IN ('TWSE', 'TPEX')
        GROUP BY s.market
        ORDER BY s.market
        """,
        (as_of_date, days, as_of_date, days),
    ).fetchall()
    return [
        {
            "market": row[0],
            "total_stocks": row[1],
            "stocks_with_branch": row[2],
            "no_branch": row[1] - row[2],
            "attempted": row[3],
            "empty_count": row[4],
            "failed_count": row[5],
            "not_attempted": row[1] - row[3],
            "remaining": row[1] - row[2],
        }
        for row in rows
    ]


def upsert_branch_rows(conn: sqlite3.Connection, rows: list[BranchRow]) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO broker_branch_topn (
            as_of_date, from_date, to_date, window_days, stock_id, name,
            rank_side, rank_no, broker_id, broker_name, buy_lot, sell_lot, net_lot,
            avg_price, source, source_url, updated_at
        )
        VALUES (
            :as_of_date, :from_date, :to_date, :window_days, :stock_id, :name,
            :rank_side, :rank_no, :broker_id, :broker_name, :buy_lot, :sell_lot, :net_lot,
            :avg_price, :source, :source_url, :updated_at
        )
        ON CONFLICT(as_of_date, window_days, stock_id, rank_side, rank_no, source)
        DO UPDATE SET
            from_date = excluded.from_date,
            to_date = excluded.to_date,
            name = excluded.name,
            broker_id = excluded.broker_id,
            broker_name = excluded.broker_name,
            buy_lot = excluded.buy_lot,
            sell_lot = excluded.sell_lot,
            net_lot = excluded.net_lot,
            avg_price = excluded.avg_price,
            source_url = excluded.source_url,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    conn.commit()


def upsert_branch_daily_rows(conn: sqlite3.Connection, rows: list[BranchRow]) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO broker_branch_daily (
            trade_date, stock_id, name, broker_name, broker_id, rank_side, rank_no,
            buy_lot, sell_lot, net_lot, avg_price, source, source_url, updated_at
        )
        VALUES (
            :trade_date, :stock_id, :name, :broker_name, :broker_id, :rank_side, :rank_no,
            :buy_lot, :sell_lot, :net_lot, :avg_price, :source, :source_url, :updated_at
        )
        ON CONFLICT(trade_date, stock_id, broker_name, source) DO UPDATE SET
            name = excluded.name,
            broker_id = excluded.broker_id,
            rank_side = excluded.rank_side,
            rank_no = excluded.rank_no,
            buy_lot = excluded.buy_lot,
            sell_lot = excluded.sell_lot,
            net_lot = excluded.net_lot,
            avg_price = excluded.avg_price,
            source_url = excluded.source_url,
            updated_at = excluded.updated_at
        """,
        [
            {
                "trade_date": row.as_of_date,
                "stock_id": row.stock_id,
                "name": row.name,
                "broker_name": row.broker_name,
                "broker_id": row.broker_id,
                "rank_side": row.rank_side,
                "rank_no": row.rank_no,
                "buy_lot": row.buy_lot,
                "sell_lot": row.sell_lot,
                "net_lot": row.net_lot,
                "avg_price": row.avg_price,
                "source": row.source,
                "source_url": row.source_url,
                "updated_at": now,
            }
            for row in rows
        ],
    )
    conn.commit()


def upsert_fetch_statuses(conn: sqlite3.Connection, statuses: list[FetchStatus]) -> None:
    if not statuses:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO branch_fetch_status (
            trade_date, stock_id, name, window_days, source, status,
            row_count, attempts, error, source_url, updated_at
        )
        VALUES (
            :trade_date, :stock_id, :name, :window_days, :source, :status,
            :row_count, :attempts, :error, :source_url, :updated_at
        )
        ON CONFLICT(trade_date, stock_id, window_days, source) DO UPDATE SET
            name = excluded.name,
            status = excluded.status,
            row_count = excluded.row_count,
            attempts = excluded.attempts,
            error = excluded.error,
            source_url = excluded.source_url,
            updated_at = excluded.updated_at
        """,
        [{**status.__dict__, "updated_at": now} for status in statuses],
    )
    conn.commit()


def successful_branch_daily_dates(conn: sqlite3.Connection, stock_id: str, dates: list[str]) -> set[str]:
    if not dates:
        return set()
    placeholders = ",".join("?" for _ in dates)
    status_rows = conn.execute(
        f"""
        SELECT trade_date
        FROM branch_fetch_status
        WHERE stock_id = ?
          AND window_days = 1
          AND source = 'histock'
          AND status = 'success'
          AND trade_date IN ({placeholders})
        """,
        [stock_id, *dates],
    ).fetchall()
    daily_rows = conn.execute(
        f"""
        SELECT trade_date
        FROM broker_branch_daily
        WHERE stock_id = ?
          AND source = 'histock'
          AND trade_date IN ({placeholders})
        GROUP BY trade_date
        HAVING COUNT(*) > 0
        """,
        [stock_id, *dates],
    ).fetchall()
    return {row[0] for row in status_rows} | {row[0] for row in daily_rows}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_rows(output_dir: Path, rows: list[BranchRow], days: int, suffix: str = "") -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = [row.__dict__ for row in rows]
    csv_path = output_dir / f"branch_topn_{days}d{suffix}.csv"
    write_csv(csv_path, records)

    md_path = output_dir / f"branch_report_{days}d{suffix}.md"
    lines = [
        "# 分點 Top N 報告",
        "",
        f"- 產生時間：{dt.datetime.now().isoformat(timespec='seconds')}",
        f"- 來源：HiStock 公開分點頁",
        "",
    ]
    grouped: dict[str, list[BranchRow]] = {}
    for row in rows:
        grouped.setdefault(row.stock_id, []).append(row)
    for stock_id, stock_rows in grouped.items():
        stock_rows = sorted(stock_rows, key=lambda row: (row.rank_side, row.rank_no))
        name = stock_rows[0].name
        lines.extend(
            [
                f"## {stock_id} {name}",
                "",
                "| 方向 | 排名 | 分點 | 買進 | 賣出 | 買賣超 | 均價 |",
                "| --- | ---: | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for row in stock_rows:
            side = "買超" if row.rank_side == "buy" else "賣超"
            lines.append(
                f"| {side} | {row.rank_no} | {row.broker_name} | {row.buy_lot or ''} | "
                f"{row.sell_lot or ''} | {row.net_lot or ''} | {row.avg_price or ''} |"
            )
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def run_branch(
    db_path: Path,
    output_dir: Path,
    days: int,
    top_n: int,
    watchlist: list[str],
    sleep_seconds: float,
    all_stocks: bool = False,
    markets: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
    skip_existing: bool = False,
    retry_sleeps: tuple[float, ...] = DEFAULT_RETRY_SLEEPS,
) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        dates = recent_dates(conn, days)
        if len(dates) < days:
            raise RuntimeError(f"資料庫只有 {len(dates)} 個交易日，少於要求的 {days} 日。")
        if all_stocks:
            selected_markets = markets or ["TWSE", "TPEX"]
            stock_ids = select_stock_ids(conn, selected_markets, limit, offset)
        else:
            stock_ids = watchlist
        if skip_existing:
            existing = existing_branch_stock_ids(conn, dates[-1], days)
            stock_ids = [stock_id for stock_id in stock_ids if stock_id not in existing]
        names = stock_names(conn, stock_ids)

        all_rows: list[BranchRow] = []
        statuses: list[FetchStatus] = []
        failures: list[dict[str, str]] = []
        for idx, stock_id in enumerate(stock_ids):
            if idx:
                time.sleep(sleep_seconds)
            rows, status = fetch_histock_branch_with_retry(
                stock_id=stock_id,
                stock_name=names[stock_id],
                from_date=dates[0],
                to_date=dates[-1],
                window_days=days,
                top_n=top_n,
                retry_sleeps=retry_sleeps,
            )
            all_rows.extend(rows)
            statuses.append(status)
            upsert_branch_rows(conn, rows)
            upsert_fetch_statuses(conn, [status])
            if status.status != "success":
                failures.append({"stock_id": stock_id, "error": status.error or status.status})

    suffix = f"_all_offset{offset}_limit{limit}" if all_stocks and limit is not None else ""
    csv_path, md_path = export_rows(output_dir, all_rows, days, suffix=suffix)
    return {
        "dates": dates,
        "requested_stock_count": len(stock_ids),
        "row_count": len(all_rows),
        "failures": failures,
        "csv": str(csv_path),
        "report": str(md_path),
    }


def run_branch_daily(
    db_path: Path,
    days: int,
    top_n: int,
    stock_ids: list[str],
    sleep_seconds: float,
    retry_sleeps: tuple[float, ...] = DEFAULT_RETRY_SLEEPS,
    only_missing: bool = False,
) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        dates = recent_dates(conn, days)
        if len(dates) < days:
            raise RuntimeError(f"資料庫只有 {len(dates)} 個交易日，少於要求的 {days} 日。")
        names = stock_names(conn, stock_ids)
        all_rows: list[BranchRow] = []
        statuses: list[FetchStatus] = []
        failures: list[dict[str, str]] = []
        request_count = 0
        for stock_id in stock_ids:
            existing_success = successful_branch_daily_dates(conn, stock_id, dates) if only_missing else set()
            for trade_date in dates:
                if trade_date in existing_success:
                    statuses.append(
                        FetchStatus(
                            trade_date=trade_date,
                            stock_id=stock_id,
                            name=names[stock_id],
                            window_days=1,
                            source="histock",
                            status="skipped",
                            row_count=0,
                            attempts=0,
                            error=None,
                            source_url=None,
                        )
                    )
                    continue
                if request_count:
                    time.sleep(sleep_seconds)
                request_count += 1
                rows, status = fetch_histock_branch_with_retry(
                    stock_id=stock_id,
                    stock_name=names[stock_id],
                    from_date=trade_date,
                    to_date=trade_date,
                    window_days=1,
                    top_n=top_n,
                    retry_sleeps=retry_sleeps,
                )
                statuses.append(status)
                all_rows.extend(rows)
                if status.status != "success":
                    failures.append({"stock_id": stock_id, "date": trade_date, "error": status.error or status.status})
        upsert_branch_daily_rows(conn, all_rows)
        upsert_fetch_statuses(conn, [status for status in statuses if status.status != "skipped"])
    return {
        "dates": dates,
        "requested_stock_count": len(stock_ids),
        "request_count": request_count,
        "row_count": len(all_rows),
        "failures": failures,
        "status_counts": {
            key: sum(1 for status in statuses if status.status == key)
            for key in ("success", "empty", "failed", "skipped")
        },
    }


def top_buy_brokers_for_window(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    as_of_date: str,
    days: int,
    top_n: int,
) -> list[dict[str, str]]:
    if not stock_ids:
        return []
    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"""
        SELECT stock_id, name, broker_name, broker_id
        FROM broker_branch_topn
        WHERE stock_id IN ({placeholders})
          AND as_of_date = ?
          AND window_days = ?
          AND rank_side = 'buy'
          AND rank_no <= ?
        ORDER BY stock_id, rank_no
        """,
        [*stock_ids, as_of_date, days, top_n],
    ).fetchall()
    brokers = [
        {
            "stock_id": row[0],
            "name": row[1],
            "broker_name": row[2],
            "broker_id": row[3] or BROKER_ID_OVERRIDES.get(row[2], ""),
        }
        for row in rows
    ]
    return [broker for broker in brokers if broker["broker_id"]]


def run_branch_trace_history(
    db_path: Path,
    days: int,
    history_days: int,
    top_n: int,
    stock_ids: list[str],
    sleep_seconds: float,
) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        window_dates = recent_dates(conn, days)
        history_dates = recent_dates(conn, history_days)
        if len(window_dates) < days:
            raise RuntimeError(f"資料庫只有 {len(window_dates)} 個交易日，少於要求的 {days} 日。")
        if len(history_dates) < history_days:
            raise RuntimeError(f"資料庫只有 {len(history_dates)} 個交易日，少於要求的 {history_days} 日。")
        brokers = top_buy_brokers_for_window(conn, stock_ids, window_dates[-1], days, top_n)
        wanted_dates = set(history_dates)
        all_rows: list[BranchRow] = []
        failures: list[dict[str, str]] = []
        for idx, broker in enumerate(brokers):
            if idx:
                time.sleep(sleep_seconds)
            try:
                rows = fetch_histock_broker_trace(
                    stock_id=broker["stock_id"],
                    stock_name=broker["name"],
                    broker_name=broker["broker_name"],
                    broker_id=broker["broker_id"],
                    wanted_dates=wanted_dates,
                )
                all_rows.extend(rows)
                if len(rows) < history_days:
                    failures.append(
                        {
                            "stock_id": broker["stock_id"],
                            "broker": broker["broker_name"],
                            "error": f"trace rows {len(rows)}/{history_days}",
                        }
                    )
            except Exception as exc:
                failures.append(
                    {
                        "stock_id": broker["stock_id"],
                        "broker": broker["broker_name"],
                        "error": str(exc),
                    }
                )
        upsert_branch_daily_rows(conn, all_rows)
    return {
        "dates": history_dates,
        "requested_brokers": len(brokers),
        "row_count": len(all_rows),
        "failures": failures,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch top broker branch flows for watchlist stocks.")
    parser.add_argument("--days", type=int, default=20, help="Recent trading-day window from local DB.")
    parser.add_argument("--top", type=int, default=10, help="Top N buy and sell branches per stock.")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite database path.")
    parser.add_argument("--output-dir", default="reports", help="Report output directory.")
    parser.add_argument(
        "--watchlist",
        default=",".join(DEFAULT_WATCHLIST),
        help="Comma-separated stock ids.",
    )
    parser.add_argument(
        "--from-scan",
        type=int,
        default=0,
        metavar="N",
        help="Also include the top N stocks by total_score from reports/scan_all_20d.csv.",
    )
    parser.add_argument("--scan-report", default="reports/scan_all_20d.csv")
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay between third-party requests.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch branch data for all stocks in selected markets instead of the watchlist.",
    )
    parser.add_argument(
        "--markets",
        default="TWSE,TPEX",
        help="Comma-separated markets for --all. ESB is intentionally excluded by default.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Batch size for --all.")
    parser.add_argument("--offset", type=int, default=0, help="Batch offset for --all.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip stocks that already have branch rows for the latest date/window.",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Only print branch-data coverage for the latest date/window.",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Fetch per-trading-day branch rows into broker_branch_daily.",
    )
    parser.add_argument(
        "--trace-history",
        action="store_true",
        help="Fetch recent per-broker history for interval top buy branches from HiStock brokertrace.",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=10,
        help="Recent trading days for --trace-history.",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="For --daily, skip stock/date pairs already marked success in branch_fetch_status.",
    )
    parser.add_argument(
        "--retry-sleeps",
        default="5,15,30",
        help="Comma-separated retry delays in seconds for empty/failed HiStock branch responses.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.from_scan > 0:
        from stock_chip.revenue import top_scan_stock_ids

        merged = parse_watchlist(args.watchlist)
        for stock_id in top_scan_stock_ids(Path(args.scan_report), args.from_scan):
            if stock_id not in merged:
                merged.append(stock_id)
        args.watchlist = ",".join(merged)
    if args.coverage:
        with connect_db(Path(args.db)) as conn:
            dates = recent_dates(conn, args.days)
            coverage = branch_coverage(conn, dates[-1], args.days)
        for row in coverage:
            print(
                f"{row['market']}: {row['stocks_with_branch']}/{row['total_stocks']} "
                f"covered, remaining={row['remaining']}"
            )
        return
    if args.daily:
        retry_sleeps = tuple(float(value) for value in parse_watchlist(args.retry_sleeps))
        result = run_branch_daily(
            db_path=Path(args.db),
            days=args.days,
            top_n=args.top,
            stock_ids=parse_watchlist(args.watchlist),
            sleep_seconds=args.sleep,
            retry_sleeps=retry_sleeps,
            only_missing=args.only_missing,
        )
        print(f"requested stocks: {result['requested_stock_count']}")
        print(f"daily requests: {result['request_count']}")
        print(f"branch daily rows: {result['row_count']}")
        print(
            "status: "
            + ", ".join(f"{key}={value}" for key, value in result["status_counts"].items())
        )
        if result["failures"]:
            print(f"failures: {len(result['failures'])}")
        print(f"dates: {', '.join(result['dates'])}")
        return
    if args.trace_history:
        result = run_branch_trace_history(
            db_path=Path(args.db),
            days=args.days,
            history_days=args.history_days,
            top_n=args.top,
            stock_ids=parse_watchlist(args.watchlist),
            sleep_seconds=args.sleep,
        )
        print(f"requested brokers: {result['requested_brokers']}")
        print(f"broker trace rows: {result['row_count']}")
        if result["failures"]:
            print(f"failures: {len(result['failures'])}")
        print(f"dates: {', '.join(result['dates'])}")
        return

    result = run_branch(
        db_path=Path(args.db),
        output_dir=Path(args.output_dir),
        days=args.days,
        top_n=args.top,
        watchlist=parse_watchlist(args.watchlist),
        sleep_seconds=args.sleep,
        all_stocks=args.all,
        markets=parse_watchlist(args.markets),
        limit=args.limit,
        offset=args.offset,
        skip_existing=args.skip_existing,
        retry_sleeps=tuple(float(value) for value in parse_watchlist(args.retry_sleeps)),
    )
    print(f"requested stocks: {result['requested_stock_count']}")
    print(f"branch rows: {result['row_count']}")
    if result["failures"]:
        print(f"failures: {len(result['failures'])}")
    print(f"dates: {', '.join(result['dates'])}")
    print(f"report: {result['report']}")
    print(f"csv: {result['csv']}")


if __name__ == "__main__":
    main()

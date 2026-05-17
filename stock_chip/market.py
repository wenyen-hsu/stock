from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

from stock_chip.official import connect_db


DB_PATH = Path("data/stock_chip.sqlite")
TWSE_TAIEX_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
TAIFEX_FUTURES_URL = "https://www.taifex.com.tw/cht/3/futContractsDate"
PRODUCTS = {
    "TXF": "大台",
    "MXF": "小台",
    "TMF": "微台",
}
PRODUCT_START_DATES = {
    "TMF": dt.date(2024, 7, 29),
}
INSTITUTION_MAP = {
    "外資": "foreign",
    "投信": "trust",
    "自營商": "dealer",
}


@dataclass
class IndexRow:
    date: str
    index_code: str
    index_name: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    source: str


@dataclass
class FuturesRow:
    date: str
    product_code: str
    product_name: str
    institution: str
    trade_long: int | None
    trade_short: int | None
    trade_net: int | None
    oi_long: int | None
    oi_short: int | None
    oi_net: int | None
    source: str


def clean_float(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"--", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clean_int(value: object) -> int | None:
    number = clean_float(value)
    if number is None:
        return None
    return int(round(number))


def roc_date(value: str) -> str:
    match = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", value.strip())
    if not match:
        return value.strip()
    year, month, day = (int(part) for part in match.groups())
    return f"{year + 1911:04d}-{month:02d}-{day:02d}"


def month_starts(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    current = start.replace(day=1)
    last = end.replace(day=1)
    while current <= last:
        yield current
        if current.month == 12:
            current = dt.date(current.year + 1, 1, 1)
        else:
            current = dt.date(current.year, current.month + 1, 1)


def fetch_taiex_month(month: dt.date, timeout: int = 20) -> list[IndexRow]:
    params = {"response": "json", "date": month.strftime("%Y%m%d")}
    response = requests.get(TWSE_TAIEX_URL, params=params, timeout=timeout, verify=False)
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") != "OK":
        return []
    rows: list[IndexRow] = []
    for item in payload.get("data") or []:
        if len(item) < 5:
            continue
        rows.append(
            IndexRow(
                date=roc_date(str(item[0])),
                index_code="TAIEX",
                index_name="加權指數",
                open=clean_float(item[1]),
                high=clean_float(item[2]),
                low=clean_float(item[3]),
                close=clean_float(item[4]),
                source="twse",
            )
        )
    return rows


def fetch_taifex_product(date: dt.date, product_code: str, timeout: int = 30) -> list[FuturesRow]:
    params = {
        "doQuery": "1",
        "queryType": "1",
        "queryDate": date.strftime("%Y/%m/%d"),
        "commodityId": product_code,
    }
    response = requests.get(TAIFEX_FUTURES_URL, params=params, timeout=timeout, verify=False)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    table = soup.find("table", class_="table_f")
    if table is None:
        return []

    rows: list[FuturesRow] = []
    product_name = PRODUCTS.get(product_code, product_code)
    for tr in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        if not cells:
            continue
        if cells[0] in INSTITUTION_MAP:
            institution_label = cells[0]
            values = cells[1:]
        elif len(cells) >= 3 and cells[2] in INSTITUTION_MAP:
            product_name = cells[1] or product_name
            institution_label = cells[2]
            values = cells[3:]
        else:
            continue
        if len(values) < 11:
            continue
        rows.append(
            FuturesRow(
                date=date.isoformat(),
                product_code=product_code,
                product_name=product_name,
                institution=INSTITUTION_MAP[institution_label],
                trade_long=clean_int(values[0]),
                trade_short=clean_int(values[2]),
                trade_net=clean_int(values[4]),
                oi_long=clean_int(values[6]),
                oi_short=clean_int(values[8]),
                oi_net=clean_int(values[10]),
                source="taifex",
            )
        )
        if len(rows) >= 3:
            break
    return rows


def upsert_index_rows(conn: sqlite3.Connection, rows: list[IndexRow]) -> int:
    if not rows:
        return 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO market_index_daily (
            date, index_code, index_name, open, high, low, close, source, updated_at
        )
        VALUES (
            :date, :index_code, :index_name, :open, :high, :low, :close, :source, :updated_at
        )
        ON CONFLICT(date, index_code, source) DO UPDATE SET
            index_name = excluded.index_name,
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    return len(rows)


def upsert_futures_rows(conn: sqlite3.Connection, rows: list[FuturesRow]) -> int:
    if not rows:
        return 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO futures_institution_oi (
            date, product_code, product_name, institution, trade_long, trade_short,
            trade_net, oi_long, oi_short, oi_net, source, updated_at
        )
        VALUES (
            :date, :product_code, :product_name, :institution, :trade_long, :trade_short,
            :trade_net, :oi_long, :oi_short, :oi_net, :source, :updated_at
        )
        ON CONFLICT(date, product_code, institution, source) DO UPDATE SET
            product_name = excluded.product_name,
            trade_long = excluded.trade_long,
            trade_short = excluded.trade_short,
            trade_net = excluded.trade_net,
            oi_long = excluded.oi_long,
            oi_short = excluded.oi_short,
            oi_net = excluded.oi_net,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    return len(rows)


def existing_index_months(conn: sqlite3.Connection, start: dt.date, end: dt.date) -> set[str]:
    rows = conn.execute(
        """
        SELECT substr(date, 1, 7), COUNT(*)
        FROM market_index_daily
        WHERE index_code = 'TAIEX'
          AND source = 'twse'
          AND date BETWEEN ? AND ?
        GROUP BY substr(date, 1, 7)
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {row[0] for row in rows if int(row[1] or 0) >= 15}


def existing_futures_dates(conn: sqlite3.Connection, product_code: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT date
        FROM futures_institution_oi
        WHERE product_code = ?
          AND source = 'taifex'
        GROUP BY date
        HAVING COUNT(DISTINCT institution) >= 3
        """,
        (product_code,),
    ).fetchall()
    return {row[0] for row in rows}


def index_dates(conn: sqlite3.Connection, start: dt.date, end: dt.date) -> list[str]:
    rows = conn.execute(
        """
        SELECT date
        FROM market_index_daily
        WHERE index_code = 'TAIEX'
          AND source = 'twse'
          AND date BETWEEN ? AND ?
        ORDER BY date
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [row[0] for row in rows]


def refresh_market_data(
    db_path: Path = DB_PATH,
    years: int = 3,
    days: int | None = None,
    products: list[str] | None = None,
    sleep_seconds: float = 0.35,
    force: bool = False,
) -> dict[str, int]:
    end = dt.date.today()
    start = end - (dt.timedelta(days=days) if days else dt.timedelta(days=365 * years + 35))
    products = products or list(PRODUCTS)
    summary = {"index_rows": 0, "futures_rows": 0, "futures_dates": 0, "errors": 0}

    with connect_db(db_path) as conn:
        covered_months = set() if force else existing_index_months(conn, start, end)
        for month in month_starts(start, end):
            if month.strftime("%Y-%m") in covered_months:
                continue
            try:
                count = upsert_index_rows(conn, fetch_taiex_month(month))
                conn.commit()
                summary["index_rows"] += count
                print(f"TAIEX {month:%Y-%m}: {count} rows")
            except Exception as exc:
                summary["errors"] += 1
                print(f"TAIEX {month:%Y-%m}: failed: {exc}")
            time.sleep(sleep_seconds)

        dates = [dt.date.fromisoformat(value) for value in index_dates(conn, start, end)]
        for product in products:
            covered_dates = set() if force else existing_futures_dates(conn, product)
            product_start = PRODUCT_START_DATES.get(product)
            product_dates = [date for date in dates if product_start is None or date >= product_start]
            missing_dates = [date for date in product_dates if date.isoformat() not in covered_dates]
            for date in missing_dates:
                try:
                    rows = fetch_taifex_product(date, product)
                    if rows:
                        summary["futures_dates"] += 1
                    count = upsert_futures_rows(conn, rows)
                    conn.commit()
                    summary["futures_rows"] += count
                    print(f"{product} {date}: {count} rows")
                except Exception as exc:
                    summary["errors"] += 1
                    print(f"{product} {date}: failed: {exc}")
                time.sleep(sleep_seconds)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch TAIEX and TAIFEX institution futures balances.")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--products", default="TXF,MXF,TMF")
    parser.add_argument("--sleep", type=float, default=0.35)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    products = [item.strip().upper() for item in args.products.split(",") if item.strip()]
    result = refresh_market_data(
        Path(args.db),
        years=args.years,
        days=args.days,
        products=products,
        sleep_seconds=args.sleep,
        force=args.force,
    )
    print(result)


if __name__ == "__main__":
    main()

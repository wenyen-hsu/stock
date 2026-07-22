from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from stock_chip.official import connect_db

# 除權息事件：供「我的持股」把配息/配股還原進成本與損益。
# TWSE 走 rwd TWT49U（除權除息計算結果表，支援日期區間、可回補歷史）：
#   純息列直接取「權值+息值」（精確）；權/權息列由參考價反推——
#   現金股利 = 除權息前收盤 − 減除股利參考價、
#   配股率(股/股) = 減除股利參考價 / 除權息參考價 − 1。
# TPEx 走 openapi tpex_exright_prepost（上櫃除權除息預告表，約涵蓋近月＋未來），
#   直接給 CashDividend 與 StockDividendRatio，未來日期事件由前端依日期套用。
TWSE_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
TPEX_URLS = (
    "https://www.tpex.org.tw/openapi/v1/tpex_exright_prepost",
    "https://www.tpex.org.tw/www/openapi/v1/tpex_exright_prepost",
)
SOURCE_TWSE = "twse_twt49u"
SOURCE_TPEX = "tpex_exright"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "accept": "application/json",
}


@dataclass
class DividendEvent:
    ex_date: str
    stock_id: str
    name: str
    market: str
    event_type: str
    cash_dividend: float | None
    stock_dividend_per_share: float | None
    source: str


def parse_roc_date(text: str | None) -> str | None:
    """民國日期 → ISO。接受 '115年06月01日'、'115/06/01'、'1150601'。"""
    raw = (text or "").strip()
    if not raw:
        return None
    match = re.match(r"^(\d{2,3})年(\d{1,2})月(\d{1,2})日$", raw)
    if not match:
        match = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", raw)
    if not match:
        match = re.match(r"^(\d{3})(\d{2})(\d{2})$", raw)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return dt.date(year + 1911, month, day).isoformat()
    except ValueError:
        return None


def to_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def split_components(
    prev_close: float | None, ref_price: float | None, cash_deducted_ref: float | None
) -> tuple[float | None, float | None]:
    """由參考價反推（現金股利, 每股配股數）。缺任一參考價時回 (None, None)。"""
    if prev_close is None or ref_price is None or ref_price <= 0:
        return None, None
    if cash_deducted_ref is None or cash_deducted_ref <= 0:
        cash_deducted_ref = ref_price
    cash = round(prev_close - cash_deducted_ref, 4)
    ratio = round(cash_deducted_ref / ref_price - 1, 6)
    return (cash if cash > 0 else 0.0), (ratio if ratio > 0 else 0.0)


def fetch_twse(start: dt.date, end: dt.date, timeout: int = 60) -> list[DividendEvent]:
    response = requests.get(
        TWSE_URL,
        params={
            "startDate": start.strftime("%Y%m%d"),
            "endDate": end.strftime("%Y%m%d"),
            "response": "json",
        },
        headers=HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("stat") not in {"OK", "ok"}:
        return []
    fields = payload.get("fields") or []
    index = {name: idx for idx, name in enumerate(fields)}
    required = ("資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價")
    if any(key not in index for key in required):
        raise RuntimeError(f"TWSE TWT49U 欄位改版：{fields}")
    events: list[DividendEvent] = []
    for row in payload.get("data") or []:
        ex_date = parse_roc_date(row[index["資料日期"]])
        stock_id = str(row[index["股票代號"]]).strip()
        if not ex_date or not re.fullmatch(r"\d{4,6}[A-Z]?", stock_id):
            continue
        event_type = str(row[index["權/息"]]).strip() if "權/息" in index else ""
        combined = to_number(row[index["權值+息值"]]) if "權值+息值" in index else None
        if event_type == "息" and combined is not None:
            cash, ratio = combined, 0.0  # 純息：權值+息值即現金股利（精確）
        else:
            cash, ratio = split_components(
                to_number(row[index["除權息前收盤價"]]),
                to_number(row[index["除權息參考價"]]),
                to_number(row[index["減除股利參考價"]]) if "減除股利參考價" in index else None,
            )
        events.append(
            DividendEvent(
                ex_date=ex_date,
                stock_id=stock_id,
                name=str(row[index["股票名稱"]]).strip(),
                market="TWSE",
                event_type=event_type,
                cash_dividend=cash,
                stock_dividend_per_share=ratio,
                source=SOURCE_TWSE,
            )
        )
    return events


# TPEx tpex_exright_prepost 實測欄名（2026-07 probe），保留同義備援
TPEX_DATE_KEYS = ("ExRrightsExDividendDate", "ExRightsExDividendDate", "Date")
TPEX_ID_KEYS = ("SecuritiesCompanyCode", "Code")
TPEX_NAME_KEYS = ("CompanyName", "Name")
TPEX_CASH_KEYS = ("CashDividend", "CashDivdend")
TPEX_STOCK_KEYS = ("StockDividendRatio", "StockDividend")
TPEX_TYPE_KEYS = ("ExRrightsExDividend", "ExRightsExDividend")


def pick(row: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def fetch_tpex(timeout: int = 60) -> list[DividendEvent]:
    last_error: Exception | None = None
    for url in TPEX_URLS:
        try:
            response = requests.get(url, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            rows = response.json()
            break
        except Exception as exc:
            last_error = exc
            rows = None
    if rows is None:
        raise RuntimeError(f"TPEx exright fetch failed: {last_error}")
    events: list[DividendEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ex_date = parse_roc_date(str(pick(row, TPEX_DATE_KEYS) or ""))
        stock_id = str(pick(row, TPEX_ID_KEYS) or "").strip()
        if not ex_date or not re.fullmatch(r"\d{4,6}[A-Z]?", stock_id):
            continue
        cash = to_number(pick(row, TPEX_CASH_KEYS)) or 0.0
        ratio = to_number(pick(row, TPEX_STOCK_KEYS)) or 0.0
        if cash <= 0 and ratio <= 0:
            continue
        events.append(
            DividendEvent(
                ex_date=ex_date,
                stock_id=stock_id,
                name=str(pick(row, TPEX_NAME_KEYS) or "").strip(),
                market="TPEX",
                event_type=str(pick(row, TPEX_TYPE_KEYS) or "").strip(),
                cash_dividend=cash,
                stock_dividend_per_share=ratio,
                source=SOURCE_TPEX,
            )
        )
    return events


def upsert_events(conn: sqlite3.Connection, events: list[DividendEvent]) -> None:
    if not events:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO dividend_events (
            ex_date, stock_id, name, market, event_type,
            cash_dividend, stock_dividend_per_share, source, updated_at
        )
        VALUES (
            :ex_date, :stock_id, :name, :market, :event_type,
            :cash_dividend, :stock_dividend_per_share, :source, :updated_at
        )
        ON CONFLICT(ex_date, stock_id, source) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            event_type = excluded.event_type,
            cash_dividend = excluded.cash_dividend,
            stock_dividend_per_share = excluded.stock_dividend_per_share,
            updated_at = excluded.updated_at
        """,
        [{**event.__dict__, "updated_at": now} for event in events],
    )
    conn.commit()


def load_dividend_events(conn: sqlite3.Connection, days: int = 400) -> dict[str, Any]:
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT ex_date, stock_id, name, market, event_type, cash_dividend, stock_dividend_per_share
        FROM dividend_events
        WHERE ex_date >= ?
        ORDER BY ex_date
        """,
        (cutoff,),
    ).fetchall()
    events = [
        {
            "ex_date": row[0],
            "stock_id": row[1],
            "name": row[2],
            "market": row[3],
            "event_type": row[4],
            "cash": row[5],
            "stock": row[6],
        }
        for row in rows
    ]
    return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "days": days, "events": events}


def run_dividends(db_path: Path, backfill_days: int) -> None:
    today = dt.date.today()
    start = today - dt.timedelta(days=backfill_days)
    with connect_db(db_path) as conn:
        totals: list[str] = []
        try:
            twse_events = fetch_twse(start, today)
            upsert_events(conn, twse_events)
            totals.append(f"TWSE {len(twse_events)} 筆")
        except Exception as exc:
            print(f"[dividends] TWSE 抓取失敗：{exc}")
        try:
            tpex_events = fetch_tpex()
            upsert_events(conn, tpex_events)
            totals.append(f"TPEX {len(tpex_events)} 筆")
        except Exception as exc:
            print(f"[dividends] TPEx 抓取失敗：{exc}")
        count = conn.execute("SELECT COUNT(*) FROM dividend_events").fetchone()[0]
    print(f"[dividends] 更新完成：{'、'.join(totals) or '無新資料'}；資料庫累計 {count} 筆")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 TWSE/TPEx 除權息事件")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite 路徑")
    parser.add_argument("--backfill-days", type=int, default=120, help="TWSE 回補天數（TPEx 端點固定回近月）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_dividends(Path(args.db), args.backfill_days)


if __name__ == "__main__":
    main()

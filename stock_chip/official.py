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

from stock_chip.probe import (
    TWSE_INST_URL,
    TWSE_PRICE_URL,
    ProbeError,
    clean_float,
    clean_number,
    get_json,
    twse_date,
)


STOCK_ID_RE = re.compile(r"^[1-9]\d{3}$")
TPEX_PRICE_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
TPEX_INST_URL = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
TWSE_MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"
TWSE_VALUATION_URL = "https://www.twse.com.tw/exchangeReport/BWIBBU_d"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php"
TPEX_VALUATION_URL = "https://www.tpex.org.tw/web/stock/aftertrading/peratio_analysis/pera_result.php"
TPEX_ESB_LATEST_URL = "https://www.tpex.org.tw/openapi/v1/tpex_esb_latest_statistics"
MIN_COMBINED_PRICE_COUNT = 1800


@dataclass
class PriceRow:
    date: str
    stock_id: str
    name: str
    market: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    turnover: int | None
    transaction_count: int | None
    avg_price: float | None
    pe_ratio: float | None = None
    dividend_yield: float | None = None
    pb_ratio: float | None = None


@dataclass
class InstitutionalRow:
    date: str
    stock_id: str
    name: str
    market: str
    foreign_buy: int | None
    foreign_sell: int | None
    foreign_net: int | None
    trust_buy: int | None
    trust_sell: int | None
    trust_net: int | None
    dealer_net: int | None


@dataclass
class MarginRow:
    date: str
    stock_id: str
    name: str
    market: str
    margin_buy: int | None
    margin_sell: int | None
    margin_cash_repay: int | None
    margin_prev_balance: int | None
    margin_balance: int | None
    margin_limit: int | None
    short_buy: int | None
    short_sell: int | None
    short_stock_repay: int | None
    short_prev_balance: int | None
    short_balance: int | None
    short_limit: int | None
    offset: int | None
    note: str | None


def is_stock_id(stock_id: str) -> bool:
    return bool(STOCK_ID_RE.match(stock_id))


def fetch_twse_prices_all(date: dt.date, stock_only: bool = True) -> list[PriceRow]:
    data = get_json(
        TWSE_PRICE_URL,
        {"date": twse_date(date), "type": "ALLBUT0999", "response": "json"},
    )
    if data.get("stat") not in (None, "OK") and not data.get("tables"):
        raise ProbeError(f"TWSE price response is not OK: {data.get('stat')}")

    valuations = fetch_twse_valuations_all(date)
    rows: list[PriceRow] = []
    for table in data.get("tables", []):
        fields = table.get("fields") or []
        required = {"證券代號", "證券名稱", "成交股數", "成交金額", "收盤價"}
        if not required.issubset(fields):
            continue
        field_index = {name: idx for idx, name in enumerate(fields)}
        for row in table.get("data", []):
            stock_id = row[field_index["證券代號"]].strip()
            if stock_only and not is_stock_id(stock_id):
                continue
            volume = clean_number(row[field_index["成交股數"]])
            turnover = clean_number(row[field_index["成交金額"]])
            avg_price = turnover / volume if turnover and volume else None
            valuation = valuations.get(stock_id)
            rows.append(
                PriceRow(
                    date=date.isoformat(),
                    stock_id=stock_id,
                    name=row[field_index["證券名稱"]].strip(),
                    market="TWSE",
                    open=clean_float(row[field_index["開盤價"]]),
                    high=clean_float(row[field_index["最高價"]]),
                    low=clean_float(row[field_index["最低價"]]),
                    close=clean_float(row[field_index["收盤價"]]),
                    volume=volume,
                    turnover=turnover,
                    transaction_count=clean_number(row[field_index["成交筆數"]]),
                    avg_price=avg_price,
                    pe_ratio=valuation.get("pe_ratio") if valuation is not None else field_float(row, field_index, "本益比"),
                    dividend_yield=valuation.get("dividend_yield") if valuation is not None else None,
                    pb_ratio=valuation.get("pb_ratio") if valuation is not None else None,
                )
            )
    return rows


def fetch_twse_valuations_all(date: dt.date) -> dict[str, dict[str, float | None]]:
    try:
        data = get_json(
            TWSE_VALUATION_URL,
            {"date": twse_date(date), "response": "json"},
        )
    except Exception:
        return {}
    if data.get("stat") not in (None, "OK"):
        return {}
    fields = data.get("fields") or []
    field_index = field_indexes(fields)
    required = {"證券代號", "本益比", "殖利率(%)", "股價淨值比"}
    if not required.issubset(field_index):
        return {}
    valuations: dict[str, dict[str, float | None]] = {}
    for row in data.get("data", []):
        stock_id = str(row[field_index["證券代號"]]).strip()
        if not is_stock_id(stock_id):
            continue
        valuations[stock_id] = {
            "pe_ratio": clean_float(row[field_index["本益比"]]),
            "dividend_yield": clean_float(row[field_index["殖利率(%)"]]),
            "pb_ratio": clean_float(row[field_index["股價淨值比"]]),
        }
    return valuations


def fetch_twse_institutional_all(date: dt.date, stock_only: bool = True) -> list[InstitutionalRow]:
    data = get_json(
        TWSE_INST_URL,
        {"date": twse_date(date), "selectType": "ALLBUT0999", "response": "json"},
    )
    if data.get("stat") != "OK":
        raise ProbeError(f"TWSE institutional response is not OK: {data.get('stat')}")

    fields = data.get("fields") or []
    field_index = {name: idx for idx, name in enumerate(fields)}
    rows: list[InstitutionalRow] = []
    for row in data.get("data", []):
        stock_id = row[field_index["證券代號"]].strip()
        if stock_only and not is_stock_id(stock_id):
            continue
        rows.append(
            InstitutionalRow(
                date=date.isoformat(),
                stock_id=stock_id,
                name=row[field_index["證券名稱"]].strip(),
                market="TWSE",
                foreign_buy=clean_number(row[field_index["外陸資買進股數(不含外資自營商)"]]),
                foreign_sell=clean_number(row[field_index["外陸資賣出股數(不含外資自營商)"]]),
                foreign_net=clean_number(row[field_index["外陸資買賣超股數(不含外資自營商)"]]),
                trust_buy=clean_number(row[field_index["投信買進股數"]]),
                trust_sell=clean_number(row[field_index["投信賣出股數"]]),
                trust_net=clean_number(row[field_index["投信買賣超股數"]]),
                dealer_net=clean_number(row[field_index["自營商買賣超股數"]]),
            )
        )
    return rows


def tpex_date(date: dt.date) -> str:
    return f"{date.year - 1911:03d}/{date.month:02d}/{date.day:02d}"


def parse_roc_compact(value: str) -> dt.date:
    text = str(value).strip()
    year = int(text[:3]) + 1911
    return dt.date(year, int(text[3:5]), int(text[5:7]))


def post_json(url: str, data: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 stock-chip-probe/0.1",
        "Accept": "application/json,text/plain,*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(url, data=data, headers=headers, timeout=timeout, verify=False)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def field_indexes(fields: list[str]) -> dict[str, int]:
    return {str(name).strip(): idx for idx, name in enumerate(fields)}


def field_float(row: list[Any], field_index: dict[str, int], *names: str) -> float | None:
    for name in names:
        idx = field_index.get(name)
        if idx is not None and idx < len(row):
            return clean_float(row[idx])
    return None


def fetch_tpex_prices_all(date: dt.date, stock_only: bool = True) -> list[PriceRow]:
    data = post_json(
        TPEX_PRICE_URL,
        {"date": tpex_date(date), "type": "EW", "response": "json"},
    )
    response_date = str(data.get("date") or "")
    if response_date != date.strftime("%Y%m%d"):
        raise ProbeError(
            f"TPEX price response date mismatch: requested {date.isoformat()}, got {response_date or 'empty'}"
        )
    valuations = fetch_tpex_valuations_all(date)
    rows: list[PriceRow] = []
    for table in data.get("tables", []):
        fields = table.get("fields") or []
        required = {"代號", "名稱", "成交股數", "成交金額(元)", "收盤"}
        field_index = field_indexes(fields)
        if not required.issubset(field_index):
            continue
        for row in table.get("data", []):
            stock_id = row[field_index["代號"]].strip()
            if stock_only and not is_stock_id(stock_id):
                continue
            volume = clean_number(row[field_index["成交股數"]])
            turnover = clean_number(row[field_index["成交金額(元)"]])
            avg_price = turnover / volume if turnover and volume else None
            valuation = valuations.get(stock_id)
            rows.append(
                PriceRow(
                    date=date.isoformat(),
                    stock_id=stock_id,
                    name=row[field_index["名稱"]].strip(),
                    market="TPEX",
                    open=clean_float(row[field_index["開盤"]]),
                    high=clean_float(row[field_index["最高"]]),
                    low=clean_float(row[field_index["最低"]]),
                    close=clean_float(row[field_index["收盤"]]),
                    volume=volume,
                    turnover=turnover,
                    transaction_count=clean_number(row[field_index["成交筆數"]]),
                    avg_price=avg_price,
                    pe_ratio=valuation.get("pe_ratio") if valuation is not None else field_float(row, field_index, "本益比"),
                    dividend_yield=valuation.get("dividend_yield") if valuation is not None else None,
                    pb_ratio=valuation.get("pb_ratio") if valuation is not None else None,
                )
            )
    return rows


def fetch_tpex_valuations_all(date: dt.date) -> dict[str, dict[str, float | None]]:
    try:
        data = get_json(
            TPEX_VALUATION_URL,
            {"l": "zh-tw", "o": "json", "d": tpex_date(date), "c": "", "s": "0,asc"},
        )
    except Exception:
        return {}
    valuations: dict[str, dict[str, float | None]] = {}
    for table in data.get("tables", []):
        fields = table.get("fields") or []
        field_index = field_indexes(fields)
        required = {"股票代號", "本益比", "殖利率(%)", "股價淨值比"}
        if not required.issubset(field_index):
            continue
        for row in table.get("data", []):
            stock_id = str(row[field_index["股票代號"]]).strip()
            if not is_stock_id(stock_id):
                continue
            valuations[stock_id] = {
                "pe_ratio": clean_float(row[field_index["本益比"]]),
                "dividend_yield": clean_float(row[field_index["殖利率(%)"]]),
                "pb_ratio": clean_float(row[field_index["股價淨值比"]]),
            }
    return valuations


def fetch_tpex_institutional_all(date: dt.date, stock_only: bool = True) -> list[InstitutionalRow]:
    data = get_json(
        TPEX_INST_URL,
        {"l": "zh-tw", "o": "json", "se": "EW", "t": "D", "d": tpex_date(date)},
    )
    rows: list[InstitutionalRow] = []
    for table in data.get("tables", []):
        for row in table.get("data", []):
            stock_id = row[0].strip()
            if stock_only and not is_stock_id(stock_id):
                continue
            rows.append(
                InstitutionalRow(
                    date=date.isoformat(),
                    stock_id=stock_id,
                    name=row[1].strip(),
                    market="TPEX",
                    foreign_buy=clean_number(row[2]),
                    foreign_sell=clean_number(row[3]),
                    foreign_net=clean_number(row[4]),
                    trust_buy=clean_number(row[11]),
                    trust_sell=clean_number(row[12]),
                    trust_net=clean_number(row[13]),
                    dealer_net=clean_number(row[22]),
                )
            )
    return rows


def fetch_twse_margin_all(date: dt.date, stock_only: bool = True) -> list[MarginRow]:
    data = get_json(
        TWSE_MARGIN_URL,
        {"date": twse_date(date), "response": "json", "selectType": "ALL"},
    )
    if data.get("stat") != "OK":
        raise ProbeError(f"TWSE margin response is not OK: {data.get('stat')}")

    rows: list[MarginRow] = []
    for table in data.get("tables", []):
        fields = table.get("fields") or []
        if not fields or fields[0] != "代號":
            continue
        for row in table.get("data", []):
            stock_id = str(row[0]).strip()
            if stock_only and not is_stock_id(stock_id):
                continue
            rows.append(
                MarginRow(
                    date=date.isoformat(),
                    stock_id=stock_id,
                    name=str(row[1]).strip(),
                    market="TWSE",
                    margin_buy=clean_number(row[2]),
                    margin_sell=clean_number(row[3]),
                    margin_cash_repay=clean_number(row[4]),
                    margin_prev_balance=clean_number(row[5]),
                    margin_balance=clean_number(row[6]),
                    margin_limit=clean_number(row[7]),
                    short_buy=clean_number(row[8]),
                    short_sell=clean_number(row[9]),
                    short_stock_repay=clean_number(row[10]),
                    short_prev_balance=clean_number(row[11]),
                    short_balance=clean_number(row[12]),
                    short_limit=clean_number(row[13]),
                    offset=clean_number(row[14]),
                    note=str(row[15]).strip() if len(row) > 15 else "",
                )
            )
    return rows


def fetch_tpex_margin_all(date: dt.date, stock_only: bool = True) -> list[MarginRow]:
    data = get_json(
        TPEX_MARGIN_URL,
        {"l": "zh-tw", "o": "json", "d": tpex_date(date)},
    )
    response_date = str(data.get("date") or "")
    if response_date and response_date != date.strftime("%Y%m%d"):
        raise ProbeError(
            f"TPEX margin response date mismatch: requested {date.isoformat()}, got {response_date}"
        )

    rows: list[MarginRow] = []
    for table in data.get("tables", []):
        fields = table.get("fields") or []
        field_index = field_indexes(fields)
        required = {"代號", "名稱", "資買", "資賣", "資餘額", "券賣", "券買", "券餘額"}
        if not required.issubset(field_index):
            continue
        for row in table.get("data", []):
            stock_id = str(row[field_index["代號"]]).strip()
            if stock_only and not is_stock_id(stock_id):
                continue
            rows.append(
                MarginRow(
                    date=date.isoformat(),
                    stock_id=stock_id,
                    name=str(row[field_index["名稱"]]).strip(),
                    market="TPEX",
                    margin_buy=clean_number(row[field_index["資買"]]),
                    margin_sell=clean_number(row[field_index["資賣"]]),
                    margin_cash_repay=clean_number(row[field_index["現償"]]),
                    margin_prev_balance=clean_number(row[field_index["前資餘額(張)"]]),
                    margin_balance=clean_number(row[field_index["資餘額"]]),
                    margin_limit=clean_number(row[field_index["資限額"]]),
                    short_buy=clean_number(row[field_index["券買"]]),
                    short_sell=clean_number(row[field_index["券賣"]]),
                    short_stock_repay=clean_number(row[field_index["券償"]]),
                    short_prev_balance=clean_number(row[field_index["前券餘額(張)"]]),
                    short_balance=clean_number(row[field_index["券餘額"]]),
                    short_limit=clean_number(row[field_index["券限額"]]),
                    offset=clean_number(row[field_index["資券相抵(張)"]]),
                    note=str(row[field_index["備註"]]).strip() if "備註" in field_index else "",
                )
            )
    return rows


def fetch_esb_latest_prices(date: dt.date, stock_only: bool = True) -> list[PriceRow]:
    data = get_json(TPEX_ESB_LATEST_URL, {})
    rows: list[PriceRow] = []
    for row in data:
        row_date = parse_roc_compact(row["Date"])
        if row_date != date:
            continue
        stock_id = row["SecuritiesCompanyCode"].strip()
        if stock_only and not is_stock_id(stock_id):
            continue
        volume = clean_number(row.get("TransactionVolume"))
        avg_price = clean_float(row.get("Average"))
        turnover = int(round(avg_price * volume)) if avg_price is not None and volume else None
        rows.append(
            PriceRow(
                date=date.isoformat(),
                stock_id=stock_id,
                name=row["CompanyName"].strip(),
                market="ESB",
                open=None,
                high=clean_float(row.get("Highest")),
                low=clean_float(row.get("Lowest")),
                close=clean_float(row.get("LatestPrice")),
                volume=volume,
                turnover=turnover,
                transaction_count=None,
                avg_price=avg_price,
            )
        )
    return rows


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stocks (
            stock_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            market TEXT NOT NULL DEFAULT 'TWSE',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock_profiles (
            stock_id TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            industry_code TEXT,
            industry_name TEXT,
            sub_industry TEXT DEFAULT '',
            business TEXT,
            chairman TEXT,
            website TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trading_days (
            date TEXT PRIMARY KEY,
            market TEXT NOT NULL DEFAULT 'TWSE',
            price_count INTEGER NOT NULL,
            institutional_count INTEGER NOT NULL,
            margin_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS daily_prices (
            date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            turnover INTEGER,
            transaction_count INTEGER,
            avg_price REAL,
            pe_ratio REAL,
            dividend_yield REAL,
            pb_ratio REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date, stock_id)
        );

        CREATE TABLE IF NOT EXISTS institutional_trades (
            date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT NOT NULL,
            foreign_buy INTEGER,
            foreign_sell INTEGER,
            foreign_net INTEGER,
            trust_buy INTEGER,
            trust_sell INTEGER,
            trust_net INTEGER,
            dealer_net INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date, stock_id)
        );

        CREATE TABLE IF NOT EXISTS margin_trades (
            date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
            margin_buy INTEGER,
            margin_sell INTEGER,
            margin_cash_repay INTEGER,
            margin_prev_balance INTEGER,
            margin_balance INTEGER,
            margin_limit INTEGER,
            short_buy INTEGER,
            short_sell INTEGER,
            short_stock_repay INTEGER,
            short_prev_balance INTEGER,
            short_balance INTEGER,
            short_limit INTEGER,
            offset INTEGER,
            note TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date, stock_id)
        );

        CREATE TABLE IF NOT EXISTS broker_branch_topn (
            as_of_date TEXT NOT NULL,
            from_date TEXT NOT NULL,
            to_date TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT NOT NULL,
            rank_side TEXT NOT NULL,
            rank_no INTEGER NOT NULL,
            broker_id TEXT,
            broker_name TEXT NOT NULL,
            buy_lot REAL,
            sell_lot REAL,
            net_lot REAL,
            avg_price REAL,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (as_of_date, window_days, stock_id, rank_side, rank_no, source)
        );

        CREATE TABLE IF NOT EXISTS broker_branch_daily (
            trade_date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT NOT NULL,
            broker_name TEXT NOT NULL,
            broker_id TEXT,
            rank_side TEXT NOT NULL,
            rank_no INTEGER NOT NULL,
            buy_lot REAL,
            sell_lot REAL,
            net_lot REAL,
            avg_price REAL,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, stock_id, broker_name, source)
        );

        CREATE TABLE IF NOT EXISTS branch_fetch_status (
            trade_date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            error TEXT,
            source_url TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trade_date, stock_id, window_days, source)
        );

        CREATE TABLE IF NOT EXISTS monthly_revenues (
            revenue_month TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT NOT NULL,
            market TEXT NOT NULL,
            revenue INTEGER,
            prev_month_revenue INTEGER,
            last_year_revenue INTEGER,
            mom_pct REAL,
            yoy_pct REAL,
            cumulative_revenue INTEGER,
            last_year_cumulative_revenue INTEGER,
            cumulative_yoy_pct REAL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (revenue_month, stock_id, source)
        );

        CREATE TABLE IF NOT EXISTS market_index_daily (
            date TEXT NOT NULL,
            index_code TEXT NOT NULL,
            index_name TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date, index_code, source)
        );

        CREATE TABLE IF NOT EXISTS futures_institution_oi (
            date TEXT NOT NULL,
            product_code TEXT NOT NULL,
            product_name TEXT NOT NULL,
            institution TEXT NOT NULL,
            trade_long INTEGER,
            trade_short INTEGER,
            trade_net INTEGER,
            oi_long INTEGER,
            oi_short INTEGER,
            oi_net INTEGER,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date, product_code, institution, source)
        );

        CREATE TABLE IF NOT EXISTS quarterly_financials (
            year_quarter TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT,
            market TEXT,
            revenue REAL,
            gross_profit REAL,
            operating_income REAL,
            net_income REAL,
            eps REAL,
            gross_margin_pct REAL,
            operating_margin_pct REAL,
            net_margin_pct REAL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (year_quarter, stock_id, source)
        );

        CREATE TABLE IF NOT EXISTS ranking_snapshots (
            snapshot_date TEXT NOT NULL,
            days INTEGER NOT NULL,
            ranking_name TEXT NOT NULL,
            rank_no INTEGER NOT NULL,
            stock_id TEXT NOT NULL,
            score REAL,
            total_score REAL,
            close REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, days, ranking_name, rank_no)
        );

        CREATE TABLE IF NOT EXISTS shareholding_dispersion (
            data_date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            level INTEGER NOT NULL,
            holder_count INTEGER,
            shares INTEGER,
            share_pct REAL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (data_date, stock_id, level, source)
        );

        CREATE TABLE IF NOT EXISTS dividend_events (
            ex_date TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            name TEXT,
            market TEXT,
            event_type TEXT,
            cash_dividend REAL,
            stock_dividend_per_share REAL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ex_date, stock_id, source)
        );

        CREATE TABLE IF NOT EXISTS etf_universe (
            data_date TEXT NOT NULL,
            etf_id TEXT NOT NULL,
            name TEXT,
            fund_type TEXT,
            has_foreign INTEGER,
            units REAL,
            nav REAL,
            aum REAL,
            is_hot INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (data_date, etf_id)
        );

        CREATE TABLE IF NOT EXISTS etf_holdings (
            data_date TEXT NOT NULL,
            etf_id TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            stock_name TEXT,
            shares REAL,
            weight_pct REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (data_date, etf_id, stock_id)
        );

        CREATE INDEX IF NOT EXISTS idx_etf_holdings_stock
            ON etf_holdings(stock_id, data_date);

        CREATE INDEX IF NOT EXISTS idx_daily_prices_stock_date
            ON daily_prices(stock_id, date);
        CREATE INDEX IF NOT EXISTS idx_institutional_stock_date
            ON institutional_trades(stock_id, date);
        CREATE INDEX IF NOT EXISTS idx_margin_trades_stock_date
            ON margin_trades(stock_id, date);
        CREATE INDEX IF NOT EXISTS idx_broker_branch_stock_window
            ON broker_branch_topn(stock_id, as_of_date, window_days);
        CREATE INDEX IF NOT EXISTS idx_broker_branch_daily_stock_date
            ON broker_branch_daily(stock_id, trade_date);
        CREATE INDEX IF NOT EXISTS idx_branch_fetch_status_stock_date
            ON branch_fetch_status(stock_id, trade_date, window_days);
        CREATE INDEX IF NOT EXISTS idx_monthly_revenues_stock_month
            ON monthly_revenues(stock_id, revenue_month);
        CREATE INDEX IF NOT EXISTS idx_market_index_daily_code_date
            ON market_index_daily(index_code, date);
        CREATE INDEX IF NOT EXISTS idx_futures_institution_product_date
            ON futures_institution_oi(product_code, date);
        CREATE INDEX IF NOT EXISTS idx_shareholding_dispersion_stock_date
            ON shareholding_dispersion(stock_id, data_date);
        CREATE INDEX IF NOT EXISTS idx_ranking_snapshots_name_date
            ON ranking_snapshots(ranking_name, snapshot_date);
        CREATE INDEX IF NOT EXISTS idx_quarterly_financials_stock
            ON quarterly_financials(stock_id, year_quarter);
        """
    )
    ensure_column(conn, "trading_days", "margin_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "daily_prices", "pe_ratio", "REAL")
    ensure_column(conn, "daily_prices", "dividend_yield", "REAL")
    ensure_column(conn, "daily_prices", "pb_ratio", "REAL")
    ensure_column(conn, "broker_branch_topn", "broker_id", "TEXT")
    ensure_column(conn, "broker_branch_daily", "broker_id", "TEXT")
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def upsert_prices(conn: sqlite3.Connection, rows: list[PriceRow]) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO daily_prices (
            date, stock_id, name, open, high, low, close, volume, turnover,
            transaction_count, avg_price, pe_ratio, dividend_yield, pb_ratio, updated_at
        )
        VALUES (
            :date, :stock_id, :name, :open, :high, :low, :close, :volume,
            :turnover, :transaction_count, :avg_price, :pe_ratio, :dividend_yield, :pb_ratio, :updated_at
        )
        ON CONFLICT(date, stock_id) DO UPDATE SET
            name = excluded.name,
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            turnover = excluded.turnover,
            transaction_count = excluded.transaction_count,
            avg_price = excluded.avg_price,
            pe_ratio = excluded.pe_ratio,
            dividend_yield = excluded.dividend_yield,
            pb_ratio = excluded.pb_ratio,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    conn.executemany(
        """
        INSERT INTO stocks (stock_id, name, market, updated_at)
        VALUES (:stock_id, :name, :market, :updated_at)
        ON CONFLICT(stock_id) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            updated_at = excluded.updated_at
        """,
        [
            {
                "stock_id": row.stock_id,
                "name": row.name,
                "market": row.market,
                "updated_at": now,
            }
            for row in rows
        ],
    )


def upsert_institutional(conn: sqlite3.Connection, rows: list[InstitutionalRow]) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO institutional_trades (
            date, stock_id, name, foreign_buy, foreign_sell, foreign_net,
            trust_buy, trust_sell, trust_net, dealer_net, updated_at
        )
        VALUES (
            :date, :stock_id, :name, :foreign_buy, :foreign_sell, :foreign_net,
            :trust_buy, :trust_sell, :trust_net, :dealer_net, :updated_at
        )
        ON CONFLICT(date, stock_id) DO UPDATE SET
            name = excluded.name,
            foreign_buy = excluded.foreign_buy,
            foreign_sell = excluded.foreign_sell,
            foreign_net = excluded.foreign_net,
            trust_buy = excluded.trust_buy,
            trust_sell = excluded.trust_sell,
            trust_net = excluded.trust_net,
            dealer_net = excluded.dealer_net,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )


def upsert_margin(conn: sqlite3.Connection, rows: list[MarginRow]) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO margin_trades (
            date, stock_id, name, market, margin_buy, margin_sell,
            margin_cash_repay, margin_prev_balance, margin_balance, margin_limit,
            short_buy, short_sell, short_stock_repay, short_prev_balance,
            short_balance, short_limit, offset, note, updated_at
        )
        VALUES (
            :date, :stock_id, :name, :market, :margin_buy, :margin_sell,
            :margin_cash_repay, :margin_prev_balance, :margin_balance, :margin_limit,
            :short_buy, :short_sell, :short_stock_repay, :short_prev_balance,
            :short_balance, :short_limit, :offset, :note, :updated_at
        )
        ON CONFLICT(date, stock_id) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            margin_buy = excluded.margin_buy,
            margin_sell = excluded.margin_sell,
            margin_cash_repay = excluded.margin_cash_repay,
            margin_prev_balance = excluded.margin_prev_balance,
            margin_balance = excluded.margin_balance,
            margin_limit = excluded.margin_limit,
            short_buy = excluded.short_buy,
            short_sell = excluded.short_sell,
            short_stock_repay = excluded.short_stock_repay,
            short_prev_balance = excluded.short_prev_balance,
            short_balance = excluded.short_balance,
            short_limit = excluded.short_limit,
            offset = excluded.offset,
            note = excluded.note,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )


def mark_trading_day(
    conn: sqlite3.Connection,
    date: dt.date,
    price_count: int,
    institutional_count: int,
    margin_count: int = 0,
) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO trading_days (date, market, price_count, institutional_count, margin_count, updated_at)
        VALUES (?, 'ALL', ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            market = excluded.market,
            price_count = excluded.price_count,
            institutional_count = excluded.institutional_count,
            margin_count = excluded.margin_count,
            updated_at = excluded.updated_at
        """,
        (date.isoformat(), price_count, institutional_count, margin_count, now),
    )


def update_day(
    conn: sqlite3.Connection,
    date: dt.date,
    stock_only: bool = True,
    include_esb: bool = True,
) -> tuple[int, int, int]:
    prices = [
        *fetch_twse_prices_all(date, stock_only=stock_only),
        *fetch_tpex_prices_all(date, stock_only=stock_only),
    ]
    if include_esb:
        prices.extend(fetch_esb_latest_prices(date, stock_only=stock_only))
    institutional = [
        *fetch_twse_institutional_all(date, stock_only=stock_only),
        *fetch_tpex_institutional_all(date, stock_only=stock_only),
    ]
    margin = [
        *fetch_twse_margin_all(date, stock_only=stock_only),
        *fetch_tpex_margin_all(date, stock_only=stock_only),
    ]
    if not prices or not institutional:
        raise ProbeError(f"{date.isoformat()} has no usable official data")
    upsert_prices(conn, prices)
    upsert_institutional(conn, institutional)
    upsert_margin(conn, margin)
    mark_trading_day(conn, date, len(prices), len(institutional), len(margin))
    conn.commit()
    return len(prices), len(institutional), len(margin)


def update_recent(
    conn: sqlite3.Connection,
    days: int,
    end_date: dt.date,
    stock_only: bool = True,
    force_refresh: bool = False,
    include_esb: bool = True,
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    cursor = end_date
    # Walk back proportionally to the requested window so deep backfills
    # (e.g. 240 trading days) are reachable; 75 keeps the original behaviour
    # for the daily 20-day updates.
    lookback_calendar_days = max(75, days * 2)
    while len(updated) < days and (end_date - cursor).days < lookback_calendar_days:
        min_price_count = MIN_COMBINED_PRICE_COUNT if stock_only else 1
        cached = conn.execute(
            """
            SELECT price_count, institutional_count, margin_count
            FROM trading_days
            WHERE date = ?
            """,
            (cursor.isoformat(),),
        ).fetchone()
        if (
            not force_refresh
            and cached
            and cached[0] >= min_price_count
            and cached[1] > 0
            and cached[2] > 0
        ):
            updated.append(
                {
                    "date": cursor.isoformat(),
                    "price_count": int(cached[0]),
                    "institutional_count": int(cached[1]),
                    "margin_count": int(cached[2]),
                    "status": "cached",
                }
            )
            cursor -= dt.timedelta(days=1)
            continue
        try:
            price_count, institutional_count, margin_count = update_day(
                conn,
                cursor,
                stock_only=stock_only,
                include_esb=include_esb,
            )
            status = "updated"
        except Exception as exc:
            if cached and cached[0] >= min_price_count and cached[1] > 0 and cached[2] > 0:
                price_count, institutional_count, margin_count = int(cached[0]), int(cached[1]), int(cached[2])
                status = "cached"
            else:
                # 靜默跳過會讓「最新交易日抓不到」完全隱形（管線照樣成功、
                # 健康檢查也比不出來，因為 trading_days 與行情一起停住）。
                # 2026-07-30 資料整日未進站即因此無人察覺。
                skipped.append({"date": cursor.isoformat(), "error": str(exc)[:200]})
                cursor -= dt.timedelta(days=1)
                continue
        updated.append(
            {
                "date": cursor.isoformat(),
                "price_count": price_count,
                "institutional_count": institutional_count,
                "margin_count": margin_count,
                "status": status,
            }
        )
        cursor -= dt.timedelta(days=1)
    if skipped:
        newest_ok = max((row["date"] for row in updated), default="")
        for row in skipped:
            # 比已入庫的最新日還新的跳過＝真的缺當日資料，值得注意；
            # 比它舊的多半是假日/非交易日，屬正常。
            marker = "!! 新於已入庫資料" if row["date"] > newest_ok else "（假日或非交易日）"
            print(f"skipped {row['date']} {marker}: {row['error']}")
    if len(updated) < days:
        raise ProbeError(f"only updated {len(updated)} trading days; requested {days}")
    return sorted(updated, key=lambda row: row["date"])


def query_summary(conn: sqlite3.Connection, days: int, limit: int | None = None) -> list[dict[str, Any]]:
    date_rows = conn.execute(
        "SELECT date FROM trading_days ORDER BY date DESC LIMIT ?",
        (days,),
    ).fetchall()
    dates = sorted(row[0] for row in date_rows)
    if not dates:
        return []
    latest_date = dates[-1]
    params: list[Any] = [latest_date, *dates]
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(limit)

    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"""
        SELECT
            s.stock_id,
            s.name,
            s.market,
            latest.close,
            CASE
                WHEN SUM(COALESCE(p.volume, 0)) > 0
                THEN 1.0 * SUM(COALESCE(p.turnover, 0)) / SUM(COALESCE(p.volume, 0))
                ELSE NULL
            END AS avg_price,
            SUM(COALESCE(i.foreign_net, 0)) AS foreign_net,
            SUM(COALESCE(i.trust_net, 0)) AS trust_net,
            SUM(COALESCE(i.dealer_net, 0)) AS dealer_net,
            SUM(COALESCE(p.volume, 0)) AS volume,
            latest_i.foreign_net AS latest_foreign_net,
            latest_i.trust_net AS latest_trust_net
        FROM stocks s
        JOIN daily_prices latest
            ON latest.stock_id = s.stock_id
           AND latest.date = ?
        LEFT JOIN institutional_trades latest_i
            ON latest_i.stock_id = s.stock_id
           AND latest_i.date = latest.date
        JOIN daily_prices p
            ON p.stock_id = s.stock_id
           AND p.date IN ({placeholders})
        LEFT JOIN institutional_trades i
            ON i.stock_id = p.stock_id
           AND i.date = p.date
        GROUP BY s.stock_id, s.name, s.market, latest.close, latest_i.foreign_net, latest_i.trust_net
        ORDER BY foreign_net DESC
        {limit_sql}
        """,
        params,
    ).fetchall()
    columns = [
        "stock_id",
        "name",
        "market",
        "close",
        f"{days}d_avg_price",
        f"{days}d_foreign_net",
        f"{days}d_trust_net",
        f"{days}d_dealer_net",
        f"{days}d_volume",
        "latest_foreign_net",
        "latest_trust_net",
    ]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def shares_to_lots(value: int | None) -> float | None:
    return round(value / 1000, 2) if value is not None else None


def export_summary(path: Path, rows: list[dict[str, Any]], days: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    export_rows: list[dict[str, Any]] = []
    for row in rows:
        export_rows.append(
            {
                "stock_id": row["stock_id"],
                "name": row["name"],
                "market": row["market"],
                "close": row["close"],
                f"{days}d_avg_price": round(row[f"{days}d_avg_price"], 4)
                if row[f"{days}d_avg_price"] is not None
                else None,
                f"{days}d_foreign_net_lot": shares_to_lots(row[f"{days}d_foreign_net"]),
                f"{days}d_trust_net_lot": shares_to_lots(row[f"{days}d_trust_net"]),
                f"{days}d_dealer_net_lot": shares_to_lots(row[f"{days}d_dealer_net"]),
                f"{days}d_volume_lot": shares_to_lots(row[f"{days}d_volume"]),
                "latest_foreign_net_lot": shares_to_lots(row["latest_foreign_net"]),
                "latest_trust_net_lot": shares_to_lots(row["latest_trust_net"]),
            }
        )
    if not export_rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(export_rows[0].keys()))
        writer.writeheader()
        writer.writerows(export_rows)


def print_update_result(updated: list[dict[str, Any]]) -> None:
    print("updated trading days:")
    for row in updated:
        print(
            f"- {row['date']} ({row.get('status', 'updated')}): prices={row['price_count']}, "
            f"institutional={row['institutional_count']}, margin={row.get('margin_count', 0)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update official TWSE/TPEX chip data.")
    parser.add_argument("--days", type=int, default=5, help="Recent trading days to update.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite database path.")
    parser.add_argument("--output-dir", default="reports", help="Report output directory.")
    parser.add_argument("--limit", type=int, default=50, help="Rows in summary CSV.")
    parser.add_argument(
        "--include-etf",
        action="store_true",
        help="Include non-four-digit securities such as ETFs.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Re-fetch dates even when complete data already exists locally.",
    )
    parser.add_argument(
        "--exclude-esb",
        action="store_true",
        help="Skip emerging-stock latest quote ingestion.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    output_dir = Path(args.output_dir)
    end_date = dt.date.fromisoformat(args.end_date)
    with connect_db(db_path) as conn:
        updated = update_recent(
            conn,
            days=args.days,
            end_date=end_date,
            stock_only=not args.include_etf,
            force_refresh=args.force_refresh,
            include_esb=not args.exclude_esb,
        )
        rows = query_summary(conn, days=args.days, limit=args.limit)
    summary_path = output_dir / f"twse_official_summary_{args.days}d.csv"
    export_summary(summary_path, rows, args.days)
    print_update_result(updated)
    print(f"database: {db_path}")
    print(f"summary: {summary_path}")


if __name__ == "__main__":
    main()

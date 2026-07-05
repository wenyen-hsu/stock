from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning

from stock_chip.official import connect_db


# MOPS t163sb04 綜合損益表彙總：一請求回該市場（sii/otc）單一季度全部公司。
# 回傳依產業分成多張表（一般業/金控/銀行/證券/保險/異業），欄名各異，
# 以表頭名稱對照解析；數值為「累計」（Q2=上半年、Q3=前三季、Q4=全年），
# 單季值由讀取端差分計算。金融業版型缺營業毛利 → 三率留空、只存淨利與 EPS。
MOPS_HOST = "mopsov.twse.com.tw"
MOPS_URL = "https://mopsov.twse.com.tw"
MOPS_FALLBACK_URL = "https://163.29.17.81"
AJAX_PATH = "/mops/web/ajax_t163sb04"
SOURCE = "mops_t163sb04"

# 表頭同義詞（依產業版型），依序找第一個命中的欄
REVENUE_HEADERS = ("營業收入", "收益", "淨收益", "收入")
GROSS_HEADERS = ("營業毛利（毛損）淨額", "營業毛利（毛損）")
OPERATING_HEADERS = ("營業利益（損失）", "營業利益")
NET_INCOME_HEADERS = ("淨利（淨損）歸屬於母公司業主", "本期淨利（淨損）", "本期稅後淨利（淨損）")
EPS_HEADERS = ("基本每股盈餘（元）", "基本每股盈餘")

# 財報公告期限（月, 日）；含 3 天緩衝
QUARTER_DEADLINES = {1: (5, 15), 2: (8, 14), 3: (11, 14), 4: (3, 31)}


@dataclass
class FinancialRow:
    year_quarter: str
    stock_id: str
    name: str
    market: str
    revenue: float | None
    gross_profit: float | None
    operating_income: float | None
    net_income: float | None
    eps: float | None
    gross_margin_pct: float | None
    operating_margin_pct: float | None
    net_margin_pct: float | None
    source: str


def parse_amount(value: str | None) -> float | None:
    text = (value or "").replace(",", "").strip()
    if not text or text in {"--", "-", "不適用"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _session_headers(base_url: str) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
    }
    if "163.29.17.81" in base_url:
        headers["Host"] = MOPS_HOST
    return headers


def fetch_quarter_html(market_typek: str, roc_year: int, season: int, timeout: int = 60) -> str:
    payload = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "isQuery": "Y",
        "TYPEK": market_typek,
        "year": str(roc_year),
        "season": f"{season:02d}",
    }
    warnings.simplefilter("ignore", InsecureRequestWarning)
    last_error: Exception | None = None
    for base_url in (MOPS_URL, MOPS_FALLBACK_URL):
        try:
            response = requests.post(
                f"{base_url}{AJAX_PATH}",
                data=payload,
                headers=_session_headers(base_url),
                timeout=timeout,
                verify=False,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            return response.text
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"MOPS t163sb04 fetch failed ({market_typek} {roc_year}Q{season}): {last_error}")


def header_index(headers: list[str], candidates: tuple[str, ...]) -> int | None:
    for candidate in candidates:
        for idx, header in enumerate(headers):
            if header == candidate:
                return idx
    return None


def margin(numerator: float | None, revenue: float | None) -> float | None:
    if numerator is None or revenue is None or revenue <= 0:
        return None
    return round(numerator / revenue * 100, 2)


def parse_income_tables(html: str, year_quarter: str, market: str) -> list[FinancialRow]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[FinancialRow] = []
    for table in soup.find_all("table"):
        header_cells = table.find_all("th")
        headers = [cell.get_text(strip=True) for cell in header_cells]
        if not headers or "公司代號" not in headers:
            continue
        idx_id = headers.index("公司代號")
        idx_name = header_index(headers, ("公司名稱",))
        idx_revenue = header_index(headers, REVENUE_HEADERS)
        idx_gross = header_index(headers, GROSS_HEADERS)
        idx_operating = header_index(headers, OPERATING_HEADERS)
        idx_net = header_index(headers, NET_INCOME_HEADERS)
        idx_eps = header_index(headers, EPS_HEADERS)
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < len(headers) or not re.fullmatch(r"\d{4,6}[A-Z]?", cells[idx_id] or ""):
                continue

            def cell(idx: int | None) -> float | None:
                if idx is None or idx >= len(cells):
                    return None
                return parse_amount(cells[idx])

            revenue = cell(idx_revenue)
            gross = cell(idx_gross)
            operating = cell(idx_operating)
            net_income = cell(idx_net)
            rows.append(
                FinancialRow(
                    year_quarter=year_quarter,
                    stock_id=cells[idx_id],
                    name=cells[idx_name] if idx_name is not None and idx_name < len(cells) else "",
                    market=market,
                    revenue=revenue,
                    gross_profit=gross,
                    operating_income=operating,
                    net_income=net_income,
                    eps=cell(idx_eps),
                    gross_margin_pct=margin(gross, revenue),
                    operating_margin_pct=margin(operating, revenue),
                    net_margin_pct=margin(net_income, revenue),
                    source=SOURCE,
                )
            )
    return rows


def upsert_financials(conn: sqlite3.Connection, rows: list[FinancialRow]) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO quarterly_financials (
            year_quarter, stock_id, name, market, revenue, gross_profit,
            operating_income, net_income, eps, gross_margin_pct,
            operating_margin_pct, net_margin_pct, source, updated_at
        )
        VALUES (
            :year_quarter, :stock_id, :name, :market, :revenue, :gross_profit,
            :operating_income, :net_income, :eps, :gross_margin_pct,
            :operating_margin_pct, :net_margin_pct, :source, :updated_at
        )
        ON CONFLICT(year_quarter, stock_id, source) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            revenue = excluded.revenue,
            gross_profit = excluded.gross_profit,
            operating_income = excluded.operating_income,
            net_income = excluded.net_income,
            eps = excluded.eps,
            gross_margin_pct = excluded.gross_margin_pct,
            operating_margin_pct = excluded.operating_margin_pct,
            net_margin_pct = excluded.net_margin_pct,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    conn.commit()


def published_quarters(today: dt.date | None = None, count: int = 8) -> list[tuple[int, int]]:
    """最近 count 個「應已公告」的季度，新到舊，(西元年, 季)。"""
    today = today or dt.date.today()
    quarters: list[tuple[int, int]] = []
    year, season = today.year, 4
    # 從當年 Q4 往回找第一個已過公告期限的季度
    candidates = [(year - offset // 4, 4 - offset % 4) for offset in range(0, count + 8)]
    for cand_year, cand_season in candidates:
        month, day = QUARTER_DEADLINES[cand_season]
        deadline_year = cand_year + 1 if cand_season == 4 else cand_year
        deadline = dt.date(deadline_year, month, day) + dt.timedelta(days=3)
        if today >= deadline:
            quarters.append((cand_year, cand_season))
        if len(quarters) >= count:
            break
    return quarters


def existing_quarter_counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        "SELECT year_quarter, market, COUNT(*) FROM quarterly_financials GROUP BY year_quarter, market"
    ).fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


def refresh_financials(
    db_path: Path,
    quarters: int = 8,
    force: bool = False,
    sleep_seconds: float = 3.0,
    retries: int = 2,
    timeout: int = 60,
) -> dict[str, Any]:
    targets = published_quarters(count=quarters)
    fetched = 0
    skipped = 0
    row_count = 0
    failures: list[str] = []
    with connect_db(db_path) as conn:
        existing = existing_quarter_counts(conn)
        for year, season in targets:
            year_quarter = f"{year}Q{season}"
            for typek, market in (("sii", "TWSE"), ("otc", "TPEX"), ("rotc", "ESB")):
                # 興櫃家數少，已入庫門檻降為 50
                threshold = 50 if market == "ESB" else 300
                if not force and existing.get((year_quarter, market), 0) >= threshold:
                    skipped += 1
                    continue
                if fetched:
                    time.sleep(sleep_seconds)
                last_error: Exception | None = None
                for attempt in range(retries + 1):
                    try:
                        html = fetch_quarter_html(typek, year - 1911, season, timeout=timeout)
                        rows = parse_income_tables(html, year_quarter, market)
                        if not rows:
                            raise RuntimeError("no parseable income tables")
                        upsert_financials(conn, rows)
                        row_count += len(rows)
                        fetched += 1
                        print(f"{year_quarter} {market}: {len(rows)} rows", flush=True)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < retries:
                            time.sleep(min(2 + attempt * 3, 8))
                if last_error is not None:
                    failures.append(f"{year_quarter} {market}: {last_error}")
                    print(f"{year_quarter} {market} failed: {last_error}", flush=True)
    return {
        "targets": [f"{y}Q{s}" for y, s in targets],
        "fetched_requests": fetched,
        "skipped_requests": skipped,
        "row_count": row_count,
        "failures": failures,
    }


def quarter_sort_key(year_quarter: str) -> tuple[int, int]:
    year, season = year_quarter.split("Q")
    return int(year), int(season)


def single_quarter_values(ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把累計值差分成單季值（Q1 即單季；Q2-Q4 減去同年度前一季累計）。

    ordered 需為同一股票、依季度舊到新排序的原始（累計）rows。"""
    by_quarter = {row["year_quarter"]: row for row in ordered}
    output: list[dict[str, Any]] = []
    for row in ordered:
        year, season = quarter_sort_key(row["year_quarter"])
        result = dict(row)
        if season > 1:
            prev = by_quarter.get(f"{year}Q{season - 1}")
            for key in ("revenue", "gross_profit", "operating_income", "net_income", "eps"):
                current = row.get(key)
                previous = prev.get(key) if prev else None
                result[key] = (
                    round(current - previous, 4)
                    if current is not None and previous is not None
                    else None
                )
        for num_key, margin_key in (
            ("gross_profit", "gross_margin_pct"),
            ("operating_income", "operating_margin_pct"),
            ("net_income", "net_margin_pct"),
        ):
            result[margin_key] = margin(result.get(num_key), result.get("revenue"))
        output.append(result)
    return output


def load_financial_rows(conn: sqlite3.Connection, stock_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """單一股票的原始（累計）季度資料，舊到新。"""
    try:
        raw = conn.execute(
            """
            SELECT year_quarter, revenue, gross_profit, operating_income, net_income, eps,
                   gross_margin_pct, operating_margin_pct, net_margin_pct
            FROM quarterly_financials
            WHERE stock_id = ?
            ORDER BY year_quarter DESC
            LIMIT ?
            """,
            (stock_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    columns = [
        "year_quarter", "revenue", "gross_profit", "operating_income", "net_income", "eps",
        "gross_margin_pct", "operating_margin_pct", "net_margin_pct",
    ]
    return [dict(zip(columns, row, strict=True)) for row in reversed(raw)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch MOPS quarterly income statements (t163sb04).")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--quarters", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sleep", type=float, default=3.0)
    parser.add_argument("--timeout", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = refresh_financials(
        Path(args.db),
        quarters=args.quarters,
        force=args.force,
        sleep_seconds=args.sleep,
        timeout=args.timeout,
    )
    print(f"targets: {', '.join(result['targets'])}")
    print(f"requests fetched/skipped: {result['fetched_requests']}/{result['skipped_requests']}")
    print(f"rows upserted: {result['row_count']}")
    if result["failures"]:
        print(f"failures: {len(result['failures'])}")
        for failure in result["failures"]:
            print(f"- {failure}")


if __name__ == "__main__":
    main()

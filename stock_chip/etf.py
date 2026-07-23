from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import warnings

import requests
from bs4 import BeautifulSoup
from urllib3.exceptions import InsecureRequestWarning

from stock_chip.dividends import parse_roc_date, to_number
from stock_chip.official import connect_db

# ETF 資金流：熱門台股 ETF 的申購/贖回熱度（發行單位數每日變化）。
# 邏輯：ETF 次級市場成交不直接買個股，真正推升成分股的是初級市場申購
# （單位數增加 → 發行商進場買現貨）。故以單位數變化當資金流訊號。
# 來源：
#   t187ap47_L（TWSE openapi）：全上市 ETF 基本資料，含基金類型、
#     是否含國外成分股、發行單位數（每日更新）
#   /rwd/zh/ETF/list：上市 ETF 標的指數名稱（分類用）
#   mis all_etf.txt：預估淨值（估算規模 AUM = 單位數 × 淨值）
# 名單：台股現貨股票型（排除債券/期貨/槓桿/反向/含國外成分）按 AUM 前 N。
# 個股級成分股映射（PCF）為 v2：官方無集中端點，需逐發行商處理。
META_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap47_L"
LIST_URL = "https://www.twse.com.tw/rwd/zh/ETF/list"
MIS_URL = "https://mis.twse.com.tw/stock/data/all_etf.txt"
# 成分股明細：MoneyDJ 主站（鏡站不含 /ETF/ 專區；分點資料已有 MoneyDJ 先例）
HOLDINGS_URL = "https://www.moneydj.com/ETF/X/Basic/Basic0007A.xdjhtm"
SOURCE = "twse_t187ap47"
HOT_COUNT = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "accept": "application/json",
}

EXCLUDE_KEYWORDS = ("期貨", "槓桿", "反向", "正向2", "債券")

RETRY_SLEEPS = (5, 20, 60)


def get_json(url: str, params: dict | None = None, timeout: int = 60):
    """帶退避重試的 JSON GET。TWSE 在管線連續請求下會限速回空頁，
    立即失敗會讓當日資料全缺；退避後重試幾乎都能成功。"""
    last_error: Exception | None = None
    for attempt, sleep_s in enumerate((0,) + RETRY_SLEEPS):
        if sleep_s:
            time.sleep(sleep_s)
        try:
            response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"fetch failed after {1 + len(RETRY_SLEEPS)} attempts: {url}: {last_error}")


@dataclass
class EtfRow:
    data_date: str
    etf_id: str
    name: str
    fund_type: str
    has_foreign: int
    units: float | None
    nav: float | None
    aum: float | None
    is_hot: int


def classify_category(name: str, index_name: str) -> str:
    text = f"{name} {index_name}"
    if any(key in text for key in ("高股息", "高息", "股利", "殖利率")):
        return "高股息"
    if any(key in text for key in ("半導體", "科技", "資訊", "電子", "AI", "晶片")):
        return "科技"
    if any(key in text for key in ("ESG", "永續", "公司治理")):
        return "ESG"
    if "主動" in text:
        return "主動式"
    if any(key in text for key in ("50", "中型100", "市值", "臺灣指數", "加權")):
        return "市值型"
    return "其他"


def is_domestic_equity(fund_type: str, has_foreign: int, name: str) -> bool:
    if has_foreign:
        return False
    if "股票" not in fund_type:
        return False
    return not any(key in fund_type or key in name for key in EXCLUDE_KEYWORDS)


def fetch_meta(timeout: int = 60) -> list[dict[str, Any]]:
    rows = get_json(META_URL, timeout=timeout)
    output = []
    for row in rows:
        etf_id = str(row.get("基金代號") or "").strip()
        if not re.fullmatch(r"\d{4,6}[A-Z]?", etf_id):
            continue
        output.append({
            "etf_id": etf_id,
            "name": str(row.get("基金簡稱") or "").strip(),
            "fund_type": str(row.get("基金類型") or "").strip(),
            "has_foreign": 0 if str(row.get("是否包含國外成分股") or "").strip() == "否" else 1,
            "units": to_number(row.get("發行單位數/轉換數")),
            "data_date": parse_roc_date(str(row.get("出表日期") or "")) or dt.date.today().isoformat(),
        })
    return output


def fetch_index_names(timeout: int = 60) -> dict[str, str]:
    try:
        payload = get_json(LIST_URL, params={"response": "json"}, timeout=timeout)
        fields = payload.get("fields") or []
        idx = {name: i for i, name in enumerate(fields)}
        if "證券代號" not in idx or "標的指數" not in idx:
            return {}
        return {
            str(row[idx["證券代號"]]).strip(): str(row[idx["標的指數"]]).strip()
            for row in payload.get("data") or []
        }
    except Exception:
        return {}


def fetch_navs(timeout: int = 60) -> dict[str, float]:
    """mis all_etf.txt：a=代號、e=預估淨值。失敗回空（AUM 退用單位數排序）。"""
    try:
        payload = get_json(MIS_URL, timeout=timeout)
        navs: dict[str, float] = {}
        for group in payload.get("a1") or []:
            for item in group.get("msgArray") or []:
                nav = to_number(item.get("e"))
                code = str(item.get("a") or "").strip()
                if code and nav and nav > 0:
                    navs[code] = nav
        return navs
    except Exception:
        return {}


def build_rows(
    meta: list[dict[str, Any]], index_names: dict[str, str], navs: dict[str, float]
) -> list[EtfRow]:
    candidates = []
    for row in meta:
        nav = navs.get(row["etf_id"])
        units = row["units"]
        aum = round(units * nav) if units and nav else None
        candidates.append(EtfRow(
            data_date=row["data_date"],
            etf_id=row["etf_id"],
            name=row["name"],
            fund_type=row["fund_type"],
            has_foreign=row["has_foreign"],
            units=units,
            nav=nav,
            aum=aum,
            is_hot=0,
        ))
    eligible = [row for row in candidates if is_domestic_equity(row.fund_type, row.has_foreign, row.name)]
    eligible.sort(key=lambda row: (row.aum or 0, row.units or 0), reverse=True)
    hot_ids = {row.etf_id for row in eligible[:HOT_COUNT]}
    for row in candidates:
        row.is_hot = 1 if row.etf_id in hot_ids else 0
    return candidates


HTML_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def get_html(url: str, params: dict | None = None, timeout: int = 30) -> str:
    # MoneyDJ 憑證缺 Subject Key Identifier，Python 3.13 驗證會拒絕；
    # 公開唯讀行情資料停用驗證（同 financials.py 對 MOPS 備援的先例）
    warnings.simplefilter("ignore", InsecureRequestWarning)
    last_error: Exception | None = None
    for sleep_s in (0, 5, 15):
        if sleep_s:
            time.sleep(sleep_s)
        try:
            response = requests.get(url, params=params, headers=HTML_HEADERS, timeout=timeout, verify=False)
            response.raise_for_status()
            if len(response.text) < 3000:
                raise RuntimeError(f"回應過短（{len(response.text)} bytes），疑似錯誤頁")
            return response.text
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"fetch failed: {url}: {last_error}")


def parse_holdings_html(html: str) -> list[dict[str, Any]]:
    """MoneyDJ Basic0007A 持股明細表：股票名稱|持股(千股)|比例|增減。"""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        header_text = table.get_text()
        if "股票名稱" not in header_text or "比例" not in header_text:
            continue
        holdings = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 3 or cells[0] in {"", "股票名稱"}:
                continue
            weight = to_number(cells[2].rstrip("%"))
            if weight is None or weight <= 0:
                continue
            change = to_number(cells[3].rstrip("%").lstrip("+")) if len(cells) > 3 else None
            holdings.append({
                "name": cells[0],
                "thousand_shares": to_number(cells[1]),
                "weight_pct": weight,
                "change_pct": change,
            })
        if holdings:
            return holdings
    return []


def fetch_holdings(etf_id: str) -> list[dict[str, Any]]:
    html = get_html(HOLDINGS_URL, params={"etfid": f"{etf_id}.TW"})
    return parse_holdings_html(html)


def stock_name_map(conn: sqlite3.Connection) -> dict[str, str]:
    """股名 → 代號（MoneyDJ 表只有名稱）。日行情簡稱為準，profile 名稱補位。"""
    mapping: dict[str, str] = {}
    for table, name_col in (("stocks", "name"), ("daily_prices", "name")):
        try:
            for stock_id, name in conn.execute(
                f"SELECT DISTINCT stock_id, {name_col} FROM {table} WHERE {name_col} IS NOT NULL"
            ):
                clean = str(name).strip()
                if clean and clean not in mapping:
                    mapping[clean] = str(stock_id)
        except sqlite3.OperationalError:
            continue
    return mapping


def upsert_holdings(conn: sqlite3.Connection, data_date: str, etf_id: str,
                    holdings: list[dict[str, Any]], name_to_id: dict[str, str]) -> None:
    if not holdings:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM etf_holdings WHERE data_date = ? AND etf_id = ?", (data_date, etf_id))
    conn.executemany(
        """
        INSERT OR REPLACE INTO etf_holdings (
            data_date, etf_id, stock_id, stock_name, shares, weight_pct, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                data_date,
                etf_id,
                name_to_id.get(item["name"], f"?{item['name']}"),
                item["name"],
                (item["thousand_shares"] or 0) * 1000,
                item["weight_pct"],
                now,
            )
            for item in holdings
        ],
    )
    conn.commit()


def upsert_rows(conn: sqlite3.Connection, rows: list[EtfRow]) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO etf_universe (
            data_date, etf_id, name, fund_type, has_foreign, units, nav, aum, is_hot, updated_at
        )
        VALUES (:data_date, :etf_id, :name, :fund_type, :has_foreign, :units, :nav, :aum, :is_hot, :updated_at)
        ON CONFLICT(data_date, etf_id) DO UPDATE SET
            name = excluded.name,
            fund_type = excluded.fund_type,
            has_foreign = excluded.has_foreign,
            units = excluded.units,
            nav = excluded.nav,
            aum = excluded.aum,
            is_hot = excluded.is_hot,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    conn.commit()


def units_change_pct(latest: float | None, past: float | None) -> float | None:
    if not latest or not past or past <= 0:
        return None
    return round((latest - past) / past * 100, 2)


def load_etf_flows(conn: sqlite3.Connection) -> dict[str, Any]:
    """熱門 ETF 的單位數 1 日 / 5 日變化（申購熱度）。"""
    dates = [row[0] for row in conn.execute(
        "SELECT DISTINCT data_date FROM etf_universe ORDER BY data_date DESC LIMIT 6"
    )]
    if not dates:
        return {"generated_at": dt.datetime.now().isoformat(timespec="seconds"), "state": "empty", "rows": []}
    latest = dates[0]
    prev = dates[1] if len(dates) > 1 else None
    week_ago = dates[-1] if len(dates) > 1 else None
    index_names = fetch_index_names()

    def units_map(date: str | None) -> dict[str, float]:
        if not date:
            return {}
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT etf_id, units FROM etf_universe WHERE data_date = ? AND units IS NOT NULL", (date,)
            )
        }

    latest_rows = conn.execute(
        """
        SELECT etf_id, name, fund_type, units, nav, aum
        FROM etf_universe WHERE data_date = ? AND is_hot = 1
        ORDER BY aum DESC, units DESC
        """,
        (latest,),
    ).fetchall()
    prev_units = units_map(prev)
    week_units = units_map(week_ago)
    rows = []
    for etf_id, name, fund_type, units, nav, aum in latest_rows:
        rows.append({
            "etf_id": etf_id,
            "name": name,
            "category": classify_category(name, index_names.get(etf_id, "")),
            "index_name": index_names.get(etf_id, ""),
            "units": units,
            "nav": nav,
            "aum": aum,
            "units_chg_1d_pct": units_change_pct(units, prev_units.get(etf_id)),
            "units_chg_5d_pct": units_change_pct(units, week_units.get(etf_id)),
        })
    # 成分股（每檔 ETF 的最新持股明細）
    holdings_date = conn.execute("SELECT MAX(data_date) FROM etf_holdings").fetchone()[0]
    holdings_by_etf: dict[str, list] = {}
    if holdings_date:
        for etf_id, stock_id, stock_name, weight, shares in conn.execute(
            """
            SELECT etf_id, stock_id, stock_name, weight_pct, shares
            FROM etf_holdings WHERE data_date = ?
            ORDER BY etf_id, weight_pct DESC
            """,
            (holdings_date,),
        ):
            holdings_by_etf.setdefault(etf_id, []).append({
                "s": stock_id if not str(stock_id).startswith("?") else None,
                "n": stock_name,
                "w": weight,
            })
    aum_by_etf = {row["etf_id"]: row["aum"] or 0 for row in rows}
    name_by_etf = {row["etf_id"]: row["name"] for row in rows}
    for row in rows:
        row["holdings"] = holdings_by_etf.get(row["etf_id"], [])
    # 全 ETF 重倉聚合：Σ(權重 × 該 ETF 規模) = 熱門 ETF 合計持有市值
    aggregate: dict[str, dict[str, Any]] = {}
    for etf_id, items in holdings_by_etf.items():
        aum = aum_by_etf.get(etf_id, 0)
        for item in items:
            if not item["s"]:
                continue
            slot = aggregate.setdefault(item["s"], {"stock_id": item["s"], "name": item["n"], "etf_count": 0, "held_value": 0.0, "etfs": []})
            slot["etf_count"] += 1
            slot["held_value"] += (item["w"] or 0) / 100 * aum
            slot["etfs"].append({"e": etf_id, "en": name_by_etf.get(etf_id, etf_id), "w": item["w"]})
    top_stocks = sorted(aggregate.values(), key=lambda slot: -slot["held_value"])[:60]
    for slot in top_stocks:
        slot["held_value"] = round(slot["held_value"])
        slot["etfs"] = sorted(slot["etfs"], key=lambda item: -(item["w"] or 0))[:3]
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "state": "ready" if prev else "accumulating",
        "latest_date": latest,
        "holdings_date": holdings_date,
        "observed_days": len(dates),
        "rows": rows,
        "top_stocks": top_stocks,
    }


def run_etf(db_path: Path) -> None:
    try:
        meta = fetch_meta()
    except Exception as exc:
        print(f"[etf] 基本資料抓取失敗（含重試）：{exc}")
        return
    index_names = fetch_index_names()
    navs = fetch_navs()
    rows = build_rows(meta, index_names, navs)
    hot = [row for row in rows if row.is_hot]
    today = dt.date.today().isoformat()
    with connect_db(db_path) as conn:
        upsert_rows(conn, rows)
        name_to_id = stock_name_map(conn)
        holdings_total = 0
        for etf in hot:
            try:
                holdings = fetch_holdings(etf.etf_id)
                upsert_holdings(conn, today, etf.etf_id, holdings, name_to_id)
                holdings_total += len(holdings)
            except Exception as exc:
                print(f"[etf] {etf.etf_id} 成分股抓取失敗：{exc}")
            time.sleep(1.0)
        total = conn.execute("SELECT COUNT(*) FROM etf_universe").fetchone()[0]
    print(f"[etf] 全量 {len(rows)} 檔（含淨值 {sum(1 for r in rows if r.nav)} 檔）；"
          f"熱門台股型 {len(hot)} 檔、成分股 {holdings_total} 列；資料庫累計 {total} 列")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 ETF 基本資料與單位數（資金流）")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite 路徑")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_etf(Path(args.db))


if __name__ == "__main__":
    main()

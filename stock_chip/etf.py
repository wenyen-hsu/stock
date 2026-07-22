from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

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
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "state": "ready" if prev else "accumulating",
        "latest_date": latest,
        "observed_days": len(dates),
        "rows": rows,
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
    with connect_db(db_path) as conn:
        upsert_rows(conn, rows)
        total = conn.execute("SELECT COUNT(*) FROM etf_universe").fetchone()[0]
    print(f"[etf] 全量 {len(rows)} 檔（含淨值 {sum(1 for r in rows if r.nav)} 檔）；"
          f"熱門台股型 {len(hot)} 檔：{'、'.join(f'{r.etf_id} {r.name}' for r in hot[:8])}…；資料庫累計 {total} 列")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 ETF 基本資料與單位數（資金流）")
    parser.add_argument("--db", default="data/stock_chip.sqlite", help="SQLite 路徑")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_etf(Path(args.db))


if __name__ == "__main__":
    main()

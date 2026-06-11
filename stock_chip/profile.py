from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any

import requests

from stock_chip.official import connect_db


PROFILE_SOURCES = [
    ("TWSE", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"),
    ("TPEX", "https://openapi.twse.com.tw/v1/opendata/t187ap03_O"),
]

# TWSE/TPEx 產業別代碼（公開資訊觀測站分類）
INDUSTRY_CODES = {
    "01": "水泥工業",
    "02": "食品工業",
    "03": "塑膠工業",
    "04": "紡織纖維",
    "05": "電機機械",
    "06": "電器電纜",
    "08": "玻璃陶瓷",
    "09": "造紙工業",
    "10": "鋼鐵工業",
    "11": "橡膠工業",
    "12": "汽車工業",
    "14": "建材營造業",
    "15": "航運業",
    "16": "觀光餐旅",
    "17": "金融保險業",
    "18": "貿易百貨業",
    "19": "綜合",
    "20": "其他業",
    "21": "化學工業",
    "22": "生技醫療業",
    "23": "油電燃氣業",
    "24": "半導體業",
    "25": "電腦及週邊設備業",
    "26": "光電業",
    "27": "通信網路業",
    "28": "電子零組件業",
    "29": "電子通路業",
    "30": "資訊服務業",
    "31": "其他電子業",
    "32": "文化創意業",
    "33": "農業科技業",
    "34": "電子商務",
    "35": "綠能環保",
    "36": "數位雲端",
    "37": "運動休閒",
    "38": "居家生活",
    "80": "管理股票",
}

BUSINESS_KEYS = ("所營業務", "主要經營業務", "營業項目", "主要業務", "公司主要經營業務")
INDUSTRY_KEYS = ("產業別", "產業類別")
WEBSITE_KEYS = ("網址", "公司網址")
CHAIRMAN_KEYS = ("董事長",)


def pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value and value != "-":
            return value
    return ""


def industry_name(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw in INDUSTRY_CODES:
        return INDUSTRY_CODES[raw]
    padded = raw.zfill(2)
    if padded in INDUSTRY_CODES:
        return INDUSTRY_CODES[padded]
    # 來源若直接給名稱就原樣使用
    return raw


def fetch_profiles(market: str, url: str, timeout_seconds: float = 30) -> list[dict[str, Any]]:
    response = requests.get(url, timeout=timeout_seconds, headers={"accept": "application/json"})
    response.raise_for_status()
    payload = response.json()
    rows: list[dict[str, Any]] = []
    now = dt.datetime.now().isoformat(timespec="seconds")
    for item in payload:
        stock_id = str(item.get("公司代號") or "").strip()
        if not stock_id:
            continue
        raw_industry = pick(item, INDUSTRY_KEYS)
        rows.append(
            {
                "stock_id": stock_id,
                "name": str(item.get("公司簡稱") or item.get("公司名稱") or "").strip(),
                "market": market,
                "industry_code": raw_industry,
                "industry_name": industry_name(raw_industry),
                "business": pick(item, BUSINESS_KEYS),
                "chairman": pick(item, CHAIRMAN_KEYS),
                "website": pick(item, WEBSITE_KEYS),
                "updated_at": now,
            }
        )
    return rows


def upsert_profiles(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO stock_profiles
            (stock_id, name, market, industry_code, industry_name, business, chairman, website, updated_at)
        VALUES
            (:stock_id, :name, :market, :industry_code, :industry_name, :business, :chairman, :website, :updated_at)
        ON CONFLICT(stock_id) DO UPDATE SET
            name = excluded.name,
            market = excluded.market,
            industry_code = excluded.industry_code,
            industry_name = excluded.industry_name,
            business = CASE WHEN excluded.business != '' THEN excluded.business ELSE stock_profiles.business END,
            chairman = excluded.chairman,
            website = excluded.website,
            updated_at = excluded.updated_at
        """,
        rows,
    )
    conn.commit()


def run_profiles(db_path: Path, timeout_seconds: float = 30) -> dict[str, Any]:
    total = 0
    failures: list[str] = []
    with connect_db(db_path) as conn:
        for market, url in PROFILE_SOURCES:
            try:
                rows = fetch_profiles(market, url, timeout_seconds=timeout_seconds)
            except Exception as exc:
                failures.append(f"{market}: {exc}")
                continue
            upsert_profiles(conn, rows)
            total += len(rows)
            with_business = sum(1 for row in rows if row["business"])
            print(f"{market}: {len(rows)} profiles (business text: {with_business})", flush=True)
    return {"row_count": total, "failures": failures}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch listed-company profiles and industry classification.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--timeout", type=float, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_profiles(Path(args.db), timeout_seconds=args.timeout)
    print(f"profiles upserted: {result['row_count']}")
    for failure in result["failures"]:
        print(f"failed: {failure}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stock_chip.official import connect_db


# TDCC 集保戶股權分散表 OpenAPI：單一 GET 回全市場「最新一週」資料（約 9-10MB），
# 無歷史回補來源，週資料自首次抓取起在本庫累積。
TDCC_URL = "https://openapi.tdcc.com.tw/v1/opendata/1-5"
SOURCE = "tdcc"
REQUEST_TIMEOUT_SECONDS = 120
RETRY_SLEEPS = (5.0, 15.0)

# 持股分級（1 張 = 1000 股）：1=1-999股、2=1-5張、…、9=50-100張、
# 10=100-200張、11=200-400張、12=400-600張、13=600-800張、14=800-1000張、
# 15=1000張以上、16=差異數調整（可為負）、17=合計
LEVEL_TOTAL = 17
LEVEL_ADJUSTMENT = 16
LEVEL_BIG = 15          # >1,000 張（千張大戶）
LEVEL_400_START = 12    # 12-15 涵蓋 >400 張
RETAIL_LEVEL_MAX = 9    # 1-9 級 <100 張（散戶）


@dataclass
class DispersionRow:
    data_date: str
    stock_id: str
    level: int
    holder_count: int | None
    shares: int | None
    share_pct: float | None
    source: str


def normalize_key(key: str) -> str:
    return key.lstrip("﻿").strip()


def parse_int(value: Any) -> int | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_float(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def iso_date(value: str) -> str:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text.replace("/", "-")


def fetch_dispersion(timeout: float = REQUEST_TIMEOUT_SECONDS) -> list[DispersionRow]:
    req = urllib.request.Request(
        TDCC_URL,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = json.loads(resp.read())
    if not isinstance(data, list) or not data:
        raise RuntimeError("TDCC 回應不是非空列表")

    sample = {normalize_key(k): k for k in data[0]}
    required = ["資料日期", "證券代號", "持股分級", "人數", "股數"]
    missing = [name for name in required if name not in sample]
    pct_key_name = next((k for k in sample if k.startswith("占集保庫存數比例")), None)
    if missing or pct_key_name is None:
        raise RuntimeError(f"TDCC 欄位不符，實際欄位：{list(data[0].keys())}")

    key_date = sample["資料日期"]
    key_id = sample["證券代號"]
    key_level = sample["持股分級"]
    key_holders = sample["人數"]
    key_shares = sample["股數"]
    key_pct = sample[pct_key_name]

    rows: list[DispersionRow] = []
    for raw in data:
        level = parse_int(raw.get(key_level))
        if level is None:
            continue
        rows.append(
            DispersionRow(
                data_date=iso_date(str(raw.get(key_date, ""))),
                stock_id=str(raw.get(key_id, "")).strip(),
                level=level,
                holder_count=parse_int(raw.get(key_holders)),
                shares=parse_int(raw.get(key_shares)),
                share_pct=parse_float(raw.get(key_pct)),
                source=SOURCE,
            )
        )
    return rows


def latest_ingested_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(data_date) FROM shareholding_dispersion").fetchone()
    return row[0] if row and row[0] else None


def upsert_dispersion(conn: sqlite3.Connection, rows: list[DispersionRow]) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT INTO shareholding_dispersion (
            data_date, stock_id, level, holder_count, shares, share_pct, source, updated_at
        )
        VALUES (:data_date, :stock_id, :level, :holder_count, :shares, :share_pct, :source, :updated_at)
        ON CONFLICT(data_date, stock_id, level, source) DO UPDATE SET
            holder_count = excluded.holder_count,
            shares = excluded.shares,
            share_pct = excluded.share_pct,
            updated_at = excluded.updated_at
        """,
        [{**row.__dict__, "updated_at": now} for row in rows],
    )
    conn.commit()


def refresh_dispersion(db_path: Path, force: bool = False, timeout: float = REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
    last_error: Exception | None = None
    rows: list[DispersionRow] = []
    for attempt in range(1 + len(RETRY_SLEEPS)):
        if attempt:
            time.sleep(RETRY_SLEEPS[attempt - 1])
        try:
            rows = fetch_dispersion(timeout=timeout)
            break
        except Exception as exc:
            last_error = exc
            print(f"TDCC fetch retry {attempt + 1}: {exc}", flush=True)
    else:
        raise RuntimeError(f"TDCC 抓取失敗：{last_error}")

    data_date = rows[0].data_date
    with connect_db(db_path) as conn:
        existing = latest_ingested_date(conn)
        if not force and existing is not None and data_date <= existing:
            return {
                "data_date": data_date,
                "row_count": 0,
                "skipped": True,
                "note": f"本週資料（{data_date}）已入庫，最新為 {existing}",
            }
        upsert_dispersion(conn, rows)
        stock_count = conn.execute(
            "SELECT COUNT(DISTINCT stock_id) FROM shareholding_dispersion WHERE data_date = ?",
            (data_date,),
        ).fetchone()[0]
    return {
        "data_date": data_date,
        "row_count": len(rows),
        "stock_count": stock_count,
        "skipped": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch TDCC weekly shareholding dispersion.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--force", action="store_true", help="即使本週已入庫也重新覆寫。")
    parser.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = refresh_dispersion(Path(args.db), force=args.force, timeout=args.timeout)
    if result["skipped"]:
        print(result["note"])
        return
    print(f"data date: {result['data_date']}")
    print(f"rows upserted: {result['row_count']}")
    print(f"stocks covered: {result['stock_count']}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests
from urllib3.exceptions import InsecureRequestWarning


requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)


WATCHLIST = {
    "2376": "技嘉",
    "2382": "廣達",
    "2324": "仁寶",
    "6196": "帆宣",
}

TWSE_PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_INST_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_BSR_WELCOME_URL = "https://bsr.twse.com.tw/bshtm/bsWelcome.aspx"
FINMIND_BRANCH_URL = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"


class ProbeError(RuntimeError):
    pass


@dataclass
class DailyPrice:
    date: str
    stock_id: str
    name: str
    close: float | None
    volume: int | None
    turnover: int | None
    avg_price: float | None


@dataclass
class InstitutionalTrade:
    date: str
    stock_id: str
    name: str
    foreign_buy: int | None
    foreign_sell: int | None
    foreign_net: int | None
    trust_buy: int | None
    trust_sell: int | None
    trust_net: int | None


@dataclass
class BranchProbe:
    source: str
    status: str
    message: str
    sample_rows: int = 0
    top_buy_branches: list[dict[str, Any]] | None = None
    top_sell_branches: list[dict[str, Any]] | None = None


def clean_number(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"--", "---", "除權息"}:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("+", "")
    try:
        return int(float(text))
    except ValueError:
        return None


def clean_float(value: Any) -> float | None:
    number = clean_number(value)
    if number is not None and str(value).strip().replace(",", "").replace(".", "").lstrip("-+").isdigit():
        return float(str(value).strip().replace(",", ""))
    text = str(value).strip().replace(",", "")
    text = re.sub(r"<[^>]+>", "", text).replace("+", "")
    if not text or text in {"--", "---"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def twse_date(date: dt.date) -> str:
    return date.strftime("%Y%m%d")


def get_json(url: str, params: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 stock-chip-probe/0.1",
        "Accept": "application/json,text/plain,*/*",
    }
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def fetch_twse_price(date: dt.date, stock_ids: set[str]) -> dict[str, DailyPrice]:
    data = get_json(
        TWSE_PRICE_URL,
        {"date": twse_date(date), "type": "ALLBUT0999", "response": "json"},
    )
    if data.get("stat") not in (None, "OK") and not data.get("tables"):
        raise ProbeError(f"TWSE price response is not OK: {data.get('stat')}")

    rows: dict[str, DailyPrice] = {}
    for table in data.get("tables", []):
        fields = table.get("fields") or []
        if "證券代號" not in fields or "成交股數" not in fields or "成交金額" not in fields:
            continue
        field_index = {name: idx for idx, name in enumerate(fields)}
        for row in table.get("data", []):
            stock_id = row[field_index["證券代號"]].strip()
            if stock_id not in stock_ids:
                continue
            volume = clean_number(row[field_index["成交股數"]])
            turnover = clean_number(row[field_index["成交金額"]])
            close = clean_float(row[field_index["收盤價"]])
            avg_price = (turnover / volume) if turnover and volume else None
            rows[stock_id] = DailyPrice(
                date=date.isoformat(),
                stock_id=stock_id,
                name=row[field_index["證券名稱"]].strip(),
                close=close,
                volume=volume,
                turnover=turnover,
                avg_price=avg_price,
            )
    return rows


def fetch_twse_institutional(date: dt.date, stock_ids: set[str]) -> dict[str, InstitutionalTrade]:
    data = get_json(
        TWSE_INST_URL,
        {"date": twse_date(date), "selectType": "ALLBUT0999", "response": "json"},
    )
    if data.get("stat") != "OK":
        raise ProbeError(f"TWSE institutional response is not OK: {data.get('stat')}")

    fields = data.get("fields") or []
    field_index = {name: idx for idx, name in enumerate(fields)}
    rows: dict[str, InstitutionalTrade] = {}
    for row in data.get("data", []):
        stock_id = row[field_index["證券代號"]].strip()
        if stock_id not in stock_ids:
            continue
        rows[stock_id] = InstitutionalTrade(
            date=date.isoformat(),
            stock_id=stock_id,
            name=row[field_index["證券名稱"]].strip(),
            foreign_buy=clean_number(row[field_index["外陸資買進股數(不含外資自營商)"]]),
            foreign_sell=clean_number(row[field_index["外陸資賣出股數(不含外資自營商)"]]),
            foreign_net=clean_number(row[field_index["外陸資買賣超股數(不含外資自營商)"]]),
            trust_buy=clean_number(row[field_index["投信買進股數"]]),
            trust_sell=clean_number(row[field_index["投信賣出股數"]]),
            trust_net=clean_number(row[field_index["投信買賣超股數"]]),
        )
    return rows


def find_recent_trading_days(
    days: int,
    stock_ids: set[str],
    end_date: dt.date,
) -> tuple[list[dt.date], dict[dt.date, dict[str, DailyPrice]], dict[dt.date, dict[str, InstitutionalTrade]]]:
    found: list[dt.date] = []
    price_cache: dict[dt.date, dict[str, DailyPrice]] = {}
    institutional_cache: dict[dt.date, dict[str, InstitutionalTrade]] = {}
    cursor = end_date
    while len(found) < days and (end_date - cursor).days < 45:
        try:
            prices = fetch_twse_price(cursor, stock_ids)
            inst = fetch_twse_institutional(cursor, stock_ids)
        except Exception:
            cursor -= dt.timedelta(days=1)
            continue
        if prices and inst:
            found.append(cursor)
            price_cache[cursor] = prices
            institutional_cache[cursor] = inst
        cursor -= dt.timedelta(days=1)
    return sorted(found), price_cache, institutional_cache


def probe_bsr_public() -> BranchProbe:
    headers = {"User-Agent": "Mozilla/5.0 stock-chip-probe/0.1"}
    resp = requests.get(TWSE_BSR_WELCOME_URL, headers=headers, timeout=20, verify=False)
    resp.raise_for_status()
    text = resp.text
    if "驗證碼" in text:
        return BranchProbe(
            source="TWSE BSR public",
            status="blocked",
            message="TWSE 買賣日報表查詢系統明確要求每檔查詢輸入驗證碼，不能當成穩定自動化來源。",
        )
    return BranchProbe(
        source="TWSE BSR public",
        status="unknown",
        message="可連到買賣日報表系統，但尚未找到無驗證碼的逐價分點資料端點。",
    )


def probe_finmind_branch(stock_id: str, date: dt.date, top_n: int) -> BranchProbe:
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        return BranchProbe(
            source="FinMind",
            status="skipped",
            message="未設定 FINMIND_TOKEN；先跳過。文件顯示分點資料需要 sponsor 權限。",
        )

    headers = {"Authorization": f"Bearer {token}", "User-Agent": "stock-chip-probe/0.1"}
    resp = requests.get(
        FINMIND_BRANCH_URL,
        params={"data_id": stock_id, "date": date.isoformat()},
        headers=headers,
        timeout=30,
    )
    if resp.status_code >= 400:
        return BranchProbe(
            source="FinMind",
            status="failed",
            message=f"HTTP {resp.status_code}: {resp.text[:200]}",
        )
    payload = resp.json()
    rows = payload.get("data") or []
    if not rows:
        return BranchProbe(
            source="FinMind",
            status="empty_or_denied",
            message=str(payload)[:300],
        )

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        branch_id = str(row.get("securities_trader_id", "")).strip()
        branch_name = str(row.get("securities_trader", "")).strip()
        key = branch_id or branch_name
        price = float(row.get("price") or 0)
        buy = int(row.get("buy") or 0)
        sell = int(row.get("sell") or 0)
        item = grouped.setdefault(
            key,
            {
                "branch_id": branch_id,
                "branch_name": branch_name,
                "buy_volume": 0,
                "sell_volume": 0,
                "buy_amount": 0.0,
                "sell_amount": 0.0,
            },
        )
        item["buy_volume"] += buy
        item["sell_volume"] += sell
        item["buy_amount"] += price * buy
        item["sell_amount"] += price * sell

    summary = []
    for item in grouped.values():
        buy_volume = item["buy_volume"]
        sell_volume = item["sell_volume"]
        item["net_volume"] = buy_volume - sell_volume
        item["buy_avg_price"] = item["buy_amount"] / buy_volume if buy_volume else None
        item["sell_avg_price"] = item["sell_amount"] / sell_volume if sell_volume else None
        summary.append(item)

    top_buy = sorted(summary, key=lambda row: row["net_volume"], reverse=True)[:top_n]
    top_sell = sorted(summary, key=lambda row: row["net_volume"])[:top_n]
    return BranchProbe(
        source="FinMind",
        status="ok",
        message="取得分點逐價資料並完成均價彙總。",
        sample_rows=len(rows),
        top_buy_branches=top_buy,
        top_sell_branches=top_sell,
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def format_shares(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value / 1000:,.0f}"


def run_probe(days: int, top_n: int, output_dir: Path, end_date: dt.date) -> dict[str, Any]:
    stock_ids = set(WATCHLIST)
    trading_days, price_cache, institutional_cache = find_recent_trading_days(days, stock_ids, end_date)
    if not trading_days:
        raise ProbeError("找不到可用交易日。")

    daily_rows: list[dict[str, Any]] = []
    latest_summary: list[dict[str, Any]] = []
    for day in trading_days:
        prices = price_cache[day]
        inst = institutional_cache[day]
        for stock_id in WATCHLIST:
            price = prices.get(stock_id)
            trade = inst.get(stock_id)
            daily_rows.append(
                {
                    "date": day.isoformat(),
                    "stock_id": stock_id,
                    "name": WATCHLIST[stock_id],
                    "close": price.close if price else None,
                    "volume": price.volume if price else None,
                    "turnover": price.turnover if price else None,
                    "avg_price": round(price.avg_price, 4) if price and price.avg_price else None,
                    "foreign_net": trade.foreign_net if trade else None,
                    "trust_net": trade.trust_net if trade else None,
                }
            )

    latest_day = trading_days[-1]
    prices = price_cache[latest_day]
    inst = institutional_cache[latest_day]
    by_stock = {stock_id: [row for row in daily_rows if row["stock_id"] == stock_id] for stock_id in WATCHLIST}
    for stock_id, name in WATCHLIST.items():
        rows = by_stock[stock_id]
        total_volume = sum(row["volume"] or 0 for row in rows)
        total_turnover = sum(row["turnover"] or 0 for row in rows)
        latest_price = prices.get(stock_id)
        latest_inst = inst.get(stock_id)
        latest_summary.append(
            {
                "stock_id": stock_id,
                "name": name,
                "latest_date": latest_day.isoformat(),
                "close": latest_price.close if latest_price else None,
                f"{days}d_avg_price": round(total_turnover / total_volume, 4) if total_volume else None,
                f"{days}d_foreign_net_lot": format_shares(sum(row["foreign_net"] or 0 for row in rows)),
                f"{days}d_trust_net_lot": format_shares(sum(row["trust_net"] or 0 for row in rows)),
                "latest_foreign_net_lot": format_shares(latest_inst.foreign_net if latest_inst else None),
                "latest_trust_net_lot": format_shares(latest_inst.trust_net if latest_inst else None),
            }
        )

    bsr_probe = probe_bsr_public()
    finmind_probe = {
        stock_id: asdict(probe_finmind_branch(stock_id, latest_day, top_n))
        for stock_id in WATCHLIST
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / f"probe_daily_{days}d.csv", daily_rows)
    write_csv(output_dir / f"probe_summary_{days}d.csv", latest_summary)

    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "watchlist": WATCHLIST,
        "trading_days": [day.isoformat() for day in trading_days],
        "official_data": {
            "status": "ok",
            "message": "TWSE 官方可取得日成交、成交均價、外資與投信買賣超。",
        },
        "public_branch_data": asdict(bsr_probe),
        "finmind_branch_data": finmind_probe,
        "summary": latest_summary,
    }
    (output_dir / "probe_result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(output_dir / "probe_report.md", report, days)
    return report


def write_markdown(path: Path, report: dict[str, Any], days: int) -> None:
    lines = [
        "# 台股籌碼資料源 Probe",
        "",
        f"- 產生時間：{report['generated_at']}",
        f"- 交易日：{', '.join(report['trading_days'])}",
        "",
        "## 官方資料",
        "",
        f"- 狀態：{report['official_data']['status']}",
        f"- 說明：{report['official_data']['message']}",
        "",
        "## 分點資料",
        "",
        f"- TWSE BSR 狀態：{report['public_branch_data']['status']}",
        f"- TWSE BSR 說明：{report['public_branch_data']['message']}",
        "",
        "## 摘要",
        "",
        f"| 股票 | 收盤價 | {days}日均價 | {days}日外資買賣超(張) | {days}日投信買賣超(張) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for row in report["summary"]:
        lines.append(
            f"| {row['stock_id']} {row['name']} | {row['close']} | {row[f'{days}d_avg_price']} | "
            f"{row[f'{days}d_foreign_net_lot']} | {row[f'{days}d_trust_net_lot']} |"
        )
    lines.extend(["", "## FinMind 分點測試", ""])
    for stock_id, probe in report["finmind_branch_data"].items():
        lines.append(f"- {stock_id} {WATCHLIST[stock_id]}：{probe['status']}，{probe['message']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Taiwan stock chip data sources.")
    parser.add_argument("--days", type=int, default=5, help="Number of recent trading days to probe.")
    parser.add_argument("--top", type=int, default=5, help="Top N branches if branch data is available.")
    parser.add_argument("--output-dir", default="reports", help="Directory for probe outputs.")
    parser.add_argument(
        "--end-date",
        default=dt.date.today().isoformat(),
        help="Inclusive end date, YYYY-MM-DD.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run_probe(
        days=args.days,
        top_n=args.top,
        output_dir=Path(args.output_dir),
        end_date=dt.date.fromisoformat(args.end_date),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

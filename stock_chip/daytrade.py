from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from pathlib import Path
from typing import Any

import requests

from stock_chip.official import connect_db

# 當沖候選池的資料來源。三組資料、四個端點，全部經 2026-08 的探測 workflow 實測：
#
# 1. TWSE TWTB4U — 當日沖銷交易標的及成交量值。回 `tables` 結構，欄位是
#    「當日沖銷交易成交股數／買進成交金額／賣出成交金額」，**沒有比率欄位**，
#    比率要自己用 daily_prices.volume 去除。這份資料含 ETF（樣本首列是 00400A），
#    而 institutional_trades 在入庫時濾掉 ETF，合併時對不齊是正常的。
# 2. 處置股 — TWSE openapi/announcement/punish 與 TPEx tpex_disposal_information。
#    處置期間改人工撮合（約 2 分鐘一次），當沖做不了，是硬排除條件。
# 3. TPEx tpex_ceil_non_trading — 漲停鎖死仍有排隊買量。
#
# 上櫃沒有當沖統計：第二輪探測把 TPEx openapi 的端點全列出來確認過，
# 命中的 12 個裡沒有任何一個是當沖成交統計。所以上櫃的可交易性只能用
# 日均額 + 漲停排隊量代理——這在 payload 與頁面都必須標示清楚，
# 否則會被當成跟上市同一個標準（PE 分位用 1 年窗口就是這樣被誤讀的）。

TWSE_DAY_TRADE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/TWTB4U"
TWSE_PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"
TPEX_DISPOSAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"
TPEX_CEILING_URL = "https://www.tpex.org.tw/openapi/v1/tpex_ceil_non_trading"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "accept": "application/json",
}

STOCK_ID_RE = re.compile(r"^[1-9]\d{3}$")


def to_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if not text or text in {"-", "--", "---"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_roc(text: str | None) -> str | None:
    """民國日期 → ISO。接受 '1150812'、'115/08/12'、'115年08月12日'。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = (
        re.match(r"^(\d{3})(\d{2})(\d{2})$", raw)
        or re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", raw)
        or re.match(r"^(\d{2,3})年(\d{1,2})月(\d{1,2})日$", raw)
    )
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return dt.date(year + 1911, month, day).isoformat()
    except ValueError:
        return None


def parse_period(text: str | None) -> tuple[str | None, str | None]:
    """處置期間 → (起, 迄)。TWSE 用全形『～』、TPEx 用半形『~』，兩者都要吃。"""
    parts = re.split(r"[~～﹏－—-]", str(text or ""), maxsplit=1)
    if len(parts) != 2:
        return None, None
    return parse_roc(parts[0]), parse_roc(parts[1])


def fetch_twse_day_trade(date: dt.date, timeout: int = 60) -> list[dict[str, Any]]:
    """TWSE 當沖標的成交統計。回 tables 結構，需逐張表找含當沖欄位的那張。"""
    response = requests.get(
        TWSE_DAY_TRADE_URL,
        params={"date": date.strftime("%Y%m%d"), "selectType": "All", "response": "json"},
        headers=HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    iso = date.isoformat()
    rows: list[dict[str, Any]] = []
    for table in payload.get("tables") or []:
        fields = table.get("fields") or []
        index = {name: idx for idx, name in enumerate(fields)}
        if "當日沖銷交易成交股數" not in index:
            continue  # 這張表是總量或其他統計
        for row in table.get("data") or []:
            stock_id = str(row[index["證券代號"]]).strip()
            if not STOCK_ID_RE.match(stock_id):
                continue  # 濾掉 ETF/權證，與 institutional_trades 的宇宙一致
            rows.append(
                {
                    "date": iso,
                    "stock_id": stock_id,
                    "name": str(row[index["證券名稱"]]).strip(),
                    "market": "TWSE",
                    "day_trade_volume": to_number(row[index["當日沖銷交易成交股數"]]),
                    "day_trade_buy_amount": to_number(row[index.get("當日沖銷交易買進成交金額", -1)])
                    if "當日沖銷交易買進成交金額" in index else None,
                    "day_trade_sell_amount": to_number(row[index.get("當日沖銷交易賣出成交金額", -1)])
                    if "當日沖銷交易賣出成交金額" in index else None,
                    "suspended_note": str(row[index["暫停現股賣出後現款買進當沖註記"]]).strip()
                    if "暫停現股賣出後現款買進當沖註記" in index else "",
                }
            )
    if not rows:
        raise RuntimeError(f"TWTB4U 沒有解析到任何列（欄位可能改版）：{list(payload)}")
    return rows


def fetch_disposals(timeout: int = 60) -> list[dict[str, Any]]:
    """上市＋上櫃處置股。任一來源失敗不影響另一邊。"""
    out: list[dict[str, Any]] = []

    try:
        response = requests.get(TWSE_PUNISH_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        for row in response.json() or []:
            stock_id = str(row.get("Code") or "").strip()
            if not STOCK_ID_RE.match(stock_id):
                continue
            start, end = parse_period(row.get("DispositionPeriod"))
            out.append(
                {
                    "stock_id": stock_id,
                    "name": str(row.get("Name") or "").strip(),
                    "market": "TWSE",
                    "announce_date": parse_roc(row.get("Date")),
                    "start_date": start,
                    "end_date": end,
                    "reason": str(row.get("ReasonsOfDisposition") or "").strip(),
                    "measure": str(row.get("DispositionMeasures") or "").strip(),
                    "source": "twse_punish",
                }
            )
    except Exception as exc:
        print(f"[daytrade] TWSE 處置股抓取失敗：{exc}")

    try:
        response = requests.get(TPEX_DISPOSAL_URL, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
        for row in response.json() or []:
            stock_id = str(row.get("SecuritiesCompanyCode") or "").strip()
            if not STOCK_ID_RE.match(stock_id):
                continue
            start, end = parse_period(row.get("DispositionPeriod"))
            out.append(
                {
                    "stock_id": stock_id,
                    "name": str(row.get("CompanyName") or "").strip(),
                    "market": "TPEX",
                    "announce_date": parse_roc(row.get("Date")),
                    "start_date": start,
                    "end_date": end,
                    "reason": str(row.get("DispositionReasons") or "").strip(),
                    "measure": "",
                    "source": "tpex_disposal",
                }
            )
    except Exception as exc:
        print(f"[daytrade] TPEx 處置股抓取失敗：{exc}")

    return out


def fetch_tpex_ceiling(timeout: int = 60) -> list[dict[str, Any]]:
    """上櫃漲停鎖死仍有排隊買量。Difference 欄位即未成交排隊量。"""
    response = requests.get(TPEX_CEILING_URL, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    rows: list[dict[str, Any]] = []
    for row in response.json() or []:
        stock_id = str(row.get("SecuritiesCompanyCode") or "").strip()
        iso = parse_roc(row.get("Date"))
        if not STOCK_ID_RE.match(stock_id) or not iso:
            continue
        traded = to_number(row.get("Trans.Vol.AtUp-limitPr."))
        ordered = to_number(row.get("OrderVol.AtUp-limitPr."))
        queue = to_number(row.get("Difference"))
        if queue is None and traded is not None and ordered is not None:
            queue = ordered - traded
        rows.append(
            {
                "date": iso,
                "stock_id": stock_id,
                "name": str(row.get("CompanyName") or "").strip(),
                "market": "TPEX",
                "close": to_number(row.get("ClosingPrice")),
                "change": to_number(row.get("Change")),
                "total_volume": to_number(row.get("Tot.Trans.Vol.")),
                "ceiling_traded_volume": traded,
                "ceiling_order_volume": ordered,
                "queue_volume": queue,
            }
        )
    return rows


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]], keys: list[str]) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat(timespec="seconds")
    columns = list(rows[0]) + ["updated_at"]
    placeholders = ", ".join(f":{c}" for c in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}",
        [{**row, "updated_at": now} for row in rows],
    )
    conn.commit()


def run_daytrade(db_path: Path, date: dt.date | None = None) -> None:
    today = date or dt.date.today()
    with connect_db(db_path) as conn:
        latest = conn.execute("SELECT MAX(date) FROM trading_days").fetchone()[0]
        target = dt.date.fromisoformat(latest) if latest else today
        summary: list[str] = []

        try:
            rows = fetch_twse_day_trade(target)
            upsert(conn, "day_trade_stats", rows, ["date", "stock_id"])
            summary.append(f"當沖統計 {len(rows)} 檔")
        except Exception as exc:
            print(f"[daytrade] TWSE 當沖統計抓取失敗：{exc}")

        disposals = fetch_disposals()
        if disposals:
            upsert(conn, "disposal_stocks", disposals, ["stock_id", "start_date", "source"])
            summary.append(f"處置股 {len(disposals)} 檔")

        try:
            ceiling = fetch_tpex_ceiling()
            upsert(conn, "ceiling_queue", ceiling, ["date", "stock_id"])
            summary.append(f"漲停排隊 {len(ceiling)} 檔")
        except Exception as exc:
            print(f"[daytrade] TPEx 漲停排隊抓取失敗：{exc}")

    print(f"[daytrade] {target} 更新完成：{'、'.join(summary) or '無資料'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取當沖統計、處置股與漲停排隊量")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    return parser.parse_args()


def main() -> None:
    run_daytrade(Path(parse_args().db))


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# 候選池與三種風格排序
# ---------------------------------------------------------------------------
#
# 硬門檻分市場：上市用當沖比率（真的知道當沖客在不在玩），上櫃只能用日均額
# 代理。兩者不是同一個標準，payload 帶 gate_basis 讓前端據實標示。

MIN_TURNOVER_TWSE = 3.0        # 上市：日均成交額（億）
MIN_TURNOVER_TPEX = 5.0        # 上櫃：沒有當沖比率，門檻拉高補償
MIN_DAY_TRADE_PCT = 15.0       # 上市：當沖成交佔總量比率（%）
MIN_CLOSE = 10.0               # 低價股跳動一檔的成本佔比太高
MAX_CLOSE = 500.0              # 高價股單張資金效率差
TOP_N = 30


def disposal_ids(conn: sqlite3.Connection, on_date: str) -> set[str]:
    """指定日期仍在處置期間的股票。處置期間人工撮合，當沖做不了。"""
    rows = conn.execute(
        """
        SELECT stock_id FROM disposal_stocks
        WHERE start_date IS NOT NULL AND end_date IS NOT NULL
          AND start_date <= ? AND end_date >= ?
        """,
        (on_date, on_date),
    ).fetchall()
    return {row[0] for row in rows}


def day_trade_ratios(conn: sqlite3.Connection, on_date: str) -> dict[str, float]:
    """當沖成交量 ÷ 當日總成交量。TWTB4U 只給張數不給比率，所以自己算——
    用我們自己的 daily_prices.volume 當分母，不依賴對方的定義。"""
    rows = conn.execute(
        """
        SELECT d.stock_id, d.day_trade_volume, p.volume
        FROM day_trade_stats d
        JOIN daily_prices p ON p.stock_id = d.stock_id AND p.date = d.date
        WHERE d.date = ? AND d.day_trade_volume IS NOT NULL AND p.volume > 0
        """,
        (on_date,),
    ).fetchall()
    return {row[0]: round(row[1] / row[2] * 100, 2) for row in rows}


def ceiling_queues(conn: sqlite3.Connection, on_date: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT stock_id, queue_volume FROM ceiling_queue WHERE date = ? AND queue_volume > 0",
        (on_date,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def amplitude_pct(conn: sqlite3.Connection, on_date: str) -> dict[str, float]:
    """當日振幅 (high−low)/close。當沖的獲利空間直接來自振幅，
    沒有振幅的股票再有題材也做不出價差。"""
    rows = conn.execute(
        """
        SELECT stock_id, high, low, close FROM daily_prices
        WHERE date = ? AND high IS NOT NULL AND low IS NOT NULL AND close > 0
        """,
        (on_date,),
    ).fetchall()
    return {row[0]: round((row[1] - row[2]) / row[3] * 100, 2) for row in rows}


def build_candidates(
    scan_rows: list[dict[str, str]],
    changes: dict[str, float],
    amplitudes: dict[str, float],
    ratios: dict[str, float],
    queues: dict[str, float],
    blocked: set[str],
    strong_sectors: set[str],
) -> list[dict[str, Any]]:
    """套硬門檻產生候選池。門檻分市場：上市有當沖比率、上櫃只能用日均額代理，
    每列帶 gate_basis 說明自己是用哪個標準過關的。"""
    from stock_chip.trend import _csv_float

    out: list[dict[str, Any]] = []
    for row in scan_rows:
        stock_id = (row.get("stock_id") or "").strip()
        if not stock_id or stock_id in blocked:
            continue
        close = _csv_float(row.get("close"))
        turnover = _csv_float(row.get("avg_turnover_100m")) or 0
        market = (row.get("market") or "").strip()
        if close is None or not (MIN_CLOSE <= close <= MAX_CLOSE):
            continue

        ratio = ratios.get(stock_id)
        if market == "TWSE":
            if ratio is None or ratio < MIN_DAY_TRADE_PCT or turnover < MIN_TURNOVER_TWSE:
                continue
            basis = "day_trade_pct"
        else:
            # 上櫃沒有當沖統計，只能用日均額代理，門檻拉高補償
            if turnover < MIN_TURNOVER_TPEX:
                continue
            basis = "turnover_proxy"

        sub = (row.get("sub_industry") or "").strip()
        industry = (row.get("industry") or "").strip()
        group = sub or industry
        out.append(
            {
                "stock_id": stock_id,
                "name": (row.get("name") or "").strip(),
                "market": market,
                "close": close,
                "industry": industry,
                "sub_industry": sub,
                "gate_basis": basis,
                "day_trade_pct": ratio,
                "avg_turnover_100m": turnover,
                "chg_1d_pct": changes.get(stock_id),
                "amplitude_pct": amplitudes.get(stock_id),
                "volume_ratio_1d": _csv_float(row.get("volume_ratio_1d")),
                "foreign_net_lot": _csv_float(row.get("latest_foreign_net_lot")),
                "trust_net_lot": _csv_float(row.get("latest_trust_net_lot")),
                "is_day_trader_branch": bool(_csv_float(row.get("top_buy_is_day_trader"))),
                "top_buy_branch_name": (row.get("top_buy_branch_name") or "").strip(),
                "ceiling_queue_lot": round(queues[stock_id] / 1000) if stock_id in queues else None,
                "in_strong_sector": group in strong_sectors,
                "margin_short_balance_lot": _csv_float(row.get("short_balance_change_lot")),
            }
        )
    return out


def _num(value: Any) -> float:
    return value if isinstance(value, (int, float)) else 0.0


def rank_candidates(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """三種風格共用候選池，只有排序不同。同一檔在三個榜的排名會完全不同——
    這正是分開做的價值，例如隔日沖旗標在追強榜是扣分、在賣壓榜是主要條件。"""

    def momentum_score(row: dict[str, Any]) -> float:
        score = _num(row.get("volume_ratio_1d")) * 2 + _num(row.get("amplitude_pct"))
        score += _num(row.get("chg_1d_pct")) * 1.5
        if row.get("in_strong_sector"):
            score += 8
        if _num(row.get("foreign_net_lot")) + _num(row.get("trust_net_lot")) > 0:
            score += 5
        if row.get("ceiling_queue_lot"):
            score += 6                      # 漲停仍有排隊買量＝明天開盤買盤
        if row.get("is_day_trader_branch"):
            score -= 10                     # 隔日沖進駐＝明天有賣壓，追強要扣
        return score

    def reversal_score(row: dict[str, Any]) -> float:
        drop = -_num(row.get("chg_1d_pct"))
        if drop <= 0:
            return -999                     # 只收昨日下跌的
        score = drop * 2 + _num(row.get("amplitude_pct"))
        if row.get("in_strong_sector"):
            score += 6                      # 族群還在，個股跌是可能的錯殺
        if _num(row.get("foreign_net_lot")) > 0:
            score += 4                      # 跌但外資仍買
        if _num(row.get("volume_ratio_1d")) > 3:
            score -= 5                      # 爆量下跌通常還有後續賣壓
        return score

    def pressure_score(row: dict[str, Any]) -> float:
        if not row.get("is_day_trader_branch"):
            return -999                     # 這個榜就是要找隔日沖進駐的
        score = _num(row.get("volume_ratio_1d")) * 3 + _num(row.get("chg_1d_pct")) * 2
        score += _num(row.get("amplitude_pct"))
        return score

    def top(scorer, key: str) -> list[dict[str, Any]]:
        scored = [{**row, "score": round(scorer(row), 2)} for row in candidates]
        kept = [row for row in scored if row["score"] > -900]
        kept.sort(key=lambda row: row["score"], reverse=True)
        return kept[:TOP_N]

    return {
        "momentum": top(momentum_score, "momentum"),
        "reversal": top(reversal_score, "reversal"),
        "pressure": top(pressure_score, "pressure"),
    }


def build_daytrade_report(db_path: Path, scan_csv_path: Path) -> dict[str, Any]:
    """當沖候選池 payload。

    這是「盤前候選」不是「進出訊號」——整套系統的資料都是盤後的，沒有開盤價、
    沒有盤中價量。名單能幫你把 2,300 檔縮到 30 檔，之後的進出點只能靠看盤。
    """
    from stock_chip.trend import SECTOR_MIN_MEMBERS, daily_changes, load_scan_rows

    scan_rows = load_scan_rows(scan_csv_path)
    empty: dict[str, Any] = {
        "state": "insufficient",
        "trade_date": None,
        "candidate_count": 0,
        "gates": {},
        "rankings": {"momentum": [], "reversal": [], "pressure": []},
        "excluded_disposal": [],
    }
    if not scan_rows:
        return empty

    with connect_db(db_path) as conn:
        latest, _prev, changes = daily_changes(conn)
        if latest is None:
            return empty
        blocked = disposal_ids(conn, latest)
        ratios = day_trade_ratios(conn, latest)
        queues = ceiling_queues(conn, latest)
        amplitudes = amplitude_pct(conn, latest)
        disposal_rows = conn.execute(
            """
            SELECT stock_id, name, market, start_date, end_date, reason
            FROM disposal_stocks
            WHERE start_date <= ? AND end_date >= ? ORDER BY end_date
            """,
            (latest, latest),
        ).fetchall()

    # 前一日強勢族群：同一族群 ≥3 檔且平均上漲，與今日趨勢頁同一套門檻
    groups: dict[str, list[float]] = {}
    for row in scan_rows:
        stock_id = (row.get("stock_id") or "").strip()
        group = (row.get("sub_industry") or "").strip() or (row.get("industry") or "").strip()
        if group and stock_id in changes:
            groups.setdefault(group, []).append(changes[stock_id])
    strong_sectors = {
        name for name, values in groups.items()
        if len(values) >= SECTOR_MIN_MEMBERS and sum(values) / len(values) > 0
    }

    candidates = build_candidates(
        scan_rows, changes, amplitudes, ratios, queues, blocked, strong_sectors
    )
    return {
        "state": "ready" if candidates else "empty",
        "trade_date": latest,
        "candidate_count": len(candidates),
        "strong_sector_count": len(strong_sectors),
        "gates": {
            "twse": {"day_trade_pct": MIN_DAY_TRADE_PCT, "turnover_100m": MIN_TURNOVER_TWSE},
            "tpex": {"turnover_100m": MIN_TURNOVER_TPEX, "note": "上櫃無當沖統計，以日均額代理"},
            "close_range": [MIN_CLOSE, MAX_CLOSE],
        },
        "day_trade_stat_count": len(ratios),
        "rankings": rank_candidates(candidates),
        "excluded_disposal": [
            {"stock_id": r[0], "name": r[1], "market": r[2],
             "start_date": r[3], "end_date": r[4], "reason": r[5]}
            for r in disposal_rows
        ],
    }

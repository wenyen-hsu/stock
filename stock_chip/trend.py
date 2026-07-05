from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any

from stock_chip.official import connect_db


# 今日趨勢：昨日（最新交易日）強弱族群、爆量雷達與族群共振。
# 全部由既有資料計算：daily_prices 單日漲跌、scan CSV 的量比/法人/細分類。
SPIKE_VOLUME_RATIO = 3.0      # 當日量 / 20 日均量 門檻
SPIKE_MIN_TURNOVER = 0.3      # 日均成交額（億）流動性門檻
SECTOR_MIN_MEMBERS = 3        # 族群聚合最少成員數
TOP_SECTORS = 8
TOP_SPIKES = 30
CLUSTER_PEERS = 8


def _csv_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def daily_changes(conn: sqlite3.Connection) -> tuple[str | None, str | None, dict[str, float]]:
    """最新兩個交易日的單日漲跌 %。回傳 (latest, prev, {stock_id: chg_pct})。"""
    dates = [
        row[0]
        for row in conn.execute(
            "SELECT date FROM trading_days ORDER BY date DESC LIMIT 2"
        ).fetchall()
    ]
    if len(dates) < 2:
        return (dates[0] if dates else None), None, {}
    latest, prev = dates
    rows = conn.execute(
        """
        SELECT a.stock_id, a.close, b.close
        FROM daily_prices a
        JOIN daily_prices b ON b.stock_id = a.stock_id AND b.date = ?
        WHERE a.date = ? AND a.close IS NOT NULL AND b.close IS NOT NULL AND b.close != 0
        """,
        (prev, latest),
    ).fetchall()
    return latest, prev, {row[0]: round((row[1] - row[2]) / row[2] * 100, 2) for row in rows}


def latest_inst_net(conn: sqlite3.Connection, trade_date: str) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT stock_id, COALESCE(foreign_net, 0) + COALESCE(trust_net, 0)
        FROM institutional_trades
        WHERE date = ?
        """,
        (trade_date,),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def load_scan_rows(scan_csv_path: Path) -> list[dict[str, str]]:
    if not scan_csv_path.exists():
        return []
    with scan_csv_path.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def build_trend_report(db_path: Path, scan_csv_path: Path, days: int) -> dict[str, Any]:
    scan_rows = load_scan_rows(scan_csv_path)
    with connect_db(db_path) as conn:
        latest, prev, changes = daily_changes(conn)
        inst_net = latest_inst_net(conn, latest) if latest else {}
    if latest is None or prev is None or not changes:
        return {
            "days": days,
            "state": "insufficient",
            "trade_date": latest,
            "prev_date": prev,
            "strong_sectors": [],
            "weak_sectors": [],
            "volume_spikes": [],
            "clusters": [],
        }

    by_stock: dict[str, dict[str, Any]] = {}
    for row in scan_rows:
        stock_id = (row.get("stock_id") or "").strip()
        if not stock_id:
            continue
        by_stock[stock_id] = row

    # ---- 族群聚合（細分類優先，無細分類者用產業別） ----
    sectors: dict[str, dict[str, Any]] = {}
    for stock_id, chg in changes.items():
        row = by_stock.get(stock_id)
        if row is None:
            continue
        sub = (row.get("sub_industry") or "").strip()
        industry = (row.get("industry") or "").strip()
        group = sub or industry
        if not group:
            continue
        bucket = sectors.setdefault(
            group,
            {
                "name": group,
                "is_sub": bool(sub),
                "industry_votes": {},
                "changes": [],
                "up": 0,
                "inst_net_lot": 0.0,
                "scores": [],
            },
        )
        bucket["changes"].append(chg)
        if chg > 0:
            bucket["up"] += 1
        bucket["inst_net_lot"] += (inst_net.get(stock_id) or 0) / 1000
        if industry:
            bucket["industry_votes"][industry] = bucket["industry_votes"].get(industry, 0) + 1
        score = _csv_float(row.get("multifactor_score"))
        if score is not None:
            bucket["scores"].append(score)

    aggregated = []
    for bucket in sectors.values():
        count = len(bucket["changes"])
        if count < SECTOR_MIN_MEMBERS:
            continue
        votes = bucket["industry_votes"]
        aggregated.append(
            {
                "name": bucket["name"],
                "is_sub": bucket["is_sub"],
                "industry": max(votes, key=votes.get) if votes else "",
                "member_count": count,
                "avg_chg_1d_pct": round(sum(bucket["changes"]) / count, 2),
                "up_count": bucket["up"],
                "up_ratio_pct": round(bucket["up"] / count * 100, 1),
                "inst_net_lot": round(bucket["inst_net_lot"]),
                "avg_multifactor": round(sum(bucket["scores"]) / len(bucket["scores"]), 2) if bucket["scores"] else None,
            }
        )
    aggregated.sort(key=lambda item: item["avg_chg_1d_pct"], reverse=True)
    strong = [item for item in aggregated if item["avg_chg_1d_pct"] > 0][:TOP_SECTORS]
    weak = sorted(
        [item for item in aggregated if item["avg_chg_1d_pct"] < 0],
        key=lambda item: item["avg_chg_1d_pct"],
    )[:TOP_SECTORS]

    # ---- 爆量雷達 ----
    spikes = []
    for stock_id, row in by_stock.items():
        ratio = _csv_float(row.get("volume_ratio_1d"))
        turnover = _csv_float(row.get("avg_turnover_100m"))
        if ratio is None or ratio < SPIKE_VOLUME_RATIO:
            continue
        if turnover is None or turnover < SPIKE_MIN_TURNOVER:
            continue
        spikes.append(
            {
                "stock_id": stock_id,
                "name": row.get("name"),
                "volume_ratio_1d": ratio,
                "chg_1d_pct": changes.get(stock_id),
                "latest_volume_lot": _csv_float(row.get("latest_volume_lot")),
                "foreign_net_lot": _csv_float(row.get("latest_foreign_net_lot")),
                "trust_net_lot": _csv_float(row.get("latest_trust_net_lot")),
                "industry": row.get("industry") or "",
                "sub_industry": row.get("sub_industry") or "",
                "multifactor_score": _csv_float(row.get("multifactor_score")),
            }
        )
    spikes.sort(key=lambda item: item["volume_ratio_1d"], reverse=True)
    spikes = spikes[:TOP_SPIKES]

    # ---- 族群共振：同細分類 ≥2 檔爆量 ----
    spike_groups: dict[str, list[dict[str, Any]]] = {}
    for spike in spikes:
        group = spike["sub_industry"] or spike["industry"]
        if group:
            spike_groups.setdefault(group, []).append(spike)
    clusters = []
    for group, members in spike_groups.items():
        if len(members) < 2:
            continue
        member_ids = {member["stock_id"] for member in members}
        peers = [
            {
                "stock_id": stock_id,
                "name": row.get("name"),
                "chg_1d_pct": changes.get(stock_id),
                "multifactor_score": _csv_float(row.get("multifactor_score")),
            }
            for stock_id, row in by_stock.items()
            if stock_id not in member_ids
            and ((row.get("sub_industry") or "").strip() == group or (not (row.get("sub_industry") or "").strip() and (row.get("industry") or "").strip() == group))
        ]
        peers.sort(key=lambda item: item["multifactor_score"] or -999, reverse=True)
        clusters.append(
            {
                "name": group,
                "spike_members": members,
                "peers": peers[:CLUSTER_PEERS],
            }
        )
    clusters.sort(key=lambda item: len(item["spike_members"]), reverse=True)

    return {
        "days": days,
        "state": "ready",
        "trade_date": latest,
        "prev_date": prev,
        "strong_sectors": strong,
        "weak_sectors": weak,
        "volume_spikes": spikes,
        "clusters": clusters,
    }

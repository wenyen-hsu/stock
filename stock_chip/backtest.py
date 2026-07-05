from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path
from typing import Any

from stock_chip.official import connect_db


# 回測：對每日排行快照計算前 N 名的後續報酬與對大盤超額報酬，
# 驗證各分數的預測力。快照自 run_scan 每日寫入，需累積數週才有結論。
DEFAULT_HORIZONS = (5, 20, 60)
DEFAULT_TOP_K = 10
READY_MIN_SNAPSHOTS = 10
SNAPSHOT_CSV_KEEP_DAYS = 400  # 匯出 CSV 保留的交易日數（防 actions cache 被逐出）


def all_trading_dates(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("SELECT date FROM trading_days ORDER BY date").fetchall()]


def taiex_closes(conn: sqlite3.Connection) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT date, close FROM market_index_daily
        WHERE index_code = 'TAIEX' AND close IS NOT NULL
        """
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def load_snapshots(conn: sqlite3.Connection, top_k: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT snapshot_date, days, ranking_name, rank_no, stock_id, score, close
        FROM ranking_snapshots
        WHERE rank_no <= ?
          AND close IS NOT NULL
        ORDER BY snapshot_date
        """,
        (top_k,),
    ).fetchall()
    columns = ["snapshot_date", "days", "ranking_name", "rank_no", "stock_id", "score", "close"]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def close_lookup(conn: sqlite3.Connection, stock_ids: set[str]) -> dict[str, dict[str, float]]:
    """stock_id -> {date: close}，只抓快照涉及的股票。"""
    output: dict[str, dict[str, float]] = {}
    ids = sorted(stock_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT stock_id, date, close FROM daily_prices
            WHERE stock_id IN ({placeholders}) AND close IS NOT NULL
            """,
            chunk,
        ).fetchall()
        for stock_id, date, close in rows:
            output.setdefault(stock_id, {})[date] = close
    return output


def forward_price_date(dates: list[str], date_index: dict[str, int], snapshot_date: str, horizon: int) -> str | None:
    idx = date_index.get(snapshot_date)
    if idx is None or idx + horizon >= len(dates):
        return None
    return dates[idx + horizon]


def compute_backtest(
    db_path: Path,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    with connect_db(db_path) as conn:
        dates = all_trading_dates(conn)
        date_index = {date: idx for idx, date in enumerate(dates)}
        taiex = taiex_closes(conn)
        snapshots = load_snapshots(conn, top_k)
        closes = close_lookup(conn, {snap["stock_id"] for snap in snapshots})
        distinct_dates = conn.execute(
            "SELECT COUNT(DISTINCT snapshot_date), MIN(snapshot_date), MAX(snapshot_date) FROM ranking_snapshots"
        ).fetchone()

    # (ranking_name, days, horizon) -> list of (return_pct, excess_pct | None)
    buckets: dict[tuple[str, int, int], list[tuple[float, float | None]]] = {}
    for snap in snapshots:
        entry_close = snap["close"]
        stock_closes = closes.get(snap["stock_id"], {})
        for horizon in horizons:
            exit_date = forward_price_date(dates, date_index, snap["snapshot_date"], horizon)
            if exit_date is None:
                continue
            exit_close = stock_closes.get(exit_date)
            if exit_close is None or not entry_close:
                continue
            ret_pct = (exit_close - entry_close) / entry_close * 100
            taiex_entry = taiex.get(snap["snapshot_date"])
            taiex_exit = taiex.get(exit_date)
            excess_pct = None
            if taiex_entry and taiex_exit:
                excess_pct = ret_pct - (taiex_exit - taiex_entry) / taiex_entry * 100
            buckets.setdefault((snap["ranking_name"], snap["days"], horizon), []).append((ret_pct, excess_pct))

    results: list[dict[str, Any]] = []
    for (name, days, horizon), values in sorted(buckets.items()):
        returns = [ret for ret, _ in values]
        excesses = [exc for _, exc in values if exc is not None]
        effective = excesses if excesses else returns
        wins = sum(1 for value in effective if value > 0)
        sorted_eff = sorted(effective)
        median = sorted_eff[len(sorted_eff) // 2] if sorted_eff else None
        results.append(
            {
                "ranking_name": name,
                "days": days,
                "horizon": horizon,
                "n_obs": len(returns),
                "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
                "avg_excess_pct": round(sum(excesses) / len(excesses), 2) if excesses else None,
                "win_rate_pct": round(wins / len(effective) * 100, 1) if effective else None,
                "median_excess_pct": round(median, 2) if median is not None else None,
                "benchmark": "TAIEX" if excesses else "raw",
                "state": "ready" if len(returns) >= READY_MIN_SNAPSHOTS * min(top_k, 10) else "accumulating",
            }
        )

    return {
        "top_k": top_k,
        "horizons": list(horizons),
        "snapshot_days": distinct_dates[0] or 0,
        "oldest_snapshot": distinct_dates[1],
        "latest_snapshot": distinct_dates[2],
        "results": results,
    }


def backtest_payload(db_path: Path) -> dict[str, Any]:
    try:
        return compute_backtest(db_path)
    except sqlite3.OperationalError:
        return {"top_k": DEFAULT_TOP_K, "horizons": list(DEFAULT_HORIZONS), "snapshot_days": 0, "results": []}


def export_snapshots_csv(db_path: Path, csv_path: Path, keep_days: int = SNAPSHOT_CSV_KEEP_DAYS) -> int:
    """把快照表 dump 到 reports/，隨每日發布 commit 到 main。
    SQLite 活在 actions cache，可能被逐出；這份 CSV 是回測歷史的持久備份。"""
    with connect_db(db_path) as conn:
        cutoff_row = conn.execute(
            """
            SELECT MIN(snapshot_date) FROM (
                SELECT DISTINCT snapshot_date FROM ranking_snapshots
                ORDER BY snapshot_date DESC LIMIT ?
            )
            """,
            (keep_days,),
        ).fetchone()
        cutoff = cutoff_row[0] if cutoff_row and cutoff_row[0] else "0000-00-00"
        rows = conn.execute(
            """
            SELECT snapshot_date, days, ranking_name, rank_no, stock_id, score, total_score, close, updated_at
            FROM ranking_snapshots
            WHERE snapshot_date >= ?
            ORDER BY snapshot_date, days, ranking_name, rank_no
            """,
            (cutoff,),
        ).fetchall()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["snapshot_date", "days", "ranking_name", "rank_no", "stock_id", "score", "total_score", "close", "updated_at"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)
    return len(rows)


def import_snapshots_csv(db_path: Path, csv_path: Path) -> int:
    """把 CSV 備份中資料庫沒有的快照回灌（cache 被逐出後的復原路徑）。"""
    if not csv_path.exists():
        return 0
    with csv_path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 0
    with connect_db(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM ranking_snapshots").fetchone()[0]
        conn.executemany(
            """
            INSERT OR IGNORE INTO ranking_snapshots (
                snapshot_date, days, ranking_name, rank_no, stock_id,
                score, total_score, close, updated_at
            )
            VALUES (:snapshot_date, :days, :ranking_name, :rank_no, :stock_id,
                    :score, :total_score, :close, :updated_at)
            """,
            [
                {key: (row[key] or None) for key in row}
                for row in rows
            ],
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM ranking_snapshots").fetchone()[0]
    return after - before


DIGEST_RANKINGS = ("multifactor_score", "big_holder_increase", "momentum_inst_buy")
DIGEST_RANKING_LABELS = {
    "multifactor_score": "多因子綜合分",
    "big_holder_increase": "大戶增持 + 法人買超",
    "momentum_inst_buy": "動能 + 法人買超",
}


def _csv_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def build_daily_digest(db_path: Path, scan_csv_path: Path, days: int, top_n: int = 20) -> dict[str, Any]:
    """今日訊號變化摘要：排行新進榜（快照比對）＋當日訊號觸發清單。

    新進榜需要至少兩天快照；不足時 state=accumulating、只出訊號清單。"""
    with connect_db(db_path) as conn:
        snapshot_dates = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT snapshot_date FROM ranking_snapshots WHERE days = ? ORDER BY snapshot_date DESC LIMIT 2",
                (days,),
            ).fetchall()
        ]
        new_entrants: dict[str, list[dict[str, Any]]] = {}
        if len(snapshot_dates) == 2:
            latest_date, prev_date = snapshot_dates
            names = {
                row[0]: row[1]
                for row in conn.execute("SELECT stock_id, name FROM stocks").fetchall()
            }
            for ranking in DIGEST_RANKINGS:
                latest_rows = conn.execute(
                    """
                    SELECT rank_no, stock_id, score FROM ranking_snapshots
                    WHERE snapshot_date = ? AND days = ? AND ranking_name = ? AND rank_no <= ?
                    ORDER BY rank_no
                    """,
                    (latest_date, days, ranking, top_n),
                ).fetchall()
                prev_ids = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT stock_id FROM ranking_snapshots
                        WHERE snapshot_date = ? AND days = ? AND ranking_name = ? AND rank_no <= ?
                        """,
                        (prev_date, days, ranking, top_n),
                    ).fetchall()
                }
                entrants = [
                    {
                        "rank_no": row[0],
                        "stock_id": row[1],
                        "name": names.get(row[1], row[1]),
                        "score": round(row[2], 2) if row[2] is not None else None,
                    }
                    for row in latest_rows
                    if row[1] not in prev_ids
                ]
                if entrants:
                    new_entrants[ranking] = entrants

    signals: dict[str, list[dict[str, Any]]] = {
        "big_holder": [],
        "branch_streak": [],
        "margin_up": [],
        "margin_down": [],
        "day_trader": [],
    }
    scan_rows: list[dict[str, str]] = []
    if scan_csv_path.exists():
        with scan_csv_path.open(encoding="utf-8-sig") as f:
            scan_rows = list(csv.DictReader(f))
    ranked = sorted(scan_rows, key=lambda r: _csv_float(r.get("multifactor_score")) or -999, reverse=True)
    for idx, row in enumerate(ranked):
        stock = {"stock_id": row.get("stock_id"), "name": row.get("name")}
        change = _csv_float(row.get("big_holder_change_1w"))
        if change is not None and change >= 0.3 and len(signals["big_holder"]) < 10:
            signals["big_holder"].append({**stock, "value": change})
        streak = _csv_float(row.get("branch_buy_streak"))
        is_day_trader = (_csv_float(row.get("top_buy_is_day_trader")) or 0) >= 1
        if streak is not None and streak >= 3 and not is_day_trader and len(signals["branch_streak"]) < 10:
            signals["branch_streak"].append({**stock, "value": int(streak), "broker": row.get("branch_streak_broker") or ""})
        margin_streak = _csv_float(row.get("gross_margin_streak"))
        if margin_streak is not None and margin_streak >= 2 and len(signals["margin_up"]) < 10:
            signals["margin_up"].append({**stock, "value": int(margin_streak)})
        if margin_streak is not None and margin_streak <= -2 and len(signals["margin_down"]) < 10:
            signals["margin_down"].append({**stock, "value": int(margin_streak)})
        if is_day_trader and idx < 100 and len(signals["day_trader"]) < 10:
            signals["day_trader"].append({**stock, "broker": row.get("top_buy_branch_name") or ""})

    return {
        "days": days,
        "state": "ready" if len(snapshot_dates) == 2 else "accumulating",
        "snapshot_dates": snapshot_dates,
        "ranking_labels": DIGEST_RANKING_LABELS,
        "new_entrants": new_entrants,
        "signals": signals,
    }


def write_summary_csv(path: Path, results: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "ranking_name", "days", "horizon", "n_obs", "avg_return_pct",
        "avg_excess_pct", "win_rate_pct", "median_excess_pct", "benchmark", "state",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(results)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute forward returns for daily ranking snapshots.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--horizons", default="5,20,60")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_K)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db)
    output_dir = Path(args.output_dir)
    horizons = tuple(int(value) for value in args.horizons.split(",") if value.strip())

    restored = import_snapshots_csv(db_path, output_dir / "ranking_snapshots.csv")
    if restored:
        print(f"restored {restored} snapshot rows from CSV backup")

    payload = compute_backtest(db_path, horizons=horizons, top_k=args.top)
    write_summary_csv(output_dir / "backtest_summary.csv", payload["results"])
    exported = export_snapshots_csv(db_path, output_dir / "ranking_snapshots.csv")

    print(f"snapshot days: {payload['snapshot_days']} ({payload['oldest_snapshot']} ~ {payload['latest_snapshot']})")
    print(f"snapshot rows exported to CSV: {exported}")
    if not payload["results"]:
        print("回測累積中：尚無可評估的快照（需要至少一個 horizon 天數的後續行情）")
        return
    print(f"buckets: {len(payload['results'])}")
    for row in payload["results"][:10]:
        print(
            f"  {row['ranking_name']} {row['days']}d +{row['horizon']}d: "
            f"n={row['n_obs']} avg={row['avg_return_pct']}% excess={row['avg_excess_pct']}% win={row['win_rate_pct']}% [{row['state']}]"
        )


if __name__ == "__main__":
    main()

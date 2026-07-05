"""今日趨勢 builder：強弱族群聚合、爆量門檻、族群共振與資料不足狀態。"""
import csv
import tempfile
from pathlib import Path

from stock_chip.official import connect_db
from stock_chip.trend import build_trend_report


def make_env(two_days: bool = True) -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "t.sqlite"
    dates = ["2026-07-02", "2026-07-03"] if two_days else ["2026-07-03"]
    stocks = {
        # (名稱, 產業, 細分類, 前日收盤, 當日收盤, 量比, 日均額)
        "1111": ("熱一", "電子", "散熱", 100.0, 105.0, 4.0, 1.0),
        "2222": ("熱二", "電機", "散熱", 50.0, 53.0, 3.5, 0.8),
        "3333": ("熱三", "電子", "散熱", 80.0, 81.0, 1.2, 0.9),
        "4444": ("航運一", "航運", "貨櫃航運", 30.0, 28.5, 1.0, 2.0),
        "5555": ("航運二", "航運", "貨櫃航運", 60.0, 57.0, 1.1, 2.0),
        "6666": ("航運三", "航運", "貨櫃航運", 90.0, 88.0, 0.9, 2.0),
        "7777": ("低量爆", "其他", "", 10.0, 11.0, 5.0, 0.1),  # 量比夠但流動性不足
    }
    with connect_db(db) as conn:
        for d in dates:
            conn.execute(
                "INSERT INTO trading_days(date, price_count, institutional_count, updated_at) VALUES (?,1,1,'now')",
                (d,),
            )
        for sid, (name, _ind, _sub, prev_close, close, _r, _t) in stocks.items():
            if two_days:
                conn.execute(
                    "INSERT INTO daily_prices(date, stock_id, name, close, updated_at) VALUES ('2026-07-02',?,?,?,'now')",
                    (sid, name, prev_close),
                )
            conn.execute(
                "INSERT INTO daily_prices(date, stock_id, name, close, updated_at) VALUES ('2026-07-03',?,?,?,'now')",
                (sid, name, close),
            )
            conn.execute(
                "INSERT INTO institutional_trades(date, stock_id, name, foreign_net, trust_net, updated_at) VALUES ('2026-07-03',?,?,1000,0,'now')",
                (sid, name),
            )
        conn.commit()

    scan_csv = tmp / "scan_all_20d.csv"
    with scan_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "stock_id", "name", "industry", "sub_industry", "multifactor_score",
                "volume_ratio_1d", "avg_turnover_100m", "latest_volume_lot",
                "latest_foreign_net_lot", "latest_trust_net_lot",
            ],
        )
        writer.writeheader()
        for sid, (name, ind, sub, _p, _c, ratio, turnover) in stocks.items():
            writer.writerow(
                {
                    "stock_id": sid, "name": name, "industry": ind, "sub_industry": sub,
                    "multifactor_score": "50", "volume_ratio_1d": str(ratio),
                    "avg_turnover_100m": str(turnover), "latest_volume_lot": "1000",
                    "latest_foreign_net_lot": "1", "latest_trust_net_lot": "0",
                }
            )
    return db, scan_csv


def test_trend_sectors_and_spikes():
    db, scan_csv = make_env()
    report = build_trend_report(db, scan_csv, 20)
    assert report["state"] == "ready"
    assert report["trade_date"] == "2026-07-03" and report["prev_date"] == "2026-07-02"
    # 散熱平均 +(5% + 6% + 1.25%)/3 > 0，最強；貨櫃航運全跌，最弱
    assert report["strong_sectors"][0]["name"] == "散熱"
    assert report["strong_sectors"][0]["up_count"] == 3
    weak_names = [sector["name"] for sector in report["weak_sectors"]]
    assert "貨櫃航運" in weak_names
    # 爆量：1111/2222 入選；3333 量比不足、7777 流動性不足
    spike_ids = [spike["stock_id"] for spike in report["volume_spikes"]]
    assert spike_ids == ["1111", "2222"]
    assert report["volume_spikes"][0]["chg_1d_pct"] == 5.0


def test_trend_cluster_groups_peers():
    db, scan_csv = make_env()
    report = build_trend_report(db, scan_csv, 20)
    clusters = report["clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["name"] == "散熱"
    assert {member["stock_id"] for member in cluster["spike_members"]} == {"1111", "2222"}
    # 同族群未爆量成員 3333 進 peers
    assert [peer["stock_id"] for peer in cluster["peers"]] == ["3333"]


def test_trend_insufficient_single_day():
    db, scan_csv = make_env(two_days=False)
    report = build_trend_report(db, scan_csv, 20)
    assert report["state"] == "insufficient"
    assert report["volume_spikes"] == []

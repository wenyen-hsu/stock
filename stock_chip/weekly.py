from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from stock_chip.dividends import parse_roc_date
from stock_chip.etf import load_etf_flows
from stock_chip.official import connect_db
from stock_chip.trend import _csv_float, load_scan_rows

# 週報：上週表現、資金主要流向、下週事件行事曆。
#
# 其他分頁都是「當日」視角（今日趨勢看昨日族群、排行看區間累計），
# 缺一個週的節奏。本模組全部由已入庫資料計算，不新增任何抓取步驟。
#
# 週界線用「最近一個完整交易週」而非滾動 5 日：週三打開看到的仍是上週，
# 一週只變一次，符合週報語感。代價是不能沿用 scan_all_5d.csv
# （那是滾動最近 5 個交易日，週三跑會橫跨兩週），漲跌與法人買賣超
# 得對固定日期區間另外下 SQL。

LIQUID_TURNOVER_100M = 0.5    # 主流強勢的日均成交額門檻（億）
TOP_GAINERS = 20              # 主流強勢取前 N
TOP_SPECULATIVE = 10          # 投機飆股取前 N
TOP_LOSERS = 10
TOP_SECTORS = 8
TOP_FLOW_STOCKS = 15
TOP_ETF = 10
SECTOR_MIN_MEMBERS = 3        # 與 trend.py 同門檻
NEWS_PER_STOCK = 3
NEWS_STOCK_LIMIT = 20


def last_complete_week(
    conn: sqlite3.Connection, today: "dt.date | None" = None
) -> tuple[str, str, int] | None:
    """最近一個「已收完」的交易週，回傳 (首個交易日, 末個交易日, 交易日數)。

    以 ISO 週分組而非固定往回數 5 天：連假造成的短週（例如只有 3 個交易日）
    仍算完整的一週，只是 trading_day_count 會較少，由前端據實說明。
    ISO 週也自然處理跨年（2025-12-29 與 2026-01-02 同屬 ISO 2026-W01）。

    「已收完」有兩個判準，缺一不可：
      (a) 資料裡已出現更晚的 ISO 週 —— 週間（例如週三）用得到；
      (b) 日曆已走過那一週的星期五 —— 週六早上資料只到週五、沒有下一週交易日時用得到。
    只用 (a) 會讓週末讀到的是上上週，正好是最該準的時候；
    只用 (b) 則會在週五盤後、當週資料還沒進站時就把半週當成完整週。

    (b) 比的是該週的星期五而不是整個 ISO 週：ISO 週到星期日才結束，
    但週六早上那個 Mon-Fri 交易週其實已經收完了。
    """
    import datetime as dt

    dates = [
        row[0]
        for row in conn.execute("SELECT date FROM trading_days ORDER BY date").fetchall()
    ]
    if not dates:
        return None
    weeks: dict[tuple[int, int], list[str]] = {}
    for value in dates:
        try:
            key = dt.date.fromisoformat(value).isocalendar()[:2]
        except ValueError:
            continue
        weeks.setdefault(key, []).append(value)
    if not weeks:
        return None
    today = today or dt.date.today()
    ordered = sorted(weeks)
    for index in range(len(ordered) - 1, -1, -1):
        key = ordered[index]
        has_later_week = index < len(ordered) - 1
        friday = dt.date.fromisocalendar(key[0], key[1], 5)
        calendar_moved_on = today > friday
        if has_later_week or calendar_moved_on:
            target = weeks[key]
            return target[0], target[-1], len(target)
    return None


def week_returns(conn: sqlite3.Connection, start: str, end: str) -> dict[str, dict[str, Any]]:
    """區間報酬與區間法人買賣超。

    報酬用「start 當日收盤 → end 當日收盤」，等同持有整週。
    法人買賣超含 start 當日（週一買的也算這週的錢）。
    """
    # 區間量能與法人買賣超先各自 GROUP BY 聚合再 join，而不是每檔跑三個
    # correlated subquery。daily_prices 的 PK 是 (date, stock_id)，日期區間
    # 掃描走 PK 一次即可，不必逐檔 seek。
    rows = conn.execute(
        """
        WITH week_volume AS (
            SELECT stock_id, SUM(volume) AS volume
            FROM daily_prices WHERE date BETWEEN ? AND ? GROUP BY stock_id
        ),
        week_inst AS (
            SELECT stock_id,
                   SUM(COALESCE(foreign_net, 0)) AS foreign_net,
                   SUM(COALESCE(trust_net, 0))   AS trust_net
            FROM institutional_trades WHERE date BETWEEN ? AND ? GROUP BY stock_id
        )
        SELECT
            a.stock_id,
            COALESCE(s.name, a.name)   AS name,
            COALESCE(s.market, '')     AS market,
            b.close                    AS start_close,
            a.close                    AS end_close,
            week_volume.volume,
            week_inst.foreign_net,
            week_inst.trust_net
        FROM daily_prices a
        JOIN daily_prices b ON b.stock_id = a.stock_id AND b.date = ?
        LEFT JOIN stocks s ON s.stock_id = a.stock_id
        LEFT JOIN week_volume ON week_volume.stock_id = a.stock_id
        LEFT JOIN week_inst ON week_inst.stock_id = a.stock_id
        WHERE a.date = ?
          AND a.close IS NOT NULL AND b.close IS NOT NULL AND b.close != 0
        """,
        (start, end, start, end, start, end),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for stock_id, name, market, start_close, end_close, volume, foreign_net, trust_net in rows:
        out[stock_id] = {
            "stock_id": stock_id,
            "name": name or "",
            "market": market or "",
            "start_close": start_close,
            "close": end_close,
            "week_return_pct": round((end_close - start_close) / start_close * 100, 2),
            "week_volume_lot": round((volume or 0) / 1000),
            "foreign_net_lot": round((foreign_net or 0) / 1000),
            "trust_net_lot": round((trust_net or 0) / 1000),
        }
    return out


def merge_scan_fields(perf: dict[str, dict[str, Any]], scan_rows: list[dict[str, str]]) -> None:
    """把產業別、細分類、多因子分、日均額從 scan CSV 併進週表現。"""
    by_stock = {(row.get("stock_id") or "").strip(): row for row in scan_rows}
    for stock_id, item in perf.items():
        row = by_stock.get(stock_id)
        if row is None:
            item.setdefault("industry", "")
            item.setdefault("sub_industry", "")
            item.setdefault("avg_turnover_100m", None)
            item.setdefault("multifactor_score", None)
            continue
        item["industry"] = (row.get("industry") or "").strip()
        item["sub_industry"] = (row.get("sub_industry") or "").strip()
        item["avg_turnover_100m"] = _csv_float(row.get("avg_turnover_100m"))
        item["multifactor_score"] = _csv_float(row.get("multifactor_score"))


def split_gainers(perf: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """漲幅榜分流：主流強勢 vs 投機飆股。

    純漲幅榜會被小型投機股佔滿——實測近 5 日漲幅前 10 名有 8 檔是低量小型股
    （長亨 +62.94% 而外資買超 0 張）。把「有量且法人有買」與「無量無法人」
    分成兩欄，讀者才不會把飆股誤當成主流資金認同的標的。
    """
    rows = sorted(perf.values(), key=lambda item: item["week_return_pct"], reverse=True)

    def liquid(item: dict[str, Any]) -> bool:
        return (item.get("avg_turnover_100m") or 0) >= LIQUID_TURNOVER_100M

    def inst_backed(item: dict[str, Any]) -> bool:
        return (item["foreign_net_lot"] + item["trust_net_lot"]) > 0

    gainers = [item for item in rows if item["week_return_pct"] > 0]
    mainstream = [item for item in gainers if liquid(item) and inst_backed(item)][:TOP_GAINERS]
    speculative = [item for item in gainers if not (liquid(item) and inst_backed(item))][:TOP_SPECULATIVE]
    losers = [
        item
        for item in sorted(perf.values(), key=lambda item: item["week_return_pct"])
        if item["week_return_pct"] < 0 and liquid(item)
    ][:TOP_LOSERS]
    return {"mainstream": mainstream, "speculative": speculative, "losers": losers}


def sector_flows(perf: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """族群週表現與週法人買賣超（細分類優先，無細分類者用產業別）。"""
    sectors: dict[str, dict[str, Any]] = {}
    for item in perf.values():
        group = item.get("sub_industry") or item.get("industry")
        if not group:
            continue
        bucket = sectors.setdefault(
            group,
            {"name": group, "is_sub": bool(item.get("sub_industry")), "returns": [],
             "up": 0, "inst_net_lot": 0.0, "industry_votes": {}},
        )
        bucket["returns"].append(item["week_return_pct"])
        if item["week_return_pct"] > 0:
            bucket["up"] += 1
        bucket["inst_net_lot"] += item["foreign_net_lot"] + item["trust_net_lot"]
        industry = item.get("industry")
        if industry:
            bucket["industry_votes"][industry] = bucket["industry_votes"].get(industry, 0) + 1

    aggregated = []
    for bucket in sectors.values():
        count = len(bucket["returns"])
        if count < SECTOR_MIN_MEMBERS:
            continue
        votes = bucket["industry_votes"]
        aggregated.append(
            {
                "name": bucket["name"],
                "is_sub": bucket["is_sub"],
                "industry": max(votes, key=votes.get) if votes else "",
                "member_count": count,
                "avg_return_pct": round(sum(bucket["returns"]) / count, 2),
                "up_count": bucket["up"],
                "up_ratio_pct": round(bucket["up"] / count * 100, 1),
                "inst_net_lot": round(bucket["inst_net_lot"]),
            }
        )
    inflow = sorted(aggregated, key=lambda item: item["inst_net_lot"], reverse=True)
    outflow = sorted(aggregated, key=lambda item: item["inst_net_lot"])
    return {
        "inflow": [item for item in inflow if item["inst_net_lot"] > 0][:TOP_SECTORS],
        "outflow": [item for item in outflow if item["inst_net_lot"] < 0][:TOP_SECTORS],
    }


def market_flow(conn: sqlite3.Connection, start: str, end: str) -> dict[str, Any]:
    """大盤層級的資金流向：TAIEX 週漲跌、外資期貨未平倉週變、全市場融資餘額週變。"""

    def index_close(date: str) -> float | None:
        row = conn.execute(
            "SELECT close FROM market_index_daily WHERE index_code = 'TAIEX' AND date = ? LIMIT 1",
            (date,),
        ).fetchone()
        return row[0] if row and row[0] else None

    start_index, end_index = index_close(start), index_close(end)
    taiex_pct = (
        round((end_index - start_index) / start_index * 100, 2)
        if start_index and end_index
        else None
    )

    def foreign_oi(date: str) -> float | None:
        row = conn.execute(
            """
            SELECT SUM(oi_net) FROM futures_institution_oi
            WHERE date = ? AND institution = 'foreign' AND product_code = 'TXF'
            """,
            (date,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    start_oi, end_oi = foreign_oi(start), foreign_oi(end)

    def margin_total(date: str) -> float | None:
        row = conn.execute(
            "SELECT SUM(margin_balance) FROM margin_trades WHERE date = ?", (date,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    start_margin, end_margin = margin_total(start), margin_total(end)

    return {
        "taiex_start": start_index,
        "taiex_end": end_index,
        "taiex_return_pct": taiex_pct,
        "foreign_futures_oi_start": start_oi,
        "foreign_futures_oi_end": end_oi,
        "foreign_futures_oi_change": (
            round(end_oi - start_oi) if start_oi is not None and end_oi is not None else None
        ),
        "margin_balance_start_lot": round(start_margin / 1000) if start_margin else None,
        "margin_balance_end_lot": round(end_margin / 1000) if end_margin else None,
        "margin_balance_change_lot": (
            round((end_margin - start_margin) / 1000)
            if start_margin is not None and end_margin is not None
            else None
        ),
    }


# 只抽「具名欄位」而不是全文抓日期。實測 naive 全文正則在未來 7 天可得 1315 筆，
# 但把「盈利警告」「停工函」誤歸成併購事件；改用欄位名錨定後降到 981 筆且幾乎沒有誤判。
# 每筆是 (事件類型, 正則, SQL LIKE 用的字面子字串)。第三項不能從正則推導——
# 「(?:普通股)?現金股利發放日期」裡的可選群組無法直接餵給 LIKE，取共同字面即可。
UPCOMING_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("法說會", r"召開法人說明會之日期", "召開法人說明會之日期"),
    ("財報董事會", r"董事會預計召開日期", "董事會預計召開日期"),
    ("除權息交易日", r"除權（息）交易日", "除權（息）交易日"),
    ("除權息基準日", r"除權（息）基準日", "除權（息）基準日"),
    ("股利發放日", r"(?:普通股)?現金股利發放日期", "現金股利發放日期"),
    ("現增認股基準日", r"現金增資認股基準日", "現金增資認股基準日"),
)
_FIELD_PATTERNS = tuple(
    (label, re.compile(pattern + r"[:：]\s*(\S{0,14})"))
    for label, pattern, _like in UPCOMING_FIELDS
)
FIELD_LIKE_TERMS = tuple(like for _label, _pattern, like in UPCOMING_FIELDS)
# 事件重要度：同一天太多筆時優先顯示法說會與財報，除權息類次之
EVENT_PRIORITY = {"法說會": 0, "財報董事會": 1, "除權息交易日": 2, "現增認股基準日": 3,
                  "除權息基準日": 4, "股利發放日": 5}


def extract_event_dates(detail: str) -> list[tuple[str, str]]:
    """從 MOPS 公告內文抽出 (事件類型, ISO 日期)。

    MOPS 重大訊息的 detail 是編號欄位格式：
    `1.召開法人說明會之日期:115/08/07 2.時間:14 時 00 分 3.地點:...`
    """
    found: list[tuple[str, str]] = []
    for label, pattern in _FIELD_PATTERNS:
        match = pattern.search(detail or "")
        if not match:
            continue
        iso = parse_roc_date(match.group(1))
        if iso:
            found.append((label, iso))
    return found


def upcoming_events(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    """下週事件行事曆：MOPS 公告內文的未來日期 + 除權息預告表。

    mops_events.event_date 是「發言日」不是事件日，所以未來事件只能從內文取；
    這也是現有 MOPS 頁「未來 30 天」篩選一直是空的原因。

    兩個來源各自檢查資料表是否存在：早期的資料庫可能只有其中一張，
    用單一來源的存在與否 gate 整個函式會讓行事曆被錯誤地清空。
    """
    seen: set[tuple[str, str, str]] = set()
    events: list[dict[str, Any]] = []

    # 先用 SQL 的 LIKE 縮小候選集再做 regex：mops_events 保留 183 天，
    # 全表拉出來逐列解析在實測 10000 列（detail 合計 4.6MB）要 368ms，
    # 而其中只有 20% 含得到我們要的欄位名。粗篩後降到 149ms。
    rows = []
    if table_exists(conn, "mops_events"):
        like_clause = " OR ".join("detail LIKE ?" for _ in UPCOMING_FIELDS)
        rows = conn.execute(
            "SELECT stock_id, company_name, title, detail FROM mops_events "
            f"WHERE detail IS NOT NULL AND ({like_clause})",
            [f"%{field_name}%" for field_name in FIELD_LIKE_TERMS],
        ).fetchall()
    for stock_id, company_name, title, detail in rows:
        for label, iso in extract_event_dates(detail):
            if not (start <= iso <= end):
                continue
            key = (stock_id or "", label, iso)
            if key in seen:
                continue  # 同一事件常被公告多次
            seen.add(key)
            events.append(
                {
                    "date": iso,
                    "stock_id": stock_id or "",
                    "name": company_name or "",
                    "event_type": label,
                    "title": title or "",
                }
            )

    # 上櫃除權息預告表（TPEx prepost）補上 MOPS 沒公告到的部分
    dividend_rows = []
    if table_exists(conn, "dividend_events"):
        dividend_rows = conn.execute(
            """
            SELECT ex_date, stock_id, name, cash_dividend, stock_dividend_per_share
            FROM dividend_events WHERE ex_date BETWEEN ? AND ?
            """,
            (start, end),
        ).fetchall()
    for ex_date, stock_id, name, cash, stock in dividend_rows:
        key = (stock_id or "", "除權息交易日", ex_date)
        if key in seen:
            continue
        seen.add(key)
        events.append(
            {
                "date": ex_date,
                "stock_id": stock_id or "",
                "name": name or "",
                "event_type": "除權息交易日",
                "title": f"現金股利 {cash or 0}元"
                + (f"、配股 {stock}股" if stock else ""),
            }
        )

    events.sort(key=lambda item: (item["date"], EVENT_PRIORITY.get(item["event_type"], 9), item["stock_id"]))
    return events


def week_news(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, list[dict[str, str]]]:
    """漲幅榜個股的近期新聞標題，用來解釋「為什麼漲」。

    stock_news 由每日管線預抓；尚未跑過時整段為空，前端不顯示這一區。
    """
    if not stock_ids:
        return {}
    placeholders = ",".join("?" for _ in stock_ids)
    rows = conn.execute(
        f"""
        SELECT stock_id, title, url, published_at
        FROM stock_news WHERE stock_id IN ({placeholders})
        ORDER BY stock_id, published_at DESC, fetched_at DESC
        """,
        stock_ids,
    ).fetchall()
    out: dict[str, list[dict[str, str]]] = {}
    for stock_id, title, url, published_at in rows:
        bucket = out.setdefault(stock_id, [])
        if len(bucket) < NEWS_PER_STOCK:
            bucket.append({"title": title, "url": url, "published_at": published_at or ""})
    return out


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def build_weekly_report(
    db_path: Path, scan_csv_path: Path, today: "dt.date | None" = None
) -> dict[str, Any]:
    import datetime as dt

    today = today or dt.date.today()

    empty: dict[str, Any] = {
        "state": "insufficient",
        "week_start": None,
        "week_end": None,
        "trading_day_count": 0,
        "next_week_start": None,
        "next_week_end": None,
        "gainers": {"mainstream": [], "speculative": [], "losers": []},
        "market_flow": {},
        "sector_flows": {"inflow": [], "outflow": []},
        "foreign_top": [],
        "trust_top": [],
        "etf_flows": [],
        "upcoming": [],
        "news": {},
    }
    scan_rows = load_scan_rows(scan_csv_path)
    with connect_db(db_path) as conn:
        window = last_complete_week(conn, today=today)
        if window is None:
            return empty
        start, end, day_count = window
        perf = week_returns(conn, start, end)
        if not perf:
            return {**empty, "week_start": start, "week_end": end, "trading_day_count": day_count}
        merge_scan_fields(perf, scan_rows)

        # 下週 = 週報所涵蓋那週的次週一 ~ 次週日，用日曆推而非交易日，
        # 因為除權息基準日、股利發放日這些不必然落在交易日。
        end_date = dt.date.fromisoformat(end)
        next_start = end_date + dt.timedelta(days=(7 - end_date.weekday()))
        next_end = next_start + dt.timedelta(days=6)

        gainers = split_gainers(perf)
        flows = sector_flows(perf)
        by_foreign = sorted(perf.values(), key=lambda item: item["foreign_net_lot"], reverse=True)
        by_trust = sorted(perf.values(), key=lambda item: item["trust_net_lot"], reverse=True)
        # 不在此 gate：upcoming_events 內部逐一檢查 mops_events 與 dividend_events。
        # 先前用 mops_events 是否存在來 gate 整段，會讓「只有除權息預告表」的
        # 資料庫拿到空的行事曆。
        events = upcoming_events(conn, next_start.isoformat(), next_end.isoformat())
        news = week_news(
            conn, [item["stock_id"] for item in gainers["mainstream"][:NEWS_STOCK_LIMIT]]
        ) if table_exists(conn, "stock_news") else {}
        try:
            etf = load_etf_flows(conn)
            etf_rows = sorted(
                [row for row in etf.get("rows", []) if row.get("units_chg_5d_pct") is not None],
                key=lambda row: row["units_chg_5d_pct"],
                reverse=True,
            )[:TOP_ETF]
        except sqlite3.Error:
            etf_rows = []

        return {
            "state": "ready",
            "week_start": start,
            "week_end": end,
            "trading_day_count": day_count,
            "next_week_start": next_start.isoformat(),
            "next_week_end": next_end.isoformat(),
            "stock_count": len(perf),
            "gainers": gainers,
            "market_flow": market_flow(conn, start, end),
            "sector_flows": flows,
            "foreign_top": [item for item in by_foreign if item["foreign_net_lot"] > 0][:TOP_FLOW_STOCKS],
            "trust_top": [item for item in by_trust if item["trust_net_lot"] > 0][:TOP_FLOW_STOCKS],
            "etf_flows": [
                {
                    "etf_id": row.get("etf_id"),
                    "name": row.get("name"),
                    "category": row.get("category"),
                    "units_chg_5d_pct": row.get("units_chg_5d_pct"),
                    "aum": row.get("aum"),
                }
                for row in etf_rows
            ],
            "upcoming": events,
            "news": news,
        }

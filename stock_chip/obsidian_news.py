from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from stock_chip.us_news import ensure_us_news_tables, load_cached_us_news


VAULT_DIR = Path("obsidian/news-vault")
RAW_RETENTION_DAYS = 3


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_date(value: object) -> dt.date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def slugify(value: str, fallback: str = "note") -> str:
    text = value.strip().lower()
    text = re.sub(r"https?://", "", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    text = text.strip("-")
    return text[:80] or fallback


def wiki_name(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    text = text.replace("/", "-")
    text = re.sub(r"[\n\r\t]+", " ", text)
    text = re.sub(r"[:*?\"<>|]+", "-", text)
    return text.strip(" .-") or fallback


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def cleanup_old_raw(vault_dir: Path, retention_days: int) -> list[str]:
    raw_dir = vault_dir / "raw"
    if not raw_dir.exists():
        return []
    today = dt.datetime.now().date()
    cutoff = today - dt.timedelta(days=max(retention_days, 1) - 1)
    removed: list[str] = []
    for path in raw_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            folder_day = dt.datetime.strptime(path.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_day < cutoff:
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


def yaml_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def row_symbols(row: dict[str, object]) -> list[str]:
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol or symbol == "MARKET":
        return []
    return [symbol]


def note_link(folder: str, name: str) -> str:
    return f"[[{folder}/{name}|{name}]]"


def source_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.")


def raw_note_path(vault_dir: Path, row: dict[str, object]) -> Path:
    fetched_day = parse_date(row.get("fetched_at")) or dt.date.today()
    unique = hashlib.sha1(str(row.get("url") or row.get("title") or "").encode("utf-8")).hexdigest()[:10]
    source = slugify(str(row.get("source") or source_host(str(row.get("url") or ""))), "source")
    title = slugify(str(row.get("title") or ""), "news")
    return vault_dir / "raw" / fetched_day.isoformat() / f"{source}-{title}-{unique}.md"


def raw_note(row: dict[str, object]) -> str:
    symbols = row_symbols(row)
    industry = wiki_name(str(row.get("industry") or "其他"), "其他")
    event_type = wiki_name(str(row.get("event_type") or "一般新聞"), "一般新聞")
    fetched_day = parse_date(row.get("fetched_at")) or dt.date.today()
    links = [note_link("industries", industry), note_link("events", event_type)]
    links.extend(note_link("companies", symbol) for symbol in symbols)
    return f"""---
date: {str(row.get("published_at") or "")[:10]}
fetched_at: "{row.get("fetched_at") or ""}"
source: "{row.get("source") or ""}"
url: "{row.get("url") or ""}"
symbols: {yaml_list(symbols)}
industries: {yaml_list([industry])}
event_type: "{event_type}"
sentiment: "{row.get("sentiment") or "中性"}"
confidence: {row.get("confidence") or 0}
classified_by: "{row.get("classified_by") or "rules"}"
classification_total_tokens: {int(row.get("classification_total_tokens") or 0)}
expires_at: {(fetched_day + dt.timedelta(days=RAW_RETENTION_DAYS)).isoformat()}
---

# {row.get("title") or "Untitled"}

來源：[{row.get("source") or source_host(str(row.get("url") or ""))}]({row.get("url") or ""})

分類：{" ".join(links)}

情緒：{row.get("sentiment") or "中性"}
分類方式：{row.get("classified_by") or "rules"}，token：{int(row.get("classification_total_tokens") or 0)}

## 摘要

{row.get("summary") or row.get("reason") or "無摘要。"}

## 分類理由

{row.get("reason") or "無分類理由。"}
"""


def group_rows(rows: list[dict[str, object]], key: str) -> dict[str, list[dict[str, object]]]:
    output: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        value = str(row.get(key) or "").strip() or "其他"
        output.setdefault(value, []).append(row)
    return output


def daily_note(day: dt.date, rows: list[dict[str, object]], vault_dir: Path = VAULT_DIR) -> str:
    by_industry = group_rows(rows, "industry")
    by_event = group_rows(rows, "event_type")
    lines = [
        f"# {day.isoformat()} 美股新聞",
        "",
        f"更新時間：{now_text()}",
        f"新聞數：{len(rows)}",
        "",
        "## 產業分布",
        "",
    ]
    for industry, items in sorted(by_industry.items(), key=lambda item: (-len(item[1]), item[0])):
        name = wiki_name(industry, "其他")
        lines.append(f"- {note_link('industries', name)}：{len(items)} 則")
    lines.extend(["", "## 事件類型", ""])
    for event_type, items in sorted(by_event.items(), key=lambda item: (-len(item[1]), item[0])):
        name = wiki_name(event_type, "一般新聞")
        lines.append(f"- {note_link('events', name)}：{len(items)} 則")
    lines.extend(["", "## 新聞", ""])
    for row in rows:
        raw_path = raw_note_path(vault_dir, row)
        raw_stem = raw_path.with_suffix("").as_posix().removeprefix(f"{vault_dir.as_posix()}/")
        lines.append(
            f"- [[{raw_stem}|{row.get('title') or 'Untitled'}]] "
            f"({row.get('source') or ''}, {row.get('sentiment') or '中性'}, token {int(row.get('classification_total_tokens') or 0)})"
        )
    return "\n".join(lines)


def company_note(symbol: str, rows: list[dict[str, object]]) -> str:
    lines = [
        f"# {symbol}",
        "",
        f"更新時間：{now_text()}",
        "",
        "## 最近新聞",
        "",
    ]
    if not rows:
        lines.append("暫無新聞。")
    for row in rows:
        day = str(row.get("published_at") or row.get("fetched_at") or "")[:10]
        industry = wiki_name(str(row.get("industry") or "其他"), "其他")
        lines.append(f"- {day} [{row.get('title') or 'Untitled'}]({row.get('url') or ''}) - {note_link('industries', industry)}")
    return "\n".join(lines)


def industry_note(industry: str, rows: list[dict[str, object]]) -> str:
    name = wiki_name(industry, "其他")
    by_symbol: dict[str, int] = {}
    for row in rows:
        for symbol in row_symbols(row):
            by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
    lines = [
        f"# {name}",
        "",
        f"更新時間：{now_text()}",
        f"SQLite 長期新聞數：{len(rows)}",
        "",
        "## 相關公司",
        "",
    ]
    if by_symbol:
        for symbol, count in sorted(by_symbol.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"- {note_link('companies', symbol)}：{count} 則")
    else:
        lines.append("- 尚未標到特定公司。")
    lines.extend(["", "## 最近新聞", ""])
    for row in rows:
        lines.append(f"- [{row.get('title') or 'Untitled'}]({row.get('url') or ''}) - {row.get('source') or ''} / {row.get('sentiment') or '中性'}")
    return "\n".join(lines)


def event_note(event_type: str, rows: list[dict[str, object]]) -> str:
    name = wiki_name(event_type, "一般新聞")
    industries = sorted({wiki_name(str(row.get("industry") or "其他"), "其他") for row in rows})
    lines = [
        f"# {name}",
        "",
        f"更新時間：{now_text()}",
        f"SQLite 長期新聞數：{len(rows)}",
        "",
        "## 關聯產業",
        "",
    ]
    lines.extend(f"- {note_link('industries', industry)}" for industry in industries)
    lines.extend(["", "## 最近新聞", ""])
    for row in rows:
        lines.append(f"- [{row.get('title') or 'Untitled'}]({row.get('url') or ''}) - {row.get('source') or ''} / {row.get('industry') or '其他'}")
    return "\n".join(lines)


def index_note(rows: list[dict[str, object]], vault_dir: Path, retention_days: int, raw_count: int) -> str:
    latest = max((str(row.get("fetched_at") or "") for row in rows), default="")
    by_industry = group_rows(rows, "industry")
    by_event = group_rows(rows, "event_type")
    lines = [
        "# 美股新聞知識庫",
        "",
        f"更新時間：{now_text()}",
        f"最近抓取：{latest or '-'}",
        f"SQLite 長期新聞數：{len(rows)}",
        f"raw 新聞數：{raw_count}",
        f"raw 新聞輸出範圍：最近 {retention_days} 日",
        "",
        "## 入口",
        "",
        "- [[daily]]",
        "- [[companies]]",
        "- [[industries]]",
        "- [[events]]",
        "- [[log]]",
        "",
        "## 產業",
        "",
    ]
    for industry, items in sorted(by_industry.items(), key=lambda item: (-len(item[1]), item[0])):
        name = wiki_name(industry, "其他")
        lines.append(f"- {note_link('industries', name)}：{len(items)} 則")
    lines.extend(["", "## 事件", ""])
    for event_type, items in sorted(by_event.items(), key=lambda item: (-len(item[1]), item[0])):
        name = wiki_name(event_type, "一般新聞")
        lines.append(f"- {note_link('events', name)}：{len(items)} 則")
    lines.extend(
        [
            "",
            "## 說明",
            "",
            "SQLite 是正式資料庫；Obsidian vault 是閱讀、關聯與圖譜層。",
            "raw 只保留最近三日；daily、industries、events、companies 會長期保留整理頁，並由 SQLite 快取重建關聯。",
        ]
    )
    return "\n".join(lines)


def log_note(raw_rows: list[dict[str, object]], history_rows: list[dict[str, object]], retention_days: int, removed_raw_dirs: list[str]) -> str:
    token_total = sum(int(row.get("classification_total_tokens") or 0) for row in history_rows)
    rules = sum(1 for row in history_rows if str(row.get("classified_by") or "") == "rules")
    ollama = len(history_rows) - rules
    return f"""# 更新紀錄

- 更新時間：{now_text()}
- SQLite 長期新聞數：{len(history_rows)}
- raw 輸出新聞數：{len(raw_rows)}
- raw 新聞輸出範圍：最近 {retention_days} 日
- 本次清除過期 raw 日期資料夾：{", ".join(removed_raw_dirs) if removed_raw_dirs else "0"}
- 規則分類：{rules} 則
- Ollama 分類：{ollama} 則
- 分類 token 合計：{token_total}

備註：此檔案由程式重寫，用於記錄最近一次 Obsidian 匯出狀態。
"""


def recent_rows(conn: sqlite3.Connection, retention_days: int, limit: int) -> list[dict[str, object]]:
    rows = load_cached_us_news(conn, limit=limit)
    today = dt.datetime.now().date()
    cutoff = today - dt.timedelta(days=retention_days - 1)
    output = []
    for row in rows:
        fetched_day = parse_date(row.get("fetched_at")) or parse_date(row.get("published_at"))
        if fetched_day and fetched_day >= cutoff:
            output.append(row)
    return output


def export_obsidian_vault(
    db_path: Path,
    vault_dir: Path = VAULT_DIR,
    retention_days: int = RAW_RETENTION_DAYS,
    limit: int = 300,
) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        ensure_us_news_tables(conn)
        raw_rows = recent_rows(conn, retention_days=retention_days, limit=max(limit, 1000))
        history_rows = load_cached_us_news(conn, limit=max(limit, 5000))

    vault_dir.mkdir(parents=True, exist_ok=True)
    removed_raw_dirs = cleanup_old_raw(vault_dir, retention_days)
    write_text(
        vault_dir / "README.md",
        "# 美股新聞 Obsidian Vault\n\n這個資料夾由台股籌碼觀察工具產生，可用 Obsidian 的 Open folder as vault 開啟。\n",
    )
    write_text(vault_dir / "index.md", index_note(history_rows, vault_dir, retention_days, len(raw_rows)))
    write_text(vault_dir / "log.md", log_note(raw_rows, history_rows, retention_days, removed_raw_dirs))

    for row in raw_rows:
        write_text(raw_note_path(vault_dir, row), raw_note(row))

    rows_by_day: dict[dt.date, list[dict[str, object]]] = {}
    for row in raw_rows:
        day = parse_date(row.get("fetched_at")) or dt.datetime.now().date()
        rows_by_day.setdefault(day, []).append(row)
    for day, day_rows in rows_by_day.items():
        write_text(vault_dir / "daily" / f"{day.isoformat()}.md", daily_note(day, day_rows, vault_dir))

    symbols = sorted({symbol for row in history_rows for symbol in row_symbols(row)})
    for symbol in symbols:
        write_text(
            vault_dir / "companies" / f"{wiki_name(symbol, 'UNKNOWN')}.md",
            company_note(symbol, [row for row in history_rows if symbol in row_symbols(row)]),
        )

    for industry, industry_rows in group_rows(history_rows, "industry").items():
        write_text(vault_dir / "industries" / f"{wiki_name(industry, '其他')}.md", industry_note(industry, industry_rows))

    for event_type, event_rows in group_rows(history_rows, "event_type").items():
        write_text(vault_dir / "events" / f"{wiki_name(event_type, '一般新聞')}.md", event_note(event_type, event_rows))

    return {
        "vault_dir": str(vault_dir),
        "news_count": len(history_rows),
        "raw_count": len(raw_rows),
        "daily_count": len(rows_by_day),
        "company_count": len(symbols),
        "industry_count": len(group_rows(history_rows, "industry")),
        "event_count": len(group_rows(history_rows, "event_type")),
        "retention_days": retention_days,
        "removed_raw_dirs": removed_raw_dirs,
        "updated_at": now_text(),
    }


def obsidian_vault_status(vault_dir: Path = VAULT_DIR) -> dict[str, object]:
    raw_dir = vault_dir / "raw"
    raw_files = list(raw_dir.glob("*/*.md")) if raw_dir.exists() else []
    daily_files = list((vault_dir / "daily").glob("*.md")) if (vault_dir / "daily").exists() else []
    industry_files = list((vault_dir / "industries").glob("*.md")) if (vault_dir / "industries").exists() else []
    event_files = list((vault_dir / "events").glob("*.md")) if (vault_dir / "events").exists() else []
    company_files = list((vault_dir / "companies").glob("*.md")) if (vault_dir / "companies").exists() else []
    log_path = vault_dir / "log.md"
    updated_at = ""
    if log_path.exists():
        updated_at = dt.datetime.fromtimestamp(log_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "vault_dir": str(vault_dir),
        "exists": vault_dir.exists(),
        "raw_count": len(raw_files),
        "daily_count": len(daily_files),
        "company_count": len(company_files),
        "industry_count": len(industry_files),
        "event_count": len(event_files),
        "retention_days": RAW_RETENTION_DAYS,
        "updated_at": updated_at,
        "index_path": str(vault_dir / "index.md"),
        "log_path": str(log_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export US news cache to an Obsidian vault.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--vault", default=str(VAULT_DIR))
    parser.add_argument("--retention-days", type=int, default=RAW_RETENTION_DAYS)
    parser.add_argument("--limit", type=int, default=300)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = export_obsidian_vault(
        Path(args.db),
        vault_dir=Path(args.vault),
        retention_days=max(1, args.retention_days),
        limit=max(1, args.limit),
    )
    print(
        f"Exported {result['news_count']} US news notes to {Path(result['vault_dir']).resolve()} "
        f"at {result['updated_at']}"
    )


if __name__ == "__main__":
    main()

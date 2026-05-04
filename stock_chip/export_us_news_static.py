from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from stock_chip.us_news import fetch_us_news, now_text


DROP_QUERY_PREFIXES = ("utm_",)
DROP_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "guccounter",
    "mod",
    "ocid",
    "source",
}


def read_existing(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    return rows if isinstance(rows, list) else []


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_url(url: object) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in DROP_QUERY_KEYS or any(lowered.startswith(prefix) for prefix in DROP_QUERY_PREFIXES):
            continue
        query.append((key, value))
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query, doseq=True),
            "",
        )
    )


def news_id(row: dict[str, object]) -> str:
    canonical = normalize_url(row.get("url"))
    if not canonical:
        canonical = "|".join(
            [
                str(row.get("source") or ""),
                str(row.get("published_at") or "")[:10],
                str(row.get("title") or "").strip().lower(),
            ]
        )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def merge_rows(existing: list[dict[str, object]], fresh: list[dict[str, object]], max_rows: int) -> list[dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for row in [*existing, *fresh]:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["news_id"] = str(item.get("news_id") or news_id(item))
        item["canonical_url"] = str(item.get("canonical_url") or normalize_url(item.get("url")))
        by_id[item["news_id"]] = {**by_id.get(item["news_id"], {}), **item}
    rows = sorted(
        by_id.values(),
        key=lambda row: str(row.get("published_at") or row.get("fetched_at") or ""),
        reverse=True,
    )
    return rows[:max_rows]


def parse_symbols(value: str) -> list[str]:
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def export_us_news_static(
    out: Path,
    symbols: list[str],
    limit: int,
    max_rows: int,
    include_symbol_news: bool,
) -> dict[str, object]:
    existing = read_existing(out)
    fresh_items = fetch_us_news(
        symbols if include_symbol_news else [],
        limit=limit,
        use_ollama=False,
    )
    fresh = [dataclasses.asdict(item) for item in fresh_items]
    rows = merge_rows(existing, fresh, max_rows=max_rows)
    payload = {
        "generated_at": now_text(),
        "classifier": "rules",
        "symbols": symbols if include_symbol_news else [],
        "include_symbol_news": include_symbol_news,
        "fetched_count": len(fresh),
        "existing_count": len(existing),
        "row_count": len(rows),
        "rows": rows,
    }
    write_json(out, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch US news and merge it into docs/data/us_news.json.")
    parser.add_argument("--out", default="docs/data/us_news.json")
    parser.add_argument("--symbols", default="AAPL,MSFT,NVDA,TSLA")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("--market-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = export_us_news_static(
        Path(args.out),
        symbols=parse_symbols(args.symbols),
        limit=max(1, min(args.limit, 25)),
        max_rows=max(1, args.max_rows),
        include_symbol_news=not args.market_only,
    )
    print(
        f"Fetched {payload['fetched_count']} US news items, "
        f"merged {payload['row_count']} rows into {Path(args.out).resolve()} "
        f"at {payload['generated_at']}"
    )


if __name__ == "__main__":
    main()

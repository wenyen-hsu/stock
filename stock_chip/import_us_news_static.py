from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from stock_chip.obsidian_news import export_obsidian_vault
from stock_chip.us_news import ensure_us_news_tables, import_static_us_news


def import_file(db_path: Path, json_path: Path) -> dict[str, object]:
    with sqlite3.connect(db_path) as conn:
        ensure_us_news_tables(conn)
        result = import_static_us_news(conn, json_path)
    obsidian = export_obsidian_vault(db_path)
    return {**result, "obsidian": obsidian}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import docs/data/ci_us_news.json into local SQLite.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--json", default="docs/data/ci_us_news.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = import_file(Path(args.db), Path(args.json))
    print(
        f"Imported {result['imported_rows']} CI news rows from {result['source_path']} "
        f"({result['inserted_rows']} inserted, {result['updated_rows']} updated) "
        f"at {result['imported_at']}"
    )


if __name__ == "__main__":
    main()

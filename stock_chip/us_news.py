from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.7",
}
YAHOO_FINANCE_RSS = "https://finance.yahoo.com/rss/headline"
CNBC_FEEDS = {
    "CNBC Top News": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "CNBC Business": "https://www.cnbc.com/id/10001147/device/rss/rss.html",
}
MARKETWATCH_FEEDS = {
    "MarketWatch Top Stories": "https://www.marketwatch.com/rss/topstories",
    "MarketWatch Realtime": "https://www.marketwatch.com/rss/realtimeheadlines",
}
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_MODEL = "gemma4:e4b"
DROP_QUERY_PREFIXES = ("utm_",)
DROP_QUERY_KEYS = {"fbclid", "gclid", "guccounter", "mod", "ocid", "source"}


INDUSTRY_RULES: list[tuple[str, list[str]]] = [
    ("半導體 / AI", ["nvidia", "nvda", "tsmc", "amd", "intel", "intc", "chip", "semiconductor", "gpu", "ai chip", "foundry", "hbm"]),
    ("AI / 伺服器", ["artificial intelligence", "ai", "data center", "server", "cloud capex"]),
    ("軟體雲端", ["microsoft", "msft", "google", "alphabet", "aws", "amazon web services", "cloud", "saas", "software"]),
    ("電動車", ["tesla", "tsla", "ev", "electric vehicle", "battery", "charging", "autonomous driving"]),
    ("金融", ["fed", "rate", "yield", "bank", "jpmorgan", "goldman", "financial", "inflation"]),
    ("能源", ["oil", "gas", "energy", "opec", "crude", "exxon", "chevron"]),
    ("生技醫療", ["fda", "drug", "clinical", "biotech", "health", "pharma", "vaccine", "medicare"]),
    ("消費零售", ["retail", "consumer", "walmart", "costco", "target", "nike", "starbucks"]),
    ("原物料", ["copper", "gold", "steel", "commodity", "mining", "materials"]),
    ("總體經濟", ["jobs", "payroll", "gdp", "recession", "economy", "tariff", "treasury", "market"]),
]
EVENT_RULES: list[tuple[str, list[str]]] = [
    ("財報", ["earnings", "revenue", "profit", "quarter", "guidance", "forecast"]),
    ("併購 / 投資", ["acquire", "acquisition", "merger", "deal", "investment", "stake"]),
    ("產品 / 技術", ["launch", "product", "chip", "platform", "model", "technology"]),
    ("法規 / 政策", ["regulator", "lawsuit", "probe", "ban", "tariff", "policy", "approval"]),
    ("總體 / 利率", ["fed", "rate", "inflation", "jobs", "yield", "gdp", "treasury"]),
]
POSITIVE_WORDS = ["rise", "rises", "gain", "gains", "beat", "beats", "surge", "strong", "upgrade", "record", "profit"]
NEGATIVE_WORDS = ["fall", "falls", "drop", "drops", "miss", "misses", "slump", "weak", "downgrade", "loss", "probe"]
ALLOWED_INDUSTRIES = {
    "半導體 / AI",
    "AI / 伺服器",
    "軟體雲端",
    "電動車",
    "金融",
    "能源",
    "生技醫療",
    "消費零售",
    "原物料",
    "總體經濟",
    "其他",
}
ALLOWED_EVENTS = {"財報", "併購 / 投資", "產品 / 技術", "法規 / 政策", "總體 / 利率", "一般新聞"}
INDUSTRY_ALIASES = {
    "半導體": "半導體 / AI",
    "AI": "AI / 伺服器",
    "伺服器": "AI / 伺服器",
    "雲端": "軟體雲端",
    "軟體": "軟體雲端",
    "EV": "電動車",
    "醫療": "生技醫療",
    "生技": "生技醫療",
    "零售": "消費零售",
    "消費": "消費零售",
    "材料": "原物料",
    "總體": "總體經濟",
    "宏觀經濟": "總體經濟",
}
EVENT_ALIASES = {
    "總體": "總體 / 利率",
    "利率": "總體 / 利率",
    "政策": "法規 / 政策",
    "法規": "法規 / 政策",
    "技術": "產品 / 技術",
    "產品": "產品 / 技術",
    "投資": "併購 / 投資",
    "併購": "併購 / 投資",
}


@dataclass(frozen=True)
class USNewsItem:
    symbol: str
    title: str
    url: str
    source: str
    published_at: str
    summary: str
    industry: str
    event_type: str
    sentiment: str
    confidence: float
    reason: str
    matched_keywords: str
    classified_by: str
    classification_input_tokens: int
    classification_output_tokens: int
    classification_total_tokens: int
    fetched_at: str


def now_text() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query, doseq=True), ""))


def normalize_published_at(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value
    if parsed.tzinfo:
        parsed = parsed.astimezone(dt.timezone(dt.timedelta(hours=8)))
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def ensure_us_news_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS us_stock_news (
            symbol TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            source TEXT NOT NULL,
            published_at TEXT,
            summary TEXT,
            industry TEXT,
            event_type TEXT,
            sentiment TEXT,
            confidence REAL,
            reason TEXT,
            matched_keywords TEXT,
            classified_by TEXT,
            classification_input_tokens INTEGER DEFAULT 0,
            classification_output_tokens INTEGER DEFAULT 0,
            classification_total_tokens INTEGER DEFAULT 0,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, source, url)
        )
        """
    )
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(us_stock_news)").fetchall()}
    for column in ("classification_input_tokens", "classification_output_tokens", "classification_total_tokens"):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE us_stock_news ADD COLUMN {column} INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_stock_news_symbol ON us_stock_news(symbol, published_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_us_stock_news_industry ON us_stock_news(industry, published_at DESC)")
    conn.commit()


def parse_rss(url: str, source: str, params: dict[str, str] | None = None, limit: int = 10) -> list[dict[str, str]]:
    response = requests.get(url, params=params, headers=REQUEST_HEADERS, timeout=15)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in root.findall("./channel/item"):
        title = clean_text(item.findtext("title"))
        link = clean_text(item.findtext("link"))
        if not title or not link or link in seen:
            continue
        seen.add(link)
        rows.append(
            {
                "title": title,
                "url": link,
                "source": source,
                "published_at": normalize_published_at(item.findtext("pubDate")),
                "summary": clean_text(item.findtext("description")),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def rule_classify(title: str, summary: str) -> dict[str, object]:
    text = f"{title} {summary}".lower()
    industry = "其他"
    industry_hits: list[str] = []
    for label, keywords in INDUSTRY_RULES:
        hits = [hit for kw in keywords for hit in keyword_hits(text, kw)]
        if hits:
            industry = label
            industry_hits = hits
            break
    event_type = "一般新聞"
    event_hits: list[str] = []
    for label, keywords in EVENT_RULES:
        hits = [hit for kw in keywords for hit in keyword_hits(text, kw)]
        if hits:
            event_type = label
            event_hits = hits
            break
    pos = sum(1 for word in POSITIVE_WORDS if word in text)
    neg = sum(1 for word in NEGATIVE_WORDS if word in text)
    sentiment = "中性"
    if pos > neg:
        sentiment = "偏多"
    elif neg > pos:
        sentiment = "偏空"
    confidence = 0.45 + min(0.35, 0.06 * (len(industry_hits) + len(event_hits)))
    return {
        "industry": industry,
        "event_type": event_type,
        "sentiment": sentiment,
        "confidence": round(confidence, 2),
        "reason": "依標題與摘要關鍵字初步分類",
        "matched_keywords": ", ".join(dedupe([*industry_hits, *event_hits])),
        "classified_by": "rules",
        "classification_input_tokens": 0,
        "classification_output_tokens": 0,
        "classification_total_tokens": 0,
    }


def normalize_label(value: str, allowed: set[str], aliases: dict[str, str], fallback: str) -> str:
    text = clean_text(value)
    if text in allowed:
        return text
    return aliases.get(text, fallback)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def keyword_hits(text: str, keyword: str) -> list[str]:
    if " " in keyword or not keyword.isascii() or not keyword.replace(" ", "").isalnum():
        return [keyword] if keyword in text else []
    tokens = re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text)
    if not tokens:
        return []
    hits: list[str] = []
    for index, token in enumerate(tokens):
        if not token_matches_keyword(token, keyword):
            continue
        previous_word = tokens[index - 1] if index > 0 else ""
        next_word = tokens[index + 1] if index + 1 < len(tokens) else ""
        if keyword == "chip" and next_word == "away":
            continue
        context = " ".join(word for word in (previous_word, token, next_word) if word)
        hits.append(context or keyword)
    return dedupe(hits)


def token_matches_keyword(token: str, keyword: str) -> bool:
    if keyword == "chip":
        return token in {"chip", "chips"}
    if keyword == "rate":
        return token in {"rate", "rates"}
    return token == keyword


def extract_json_object(text: str) -> dict[str, object] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def ollama_classify(title: str, summary: str, model: str = DEFAULT_MODEL, timeout: float = 35) -> dict[str, object] | None:
    prompt = f"""
你是金融新聞分類器。只輸出 JSON，不要解釋。
可用產業分類：半導體 / AI、AI / 伺服器、軟體雲端、電動車、金融、能源、生技醫療、消費零售、原物料、總體經濟、其他。
可用事件類型：財報、併購 / 投資、產品 / 技術、法規 / 政策、總體 / 利率、一般新聞。
可用情緒：偏多、偏空、中性。
請根據標題與摘要輸出：
{{"industry":"", "event_type":"", "sentiment":"", "confidence":0.0, "reason":""}}
標題：{title}
摘要：{summary}
""".strip()
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0}},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        return None
    data = extract_json_object(str(payload.get("response") or ""))
    if not data:
        return None
    industry = normalize_label(str(data.get("industry") or ""), ALLOWED_INDUSTRIES, INDUSTRY_ALIASES, "其他")
    event_type = normalize_label(str(data.get("event_type") or ""), ALLOWED_EVENTS, EVENT_ALIASES, "一般新聞")
    sentiment = str(data.get("sentiment") or "").strip()
    if sentiment not in {"偏多", "偏空", "中性"}:
        return None
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    return {
        "industry": industry,
        "event_type": event_type,
        "sentiment": sentiment,
        "confidence": round(max(0.0, min(confidence, 1.0)), 2),
        "reason": clean_text(str(data.get("reason") or ""))[:260],
        "classified_by": f"ollama:{model}",
        "classification_input_tokens": int(payload.get("prompt_eval_count") or 0),
        "classification_output_tokens": int(payload.get("eval_count") or 0),
        "classification_total_tokens": int(payload.get("prompt_eval_count") or 0) + int(payload.get("eval_count") or 0),
    }


def classify_news(title: str, summary: str, use_ollama: bool, model: str) -> dict[str, object]:
    fallback = rule_classify(title, summary)
    if not use_ollama:
        return fallback
    ai = ollama_classify(title, summary, model=model)
    if not ai:
        return fallback
    ai["matched_keywords"] = fallback.get("matched_keywords", "")
    return ai


def fetch_us_news(symbols: list[str], limit: int = 5, use_ollama: bool = True, model: str = DEFAULT_MODEL) -> list[USNewsItem]:
    fetched_at = now_text()
    rows: list[USNewsItem] = []
    seen_keys: set[tuple[str, str]] = set()
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        try:
            symbol_rows = parse_rss(YAHOO_FINANCE_RSS, "Yahoo Finance", {"s": symbol}, limit=limit)
        except Exception:
            symbol_rows = []
        for item in symbol_rows:
            key = (symbol, item["url"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            classification = classify_news(item["title"], item["summary"], use_ollama, model)
            rows.append(USNewsItem(symbol=symbol, fetched_at=fetched_at, **item, **classification))
            time.sleep(0.05)
    market_limit = min(limit, 25)
    for source, url in {**CNBC_FEEDS, **MARKETWATCH_FEEDS}.items():
        try:
            feed_rows = parse_rss(url, source, limit=market_limit)
        except Exception:
            continue
        for item in feed_rows:
            key = ("MARKET", item["url"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            classification = classify_news(item["title"], item["summary"], use_ollama, model)
            rows.append(USNewsItem(symbol="MARKET", fetched_at=fetched_at, **item, **classification))
            time.sleep(0.05)
    return rows


def upsert_us_news(conn: sqlite3.Connection, rows: list[USNewsItem]) -> None:
    ensure_us_news_tables(conn)
    conn.executemany(
        """
        INSERT INTO us_stock_news (
            symbol, title, url, source, published_at, summary, industry, event_type,
            sentiment, confidence, reason, matched_keywords, classified_by,
            classification_input_tokens, classification_output_tokens, classification_total_tokens,
            fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, source, url) DO UPDATE SET
            title = excluded.title,
            published_at = excluded.published_at,
            summary = excluded.summary,
            industry = excluded.industry,
            event_type = excluded.event_type,
            sentiment = excluded.sentiment,
            confidence = excluded.confidence,
            reason = excluded.reason,
            matched_keywords = excluded.matched_keywords,
            classified_by = excluded.classified_by,
            classification_input_tokens = excluded.classification_input_tokens,
            classification_output_tokens = excluded.classification_output_tokens,
            classification_total_tokens = excluded.classification_total_tokens,
            fetched_at = excluded.fetched_at
        """,
        [
            (
                row.symbol,
                row.title,
                row.url,
                row.source,
                row.published_at,
                row.summary,
                row.industry,
                row.event_type,
                row.sentiment,
                row.confidence,
                row.reason,
                row.matched_keywords,
                row.classified_by,
                row.classification_input_tokens,
                row.classification_output_tokens,
                row.classification_total_tokens,
                row.fetched_at,
            )
            for row in rows
        ],
    )
    conn.commit()


def item_from_static_row(row: dict[str, object]) -> USNewsItem:
    classification = rule_classify(str(row.get("title") or ""), str(row.get("summary") or ""))
    return USNewsItem(
        symbol=str(row.get("symbol") or "MARKET"),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        source=str(row.get("source") or ""),
        published_at=str(row.get("published_at") or ""),
        summary=str(row.get("summary") or ""),
        industry=str(classification["industry"]),
        event_type=str(classification["event_type"]),
        sentiment=str(classification["sentiment"]),
        confidence=float(classification["confidence"]),
        reason=str(classification["reason"]),
        matched_keywords=str(classification["matched_keywords"]),
        classified_by="rules",
        classification_input_tokens=0,
        classification_output_tokens=0,
        classification_total_tokens=0,
        fetched_at=str(row.get("fetched_at") or now_text()),
    )


def import_static_us_news(conn: sqlite3.Connection, json_path: Path) -> dict[str, object]:
    if not json_path.exists():
        raise FileNotFoundError(f"找不到 CI 新聞檔：{json_path}")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    raw_rows = payload.get("rows", []) if isinstance(payload, dict) else []
    if not isinstance(raw_rows, list):
        raw_rows = []
    items = [
        item_from_static_row(row)
        for row in raw_rows
        if isinstance(row, dict) and str(row.get("title") or "").strip() and str(row.get("url") or "").strip()
    ]
    before = conn.execute("SELECT COUNT(*) FROM us_stock_news").fetchone()[0]
    existing = conn.execute("SELECT symbol, source, url FROM us_stock_news").fetchall()
    incoming = {(item.symbol, item.source, normalize_url(item.url)) for item in items}
    for symbol, source, url in existing:
        if (symbol, source, normalize_url(url)) in incoming and url not in {item.url for item in items}:
            conn.execute("DELETE FROM us_stock_news WHERE symbol = ? AND source = ? AND url = ?", (symbol, source, url))
    upsert_us_news(conn, items)
    after = conn.execute("SELECT COUNT(*) FROM us_stock_news").fetchone()[0]
    return {
        "source_path": str(json_path),
        "json_generated_at": payload.get("generated_at", "") if isinstance(payload, dict) else "",
        "json_rows": len(raw_rows),
        "imported_rows": len(items),
        "inserted_rows": max(0, after - before),
        "updated_rows": max(0, len(items) - max(0, after - before)),
        "db_rows": after,
        "imported_at": now_text(),
    }


def load_cached_us_news(
    conn: sqlite3.Connection,
    symbols: list[str] | None = None,
    industry: str = "",
    limit: int = 80,
) -> list[dict[str, object]]:
    ensure_us_news_tables(conn)
    clauses: list[str] = []
    args: list[object] = []
    clean_symbols = [item.strip().upper() for item in symbols or [] if item.strip()]
    if clean_symbols:
        placeholders = ",".join("?" for _ in clean_symbols)
        clauses.append(f"symbol IN ({placeholders})")
        args.extend(clean_symbols)
    if industry:
        clauses.append("industry = ?")
        args.append(industry)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    raw = conn.execute(
        f"""
        SELECT symbol, title, url, source, published_at, summary, industry, event_type,
               sentiment, confidence, reason, matched_keywords, classified_by,
               classification_input_tokens, classification_output_tokens, classification_total_tokens,
               fetched_at
        FROM us_stock_news
        {where}
        ORDER BY COALESCE(published_at, fetched_at) DESC, fetched_at DESC
        LIMIT ?
        """,
        [*args, limit],
    ).fetchall()
    keys = [
        "symbol",
        "title",
        "url",
        "source",
        "published_at",
        "summary",
        "industry",
        "event_type",
        "sentiment",
        "confidence",
        "reason",
        "matched_keywords",
        "classified_by",
        "classification_input_tokens",
        "classification_output_tokens",
        "classification_total_tokens",
        "fetched_at",
    ]
    output = [dict(zip(keys, row, strict=True)) for row in raw]
    for row in output:
        row["industry"] = normalize_label(str(row.get("industry") or ""), ALLOWED_INDUSTRIES, INDUSTRY_ALIASES, "其他")
        row["event_type"] = normalize_label(str(row.get("event_type") or ""), ALLOWED_EVENTS, EVENT_ALIASES, "一般新聞")
    return output


def refresh_us_news(
    db_path: Path,
    symbols: list[str],
    limit: int = 5,
    use_ollama: bool = True,
    model: str = DEFAULT_MODEL,
    include_symbol_news: bool = True,
) -> dict[str, object]:
    clean_symbols = [item.strip().upper() for item in symbols if item.strip()]
    fetch_symbols = clean_symbols if include_symbol_news else []
    rows = fetch_us_news(fetch_symbols, limit=limit, use_ollama=use_ollama, model=model)
    with sqlite3.connect(db_path) as conn:
        upsert_us_news(conn, rows)
        cached = load_cached_us_news(conn, limit=80)
    obsidian = {}
    try:
        from stock_chip.obsidian_news import export_obsidian_vault

        obsidian = export_obsidian_vault(db_path)
    except Exception as exc:
        obsidian = {"error": str(exc)}
    return {
        "symbols": clean_symbols,
        "include_symbol_news": include_symbol_news,
        "fetched_count": len(rows),
        "fetched_at": rows[0].fetched_at if rows else now_text(),
        "classifier": f"ollama:{model}" if use_ollama else "rules",
        "classification_total_tokens": sum(row.classification_total_tokens for row in rows),
        "obsidian": obsidian,
        "rows": cached,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch US stock news and classify industry labels.")
    parser.add_argument("--db", default="data/stock_chip.sqlite")
    parser.add_argument("--symbols", default="AAPL,MSFT,NVDA,TSLA")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--no-ollama", action="store_true")
    parser.add_argument("--market-only", action="store_true", help="Only fetch broad market feeds, not Yahoo Finance symbol feeds.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = refresh_us_news(
        Path(args.db),
        [item for item in args.symbols.split(",") if item.strip()],
        limit=args.limit,
        use_ollama=not args.no_ollama,
        model=args.model,
        include_symbol_news=not args.market_only,
    )
    print(
        f"Fetched {result['fetched_count']} US news items for {','.join(result['symbols'])} "
        f"with {result['classifier']} at {result['fetched_at']} "
        f"(classification tokens: {result['classification_total_tokens']})"
    )


if __name__ == "__main__":
    main()

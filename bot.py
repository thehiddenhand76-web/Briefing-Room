import html
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "@thebriefingroom99").strip()

DB_PATH = "seen_items.db"
FEED_JSON_PATH = "public/feed.json"
MAX_FEED_ITEMS = 120
MAX_POSTS_PER_RUN = 8

FEEDS = [
    # CRYPTO
    ("CRYPTO", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CRYPTO", "https://cointelegraph.com/rss"),
    ("CRYPTO", "https://www.theblock.co/rss.xml"),
    ("CRYPTO", "https://decrypt.co/feed"),
    # STOCKS & MARKETS
    ("STOCKS", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
    ("STOCKS", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("STOCKS", "https://finance.yahoo.com/news/rssindex"),
    # MACRO & ECONOMY
    ("MACRO", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("MACRO", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"),
    # POLITICS & TECH / AI
    ("POLITICS", "https://rss.politico.com/politics-news.xml"),
    ("POLITICS", "https://www.theverge.com/rss/index.xml"),
]

BLOCKED_PHRASES = (
    "should i ",
    "retirement",
    "annuity",
    "mortgage rates",
    "credit card",
    "how to save",
    "my husband",
    "my wife",
    "personal finance",
    "opinion:",
    "horoscope",
    "wordle",
    "sponsored",
)

HIGH_SIGNAL_KEYWORDS = (
    "just in", "breaking", "surges", "plunges", "crashes", "wiped out",
    "selloff", "rally", "odds", "polymarket", "kalshi", "liquidat",
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp",
    "fed", "powell", "fomc", "cpi", "inflation", "interest rate", "tariff",
    "treasury", "recession", "gdp", "jobs report", "unemployment",
    "sec", "etf", "blackrock", "goldman", "jpmorgan", "morgan stanley",
    "nvidia", "nvda", "apple", "aapl", "tesla", "tsla", "microsoft", "msft",
    "amazon", "amzn", "meta", "google", "alphabet", "openai", "anthropic",
    "neuralink", "spacex", "ai", "ipo", "earnings", "billion", "trillion",
    "white house", "congress", "senate", "trump", "sanctions", "oil", "gold"
)

URGENT_TRIGGERS = (
    "plunges", "crashes", "wiped out", "surges", "selloff",
    "warns", "odds", "sec ", "fed ", "tariff", "billion",
    "trillion", "liquidat", "emergency", "approved", "rejects"
)


def init_db() -> tuple[sqlite3.Connection, str]:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen (
            link TEXT PRIMARY KEY,
            title TEXT,
            category TEXT,
            created_at TEXT
        )
        """
    )

    cols = [row[1] for row in conn.execute("PRAGMA table_info(seen)").fetchall()]
    if "link" in cols:
        key_col = "link"
    elif cols:
        key_col = cols[0]
    else:
        key_col = "link"

    for col_name in ("title", "category", "created_at"):
        if col_name not in cols:
            conn.execute(f"ALTER TABLE seen ADD COLUMN {col_name} TEXT")

    conn.commit()
    return conn, key_col


def is_seen(conn: sqlite3.Connection, key_col: str, link: str) -> bool:
    cur = conn.execute(f"SELECT 1 FROM seen WHERE {key_col} = ?", (link,))
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, key_col: str, link: str, title: str, category: str) -> None:
    conn.execute(
        f"INSERT OR IGNORE INTO seen ({key_col}, title, category, created_at) VALUES (?, ?, ?, ?)",
        (link, title, category, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def clean_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()).strip()


def is_high_signal(title: str) -> bool:
    t = title.lower()
    if any(bad in t for bad in BLOCKED_PHRASES):
        return False
    return any(good in t for good in HIGH_SIGNAL_KEYWORDS)


def detect_category(title: str, default_cat: str) -> str:
    t = title.lower()
    if any(k in t for k in ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "crypto", "polymarket", "stablecoin", "defi")):
        return "CRYPTO"
    if any(k in t for k in ("fed", "powell", "cpi", "inflation", "gdp", "treasury", "recession", "unemployment", "yield", "oil", "gold")):
        return "MACRO"
    if any(k in t for k in ("white house", "senate", "congress", "trump", "election", "tariff", "sanctions", "pentagon")):
        return "POLITICS"
    if any(k in t for k in ("stock", "nasdaq", "s&p", "dow", "nvda", "nvidia", "aapl", "apple", "tsla", "tesla", "ipo", "earnings", "goldman", "jpmorgan")):
        return "STOCKS"
    return default_cat


def format_headline(title: str) -> str:
    clean = clean_text(title)
    if not clean.upper().startswith(("JUST IN:", "BREAKING:")):
        if any(w in clean.lower() for w in URGENT_TRIGGERS):
            clean = f"JUST IN: {clean}"
    return clean


def extract_image(entry) -> str:
    media_content = entry.get("media_content") or []
    for m in media_content:
        url = (m.get("url") or "").strip()
        if url.startswith("http"):
            return url

    media_thumb = entry.get("media_thumbnail") or []
    for m in media_thumb:
        url = (m.get("url") or "").strip()
        if url.startswith("http"):
            return url

    for enc in entry.get("enclosures") or []:
        url = (enc.get("href") or enc.get("url") or "").strip()
        if url.startswith("http") and any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp")):
            return url

    summary_html = entry.get("summary") or entry.get("description") or ""
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary_html, re.IGNORECASE)
    if match and match.group(1).startswith("http"):
        return match.group(1).strip()

    return ""


def extract_source(link: str) -> str:
    try:
        host = urlparse(link).netloc.lower()
        return host.removeprefix("www.").upper() or "BRIEFING"
    except Exception:
        return "BRIEFING"


def send_telegram(headline: str, category: str, link: str, image_url: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    escaped_title = html.escape(headline)
    escaped_cat = html.escape(category)
    escaped_link = html.escape(link)

    caption = (
        f"<b>{escaped_title}</b>\n\n"
        f"#{escaped_cat} • <a href=\"{escaped_link}\">Source</a>\n"
        f"@thebriefingroom99"
    )

    if image_url:
        photo_resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": image_url,
                "caption": caption,
                "parse_mode": "HTML",
            },
            timeout=15,
        )
        if photo_resp.ok:
            return

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )


def load_existing_feed() -> list:
    if not os.path.exists(FEED_JSON_PATH):
        return []
    try:
        with open(FEED_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else data.get("items", [])
    except Exception:
        return []


def save_feed(items: list) -> None:
    os.makedirs(os.path.dirname(FEED_JSON_PATH), exist_ok=True)
    with open(FEED_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(items[:MAX_FEED_ITEMS], f, ensure_ascii=False, indent=2)


def main() -> None:
    conn, key_col = init_db()
    existing_items = load_existing_feed()
    new_items = []
    posted_count = 0

    headers = {"User-Agent": "Mozilla/5.0 (compatible; TheBriefingRoomBot/1.0)"}

    for default_cat, feed_url in FEEDS:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=15)
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"Feed error ({feed_url}): {exc}")
            continue

        for entry in parsed.entries[:15]:
            link = (entry.get("link") or "").strip()
            raw_title = clean_text(entry.get("title") or "")
            if not link or not raw_title:
                continue

            if is_seen(conn, key_col, link):
                continue

            if not is_high_signal(raw_title):
                mark_seen(conn, key_col, link, raw_title, default_cat)
                continue

            category = detect_category(raw_title, default_cat)
            headline = format_headline(raw_title)
            image_url = extract_image(entry)
            source = extract_source(link)
            published_iso = datetime.now(timezone.utc).isoformat()

            mark_seen(conn, key_col, link, headline, category)

            item = {
                "title": headline,
                "category": category,
                "source": source,
                "link": link,
                "image": image_url,
                "published": published_iso,
            }
            new_items.append(item)

            if posted_count < MAX_POSTS_PER_RUN:
                try:
                    send_telegram(headline, category, link, image_url)
                    posted_count += 1
                    time.sleep(1.5)
                except Exception as exc:
                    print(f"Telegram send error: {exc}")

    if new_items:
        merged = new_items + existing_items
        save_feed(merged)
        print(f"Added {len(new_items)} new items ({posted_count} sent to Telegram).")
    else:
        print("No new high-signal items found.")

    conn.close()


if __name__ == "__main__":
    main()
         

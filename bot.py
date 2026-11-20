import hashlib
import html
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
import feedparser
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHANNEL_HANDLE = os.environ.get("CHANNEL_HANDLE", "@thebriefingroom99")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

DB_PATH = Path("seen_items.db")
FEED_JSON_PATH = Path("public/feed.json")

FEEDS = [
    ("POLITICS", "https://feeds.npr.org/1014/rss.xml"),
    ("MACRO", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("STOCKS", "https://feeds.marketwatch.com/marketwatch/marketpulse/"),
    ("CRYPTO", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]

CATEGORY_TAGS = {
    "POLITICS": "🏛 <b>THE BRIEFING ROOM // POLITICS</b>",
    "MACRO": "🌐 <b>THE BRIEFING ROOM // MACRO</b>",
    "STOCKS": "📈 <b>THE BRIEFING ROOM // STOCKS</b>",
    "CRYPTO": "₿ <b>THE BRIEFING ROOM // CRYPTO</b>",
}

KEYWORDS = [
    "fed", "federal reserve", "inflation", "cpi", "gdp", "rates", "treasury",
    "sec", "congress", "white house", "election", "tariff", "sanctions",
    "stock", "stocks", "market", "s&p", "nasdaq", "earnings", "ipo",
    "bitcoin", "btc", "ethereum", "eth", "crypto", "etf", "solana", "token",
]


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (uid TEXT PRIMARY KEY)")
    conn.commit()
    return conn


def already_seen(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute("SELECT 1 FROM seen WHERE uid = ?", (uid,)).fetchone() is not None


def mark_seen(conn: sqlite3.Connection, uid: str) -> None:
    conn.execute("INSERT OR IGNORE INTO seen (uid) VALUES (?)", (uid,))
    conn.commit()


def is_relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(k in text for k in KEYWORDS)


def format_alert(category: str, title: str, link: str, source: str) -> str:
    header = CATEGORY_TAGS.get(category, f"🚨 <b>THE BRIEFING ROOM // {html.escape(category)}</b>")
    safe_title = html.escape(title.strip())
    safe_source = html.escape(source.strip())
    safe_link = html.escape(link.strip(), quote=True)
    safe_handle = html.escape(CHANNEL_HANDLE.strip())

    return (
        f"{header}\n\n"
        f"{safe_title}\n\n"
        f"<a href=\"{safe_link}\">Source: {safe_source}</a>  •  {safe_handle}"
    )


def send_telegram(html_text: str) -> None:
    resp = requests.post(
        API_URL,
        json={
            "chat_id": CHAT_ID,
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    resp.raise_for_status()


def load_existing_feed() -> list[dict]:
    if not FEED_JSON_PATH.exists():
        return []
    try:
        data = json.loads(FEED_JSON_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_feed(items: list[dict]) -> None:
    FEED_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEED_JSON_PATH.write_text(json.dumps(items[:100], indent=2), encoding="utf-8")


def poll_once() -> None:
    conn = init_db()
    existing_items = load_existing_feed()
    new_items = []

    try:
        for category, url in FEEDS:
            parsed = feedparser.parse(url)
            source_name = parsed.feed.get("title", category).split("-")[0].strip()
            for entry in parsed.entries[:10]:
                link = entry.get("link", "").strip()
                title = entry.get("title", "").strip()
                summary = entry.get("summary", "")
                if not link or not title:
                    continue

                uid = hashlib.sha256(link.encode()).hexdigest()
                if already_seen(conn, uid):
                    continue

                if not is_relevant(title, summary):
                    mark_seen(conn, uid)
                    continue

                send_telegram(format_alert(category, title, link, source_name))
                mark_seen(conn, uid)
                new_items.append({
                    "id": uid[:12],
                    "category": category,
                    "title": title,
                    "link": link,
                    "source": source_name,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                })
                time.sleep(2)
    finally:
        conn.close()

    if new_items:
        save_feed(new_items + existing_items)
        print(f"Posted {len(new_items)} new briefing(s).")
    else:
        print("No new matching items.")


if __name__ == "__main__":
    poll_once()
              

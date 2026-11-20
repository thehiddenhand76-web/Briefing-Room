import hashlib
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

DB_PATH = os.getenv("SEEN_DB_PATH", "seen_items.db")
FEED_OUTPUT_PATH = os.getenv("FEED_OUTPUT_PATH", "public/feed.json")
MAX_ITEMS_IN_FEED = int(os.getenv("MAX_ITEMS_IN_FEED", "60"))
MAX_NEW_PER_RUN = int(os.getenv("MAX_NEW_PER_RUN", "12"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHANNEL_HANDLE = os.getenv("CHANNEL_HANDLE", "@thebriefingroom99").strip()

CATEGORY_FALLBACKS = {
    "POLITICS": "politics.jpg",
    "MACRO": "macro.jpg",
    "STOCKS": "stocks.jpg",
    "CRYPTO": "crypto.jpg",
}

RSS_SOURCES = [
    ("POLITICS", "The Hill", "https://thehill.com/feed/"),
    ("POLITICS", "Politico", "https://rss.politico.com/politics-news.xml"),
    ("MACRO", "Reuters Business", "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best"),
    ("MACRO", "CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("STOCKS", "Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("STOCKS", "MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("CRYPTO", "CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CRYPTO", "Cointelegraph", "https://cointelegraph.com/rss"),
]


def is_valid_http_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def looks_like_image_url(url: str, mime_type: str = "") -> bool:
    if not is_valid_http_url(url):
        return False
    mime = (mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    path = urlparse(url).path.lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def extract_entry_image(entry, category: str) -> str:
    cat = (category or "MACRO").upper()
    fallback = CATEGORY_FALLBACKS.get(cat, "macro.jpg")

    for media in entry.get("media_content", []) or []:
        url = (media.get("url") or "").strip()
        mime = (media.get("type") or "").strip()
        if is_valid_http_url(url) and (not mime or mime.startswith("image/") or looks_like_image_url(url, mime)):
            return url

    for thumb in entry.get("media_thumbnail", []) or []:
        url = (thumb.get("url") or "").strip()
        if is_valid_http_url(url):
            return url

    for enc in entry.get("enclosures", []) or []:
        url = (enc.get("href") or enc.get("url") or "").strip()
        mime = (enc.get("type") or "").strip()
        if looks_like_image_url(url, mime):
            return url

    return fallback


def clean_text(raw: str, limit: int = 240) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def entry_uid(category: str, source: str, link: str, title: str) -> str:
    seed = f"{category}|{source}|{(link or title).strip()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def parse_published_iso(entry) -> str:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if struct:
        try:
            dt = datetime(*struct[:6], tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_items (
            uid TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            image TEXT NOT NULL,
            published_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def is_seen(conn: sqlite3.Connection, uid: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen_items WHERE uid = ? LIMIT 1", (uid,))
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, item: dict) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_items (uid, category, source, title, link, image, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["id"],
            item["category"],
            item["source"],
            item["title"],
            item["link"],
            item["image"],
            item["published_at"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def send_telegram_alert(item: dict) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID; skipping Telegram post.")
        return False

    handle = CHANNEL_HANDLE if CHANNEL_HANDLE.startswith("@") else f"@{CHANNEL_HANDLE}"
    title_esc = html.escape(item["title"])
    source_esc = html.escape(item["source"])
    cat_esc = html.escape(item["category"])
    summary_esc = html.escape(item["summary"]) if item.get("summary") else ""

    lines = [
        f"<b>[{cat_esc}]</b> {title_esc}",
        f"<i>Source:</i> {source_esc}",
    ]
    if summary_esc:
        lines.append(summary_esc)
    if is_valid_http_url(item["link"]):
        lines.append(f'<a href="{html.escape(item["link"])}">Read dispatch</a>')
    if handle and handle != "@":
        lines.append(html.escape(handle))

    message = "\n".join(lines)[:4090]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            return True
        print(f"Telegram API error ({resp.status_code}): {data}")
        return False
    except Exception as exc:
        print(f"Telegram request failed: {exc}")
        return False


def load_existing_feed(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
    except Exception as exc:
        print(f"Could not read existing {path}: {exc}")
    return []


def write_feed_json(path: str, items: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items[:MAX_ITEMS_IN_FEED],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def run() -> None:
    conn = init_db(DB_PATH)
    existing_items = load_existing_feed(FEED_OUTPUT_PATH)
    existing_by_id = {it.get("id"): it for it in existing_items if isinstance(it, dict) and it.get("id")}

    newly_collected = []

    for category, source, feed_url in RSS_SOURCES:
        try:
            resp = requests.get(
                feed_url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (compatible; BriefingRoomBot/1.0)"},
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"Feed fetch failed for {source} ({feed_url}): {exc}")
            continue

        for entry in (parsed.entries or [])[:8]:
            title = clean_text(entry.get("title", ""), limit=220)
            link = (entry.get("link") or "").strip()
            if not title or not is_valid_http_url(link):
                continue

            uid = entry_uid(category, source, link, title)
            if is_seen(conn, uid):
                continue

            summary = clean_text(entry.get("summary") or entry.get("description") or "", limit=220)
            image_url = extract_entry_image(entry, category)
            published_at = parse_published_iso(entry)

            item = {
                "id": uid,
                "category": category.upper(),
                "source": source,
                "title": title,
                "summary": summary,
                "link": link,
                "image": image_url,
                "published_at": published_at,
            }

            if len(newly_collected) < MAX_NEW_PER_RUN:
                posted = send_telegram_alert(item)
                mark_seen(conn, item)
                if posted:
                    newly_collected.append(item)
                    time.sleep(0.4)
            else:
                mark_seen(conn, item)

    merged = newly_collected + [
        it for it in existing_items if isinstance(it, dict) and it.get("id") not in {n["id"] for n in newly_collected}
    ]
    write_feed_json(FEED_OUTPUT_PATH, merged)
    conn.close()
    print(f"Done. New dispatches posted: {len(newly_collected)}, Total in feed: {min(len(merged), MAX_ITEMS_IN_FEED)}")


if __name__ == "__main__":
    run()
               

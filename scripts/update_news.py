#!/usr/bin/env python3
"""
Refreshes the "Latest news" cards in the UmrahNow HTML page.

What it does, in order:
  1. Pulls headlines from several Google News RSS searches (no API key needed).
  2. Keeps only stories about Saudi Arabia's own visas, hotels, Umrah/Nusuk,
     shopping and travel advisories, plus India-specific Umrah news -- and
     drops anything tied to other countries' pilgrims (Pakistan, Iran, ...)
     or to war/violence.
  3. Merges the new stories with the cards already in the page (so
     hand-written summaries are kept), removes duplicates, DELETES anything
     older than MAX_AGE_DAYS (60), sorts newest-first and keeps the latest
     MAX_POOL.
  4. Rewrites only the block between <!-- NEWS_LIST_START --> and
     <!-- NEWS_LIST_END -->. The page's own script then shows the 4 newest.

Usage:  python scripts/update_news.py [path/to/page.html]
Only the Python standard library is used.

Safety: if every feed fails, or nothing usable comes back, the page is left
untouched (it never wipes the list).
"""
import html
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ----------------------------------------------------------------- settings
MAX_POOL = 12          # cards kept in the HTML (page displays the newest 6)
MAX_AGE_DAYS = 60      # nothing older than this is added OR kept on the page
MAX_NEW_PER_RUN = 6    # cap on brand-new cards added in one run
START, END = "<!-- NEWS_LIST_START -->", "<!-- NEWS_LIST_END -->"

# Google News RSS searches. "when:Nd" limits to the last N days.
QUERIES = [
    "Saudi Arabia Umrah visa",
    "Saudi Arabia tourist visa",
    "Nusuk Umrah",
    "Makkah hotel",
    "Madinah hotel",
    "Makkah OR Madinah mall OR shopping",
    "Saudi Arabia pilgrims advisory OR heat OR health",
    "Ministry of Hajj and Umrah",
    # India-specific Umrah news (Indian pilgrims, visas, fares, packages)
    "Umrah India",
    "Indian pilgrims Umrah Saudi visa",
    "India Umrah package OR fare OR flights Jeddah OR Madinah",
]

# A headline must mention Saudi Arabia (or one of its holy cities)...
PLACE_WORDS = ["saudi", "makkah", "mecca", "madinah", "medina", "jeddah",
               "kingdom", "ksa", "haramain", "nusuk", "umrah", "hajj"]
# ...and one of the topics you asked for.
TOPIC_WORDS = ["visa", "umrah", "nusuk", "hotel", "hotels", "hospitality",
               "resort", "mall", "shopping", "retail", "advisory", "heat",
               "weather", "health ministry", "ministry of health", "permit",
               "rawdah", "tourism", "tourist", "haramain", "flight", "airport",
               "booking", "package"]
# Anything tied to other countries' pilgrims, or to conflict, is dropped.
# (India is deliberately NOT blocked: India-specific Umrah news is wanted.)
BLOCK_WORDS = ["pakistan", "pakistani", "iran", "iranian",
               "indonesia", "malaysia", "bangladesh", "nigeria", "nigerian",
               "turkey", "egypt", "yemen", "iraq", "syria", "israel", "gaza",
               "afghanistan", "quota", "war", "missile", "strike", "attack",
               "killed", "dead", "death", "died", "arrested", "terror"]

CATEGORY_ICONS = {
    "visa": '<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M3 10h18M7 15h3"/>',
    "hotel": '<path d="M4 21V10l8-6 8 6v11"/><path d="M10 21v-6h4v6"/>',
    "shopping": '<path d="M6 8h12l-1 12H7z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/>',
    "advisory": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4 12H2m20 0h-2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"/>',
    "default": '<circle cx="12" cy="12" r="9"/><path d="M9 12l2 2 4-4"/>',
}


# ------------------------------------------------------------------ helpers
def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (UmrahNow news updater)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def feed_url(query):
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-SA&gl=SA&ceid=SA:en"


def has_word(text, words):
    t = text.lower()
    return any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in words)


def is_relevant(title):
    return has_word(title, PLACE_WORDS) and has_word(title, TOPIC_WORDS) and not has_word(title, BLOCK_WORDS)


def category(title):
    t = title.lower()
    if re.search(r"\b(visa|permit|nusuk|entry)\b", t):
        return "visa"
    if re.search(r"\b(hotel|hotels|hospitality|resort|rooms)\b", t):
        return "hotel"
    if re.search(r"\b(mall|shopping|retail|market)\b", t):
        return "shopping"
    if re.search(r"\b(advisory|heat|weather|health|warning|safety)\b", t):
        return "advisory"
    return "default"


def norm_title(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def parse_feed(xml_bytes):
    """Yield dicts: title, link, date (date), source, domain."""
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        raw_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        domain = ""
        if src_el is not None and src_el.get("url"):
            domain = urllib.parse.urlparse(src_el.get("url")).netloc.replace("www.", "")
        # Google News titles look like "Headline - Source Name"
        title = raw_title
        if source and raw_title.endswith(" - " + source):
            title = raw_title[: -(len(source) + 3)].strip()
        try:
            date = parsedate_to_datetime(pub).astimezone(timezone.utc).date()
        except Exception:
            continue
        if title and link:
            yield {"title": title, "link": link, "date": date, "source": source or "News", "domain": domain}


def build_card(s):
    icon = CATEGORY_ICONS[category(s["title"])]
    esc = html.escape
    fav = (f'<img src="https://www.google.com/s2/favicons?sz=64&domain={esc(s["domain"])}" alt="" loading="lazy">'
           if s["domain"] else "")
    date_txt = f'{s["date"].day} {s["date"]:%b %Y}'
    link = esc(s["link"], quote=True)
    return f'''      <article class="news-card" data-date="{s["date"].isoformat()}">
        <div class="news-card-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{icon}</svg></div>
        <div class="news-meta"><span class="news-source">{fav}{esc(s["source"])}</span><span class="news-date">{date_txt}</span></div>
        <h3><a href="{link}" target="_blank" rel="noopener nofollow">{esc(s["title"])}</a></h3>
        <a class="map-link" href="{link}" target="_blank" rel="noopener nofollow">Read more &#8599;</a>
      </article>'''


def parse_existing(block):
    """Return [(date, title_norm, raw_html)] for cards already on the page."""
    cards = []
    for m in re.finditer(r"<article class=\"news-card\"[^>]*>.*?</article>", block, re.S):
        raw = "      " + m.group(0)
        d = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', raw)
        t = re.search(r"<h3><a [^>]*>(.*?)</a></h3>", raw, re.S)
        if not d:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", t.group(1))) if t else ""
        cards.append((datetime.strptime(d.group(1), "%Y-%m-%d").date(), norm_title(title), raw))
    return cards


# --------------------------------------------------------------------- main
def main():
    page = Path(sys.argv[1]) if len(sys.argv) > 1 else next(
        (Path(n) for n in ("index.html", "umrahnow.html") if Path(n).exists()), None)
    if not page or not page.exists():
        sys.exit("Page not found. Pass the HTML path: python scripts/update_news.py index.html")
    text = page.read_text(encoding="utf-8")
    if START not in text or END not in text:
        sys.exit("NEWS_LIST_START / NEWS_LIST_END markers not found in the page.")
    head, rest = text.split(START, 1)
    block, tail = rest.split(END, 1)

    existing = parse_existing(block)
    seen = {t for _, t, _ in existing}
    today = datetime.now(timezone.utc).date()
    oldest = today - timedelta(days=MAX_AGE_DAYS)

    fetched, ok_feeds = [], 0
    for q in QUERIES:
        try:
            fetched.extend(parse_feed(fetch(feed_url(q))))
            ok_feeds += 1
        except Exception as e:  # network/parse problem: skip this feed
            print(f"  ! feed failed ({q}): {e}")
    if ok_feeds == 0:
        sys.exit("All feeds failed -- page left unchanged.")

    fetched.sort(key=lambda s: s["date"], reverse=True)
    print(f"Fetched {len(fetched)} headlines in total.")
    new_cards, rejected = [], {"duplicate": [], "too old / future": [], "not relevant": []}
    for s in fetched:
        key = norm_title(s["title"])
        if key in seen:
            rejected["duplicate"].append(s["title"]); continue
        if s["date"] > today or s["date"] < oldest:
            rejected["too old / future"].append(s["title"]); continue
        if not is_relevant(s["title"]):
            rejected["not relevant"].append(s["title"]); continue
        seen.add(key)
        new_cards.append((s["date"], key, build_card(s)))
        if len(new_cards) >= MAX_NEW_PER_RUN:
            break
    for reason, titles in rejected.items():
        print(f"  rejected ({reason}): {len(titles)}")
        for t in titles[:5]:
            print(f"     - {t}")
    for c in new_cards:
        print(f"  + added: {c[0]}  {c[1][:70]}")

    fresh_existing = [c for c in existing if c[0] >= oldest]      # drop stale cards
    pool = sorted(new_cards + fresh_existing, key=lambda c: c[0], reverse=True)[:MAX_POOL]
    dropped = len(existing) - len(fresh_existing)
    print(f"{ok_feeds}/{len(QUERIES)} feeds ok, {len(new_cards)} new stories, "
          f"{dropped} stale removed, {len(pool)} kept.")
    if not new_cards and not dropped:
        print("Nothing to change -- page left unchanged.")
        return

    new_block = "\n" + "\n".join(c[2] for c in pool) + "\n      "
    page.write_text(head + START + new_block + END + tail, encoding="utf-8")
    print(f"Updated {page}.")


if __name__ == "__main__":
    main()

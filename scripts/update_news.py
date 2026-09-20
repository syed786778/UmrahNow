#!/usr/bin/env python3
"""
scripts/update_news.py
------------------------------------------------------------------
Fetches recent Umrah/Hajj/Saudi-travel news, filters and dedupes it,
and writes the result directly into umrah-portal.html between the
NEWS_LIST_START / NEWS_LIST_END markers — so the headlines are part
of the static HTML a search crawler actually receives, not something
assembled client-side after the page loads. That distinction is the
entire point of this script; see the product notes for why.

Run manually:   python scripts/update_news.py
Run in CI:      see .github/workflows/update-news.yml (scheduled)

No API key required — uses Google News' public RSS search endpoint,
which is free and has no meaningful rate limit at this call volume.
------------------------------------------------------------------
"""

import re
import sys
import html
import datetime
import urllib.parse
from pathlib import Path

import feedparser
import requests

HTML_FILE = Path(__file__).resolve().parent.parent / "index.html"
START_MARKER = "<!-- NEWS_LIST_START -->"
END_MARKER = "<!-- NEWS_LIST_END -->"

# Broad enough to catch real coverage, specific enough to stay on-topic.
SEARCH_QUERY = '(Umrah OR Hajj OR Nusuk) (Saudi OR Makkah OR Mecca OR Madinah OR pilgrim)'
RSS_URL = (
    "https://news.google.com/rss/search?q="
    + urllib.parse.quote(SEARCH_QUERY)
    + "&hl=en-US&gl=US&ceid=US:en"
)

MAX_ITEMS = 8
MAX_AGE_DAYS = 21
REQUEST_TIMEOUT = 6            # seconds per outbound request — keep the job fast and resilient
MIN_ITEMS_TO_PUBLISH = 3       # below this, leave the existing section alone rather than gut it

# Require at least one of these alongside a core term, to filter out
# unrelated stories where "Hajj" or "Umrah" is someone's given name, a
# company name, etc., rather than the pilgrimage.
CORE_TERMS = ["umrah", "hajj", "nusuk"]
CONTEXT_TERMS = ["saudi", "makkah", "mecca", "madinah", "medina", "pilgrim", "nusuk", "kaaba", "haram"]

# Add lowercase source names here to exclude specific outlets regardless
# of what Google surfaces (low-quality aggregators, etc.).
SOURCE_BLOCKLIST = set()


def is_relevant(title, summary):
    text = f"{title} {summary}".lower()
    has_core = any(t in text for t in CORE_TERMS)
    has_context = any(t in text for t in CONTEXT_TERMS)
    return has_core and has_context


def strip_source_suffix(title):
    """Remove Google's trailing " - Source Name" from a title — we already
    show the source as its own badge, so keeping it in the headline too
    just reads as a duplicate."""
    return re.sub(r"\s+-\s+[^-]+$", "", title).strip()


def normalize_title(title):
    """Collapse near-duplicate titles (the same wire story reprinted by
    several outlets) down to one comparable key."""
    title = strip_source_suffix(title)
    title = re.sub(r"[^a-z0-9 ]", "", title.lower())
    return re.sub(r"\s+", " ", title).strip()


def resolve_and_summarize(google_url):
    """Follow Google's redirect to the real article and pull its meta
    description. That text is written by the publisher specifically to be
    reused as a summary elsewhere (search results, social shares), which
    makes it a safe, accurate 1-3 sentence blurb rather than something we
    scraped from the article body."""
    try:
        resp = requests.get(
            google_url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UmrahNowBot/1.0; +https://umrahnow.in)"}
        )
        final_url = resp.url
        text = resp.text[:60000]  # meta tags always sit near the top; no need to read the whole page
        match = (
            re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', text, re.I)
            or re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', text, re.I)
        )
        summary = html.unescape(match.group(1)).strip() if match else ""
        return final_url, summary
    except Exception:
        return google_url, ""


def fetch_candidates():
    feed = feedparser.parse(RSS_URL)
    return feed.entries or []


def build_items():
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=MAX_AGE_DAYS)
    seen_titles = set()
    items = []

    for entry in fetch_candidates():
        title = html.unescape(getattr(entry, "title", "")).strip()
        if not title:
            continue

        published_struct = getattr(entry, "published_parsed", None)
        published_dt = datetime.datetime(*published_struct[:6]) if published_struct else None
        if published_dt and published_dt < cutoff:
            continue

        norm = normalize_title(title)
        if norm in seen_titles:
            continue

        source_obj = getattr(entry, "source", None)
        source_name = (source_obj.get("title") if isinstance(source_obj, dict) else None) or "Google News"
        if source_name.lower() in SOURCE_BLOCKLIST:
            continue

        google_link = getattr(entry, "link", "")
        final_url, summary = resolve_and_summarize(google_link)

        if not is_relevant(title, summary):
            continue

        if not summary:
            summary = f"Read the full story at {source_name}."

        seen_titles.add(norm)
        items.append({
            "title": strip_source_suffix(title),
            "url": final_url,
            "source": source_name,
            "date": published_dt,
            "summary": summary,
        })

        if len(items) >= MAX_ITEMS:
            break

    items.sort(key=lambda i: i["date"] or datetime.datetime.min, reverse=True)
    return items


def escape(text):
    return html.escape(text or "", quote=True)


def render_items_html(items):
    cards = []
    for item in items:
        date_str = item["date"].strftime("%d %b %Y") if item["date"] else ""
        cards.append(
            f'      <article class="news-card">\n'
            f'        <div class="news-meta"><span class="news-source">{escape(item["source"])}</span>'
            f'<span class="news-date">{escape(date_str)}</span></div>\n'
            f'        <h3><a href="{escape(item["url"])}" target="_blank" rel="noopener nofollow">'
            f'{escape(item["title"])}</a></h3>\n'
            f'        <p>{escape(item["summary"])}</p>\n'
            f'        <a class="map-link" href="{escape(item["url"])}" target="_blank" rel="noopener nofollow">'
            f'Read more \u2197</a>\n'
            f'      </article>'
        )
    return "\n".join(cards)


def update_html(items):
    html_text = HTML_FILE.read_text(encoding="utf-8")
    if START_MARKER not in html_text or END_MARKER not in html_text:
        print("ERROR: news markers not found in HTML \u2014 aborting without changes.", file=sys.stderr)
        sys.exit(1)

    before, rest = html_text.split(START_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    new_block = f"{START_MARKER}\n{render_items_html(items)}\n      {END_MARKER}"
    new_html = before + new_block + after

    # Plain string replacement rather than re.sub here — deliberately, since
    # mixing regex backreferences (\1/\2) with a \uXXXX escape in the same
    # raw replacement string doesn't work the way it looks like it should
    # (re.sub's replacement parser doesn't resolve \u escapes the way a
    # normal Python string literal does), and this format is fully known
    # anyway so a plain find/replace is both simpler and correct.
    today_str = datetime.datetime.utcnow().strftime("%d %b %Y")
    marker = '<p class="news-updated-note" id="newsUpdatedNote">Headlines refresh automatically — last updated '
    start = new_html.find(marker)
    if start != -1:
        content_start = start + len(marker)
        content_end = new_html.find("</p>", content_start)
        new_html = new_html[:content_start] + today_str + "." + new_html[content_end:]

    HTML_FILE.write_text(new_html, encoding="utf-8")


def main():
    items = build_items()
    print(f"Fetched {len(items)} relevant, deduped news item(s).")

    # Never blank the section over a bad run (empty feed, network hiccup,
    # everything filtered out) — leave the existing content in place instead.
    if len(items) < MIN_ITEMS_TO_PUBLISH:
        print(f"Fewer than {MIN_ITEMS_TO_PUBLISH} items found \u2014 leaving existing news section unchanged.")
        return

    update_html(items)
    print("index.html news section updated.")


if __name__ == "__main__":
    main()

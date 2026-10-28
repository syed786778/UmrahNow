#!/usr/bin/env python3
"""
UmrahNow automation -- ONE file that keeps the page fresh:

  1. Latest Updates news cards   (Google News + Bing News, filtered to Saudi
     visa / hotel / Umrah / shopping / advisory stories, India news included,
     duplicates and anything older than 60 days removed)
  2. Travel-advisory ticker      (UK FCDO, US State Dept / US Embassy, Indian
     Embassy Riyadh / Consulate Jeddah -- official sources first)
  3. Daily email digest          (optional; sent through Brevo)

Usage (the page is index.html in your repo):
    python scripts/update_news.py index.html                 # 1 + 2  (what the workflow runs)
    python scripts/update_news.py index.html --digest        # prepare the daily email; Brevo delivers it at 08:30 IST
    python scripts/update_news.py index.html --digest --now  # send it immediately (manual test)
    python scripts/update_news.py index.html --digest --dry-run   # build the email, don't send
    python scripts/update_news.py index.html --advisories-only --dry-run   # preview advisories
    (also: --news-only)

If one part fails the others still run. Standard library only.
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


def bing_url(query):
    return "https://www.bing.com/news/search?q=" + urllib.parse.quote(query) + "&format=rss&setmkt=en-SA"


def parse_bing(xml_bytes):
    """Same output as parse_feed, for Bing News RSS (gives the real article URL)."""
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        source = ""
        for child in item:
            if child.tag.split("}")[-1] == "Source":
                source = (child.text or "").strip()
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        domain = urllib.parse.urlparse(real).netloc.replace("www.", "")
        try:
            date = parsedate_to_datetime(pub).astimezone(timezone.utc).date()
        except Exception:
            continue
        if title and real:
            yield {"title": title, "link": real, "date": date, "source": source or domain or "News", "domain": domain}


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


# Words too common to count as a "this is the same story" signal -- without
# this, almost every Saudi headline shares "saudi"/"arabia"/"to" and the
# overlap check below would call unrelated stories duplicates of each other.
STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "with", "as", "at", "by", "from", "after", "before", "its", "amid", "over",
    "new", "says", "say", "said", "set", "will", "now", "more", "than", "into",
    "amid", "out", "up", "this", "that", "has", "have", "had", "be", "been",
}


def title_tokens(title):
    """The meaningful words in a headline, for duplicate detection."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if w not in STOPWORDS and len(w) > 2)


def is_near_duplicate(tokens, seen_token_sets, threshold=0.6):
    """True if `tokens` shares threshold-or-more of its words with any
    already-seen headline -- catches the same story reworded by a different
    outlet ("expands visa eligibility" vs "...visa access" vs "...visa
    scheme"), which an exact-text match misses entirely."""
    if not tokens:
        return False
    for other in seen_token_sets:
        if not other:
            continue
        smaller = min(len(tokens), len(other))
        if smaller and len(tokens & other) / smaller >= threshold:
            return True
    return False


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
    """Return [(date, tokens, raw_html, title)] for cards already on the page."""
    cards = []
    for m in re.finditer(r"<article class=\"news-card\"[^>]*>.*?</article>", block, re.S):
        raw = "      " + m.group(0)
        d = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', raw)
        t = re.search(r"<h3><a [^>]*>(.*?)</a></h3>", raw, re.S)
        if not d:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", t.group(1))) if t else ""
        cards.append((datetime.strptime(d.group(1), "%Y-%m-%d").date(), title_tokens(title), raw, title))
    return cards


# --------------------------------------------------------------------- news_main
def news_main():
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
    seen_tokens = [tk for _, tk, _, _ in existing]
    today = datetime.now(timezone.utc).date()
    oldest = today - timedelta(days=MAX_AGE_DAYS)

    fetched, ok_feeds = [], 0
    sources = (("google", feed_url, parse_feed), ("bing", bing_url, parse_bing))
    for q in QUERIES:
        for name, url_fn, parser in sources:
            try:
                fetched.extend(parser(fetch(url_fn(q))))
                ok_feeds += 1
            except Exception as e:  # network/parse problem: skip this feed
                print(f"  ! {name} feed failed ({q}): {e}")
    if ok_feeds == 0:
        sys.exit("All feeds failed -- page left unchanged.")

    fetched.sort(key=lambda s: s["date"], reverse=True)
    print(f"Fetched {len(fetched)} headlines in total.")
    new_cards, rejected = [], {"duplicate": [], "too old / future": [], "not relevant": []}
    for s in fetched:
        tokens = title_tokens(s["title"])
        if is_near_duplicate(tokens, seen_tokens):
            rejected["duplicate"].append(s["title"]); continue
        if s["date"] > today or s["date"] < oldest:
            rejected["too old / future"].append(s["title"]); continue
        if not is_relevant(s["title"]):
            rejected["not relevant"].append(s["title"]); continue
        seen_tokens.append(tokens)
        new_cards.append((s["date"], tokens, build_card(s), s["title"]))
        if len(new_cards) >= MAX_NEW_PER_RUN:
            break
    for reason, titles in rejected.items():
        print(f"  rejected ({reason}): {len(titles)}")
        for t in titles[:5]:
            print(f"     - {t}")
    for c in new_cards:
        print(f"  + added: {c[0]}  {c[3][:70]}")

    fresh_existing = [c for c in existing if c[0] >= oldest]      # drop stale cards
    combined = sorted(new_cards + fresh_existing, key=lambda c: c[0], reverse=True)

    # Final sweep over the WHOLE pool (new + previously-saved cards), not just
    # the incoming batch -- cleans up near-duplicate cards that were saved on
    # different days by earlier runs, before this check existed, same as the
    # three "Saudi Arabia expands tourist visa..." cards from different
    # outlets that prompted this fix.
    pool, kept_tokens, dupe_cleanup = [], [], 0
    for item in combined:
        if is_near_duplicate(item[1], kept_tokens):
            dupe_cleanup += 1
            continue
        kept_tokens.append(item[1])
        pool.append(item)
    pool = pool[:MAX_POOL]

    dropped = len(existing) - len(fresh_existing)
    print(f"{ok_feeds}/{len(QUERIES) * 2} feeds ok, {len(new_cards)} new stories, "
          f"{dropped} stale removed, {dupe_cleanup} duplicate(s) cleaned up, {len(pool)} kept.")
    if not new_cards and not dropped and not dupe_cleanup:
        print("Nothing to change -- page left unchanged.")
        return

    new_block = "\n" + "\n".join(c[2] for c in pool) + "\n      "
    page.write_text(head + START + new_block + END + tail, encoding="utf-8")
    print(f"Updated {page}.")




# =====================================================================
# Travel advisories
# =====================================================================
import html as htmllib
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ADV_START, ADV_END = "<!-- ADVISORY_LIST_START -->", "<!-- ADVISORY_LIST_END -->"
ADV_UA = "Mozilla/5.0 (compatible; UmrahNowBot/1.0; +https://umrahnow.in)"
REFRESH_AFTER_HOURS = 20       # re-stamp "checked" at most about once a day
MAX_TEXT = 340                 # characters shown per ticker item
PER_KEY_LIMIT = {"in": 3, "us": 2, "uk": 1}
ORDER = ["in", "us", "uk"]     # display order: Indian Embassy first
IN_MAX_AGE_DAYS = 45           # Indian notices drop off after this
WIRE_MAX_AGE_DAYS = 14         # news-wire fallback items drop off sooner
BADGE = {"in": "IN", "us": "US", "uk": "UK"}

# Embassy of India, Riyadh standing 24x7 helpline (published on the Embassy's
# own advisories / site).
IN_HELPLINE = "24×7 helpline 800 247 1234 (toll-free in Saudi Arabia)"

GOVUK_API = "https://www.gov.uk/api/content/foreign-travel-advice/saudi-arabia"
GOVUK_PAGE = "https://www.gov.uk/foreign-travel-advice/saudi-arabia/warnings-and-insurance"
GOVUK_URL = "https://www.gov.uk/foreign-travel-advice/saudi-arabia"
US_PAGE = "https://travel.state.gov/en/international-travel/travel-advisories/saudi-arabia.html"
US_EMB_FEED = "https://sa.usembassy.gov/feed/"
IN_PAGES = [
    ("https://www.eoiriyadh.gov.in/section/public-advisories/", "Embassy of India, Riyadh"),
    ("https://www.eoiriyadh.gov.in/section/news/", "Embassy of India, Riyadh"),
    ("https://www.cgijeddah.gov.in/", "Consulate of India, Jeddah"),
]
US_LEVELS = {1: "Exercise normal precautions", 2: "Exercise increased caution",
             3: "Reconsider travel", 4: "Do not travel"}


# ------------------------------------------------------------------ helpers
def fetch_text(url, timeout=25):
    """GET a URL (cookies enabled -- some government sites redirect-loop
    without them) and return the body as text."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", ADV_UA),
        ("Accept", "text/html,application/json,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en"),
    ]
    with opener.open(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_tags(s):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s or "")
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()


def clip(text, n=MAX_TEXT):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut + "…"


def sentence_case(title):
    t = re.sub(r"\s+", " ", title).strip(" -–—:")
    return t[:1].upper() + t[1:].lower() if t.isupper() else t


def parse_any_date(s):
    """'October 08, 2026' | '8 October 2026' | '08-10-2026' (D-M-Y) -> date."""
    s = s.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


DATE_RE = re.compile(r"([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})")


def mk(key, label, text, d, url, checked):
    return {"key": key, "label": label, "text": clip(text), "date": d, "url": url, "checked": checked}


# --------------------------------------------------------- news-wire fallback
def _google_url(q):
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en&gl=US&ceid=US:en"


def _bing_url(q):
    return "https://www.bing.com/news/search?q=" + urllib.parse.quote(q) + "&format=rss"


def _rss_items(xml_text):
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = ""
        for child in it:
            if child.tag.split("}")[-1].lower() == "source":
                src = (child.text or "").strip()
        if "bing.com" in link:
            link = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        try:
            d = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).date()
        except Exception:
            continue
        if src and title.endswith(" - " + src):
            title = title[: -(len(src) + 3)].strip()
        if title and link:
            yield {"title": title, "link": link, "date": d, "outlet": src}


def wire_fallback(queries, must, max_age_days, limit):
    """Headlines (from Google/Bing News RSS) matching ALL regexes in `must`."""
    found, seen = [], set()
    today = datetime.now(timezone.utc).date()
    for q in queries:
        for url_fn in (_google_url, _bing_url):
            try:
                for it in _rss_items(fetch_text(url_fn(q))):
                    key = re.sub(r"[^a-z0-9]+", " ", it["title"].lower())
                    if key in seen:
                        continue
                    if (today - it["date"]).days > max_age_days or it["date"] > today:
                        continue
                    if all(re.search(rx, it["title"], re.I) for rx in must):
                        seen.add(key)
                        found.append(it)
            except Exception as e:
                print(f"    wire fallback failed ({url_fn.__name__}, {q!r}): {e}")
    found.sort(key=lambda i: i["date"], reverse=True)
    return found[:limit]


# ------------------------------------------------------------------------ UK
def fcdo_areas(fragment):
    """The bulleted 'FCDO advises against ...' list from the warnings text."""
    for pat in (r"advises\s+against\s+travel", r"FCDO\s+advises\s+against"):
        m = re.search(pat, fragment, re.I)
        if not m:
            continue
        rest = fragment[m.end():]
        ul = re.search(r"<ul[^>]*>(.*?)</ul>", rest, re.S | re.I)
        if ul:
            lis = [strip_tags(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", ul.group(1), re.S | re.I)]
        else:  # markdown-style bullets
            lis = [re.sub(r"\s+", " ", x).strip() for x in re.findall(r"(?m)^\s*[-*]\s+(.+)$", rest[:3000])]
        lis = [x.rstrip(" .;") for x in lis if x]
        if lis:
            return lis[:6]
    return []


def uk_text(areas, change):
    if areas:
        return "Advises against: " + " · ".join(areas)
    return ("Latest update: " + change) if change else ""


def uk_from_api():
    data = json.loads(fetch_text(GOVUK_API))
    details = data.get("details") or {}
    pub = (data.get("public_updated_at") or details.get("updated_at") or "")[:10]
    body = " ".join((p.get("body") or "") for p in (details.get("parts") or [])[:3])
    return pub, fcdo_areas(body), strip_tags(details.get("change_description") or "")


def uk_from_page():
    page = fetch_text(GOVUK_PAGE)
    m = (re.search(r'name="govuk:public-updated-at"\s+content="([^"]+)"', page)
         or re.search(r'content="([^"]+)"\s+name="govuk:public-updated-at"', page))
    pub = m.group(1)[:10] if m else ""
    if not pub:
        dm = re.search(r"Updated:\s*(?:<[^>]+>\s*)*(\d{1,2}\s+[A-Z][a-z]+\s+\d{4})", page)
        d = parse_any_date(dm.group(1)) if dm else None
        pub = d.isoformat() if d else ""
    cm = re.search(r"Latest update:\s*(?:<[^>]+>\s*)*(.+?)\s*(?:<|$)", page, re.S)
    return pub, fcdo_areas(page), strip_tags(cm.group(1)) if cm else ""


def get_uk(now):
    for name, fn in (("GOV.UK API", uk_from_api), ("GOV.UK page", uk_from_page)):
        try:
            pub, areas, change = fn()
            text = uk_text(areas, change)
            d = date.fromisoformat(pub) if pub else None
            if text and d:
                print(f"  UK: ok via {name} ({d})")
                return [mk("uk", "UK FCDO", text, d, GOVUK_URL, now)]
            print(f"  UK: {name} returned no usable text/date")
        except Exception as e:
            print(f"  UK: {name} failed: {e}")
    hits = wire_fallback(["UK Foreign Office Saudi Arabia travel advice", "FCDO Saudi Arabia travel advice"],
                         [r"saudi", r"foreign office|fcdo|british nationals|uk (advis|warn|government)"], 21, 1)
    out = [mk("uk", f"UK FCDO · via {h['outlet'] or 'news'}", h["title"], h["date"], h["link"], now) for h in hits]
    print(f"  UK: wire fallback gave {len(out)} item(s)")
    return out


# ------------------------------------------------------------------------ US
def parse_us_page(page):
    t = strip_tags(page)
    m = re.search(r"\bLevel\s+([1-4])\b[^A-Za-z0-9]{0,6}[A-Za-z ]{0,40}?\s*Date issued:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", t)
    if m:
        level, d = int(m.group(1)), parse_any_date(m.group(2))
    else:
        lm, dm = re.search(r"\bLevel\s+([1-4])\b", t), re.search(r"Date issued:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", t)
        if not (lm and dm):
            return None
        level, d = int(lm.group(1)), parse_any_date(dm.group(1))
    if not d:
        return None
    text = f"Level {level}: {US_LEVELS[level]}"
    rm = re.search(r"(?:Reconsider travel|Exercise increased caution|Do not travel|Exercise normal precautions)"
                   r" to Saudi Arabia due to (.{20,420}?)\.(?:\s|$)", t, re.I)
    if rm:
        text += " — " + rm.group(1).strip()
    if re.search(r"Do not travel to[^.]{0,60}Yemen", t, re.I):
        text += ". Do not travel to the Yemen border area"
    return text, d


def get_us(now):
    out = []
    try:
        res = parse_us_page(fetch_text(US_PAGE))
        if res:
            out.append(mk("us", "US State Dept", res[0], res[1], US_PAGE, now))
            print(f"  US: advisory page ok ({res[1]})")
        else:
            print("  US: advisory page fetched but level/date not found")
    except Exception as e:
        print(f"  US: advisory page failed: {e}")

    try:
        today = datetime.now(timezone.utc).date()
        alerts = []
        for it in _rss_items(fetch_text(US_EMB_FEED)):
            if re.search(r"security alert|demonstration alert|travel advisory", it["title"], re.I) \
                    and (today - it["date"]).days <= 21 and it["date"] <= today:
                alerts.append(it)
        alerts.sort(key=lambda i: i["date"], reverse=True)
        # The feed's description holds the alert text; re-read it for "Event:".
        descs = {}
        root = ET.fromstring(fetch_text(US_EMB_FEED))
        for el in root.iter("item"):
            descs[(el.findtext("link") or "").strip()] = strip_tags(el.findtext("description") or "")
        for it in alerts[:1]:
            ev = re.search(r"Event:\s*(.+?)(?:Actions to Take|$)", descs.get(it["link"], ""))
            text = ("Security alert: " + ev.group(1).strip()) if ev else it["title"]
            out.append(mk("us", "US Embassy", text, it["date"], it["link"], now))
        print(f"  US: embassy feed ok ({len(alerts)} recent alert/advisory post(s))")
    except Exception as e:
        print(f"  US: embassy feed failed: {e}")

    if not out:
        hits = wire_fallback(["US State Department Saudi Arabia travel advisory", "US Embassy Riyadh security alert"],
                             [r"saudi", r"state department|u\.?s\.? embassy|travel advisory|level [1-4]"], 21, 1)
        out = [mk("us", f"US · via {h['outlet'] or 'news'}", h["title"], h["date"], h["link"], now) for h in hits]
        print(f"  US: wire fallback gave {len(out)} item(s)")
    return out


# --------------------------------------------------------------------- India
IN_RELEVANT = re.compile(r"advisory|alert|attack|security situation|safety|vigilan|stay indoors|evacuat|helpline", re.I)
IN_EXCLUDE = re.compile(r"tender|vacanc|recruit|scholarship|corrigendum|auction|camp\b|yoga|cultural", re.I)


def parse_in_page(page, base_url, label):
    items, seen = [], set()
    for m in re.finditer(r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        if href.startswith(("javascript:", "#", "mailto:")):
            continue
        title = strip_tags(inner)
        tail = strip_tags(page[m.end(): m.end() + 260])[:120]
        dm = DATE_RE.search(title) or DATE_RE.search(tail)
        d = parse_any_date(dm.group(1)) if dm else None
        if not d:
            continue
        title = DATE_RE.sub("", title)
        title = re.sub(r"(?i)\bnew icon\b|\bnew\b\s*$", "", title)
        title = re.sub(r"\s+", " ", title).strip(" -–—:|")
        if len(title) < 8 or not IN_RELEVANT.search(title) or IN_EXCLUDE.search(title):
            continue
        key = (title.lower(), d)
        if key in seen:
            continue
        seen.add(key)
        items.append({"title": title, "date": d, "url": urllib.parse.urljoin(base_url, href), "label": label})
    return items


def get_in(now):
    today = datetime.now(timezone.utc).date()
    official, ok_pages = [], 0
    for url, label in IN_PAGES:
        try:
            found = parse_in_page(fetch_text(url), url, label)
            ok_pages += 1
            official.extend(found)
            print(f"  IN: {url} ok ({len(found)} relevant notice(s))")
        except Exception as e:
            print(f"  IN: {url} failed: {e}")
    official = [o for o in official if 0 <= (today - o["date"]).days <= IN_MAX_AGE_DAYS]
    official.sort(key=lambda o: o["date"], reverse=True)
    dedup, seen = [], set()
    for o in official:
        k = (o["title"].lower(), o["date"])
        if k not in seen:
            seen.add(k)
            dedup.append(o)
    out = []
    for i, o in enumerate(dedup[:2]):
        text = sentence_case(o["title"])
        if i == 0:
            text += f" · {IN_HELPLINE}"
        out.append(mk("in", o["label"], text, o["date"], o["url"], now))

    # The Embassy's notice titles are often just "IMPORTANT ADVISORY"; add the
    # newest wire report of the Embassy's advice so the ticker says something.
    generic = (not out) or re.match(r"(?i)\s*(important )?advisory", dedup[0]["title"] if dedup else "")
    if generic:
        hits = wire_fallback(
            ["Indian Embassy Riyadh advisory", "Indian Consulate Jeddah advisory"],
            [r"\b(embassy|consulate|mission)\b", r"\bindia", r"\b(riyadh|jeddah|saudi)\b",
             r"advis|alert|urges|asks|advises|stay indoors|vigilan"], WIRE_MAX_AGE_DAYS, 1)
        for h in hits:
            out.append(mk("in", f"Embassy of India · via {h['outlet'] or 'news'}", h["title"], h["date"], h["link"], now))
        print(f"  IN: wire report(s) added: {len(hits)}")
    return out


# --------------------------------------------------------------- page I/O
def render(it):
    esc = htmllib.escape
    d = it["date"]
    chk = it["checked"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f'      <li data-key="{it["key"]}" data-date="{d.isoformat()}" data-checked="{chk}">'
            f'<a class="adv-link" href="{esc(it["url"], quote=True)}" target="_blank" rel="noopener">'
            f'<span class="adv-badge adv-badge--{it["key"]}">{BADGE[it["key"]]}</span>'
            f'<span class="adv-src">{esc(it["label"])}</span>'
            f'<span class="adv-txt">{esc(it["text"])}</span>'
            f'<span class="adv-date">{d.day} {d:%b}</span></a></li>')


def adv_parse_existing(block):
    items = []
    for m in re.finditer(r'<li data-key="(\w+)" data-date="([\d-]+)" data-checked="([^"]+)">(.*?)</li>', block, re.S):
        key, d, chk, inner = m.groups()
        href = re.search(r'href="([^"]*)"', inner)
        label = re.search(r'class="adv-src">(.*?)</span>', inner, re.S)
        text = re.search(r'class="adv-txt">(.*?)</span>', inner, re.S)
        if key not in BADGE or not (href and label and text):
            continue
        try:
            items.append({"key": key, "date": date.fromisoformat(d),
                          "checked": datetime.strptime(chk, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
                          "url": htmllib.unescape(href.group(1)), "label": htmllib.unescape(label.group(1)),
                          "text": htmllib.unescape(text.group(1))})
        except ValueError:
            continue
    return items


def signature(items):
    return [(i["key"], i["date"].isoformat(), i["url"], i["label"], i["text"]) for i in items]


def advisories_main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    page = Path(args[0]) if args else next((Path(n) for n in ("index.html", "umrahnow.html") if Path(n).exists()), None)
    if not page or not page.exists():
        sys.exit("Page not found. Usage: python scripts/update_advisories.py index.html [--dry-run]")
    text = page.read_text(encoding="utf-8")
    if ADV_START not in text or ADV_END not in text:
        sys.exit("ADVISORY_LIST_START / ADVISORY_LIST_END markers not found in the page.")
    head, rest = text.split(ADV_START, 1)
    block, tail = rest.split(ADV_END, 1)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    existing = adv_parse_existing(block)
    print("Fetching advisories...")
    fresh = {"in": get_in(now), "us": get_us(now), "uk": get_uk(now)}

    final, refreshed_keys = [], set()
    for key in ORDER:
        items = fresh[key]
        if items:
            refreshed_keys.add(key)
        else:
            items = [e for e in existing if e["key"] == key]
            if key == "in":  # old Indian notices expire; US/UK standing advice does not
                cutoff = (now - timedelta(days=IN_MAX_AGE_DAYS)).date()
                items = [e for e in items if e["date"] >= cutoff]
            print(f"  {key.upper()}: nothing new fetched -- keeping {len(items)} saved item(s)")
        items.sort(key=lambda i: i["date"], reverse=True)
        final.extend(items[:PER_KEY_LIMIT[key]])

    if not final:
        print("No items at all -- page left unchanged.")
        return

    changed = signature(final) != signature(existing)
    stale_stamp = any(
        e["checked"] < now - timedelta(hours=REFRESH_AFTER_HOURS)
        for e in existing if e["key"] in refreshed_keys)
    if dry:
        print("\n--dry-run: would write these items:")
        for i in final:
            print(f"  [{i['key'].upper()}] {i['date']} {i['label']}: {i['text']}\n        {i['url']}")
        return
    if not changed and not stale_stamp:
        print("Nothing changed -- page left unchanged.")
        return

    oldest = min(i["checked"] for i in final).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_head = re.sub(r'(id="advisoryBar"[^>]*?data-checked=")[^"]*(")', rf"\g<1>{oldest}\g<2>", head, count=1)
    if new_head == head and 'id="advisoryBar"' not in head:
        print("Warning: advisory bar element not found; list written but timestamp not updated.")
    page.write_text(new_head + ADV_START + "\n" + "\n".join(render(i) for i in final) + "\n    " + ADV_END + tail,
                    encoding="utf-8")
    print(f"Updated {page}: {len(final)} item(s) ({'content changed' if changed else 'timestamps refreshed'}).")




# =====================================================================
# Daily email digest
# =====================================================================
import html as htmllib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

API = "https://api.brevo.com/v3"
DIG_UA = "UmrahNowDigest/1.0 (+https://umrahnow.in)"
MAX_NEWS = 4
BADGE_COLORS = {"in": "#f0b24a", "us": "#9bbcf0", "uk": "#f0a3a3"}


# ------------------------------------------------------------ read the page
def read_advisories(page_html):
    if "<!-- ADVISORY_LIST_START -->" not in page_html:
        return []
    block = page_html.split("<!-- ADVISORY_LIST_START -->", 1)[1].split("<!-- ADVISORY_LIST_END -->", 1)[0]
    out = []
    for m in re.finditer(r'<li data-key="(\w+)" data-date="([\d-]+)"[^>]*>(.*?)</li>', block, re.S):
        key, d, inner = m.groups()
        href = re.search(r'href="([^"]*)"', inner)
        label = re.search(r'class="adv-src">(.*?)</span>', inner, re.S)
        text = re.search(r'class="adv-txt">(.*?)</span>', inner, re.S)
        if href and label and text:
            out.append({"key": key, "date": d, "url": htmllib.unescape(href.group(1)),
                        "label": htmllib.unescape(label.group(1)), "text": htmllib.unescape(text.group(1))})
    return out


def read_news(page_html):
    if "<!-- NEWS_LIST_START -->" not in page_html:
        return []
    block = page_html.split("<!-- NEWS_LIST_START -->", 1)[1].split("<!-- NEWS_LIST_END -->", 1)[0]
    out = []
    for m in re.finditer(r'<article class="news-card" data-date="([\d-]+)">(.*?)</article>', block, re.S):
        d, inner = m.groups()
        a = re.search(r'<h3><a href="([^"]*)"[^>]*>(.*?)</a></h3>', inner, re.S)
        src = re.search(r'<span class="news-source">(.*?)</span>', inner, re.S)
        if a:
            out.append({"date": d, "url": htmllib.unescape(a.group(1)),
                        "title": htmllib.unescape(re.sub(r"<[^>]+>", "", a.group(2))).strip(),
                        "source": htmllib.unescape(re.sub(r"<[^>]+>", "", src.group(1))).strip() if src else ""})
    out.sort(key=lambda n: n["date"], reverse=True)
    return out[:MAX_NEWS]


def pretty(d):
    try:
        x = date.fromisoformat(d)
        return f"{x.day} {x:%b}"
    except ValueError:
        return d


# ------------------------------------------------------------ build the email
def build_email(advisories, news, site, unsubscribe_tag="{{ unsubscribe }}", when=None):
    esc = htmllib.escape
    today = when or datetime.now(timezone.utc).date()
    subject = f"UmrahNow daily · Saudi travel advisories & Umrah updates · {today.day} {today:%b}"
    preheader = "Today's travel advisories for Saudi Arabia and the latest Umrah news."

    adv_rows = ""
    for a in advisories:
        color = BADGE_COLORS.get(a["key"], "#f0b24a")
        adv_rows += (
            '<div style="padding:12px 0;border-bottom:1px solid #eadfbf;">'
            f'<span style="background:{color};color:#1b1b18;font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;">{esc(a["key"].upper())}</span> '
            f'<strong style="color:#0E2A28;">{esc(a["label"])}</strong> '
            f'<span style="color:#8a8578;font-size:13px;">{esc(pretty(a["date"]))}</span>'
            f'<div style="margin:6px 0 4px;color:#3f3f38;font-size:15px;line-height:1.5;">{esc(a["text"])}</div>'
            f'<a href="{esc(a["url"], quote=True)}" style="color:#144C46;font-size:14px;font-weight:600;">Read the official page &rarr;</a>'
            '</div>')
    if not adv_rows:
        adv_rows = '<p style="color:#3f3f38;">No advisory updates today — see the site for the latest.</p>'

    news_rows = ""
    for n in news:
        news_rows += (
            '<div style="padding:10px 0;border-bottom:1px solid #eadfbf;">'
            f'<a href="{esc(n["url"], quote=True)}" style="color:#0E2A28;font-size:16px;font-weight:700;text-decoration:none;">{esc(n["title"])}</a>'
            f'<div style="color:#8a8578;font-size:13px;margin-top:2px;">{esc(n["source"])} · {esc(pretty(n["date"]))}</div></div>')

    cta = f"{site.rstrip('/')}/#news"
    body = f"""<!doctype html><html><body style="margin:0;background:#F6F1E4;font-family:Arial,Helvetica,sans-serif;">
<span style="display:none;max-height:0;overflow:hidden;opacity:0;">{esc(preheader)}</span>
<div style="max-width:600px;margin:0 auto;background:#ffffff;">
  <div style="background:#0E2A28;padding:22px 24px;">
    <div style="font-family:Georgia,serif;font-size:26px;color:#F6F1E4;">UmrahNow</div>
    <div style="color:#E7C77E;font-size:13px;letter-spacing:0.05em;">Your Umrah Companion · daily update</div>
  </div>
  <div style="padding:22px 24px;">
    <h2 style="font-family:Georgia,serif;font-size:20px;color:#0E2A28;margin:0 0 4px;">Travel advisories for Saudi Arabia</h2>
    <div style="color:#8a8578;font-size:13px;margin-bottom:6px;">From the Indian Embassy, the US and the UK — summaries only; read the full advisory before you travel.</div>
    {adv_rows}
    <h2 style="font-family:Georgia,serif;font-size:20px;color:#0E2A28;margin:26px 0 4px;">Latest Umrah updates</h2>
    {news_rows or '<p style="color:#3f3f38;">See the site for the latest updates.</p>'}
    <div style="text-align:center;margin:28px 0 8px;">
      <a href="{esc(cta, quote=True)}" style="background:#B7862C;color:#0E2A28;font-weight:700;text-decoration:none;padding:13px 28px;border-radius:2px;display:inline-block;">See all latest updates</a>
    </div>
  </div>
  <div style="background:#F6F1E4;padding:16px 24px;color:#8a8578;font-size:12px;line-height:1.6;">
    You're receiving this because you subscribed at umrahnow.in. UmrahNow is an independent guide, not a government body — advisories change, so always check the official source.<br>
    <a href="{unsubscribe_tag}" style="color:#144C46;">Unsubscribe</a>
  </div>
</div></body></html>"""
    return subject, body


# ------------------------------------------------------------------- Brevo
class BrevoError(RuntimeError):
    pass


def brevo(method, path, key, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "api-key": key, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": DIG_UA})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raise BrevoError(f"HTTP {e.code} from Brevo {method} {path}: {e.read().decode('utf-8', errors='replace')[:400]}")


def next_send_time(now=None):
    """Next delivery moment (UTC). GitHub starts scheduled jobs hours late, so
    the job only PREPARES the email; Brevo delivers it at this exact time.
    Default 03:00 UTC = 08:30 IST; change with the DIGEST_SEND_AT_UTC variable (HH:MM)."""
    now = now or datetime.now(timezone.utc)
    hh, mm = (os.environ.get("DIGEST_SEND_AT_UTC") or "03:00").strip().split(":")
    t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if t <= now + timedelta(minutes=20):
        t += timedelta(days=1)
    return t


def send(advisories, news, site, key, list_id, sender_email, sender_name, send_at=None):
    """send_at=None sends immediately; otherwise Brevo delivers at that UTC time."""
    when = (send_at or datetime.now(timezone.utc)).date()
    name = f"UmrahNow daily digest {when.isoformat()}"

    # Never create two digests for the same delivery day (re-runs, manual tests).
    try:
        recent = brevo("GET", "/emailCampaigns?limit=30&sort=desc", key).get("campaigns", [])
        if any(c.get("name") == name for c in recent):
            print(f"A digest for {when.isoformat()} already exists -- not creating another.")
            return
    except Exception as e:
        print(f"(could not check for an existing campaign: {e}; continuing)")

    last_err = None
    for tag in ("{{ unsubscribe }}", "[UNSUBSCRIBE]"):  # current tag first, legacy tag as a fallback
        subject, body = build_email(advisories, news, site, tag, when)
        payload = {"name": name, "subject": subject, "htmlContent": body,
                   "sender": {"name": sender_name, "email": sender_email},
                   "recipients": {"listIds": [int(list_id)]}}
        if send_at:
            payload["scheduledAt"] = send_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            created = brevo("POST", "/emailCampaigns", key, payload)
            cid = created.get("id")
            if not cid:
                raise BrevoError(f"campaign created but no id returned: {created}")
            if not send_at:
                brevo("POST", f"/emailCampaigns/{cid}/sendNow", key)
                print(f"Sent campaign {cid}: {subject}")
                return
            # Scheduled: confirm Brevo really queued it; if it's still a draft, send now rather than never.
            status = ""
            try:
                status = (brevo("GET", f"/emailCampaigns/{cid}", key).get("status") or "")
            except BrevoError as e:
                print(f"(could not read campaign status: {e})")
            if status == "draft":
                print(f"Campaign {cid} was left as a draft by Brevo -- sending it now instead.")
                brevo("POST", f"/emailCampaigns/{cid}/sendNow", key)
                print(f"Sent campaign {cid}: {subject}")
            else:
                print(f"Scheduled campaign {cid} for {send_at:%Y-%m-%d %H:%M} UTC "
                      f"(status: {status or 'unknown'}): {subject}")
            return
        except BrevoError as e:
            last_err = e
            if "unsubscribe" in str(e).lower() and tag == "{{ unsubscribe }}":
                print("Brevo didn't accept the unsubscribe tag; retrying with the legacy tag...")
                continue
            break
    raise last_err


def digest_main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    page = Path(args[0]) if args else next((Path(n) for n in ("index.html", "umrahnow.html") if Path(n).exists()), None)
    if not page or not page.exists():
        sys.exit("Page not found. Usage: python scripts/send_digest.py index.html [--dry-run]")
    page_html = page.read_text(encoding="utf-8")
    advisories, news = read_advisories(page_html), read_news(page_html)
    site = os.environ.get("SITE_URL") or "https://umrahnow.in"
    print(f"Digest content: {len(advisories)} advisory item(s), {len(news)} update(s).")

    if dry:
        subject, body = build_email(advisories, news, site)
        Path("digest-preview.html").write_text(body, encoding="utf-8")
        print(f"--dry-run: subject would be: {subject}\nPreview written to digest-preview.html")
        return

    key = os.environ.get("BREVO_API_KEY", "").strip()
    list_id = os.environ.get("BREVO_LIST_ID", "").strip()
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "").strip() or "UmrahNow"
    missing = [n for n, v in (("BREVO_API_KEY", key), ("BREVO_LIST_ID", list_id), ("BREVO_SENDER_EMAIL", sender_email)) if not v]
    if missing:
        print("Email digest not configured yet (missing: " + ", ".join(missing) + ") -- skipping. See the setup notes.")
        return
    if not advisories and not news:
        print("Nothing to put in the digest -- not sending.")
        return
    if not list_id.isdigit():
        sys.exit("BREVO_LIST_ID must be the numeric list ID.")
    send_at = None if "--now" in sys.argv else next_send_time()
    try:
        send(advisories, news, site, key, list_id, sender_email, sender_name, send_at)
    except BrevoError as e:
        sys.exit(f"Brevo error: {e}")




# =====================================================================
# Entry point
# =====================================================================
def main():
    argv = sys.argv[1:]
    flags = [a for a in argv if a.startswith("--")]
    paths = [a for a in argv if not a.startswith("--")]
    page = paths[0] if paths else next((n for n in ("index.html", "umrahnow.html") if Path(n).exists()), None)
    if not page or not Path(page).exists():
        sys.exit("Page not found. Usage: python scripts/update_news.py index.html "
                 "[--digest [--now]] [--dry-run] [--news-only] [--advisories-only]")
    prog = sys.argv[0]
    dry = ["--dry-run"] if "--dry-run" in flags else []

    if "--digest" in flags:
        sys.argv = [prog, page] + dry + (["--now"] if "--now" in flags else [])
        digest_main()
        return

    steps = []
    if "--advisories-only" not in flags:
        steps.append(("news cards", news_main, []))
    if "--news-only" not in flags:
        steps.append(("travel advisories", advisories_main, dry))
    failed = []
    for name, fn, extra in steps:
        print(f"\n===== {name} =====")
        sys.argv = [prog, page] + extra
        try:
            fn()
        except SystemExit as e:
            if e.code not in (None, 0):
                print(f"WARNING [{name}]: {e.code}")
                failed.append(name)
    if failed and len(failed) == len(steps):
        sys.exit("Everything failed: " + ", ".join(failed))
    if failed:
        print("\nNote: " + ", ".join(failed) + " failed (see above); the rest completed.")


if __name__ == "__main__":
    main()

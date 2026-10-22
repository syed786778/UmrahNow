#!/usr/bin/env python3
"""
Refreshes the "Travel advisories" ticker in the UmrahNow HTML page.

Sources (official first, news wires only as a fallback):
  IN  Embassy of India, Riyadh + Consulate General of India, Jeddah (notices)
  US  travel.state.gov Saudi Arabia advisory + US Embassy security alerts
  UK  GOV.UK / FCDO "Saudi Arabia travel advice"

How it behaves
  * Each source is fetched independently. If one fails (site down, blocks the
    request, page layout changed), its previously saved items are KEPT -- the
    ticker is never blanked -- and the failure is printed in the log.
  * Every item remembers when it was last successfully checked
    (data-checked); the page shows "checked N hours ago" and flags the bar as
    stale if that gets old, so visitors are never told something is current
    when the updater has stopped working.
  * The page is only rewritten when something changed (or a timestamp is
    >20h old), so scheduled runs don't create pointless commits.
  * Only text the sources themselves publish is summarised -- nothing is
    interpreted or added. Each item links to the official page.

Usage:
    python scripts/update_news.py path/to/page.html [--dry-run]

Standard library only.
"""
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

START, END = "<!-- ADVISORY_LIST_START -->", "<!-- ADVISORY_LIST_END -->"
UA = "Mozilla/5.0 (compatible; UmrahNowBot/1.0; +https://umrahnow.in)"
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
        ("User-Agent", UA),
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


def parse_existing(block):
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


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    page = Path(args[0]) if args else next((Path(n) for n in ("index.html", "umrahnow.html") if Path(n).exists()), None)
    if not page or not page.exists():
        sys.exit("Page not found. Usage: python scripts/update_news.py index.html [--dry-run]")
    text = page.read_text(encoding="utf-8")
    if START not in text or END not in text:
        sys.exit("ADVISORY_LIST_START / ADVISORY_LIST_END markers not found in the page.")
    head, rest = text.split(START, 1)
    block, tail = rest.split(END, 1)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    existing = parse_existing(block)
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
    page.write_text(new_head + START + "\n" + "\n".join(render(i) for i in final) + "\n    " + END + tail,
                    encoding="utf-8")
    print(f"Updated {page}: {len(final)} item(s) ({'content changed' if changed else 'timestamps refreshed'}).")


if __name__ == "__main__":
    main()

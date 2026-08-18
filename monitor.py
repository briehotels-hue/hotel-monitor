"""
BRE Hotels & Resorts — Daily Distressed Hotel Monitor
Sources: RealINSIGHT/JLL, Ten-X, Google News RSS, CourtListener/PACER, Industry RSS
Sends formatted digest email via Gmail every morning
"""

import os
import re
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import dataclass
from typing import List, Optional
import time

import requests
from bs4 import BeautifulSoup
import feedparser

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL    = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)
MIN_LOAN_SIZE      = int(os.environ.get("MIN_LOAN_SIZE", "20000000"))
MIN_KEYS           = int(os.environ.get("MIN_KEYS", "75"))

# ── BRE TARGET MARKETS ────────────────────────────────────────────────────────
BRE_MARKETS = [
    "san francisco", "silicon valley", "palo alto", "menlo park", "san jose",
    "miami", "miami beach", "bal harbour", "boca raton", "coral gables",
    "fort lauderdale", "palm beach", "surfside",
    "key west", "florida keys", "islamorada", "duck key",
    "aspen", "vail", "jackson hole", "jackson", "teton",
    "honolulu", "waikiki", "maui", "kauai", "hawaii",
    "santa monica", "napa", "sonoma",
]

# ── WATCH SPONSORS ────────────────────────────────────────────────────────────
WATCH_SPONSORS = [
    "artic", "al rayyan", "ashford", "braemar", "ksl capital",
    "brookfield hotel", "atrium hospitality", "manchester financial",
    "slatkin", "kor group", "westbrook", "driftwood",
    "columbia sussex", "lone star", "thor urban", "crescent real estate",
    "penn-florida", "five star development", "sterling global",
]

# ── KEYWORDS ──────────────────────────────────────────────────────────────────
HOTEL_KEYWORDS = [
    "hotel", "resort", "inn", "suites", "hospitality",
    "ritz", "marriott", "hilton", "hyatt", "westin", "sheraton",
    "four seasons", "st. regis", "waldorf", "intercontinental",
    "fairmont", "kimpton", "thompson", "autograph", "luxury collection",
    "w hotel", "edition", "andaz", "park hyatt", "grand hyatt",
    "loews", "omni", "rosewood", "auberge", "montage", "pendry",
    "proper hotel", "ace hotel", "1 hotel", "nobu hotel",
]

DISTRESS_KEYWORDS = [
    "foreclosure", "special servicer", "reo", "note sale", "npl",
    "non-performing", "default", "distressed", "credit bid",
    "deed in lieu", "chapter 11", "bankruptcy", "trustee sale",
    "auction", "receivership", "matured balloon", "watchlist",
    "transferred to special", "midland loan", "lnr partners",
    "situs", "trimont", "cwcapital", "rialto", "stalking horse",
    "realinsight", "ten-x auction",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── DATA CLASS ────────────────────────────────────────────────────────────────
@dataclass
class HotelAlert:
    name: str
    location: str
    source: str
    url: str
    description: str = ""
    loan_size: Optional[int] = None
    keys: Optional[int] = None
    auction_date: Optional[str] = None
    sponsor: str = ""
    score: int = 0
    action: str = ""

# ── SCORING ───────────────────────────────────────────────────────────────────
def score_asset(a: HotelAlert) -> int:
    score = 0
    text = (a.name + " " + a.location + " " + a.description + " " + a.sponsor).lower()

    if any(m in text for m in BRE_MARKETS):
        score += 3

    luxury = [
        "ritz", "four seasons", "st. regis", "waldorf", "park hyatt",
        "fairmont", "rosewood", "auberge", "montage", "proper hotel",
        "intercontinental", "grand hyatt", "westin", "loews",
        "thompson", "kimpton", "nobu", "edition", "1 hotel",
    ]
    if any(b in text for b in luxury):
        score += 2

    if a.auction_date:
        try:
            days = (datetime.strptime(a.auction_date, "%Y-%m-%d") - datetime.now()).days
            if 0 <= days <= 30:
                score += 3
            elif 31 <= days <= 60:
                score += 1
        except:
            score += 1

    if any(s in text for s in WATCH_SPONSORS):
        score += 2

    if a.loan_size:
        if a.loan_size >= 200_000_000:
            score += 2
        elif a.loan_size >= 80_000_000:
            score += 1

    if a.source == "PACER / CourtListener":
        score += 2

    if "notice of default" in text or "notice of sale" in text:
        score += 2

    if "stalking horse" in text or "credit bid" in text:
        score += 2

    return score

# ── HELPERS ───────────────────────────────────────────────────────────────────
def parse_dollar(text: str) -> Optional[int]:
    text = str(text).replace(",", "").lower()
    patterns = [
        (r'\$(\d+(?:\.\d+)?)\s*b(?:illion)?', 1_000_000_000),
        (r'\$(\d+(?:\.\d+)?)\s*mm', 1_000_000),
        (r'\$(\d+(?:\.\d+)?)\s*m(?:illion)?', 1_000_000),
        (r'\$(\d{6,})', 1),
    ]
    amounts = []
    for pattern, multiplier in patterns:
        for match in re.finditer(pattern, text):
            try:
                amounts.append(int(float(match.group(1)) * multiplier))
            except:
                pass
    return max(amounts) if amounts else None

def parse_date(text: str) -> Optional[str]:
    for fmt in ["%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(text.strip(), fmt).strftime("%Y-%m-%d")
        except:
            continue
    return None

def extract_location(text: str) -> str:
    for market in BRE_MARKETS:
        if market in text.lower():
            return market.title()
    states = re.findall(r'\b([A-Z]{2})\b', text)
    us_states = {"CA","FL","NY","TX","HI","CO","WY","AZ","GA","IL","WA","OR","NV","NC","SC"}
    for st in states:
        if st in us_states:
            return st
    return ""

def action_for(a: HotelAlert) -> str:
    if a.score >= 7:
        if a.auction_date:
            return f"⚡ Active auction — date: {a.auction_date}. Register immediately."
        if a.source == "PACER / CourtListener":
            return "⚡ Chapter 11 filing — pull PACER, identify stalking horse / auction timeline"
        if any(m in a.location.lower() for m in BRE_MARKETS):
            return "⚡ BRE market — pull TREPP detail, call servicer this week"
        return "⚡ Pull TREPP detail — research sponsor and capital stack immediately"
    if a.score >= 4:
        return "📋 Watch — pull TREPP detail if loan size confirms"
    return "👁 FYI — monitor for developments"

# ── SCRAPER: REALINSIGHT + TEN-X ──────────────────────────────────────────────
def scrape_auction_platforms() -> List[HotelAlert]:
    alerts = []
    log.info("Scraping auction platforms (RealINSIGHT, Ten-X)...")

    platforms = [
        ("RealINSIGHT / JLL", [
            "https://www.realinsightmarketplace.com/properties?type=hotel",
            "https://www.realinsightmarketplace.com/properties?type=hospitality",
        ], "https://www.realinsightmarketplace.com"),
        ("Ten-X", [
            "https://www.ten-x.com/properties/?type=hospitality",
            "https://www.ten-x.com/properties/?type=hotel",
        ], "https://www.ten-x.com"),
    ]

    for platform_name, urls, base_url in platforms:
        for url in urls:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=15)
                if resp.status_code != 200:
                    log.warning(f"{platform_name} returned {resp.status_code}")
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.find_all(
                    ["div", "article", "li", "section"],
                    class_=re.compile(r"property|listing|card|asset|result|item", re.I)
                )
                for card in cards[:40]:
                    text = card.get_text(" ", strip=True)
                    if not any(k in text.lower() for k in HOTEL_KEYWORDS):
                        continue
                    name_el = card.find(["h2", "h3", "h4", "a"])
                    name = name_el.get_text(strip=True) if name_el else "Unknown Property"
                    if len(name) < 5:
                        continue
                    price = parse_dollar(text)
                    if price and price < MIN_LOAN_SIZE:
                        continue
                    location = extract_location(text)
                    date_el = card.find(class_=re.compile(r"date|auction|end|close", re.I))
                    auction_date = parse_date(date_el.get_text(strip=True)) if date_el else None
                    link_el = card.find("a", href=True)
                    link = link_el["href"] if link_el else url
                    if link.startswith("/"):
                        link = base_url + link
                    a = HotelAlert(
                        name=name[:100], location=location,
                        source=platform_name, url=link,
                        description=text[:300], loan_size=price,
                        auction_date=auction_date,
                    )
                    a.score = score_asset(a)
                    a.action = action_for(a)
                    alerts.append(a)
            except Exception as e:
                log.error(f"{platform_name} scrape error: {e}")
            time.sleep(2)

    # Google News specifically for auction platform mentions
    auction_queries = [
        "hotel auction RealINSIGHT JLL 2026",
        "hotel foreclosure auction Ten-X 2026",
        "luxury hotel credit bid auction 2026",
        "hotel stalking horse bid Chapter 11 2026",
        "hotel trustee sale auction California Florida 2026",
    ]
    cutoff = datetime.now() - timedelta(days=3)
    for query in auction_queries:
        try:
            encoded = requests.utils.quote(query)
            rss = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(rss)
            for entry in feed.entries[:6]:
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    if datetime(*entry.published_parsed[:6]) < cutoff:
                        continue
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", "")
                combined = (title + " " + summary).lower()
                if not any(k in combined for k in HOTEL_KEYWORDS):
                    continue
                if not any(k in combined for k in DISTRESS_KEYWORDS):
                    continue
                price = parse_dollar(combined)
                if price and price < MIN_LOAN_SIZE:
                    continue
                a = HotelAlert(
                    name=title[:100], location=extract_location(combined),
                    source="Auction News", url=link,
                    description=summary[:300], loan_size=price,
                )
                a.score = score_asset(a) + 1
                a.action = action_for(a)
                if not any(x.url == link for x in alerts):
                    alerts.append(a)
        except Exception as e:
            log.error(f"Auction news RSS error: {e}")
        time.sleep(1)

    log.info(f"Auction platforms: {len(alerts)} alerts")
    return alerts

# ── SCRAPER: GOOGLE NEWS RSS ──────────────────────────────────────────────────
def scrape_google_news() -> List[HotelAlert]:
    alerts = []
    log.info("Scraping Google News RSS...")

    queries = [
        # General distress
        "hotel foreclosure special servicer 2026",
        "hotel CMBS special servicer transferred 2026",
        "luxury hotel note sale distressed 2026",
        "hotel REO credit bid foreclosure 2026",
        "hotel Chapter 11 bankruptcy 2026",
        "hotel deed in lieu special servicer 2026",
        "hotel non-performing loan sale 2026",
        "hotel default matured loan workout 2026",
        # BRE target markets
        "San Francisco hotel foreclosure distressed 2026",
        "Miami hotel foreclosure special servicer 2026",
        "Key West hotel foreclosure 2026",
        "Hawaii hotel foreclosure distressed 2026",
        "Aspen Vail Jackson Hole hotel foreclosure 2026",
        "Santa Monica hotel note sale foreclosure 2026",
        "Napa Sonoma hotel foreclosure distressed 2026",
        # Watch sponsors
        "Ashford Hospitality hotel foreclosure 2026",
        "Brookfield hotel foreclosure 2026",
        "KSL Capital hotel distressed 2026",
        # Servicers — signals transfer of any hotel
        "Situs hotel special servicer 2026",
        "LNR Partners hotel special servicer 2026",
        "Midland Loan Services hotel transferred 2026",
    ]

    cutoff = datetime.now() - timedelta(days=3)

    for query in queries:
        try:
            encoded = requests.utils.quote(query)
            rss = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(rss)
            for entry in feed.entries[:6]:
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    if datetime(*entry.published_parsed[:6]) < cutoff:
                        continue
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", "")
                combined = (title + " " + summary).lower()
                if not any(k in combined for k in HOTEL_KEYWORDS):
                    continue
                if not any(k in combined for k in DISTRESS_KEYWORDS):
                    continue
                price = parse_dollar(combined)
                if price and price < MIN_LOAN_SIZE:
                    continue
                a = HotelAlert(
                    name=title[:100], location=extract_location(combined),
                    source="Google News", url=link,
                    description=summary[:400], loan_size=price,
                )
                a.score = score_asset(a)
                a.action = action_for(a)
                if not any(x.url == link for x in alerts):
                    alerts.append(a)
        except Exception as e:
            log.error(f"Google News RSS error: {e}")
        time.sleep(1)

    log.info(f"Google News: {len(alerts)} alerts")
    return alerts

# ── SCRAPER: COURTLISTENER / PACER ────────────────────────────────────────────
def scrape_courtlistener() -> List[HotelAlert]:
    alerts = []
    log.info("Scraping CourtListener (PACER proxy)...")
    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    url = (
        f"https://www.courtlistener.com/api/rest/v3/dockets/"
        f"?date_filed__gte={cutoff}&order_by=-date_filed&format=json"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            for case in resp.json().get("results", [])[:40]:
                name = case.get("case_name", "")
                if not any(k in name.lower() for k in HOTEL_KEYWORDS):
                    continue
                case_url = "https://www.courtlistener.com" + case.get("absolute_url", "")
                filed = case.get("date_filed", "")
                court = case.get("court", "")
                a = HotelAlert(
                    name=name[:100], location=court,
                    source="PACER / CourtListener", url=case_url,
                    description=f"Chapter 11 bankruptcy filing dated {filed}. "
                                f"Court: {court}. Monitor for stalking horse bid and auction date.",
                )
                a.score = score_asset(a) + 2
                a.action = action_for(a)
                alerts.append(a)
    except Exception as e:
        log.error(f"CourtListener error: {e}")
    log.info(f"CourtListener: {len(alerts)} alerts")
    return alerts

# ── SCRAPER: INDUSTRY NEWS RSS ────────────────────────────────────────────────
def scrape_industry_news() -> List[HotelAlert]:
    alerts = []
    log.info("Scraping industry news RSS...")

    feeds = [
        ("Hotel News Now",   "https://www.hotelnewsnow.com/rss"),
        ("Hotel Management", "https://www.hotelmanagement.net/rss.xml"),
        ("Bisnow",           "https://www.bisnow.com/rss/national/hospitality"),
        ("The Real Deal",    "https://therealdeal.com/feed/"),
        ("CoStar News",      "https://www.costar.com/rss/news"),
        ("GlobeSt",          "https://www.globest.com/feed/"),
    ]

    cutoff = datetime.now() - timedelta(days=2)

    for source_name, feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    if datetime(*entry.published_parsed[:6]) < cutoff:
                        continue
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                link = entry.get("link", "")
                combined = (title + " " + summary).lower()
                if not any(k in combined for k in DISTRESS_KEYWORDS):
                    continue
                if not any(k in combined for k in HOTEL_KEYWORDS):
                    continue
                price = parse_dollar(combined)
                if price and price < MIN_LOAN_SIZE:
                    continue
                a = HotelAlert(
                    name=title[:100], location=extract_location(combined),
                    source=source_name, url=link,
                    description=summary[:400], loan_size=price,
                )
                a.score = score_asset(a)
                a.action = action_for(a)
                if not any(x.url == link for x in alerts):
                    alerts.append(a)
        except Exception as e:
            log.error(f"{source_name} RSS error: {e}")
        time.sleep(1)

    log.info(f"Industry news: {len(alerts)} alerts")
    return alerts

# ── EMAIL ─────────────────────────────────────────────────────────────────────
def build_html(alerts: List[HotelAlert]) -> str:
    run_date = datetime.now().strftime("%A, %B %d, %Y")
    immediate = [a for a in alerts if a.score >= 7]
    watch     = [a for a in alerts if 4 <= a.score < 7]
    fyi       = [a for a in alerts if a.score < 4]

    def fmt_loan(v):
        if not v: return "N/A"
        if v >= 1_000_000_000: return f"${v/1e9:.1f}B"
        return f"${v/1e6:.0f}mm"

    def card(a, bg, border):
        auction_str = f"<br><b>🗓 Auction Date:</b> <span style='color:#C00000;font-weight:bold;'>{a.auction_date}</span>" if a.auction_date else ""
        keys_str = f"<br><b>Keys:</b> {a.keys}" if a.keys else ""
        return f"""
        <div style="background:{bg};border-left:4px solid {border};
                    margin:8px 0;padding:12px;border-radius:4px;
                    font-family:Arial,sans-serif;">
          <div style="font-size:14px;font-weight:bold;color:#1F4E79;">
            {a.name}
          </div>
          <div style="font-size:11px;color:#595959;margin:4px 0;">
            <b>Source:</b> {a.source} &nbsp;|&nbsp;
            <b>Location:</b> {a.location or "—"} &nbsp;|&nbsp;
            <b>Loan Size:</b> {fmt_loan(a.loan_size)} &nbsp;|&nbsp;
            <b>Score:</b> {a.score}
            {auction_str}{keys_str}
          </div>
          <div style="font-size:11px;color:#333;margin:6px 0;
                      background:rgba(0,0,0,0.04);padding:6px;border-radius:3px;">
            {a.description[:280]}...
          </div>
          <div style="font-size:12px;font-weight:bold;color:#C00000;margin-top:6px;">
            {a.action}
          </div>
          <a href="{a.url}" style="font-size:10px;color:#0070C0;
                                    text-decoration:none;">
            View Source →
          </a>
        </div>"""

    def section(items, title, color, bg, border):
        body = "".join(card(a, bg, border) for a in items) if items else \
               "<p style='color:#aaa;font-size:11px;font-style:italic;padding:8px;'>None today.</p>"
        return f"""
        <h2 style="color:{color};font-size:13px;
                   border-bottom:2px solid {color};
                   padding-bottom:4px;margin-top:24px;">
          {title} ({len(items)})
        </h2>{body}"""

    fyi_rows = "".join(
        f"<div style='font-size:10px;padding:4px 0;border-bottom:1px solid #eee;'>"
        f"<b>{a.name}</b> — {a.location or '—'} — "
        f"{fmt_loan(a.loan_size)} — "
        f"<a href='{a.url}' style='color:#0070C0;'>{a.source}</a>"
        f"</div>"
        for a in fyi[:10]
    ) or "<p style='color:#aaa;font-size:11px;font-style:italic;'>None today.</p>"

    no_situations = ""
    if not alerts:
        no_situations = """
        <div style="text-align:center;padding:40px;color:#595959;font-style:italic;">
          No new distressed hotel situations flagged today.<br>
          <span style="font-size:11px;">The monitor ran successfully across all sources.</span>
        </div>"""

    return f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:780px;
                 margin:0 auto;padding:20px;background:#FAFAFA;">

      <div style="background:#1F4E79;padding:18px 20px;
                  border-radius:8px;margin-bottom:4px;">
        <h1 style="color:white;margin:0;font-size:18px;letter-spacing:-0.5px;">
          BRE Hotels — Daily Distressed Pipeline Monitor
        </h1>
        <p style="color:#DDEEFF;margin:6px 0 0;font-size:11px;">
          {run_date} &nbsp;·&nbsp; {len(alerts)} total situations &nbsp;·&nbsp;
          {len(immediate)} immediate &nbsp;·&nbsp;
          {len(watch)} watch &nbsp;·&nbsp;
          {len(fyi)} FYI
        </p>
      </div>

      <div style="background:#E8F0E8;padding:6px 12px;font-size:9px;
                  color:#375623;margin-bottom:16px;border-radius:0 0 4px 4px;">
        Sources: RealINSIGHT/JLL · Ten-X · Google News RSS (21 queries) ·
        CourtListener/PACER · Hotel News Now · Bisnow ·
        Hotel Management · The Real Deal · GlobeSt &nbsp;|&nbsp;
        Filters: Hotel/resort · Loan ≥${MIN_LOAN_SIZE//1_000_000}mm · Keys ≥{MIN_KEYS}
      </div>

      {no_situations}
      {section(immediate, "🔴 IMMEDIATE FLAGS", "#C00000", "#FCE4D6", "#C00000")}
      {section(watch,     "🟡 WATCH",           "#BF9000", "#FFF2CC", "#BF9000")}

      <h2 style="color:#595959;font-size:13px;border-bottom:1px solid #ddd;
                 padding-bottom:4px;margin-top:24px;">
        FYI ({len(fyi)})
      </h2>
      {fyi_rows}

    </body>
    </html>"""


def send_email(alerts: List[HotelAlert]):
    run_date = datetime.now().strftime("%b %d")
    n_imm = len([a for a in alerts if a.score >= 7])
    n_watch = len([a for a in alerts if 4 <= a.score < 7])

    if not alerts:
        subject = f"Hotel Monitor — No New Situations — {run_date}"
    elif n_imm:
        subject = f"🔴 Hotel Monitor — {n_imm} Immediate Flag(s) — {run_date}"
    elif n_watch:
        subject = f"🟡 Hotel Monitor — {n_watch} Watch Item(s) — {run_date}"
    else:
        subject = f"Hotel Monitor — {len(alerts)} FYI Item(s) — {run_date}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(build_html(alerts), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    log.info(f"Email sent — {subject}")


# ── DEDUPLICATE ───────────────────────────────────────────────────────────────
def deduplicate(alerts: List[HotelAlert]) -> List[HotelAlert]:
    seen_urls, seen_names, unique = set(), set(), []
    for a in sorted(alerts, key=lambda x: x.score, reverse=True):
        key = re.sub(r'[^a-z0-9]', '', a.name.lower())[:40]
        if a.url in seen_urls:
            continue
        if key in seen_names and len(key) > 10:
            continue
        seen_urls.add(a.url)
        seen_names.add(key)
        unique.append(a)
    return unique


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("BRE Hotel Monitor starting...")
    log.info(f"Config: min_loan=${MIN_LOAN_SIZE//1e6:.0f}mm, min_keys={MIN_KEYS}")

    alerts: List[HotelAlert] = []
    alerts += scrape_auction_platforms()
    alerts += scrape_google_news()
    alerts += scrape_courtlistener()
    alerts += scrape_industry_news()

    alerts = deduplicate(alerts)
    alerts.sort(key=lambda x: x.score, reverse=True)

    n_imm   = len([a for a in alerts if a.score >= 7])
    n_watch = len([a for a in alerts if 4 <= a.score < 7])
    n_fyi   = len([a for a in alerts if a.score < 4])
    log.info(f"Final: {len(alerts)} unique — {n_imm} immediate / {n_watch} watch / {n_fyi} FYI")

    send_email(alerts)
    log.info("Done.")


if __name__ == "__main__":
    main()

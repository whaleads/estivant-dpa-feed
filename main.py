#!/usr/bin/env python3
"""
Estivant DPA feed crawler
=========================
Crawlt dagelijks estivant.nl en bouwt twee Meta 'destinations'-catalogusfeeds:
  - EOG  (eenoudervakanties)  -> output/feed_eog.xml  + output/feed_eog.csv
  - SNG  (singlereizen)       -> output/feed_sng.xml  + output/feed_sng.csv

Bron van waarheid:
  1. /sitemap.xml  -> lijst met alle reis-URL's  (patroon /<sectie>/<land>/<slug>)
  2. per reis: de GA4 `dataLayer` view_item  (item_id, naam, land, categorie, thema, prijs)
     + <meta name=description> + <link rel=canonical> + hero-afbeelding

Een pagina telt alleen als boekbare reis wanneer er een view_item is MET price > 0.
Zo vallen land-/thema-/overzichtspagina's (price 0) automatisch af.

BELANGRIJK (pixel-matching): destination_id == item_id uit de dataLayer, want dat is
de waarde die de Meta-pixel als content_ids meestuurt. Niet aanpassen/prefixen.

Optioneel: als GOOGLE_SERVICE_ACCOUNT_JSON + SPREADSHEET_ID gezet zijn, worden de
feeds ook naar twee Google Sheet-tabs (EOG / SNG) geschreven.
"""

import asyncio
import csv
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from playwright.async_api import async_playwright

BASE = "https://www.estivant.nl"
SITEMAP_URL = f"{BASE}/sitemap.xml"
OUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# Dutch country slugs used in trip URLs (segment[1]). Vul aan als er landen bijkomen.
COUNTRY_NAME = {
    "belgie": "België", "denemarken": "Denemarken", "duitsland": "Duitsland",
    "frankrijk": "Frankrijk", "griekenland": "Griekenland", "italie": "Italië",
    "kroatie": "Kroatië", "nederland": "Nederland", "oostenrijk": "Oostenrijk",
    "albanie": "Albanië", "egypte": "Egypte", "georgie": "Georgië",
    "portugal": "Portugal", "slovenie": "Slovenië", "spanje": "Spanje",
    "zweden": "Zweden", "turkije": "Turkije", "marokko": "Marokko",
    "malta": "Malta", "cyprus": "Cyprus", "montenegro": "Montenegro",
    "noorwegen": "Noorwegen", "finland": "Finland", "ijsland": "IJsland",
    "zwitserland": "Zwitserland", "tsjechie": "Tsjechië", "polen": "Polen",
    "hongarije": "Hongarije", "roemenie": "Roemenië", "bulgarije": "Bulgarije",
    "kaapverdie": "Kaapverdië", "tunesie": "Tunesië",
}

SEASON_KEYWORDS = [
    "Wintersport", "Voorjaarsvakantie", "Kerstvakantie", "Herfstvakantie",
    "Meivakantie", "Zomervakantie", "Zomer", "Winter", "Hemelvaart", "Pinksteren",
]

# JS die in de gerenderde pagina de feed-velden ophaalt.
EXTRACT_JS = r"""
() => {
  const dl = window.dataLayer || [];
  const vi = dl.find(e => e && e.event === 'view_item' && e.ecommerce && e.ecommerce.items && e.ecommerce.items.length);
  const item = vi ? vi.ecommerce.items[0] : null;
  const currency = vi && vi.ecommerce ? (vi.ecommerce.currency || 'EUR') : 'EUR';
  const metaDesc = (document.querySelector('meta[name="description"]') || {}).content || '';
  const canonical = (document.querySelector('link[rel="canonical"]') || {}).href || location.href;
  const ogImg = (document.querySelector('meta[property="og:image"]') || {}).content || '';
  // hero: liefst een estivant media-afbeelding op 1440px, anders eerste media-afbeelding, anders og:image
  const media = [...document.querySelectorAll('img')].map(i => i.currentSrc || i.src).filter(Boolean);
  const hero =
      media.find(s => /estivant\.(nl|com)\/media/i.test(s) && /width=1440/.test(s)) ||
      media.find(s => /estivant\.(nl|com)\/media/i.test(s)) ||
      ogImg || '';
  // galerij: unieke estivant media-afbeeldingen (zonder query), voor additional_image_link
  const gallery = [...new Set(
      media.filter(s => /estivant\.(nl|com)\/media/i.test(s)).map(s => s.split('?')[0])
  )];
  return { item, currency, metaDesc, canonical, hero, gallery };
}
"""


# JS die de vertrekkalender uitleest. Robuust tegen onbekende layout: pak elke link
# naar een reis-detailpagina en lees de tekst van de omliggende rij-container.
CALENDAR_JS = r"""
() => {
  const body = document.body.innerText || '';
  if (/aan deze pagina wordt gewerkt|nog even geduld|in aanbouw/i.test(body)) {
    return { underConstruction: true, rows: [] };
  }
  const tripRe = /\/(eenoudervakanties|singlereizen)\/[^\/]+\/[^\/]+\/?$/;
  const rows = [];
  const anchors = [...document.querySelectorAll('a[href]')].filter(a => {
    try { return tripRe.test(new URL(a.href, location.origin).pathname); } catch (e) { return false; }
  });
  for (const a of anchors) {
    const path = new URL(a.href, location.origin).pathname.replace(/\/$/, '');
    // loop omhoog naar een rij-achtige container (tabelrij of element met row/rij/vertrek in class)
    let el = a, cont = a;
    for (let i = 0; i < 6 && el; i++) {
      el = el.parentElement;
      if (!el) break;
      const cls = (el.className && el.className.toString ? el.className.toString() : '');
      if (el.tagName === 'TR' || el.getAttribute('role') === 'row' || /row|rij|vertrek|departure|kalender|listing|card/i.test(cls)) { cont = el; break; }
      cont = el;
    }
    rows.push({ path, text: (cont.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300) });
  }
  return { underConstruction: false, rows };
}
"""

_MONTHS = "jan|feb|mrt|maa|apr|mei|jun|jul|aug|sep|okt|nov|dec"


def parse_calendar_rows(raw_rows):
    """Bouw map: detail-path -> {duration, next_departure, availability, departures}."""
    cal = {}
    for r in raw_rows:
        path = (r.get("path") or "").rstrip("/")
        text = r.get("text") or ""
        if not path:
            continue
        date_m = re.search(rf"\b(\d{{1,2}}\s+(?:{_MONTHS})[a-z]*)\b", text, re.IGNORECASE)
        dur_m = re.search(r"(\d+)\s*(?:dg|dgn|dagen|nachten)", text, re.IGNORECASE)
        avail_bookable = bool(re.search(r"beschikbaar|bijna vol|boek", text, re.IGNORECASE))
        avail_sold = bool(re.search(r"uitverkocht|\bvol\b|geen beschikbaarheid", text, re.IGNORECASE))
        entry = cal.setdefault(path, {
            "next_departure": "", "duration": 0, "departures": 0,
            "any_bookable": False, "any_sold": False,
        })
        entry["departures"] += 1
        if not entry["next_departure"] and date_m:
            entry["next_departure"] = date_m.group(1)   # kalender is chronologisch: eerste = eerstvolgende
        if not entry["duration"] and dur_m:
            entry["duration"] = int(dur_m.group(1))
        entry["any_bookable"] = entry["any_bookable"] or avail_bookable
        entry["any_sold"] = entry["any_sold"] or avail_sold
    # availability afleiden
    for e in cal.values():
        if e["any_bookable"]:
            e["availability"] = "in stock"
        elif e["any_sold"]:
            e["availability"] = "out of stock"
        else:
            e["availability"] = ""   # onbekend -> laat feed-default staan
    return cal


async def fetch_calendar(page, seg):
    """Laad de vertrekkalender voor een segment en geef de geparste map terug (leeg = niet live)."""
    url = f"{BASE}/{'eenoudervakanties' if seg == 'eog' else 'singlereizen'}/vertrekkalender"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_function(
                "() => { const b=document.body.innerText||''; "
                "return /aan deze pagina wordt gewerkt|nog even geduld|in aanbouw/i.test(b) "
                "|| [...document.querySelectorAll('a[href]')].some(a => /\\/(eenoudervakanties|singlereizen)\\/[^/]+\\/[^/]+/.test(a.getAttribute('href')||'')); }",
                timeout=8000,
            )
        except Exception:
            pass
        data = await page.evaluate(CALENDAR_JS)
    except Exception as e:
        print(f"[vertrekkalender] {seg}: fout bij laden ({e}) — overgeslagen", flush=True)
        return {}
    if data.get("underConstruction"):
        print(f"[vertrekkalender] {seg}: nog in aanbouw — enrichment overgeslagen", flush=True)
        return {}
    cal = parse_calendar_rows(data.get("rows") or [])
    print(f"[vertrekkalender] {seg}: {len(cal)} reizen met vertrekdata", flush=True)
    return cal


def fetch_sitemap_paths():
    req = urllib.request.Request(SITEMAP_URL, headers={"User-Agent": "EstivantFeedBot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        xml = r.read().decode("utf-8", "replace")
    locs = re.findall(r"<loc>([^<]+)</loc>", xml)
    paths = []
    for u in locs:
        m = re.match(r"https?://[^/]+(/.*)$", u)
        paths.append(m.group(1) if m else u)
    return paths


def is_trip_path(path):
    """Reis-detailpagina = /<sectie>/<land>/<slug> met land in COUNTRY_NAME."""
    segs = [s for s in path.split("/") if s]
    return (
        len(segs) == 3
        and segs[0] in ("eenoudervakanties", "singlereizen")
        and segs[1] in COUNTRY_NAME
    )


def segment_of(path):
    return "eog" if path.startswith("/eenoudervakanties/") else "sng"


def clean_place(item_name, slug):
    """Zuivere bestemming (stad) afleiden: prefix + seizoenswoorden eraf."""
    if item_name:
        name = re.sub(r"^(Eenoudervakantie|Singlereis|Singlevakantie)\s*", "", item_name).strip()
    else:
        name = slug.replace("-", " ").title()
    # seizoen/periode-achtervoegsels uit de stadsnaam halen (bv. "Vielsalm Herfst" -> "Vielsalm")
    suffixes = SEASON_KEYWORDS + [
        "Herfst", "Kerst", "Voorjaar", "Zomer", "Winter", "Pasen",
        "Feestdagen", "Meivakantie", "Wintersport",
    ]
    changed = True
    while changed:
        changed = False
        for kw in suffixes:
            new = re.sub(rf"\s*\b{re.escape(kw)}\b\s*$", "", name, flags=re.IGNORECASE).strip()
            if new != name and new:
                name, changed = new, True
    return name or slug.replace("-", " ").title()


def derive_season(thema):
    for kw in SEASON_KEYWORDS:
        if kw.lower() in (thema or "").lower():
            return kw
    return ""


async def crawl():
    paths = fetch_sitemap_paths()
    trips = [p for p in paths if is_trip_path(p)]
    print(f"[sitemap] {len(paths)} URL's, {len(trips)} kandidaat-reizen", flush=True)

    listings, skipped = [], []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; EstivantFeedBot/1.0)",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()

        # Vertrekkalender-verrijking (no-op zolang de kalender in aanbouw is)
        calendar = {}
        for seg in ("eog", "sng"):
            calendar.update(await fetch_calendar(page, seg))

        for path in trips:
            url = BASE + path
            slug = path.split("/")[-1]
            seg = segment_of(path)
            country = COUNTRY_NAME.get(path.split("/")[2], "")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await page.wait_for_function(
                        "() => (window.dataLayer||[]).some(e => e && e.event==='view_item')",
                        timeout=8000,
                    )
                except Exception:
                    pass
                data = await page.evaluate(EXTRACT_JS)
            except Exception as e:
                skipped.append((path, f"error: {e}"))
                print(f"  ! {path} -> {e}", flush=True)
                continue

            item = data.get("item")
            if not item:
                skipped.append((path, "geen view_item"))
                continue
            try:
                price = float(item.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                skipped.append((path, "price 0 (overzichts-/landingpagina)"))
                continue

            thema = item.get("thema") or ""
            place = clean_place(item.get("item_name"), slug)
            tags = [t.strip() for t in thema.split(",") if t.strip()]
            age_groups = [
                t for t in tags
                if "km" not in t.lower()
                and re.search(r"\d+\s*(?:-\s*\d+\s*)?jaar|\d+\s*(?:plus|\+)", t.lower())
            ]
            hero = data.get("hero") or ""
            hero_base = hero.split("?")[0]
            gallery = data.get("gallery") or []
            additional_images = [g for g in gallery if g != hero_base][:10]
            cal = calendar.get(path.rstrip("/"), {})
            listing = {
                "destination_id": str(item.get("item_id") or "").strip(),
                "name": (item.get("item_name") or place).strip(),
                "description": (data.get("metaDesc") or "").strip(),
                "url": data.get("canonical") or url,
                "price": f"{price:.2f} {data.get('currency', 'EUR')}",
                "price_number": int(round(price)),
                "image_url": data.get("hero") or "",
                "image_tag": (item.get("item_category") or "").strip(),
                "city": place,
                "region": country,
                "country": country,
                "neighborhood": place,
                "type": (item.get("item_category") or "").strip(),
                "segment": "Eenoudervakantie" if seg == "eog" else "Singlereis",
                "season": derive_season(thema),
                "themes": tags,
                "age_groups": ", ".join(age_groups),
                "additional_images": additional_images,
                # vertrekkalender-verrijking (leeg zolang kalender in aanbouw is)
                "duration_nights": cal.get("duration", 0),
                "next_departure": cal.get("next_departure", ""),
                "departures": cal.get("departures", 0),
                "cal_availability": cal.get("availability", ""),
                "seg": seg,
            }
            if not listing["destination_id"]:
                skipped.append((path, "geen item_id"))
                continue
            listings.append(listing)
            print(f"  + [{seg}] {listing['destination_id']:>5}  {listing['name']}  ({listing['price']})", flush=True)

        await browser.close()

    return listings, skipped


# ---------- feed-writers ----------

def _xml_listing(d):
    themes = d["themes"] + [d["country"], d["segment"]]
    themes = [t for t in dict.fromkeys(themes) if t]  # dedup, non-empty
    parts = ["  <listing>"]
    parts.append("    <image>")
    parts.append(f"      <url>{escape(d['image_url'])}</url>")
    if d["image_tag"]:
        parts.append(f"      <tag>{escape(d['image_tag'])}</tag>")
    parts.append("    </image>")
    parts.append(f"    <destination_id>{escape(d['destination_id'])}</destination_id>")
    parts.append(f"    <url>{escape(d['url'])}</url>")
    parts.append(f"    <name>{escape(d['name'])}</name>")
    parts.append(f"    <description>{escape(d['description'])}</description>")
    parts.append(f"    <price>{escape(d['price'])}</price>")
    parts.append('    <address format="simple">')
    parts.append(f"      <component name=\"city\">{escape(d['city'])}</component>")
    parts.append(f"      <component name=\"region\">{escape(d['region'])}</component>")
    parts.append(f"      <component name=\"country\">{escape(d['country'])}</component>")
    parts.append("    </address>")
    parts.append(f"    <neighborhood>{escape(d['neighborhood'])}</neighborhood>")
    if d["type"]:
        parts.append(f"    <type>{escape(d['type'])}</type>")
    parts.append(f"    <type>{escape(d['segment'])}</type>")
    parts.append(f"    <custom_number_0>{d['price_number']}</custom_number_0>")
    parts.append(f"    <custom_label_0>{escape(d['segment'])}</custom_label_0>")
    if d["type"]:
        parts.append(f"    <custom_label_1>{escape(d['type'])}</custom_label_1>")
    if d["season"]:
        parts.append(f"    <custom_label_2>{escape(d['season'])}</custom_label_2>")
    parts.append(f"    <custom_label_3>{escape(d['country'])}</custom_label_3>")
    for t in themes:
        parts.append(f"    <product_tags>{escape(t)}</product_tags>")
    parts.append("  </listing>")
    return "\n".join(parts)


def write_xml(path, title, rows):
    body = "\n".join(_xml_listing(d) for d in rows)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<listings>\n"
        f"  <title>{escape(title)}</title>\n"
        f"{body}\n"
        "</listings>\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)


CSV_HEADERS = [
    "destination_id", "name", "description", "url", "price",
    "image[0].url", "image[0].tag",
    "address.city", "address.region", "address.country",
    "neighborhood", "types",
    "custom_label_0", "custom_label_1", "custom_label_2", "custom_label_3",
    "custom_number_0", "product_tags",
]


def _csv_row(d):
    themes = [t for t in dict.fromkeys(d["themes"] + [d["country"], d["segment"]]) if t]
    types = ",".join([t for t in [d["type"], d["segment"]] if t])
    return [
        d["destination_id"], d["name"], d["description"], d["url"], d["price"],
        d["image_url"], d["image_tag"],
        d["city"], d["region"], d["country"],
        d["neighborhood"], types,
        d["segment"], d["type"], d["season"], d["country"],
        d["price_number"], ",".join(themes),
    ]


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADERS)
        for d in rows:
            w.writerow(_csv_row(d))


# ---------- products-feed (Meta e-commerce catalogus, RSS 2.0 + g: namespace) ----------

def _product_type(d):
    return " > ".join([x for x in [d["segment"], d["country"], d["type"]] if x])


def _availability(d):
    # kalender wint zodra die live is; anders "in stock" (reis staat alleen in feed bij price>0)
    return d.get("cal_availability") or "in stock"


def _rss_item(d):
    p = ["  <item>"]
    p.append(f"    <g:id>{escape(d['destination_id'])}</g:id>")
    p.append(f"    <g:title>{escape(d['name'])}</g:title>")
    p.append(f"    <g:description>{escape(d['description'])}</g:description>")
    p.append(f"    <g:availability>{_availability(d)}</g:availability>")
    p.append("    <g:condition>new</g:condition>")
    p.append(f"    <g:price>{escape(d['price'])}</g:price>")
    p.append(f"    <g:link>{escape(d['url'])}</g:link>")
    p.append(f"    <g:image_link>{escape(d['image_url'])}</g:image_link>")
    for img in d.get("additional_images", []):
        p.append(f"    <g:additional_image_link>{escape(img)}</g:additional_image_link>")
    p.append("    <g:brand>Estivant</g:brand>")
    pt = _product_type(d)
    if pt:
        p.append(f"    <g:product_type>{escape(pt)}</g:product_type>")
    p.append(f"    <g:custom_label_0>{escape(d['segment'])}</g:custom_label_0>")
    if d["type"]:
        p.append(f"    <g:custom_label_1>{escape(d['type'])}</g:custom_label_1>")
    if d["season"]:
        p.append(f"    <g:custom_label_2>{escape(d['season'])}</g:custom_label_2>")
    p.append(f"    <g:custom_label_3>{escape(d['country'])}</g:custom_label_3>")
    if d.get("age_groups"):
        p.append(f"    <g:custom_label_4>{escape(d['age_groups'])}</g:custom_label_4>")
    p.append(f"    <g:custom_number_0>{d['price_number']}</g:custom_number_0>")
    if d.get("duration_nights"):
        p.append(f"    <g:custom_number_1>{d['duration_nights']}</g:custom_number_1>")
    if d.get("departures"):
        p.append(f"    <g:custom_number_2>{d['departures']}</g:custom_number_2>")
    p.append("  </item>")
    return "\n".join(p)


def write_products_xml(path, title, rows):
    body = "\n".join(_rss_item(d) for d in rows)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(BASE)}</link>\n"
        "    <description>Boekbare Estivant-reizen (dagelijks bijgewerkt)</description>\n"
        f"{body}\n"
        "  </channel>\n"
        "</rss>\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)


PRODUCTS_CSV_HEADERS = [
    "id", "title", "description", "availability", "condition", "price",
    "link", "image_link", "additional_image_link", "brand", "product_type",
    "custom_label_0", "custom_label_1", "custom_label_2", "custom_label_3", "custom_label_4",
    "custom_number_0", "custom_number_1", "custom_number_2", "next_departure",
]


def _products_csv_row(d):
    return [
        d["destination_id"], d["name"], d["description"], _availability(d), "new", d["price"],
        d["url"], d["image_url"], ",".join(d.get("additional_images", [])), "Estivant", _product_type(d),
        d["segment"], d["type"], d["season"], d["country"], d.get("age_groups", ""),
        d["price_number"], d.get("duration_nights", "") or "", d.get("departures", "") or "",
        d.get("next_departure", ""),
    ]


def write_products_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(PRODUCTS_CSV_HEADERS)
        for d in rows:
            w.writerow(_products_csv_row(d))


def push_to_sheets(eog, sng):
    """Optioneel: schrijf naar Google Sheet-tabs als creds aanwezig zijn."""
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not creds_json or not spreadsheet_id:
        print("[sheets] overgeslagen (GOOGLE_SERVICE_ACCOUNT_JSON / SPREADSHEET_ID niet gezet)", flush=True)
        return
    import json
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    for tab, rows in (("EOG", eog), ("SNG", sng)):
        try:
            ws = sh.worksheet(tab)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab, rows=max(10, len(rows) + 5), cols=len(CSV_HEADERS))
        values = [CSV_HEADERS] + [_csv_row(d) for d in rows]
        ws.update(values, "A1")
        print(f"[sheets] tab {tab}: {len(rows)} reizen geschreven", flush=True)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    listings, skipped = asyncio.run(crawl())
    eog = [d for d in listings if d["seg"] == "eog"]
    sng = [d for d in listings if d["seg"] == "sng"]

    # Products-catalogus (primair — matcht huidige pixel content_type=product + content_ids=item_id)
    write_products_xml(os.path.join(OUT_DIR, "feed_eog_products.xml"), "Estivant Eenoudervakanties", eog)
    write_products_xml(os.path.join(OUT_DIR, "feed_sng_products.xml"), "Estivant Singlereizen", sng)
    write_products_csv(os.path.join(OUT_DIR, "feed_eog_products.csv"), eog)
    write_products_csv(os.path.join(OUT_DIR, "feed_sng_products.csv"), sng)

    # Destinations-catalogus (behouden als fallback; vereist content_type=destination in de pixel)
    write_xml(os.path.join(OUT_DIR, "feed_eog.xml"), "Estivant Eenoudervakanties", eog)
    write_xml(os.path.join(OUT_DIR, "feed_sng.xml"), "Estivant Singlereizen", sng)
    write_csv(os.path.join(OUT_DIR, "feed_eog.csv"), eog)
    write_csv(os.path.join(OUT_DIR, "feed_sng.csv"), sng)

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(os.path.join(OUT_DIR, "last_run.txt"), "w") as f:
        f.write(f"{stamp}\nEOG: {len(eog)}\nSNG: {len(sng)}\nSkipped: {len(skipped)}\n")

    try:
        push_to_sheets(eog, sng)
    except Exception as e:
        print(f"[sheets] fout: {e}", flush=True)

    print(f"\nKLAAR — EOG: {len(eog)} reizen, SNG: {len(sng)} reizen, overgeslagen: {len(skipped)}", flush=True)
    if skipped:
        print("Overgeslagen (eerste 20):", flush=True)
        for p, why in skipped[:20]:
            print(f"  - {p}: {why}", flush=True)
    if not listings:
        sys.exit("FOUT: geen reizen gevonden — site-structuur mogelijk gewijzigd.")


if __name__ == "__main__":
    main()

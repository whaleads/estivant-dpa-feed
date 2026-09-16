# Estivant — Dynamic Product Ads feed (Meta destinations)

Dagelijkse crawler die `estivant.nl` uitleest en twee Meta **destinations**-catalogusfeeds bouwt:

| Segment | Bestand | Reizen (nu) |
|---|---|---|
| Eenoudervakanties (EOG) | `output/feed_eog.xml` / `.csv` | 10 boekbaar |
| Singlereizen (SNG) | `output/feed_sng.xml` / `.csv` | 20 boekbaar |

De feeds voldoen aan het meegeleverde Meta destinations-XML-template. 100% gratis stack: **GitHub Actions** (dagelijkse cron) + **Playwright** + feeds als bestand in de repo (Meta haalt ze op via de raw-URL). Google Sheets-export is optioneel.

---

## 1. Hoe het werkt

1. **Bron van waarheid = `sitemap.xml`** → alle reis-URL's (patroon `/<sectie>/<land>/<slug>`).
2. Per reis wordt de pagina **gerenderd** (headless Chromium) en uitgelezen uit de GA4 `dataLayer.view_item`:
   `item_id`, `item_name`, `item_country`, `item_category`, `thema`, `price` (+ `currency`), plus `<meta description>`, `<link canonical>` en de hero-afbeelding.
3. **Filter:** een pagina wordt alleen opgenomen als er een `view_item` is **met `price > 0`**.
   Land-/thema-/overzichtspagina's en reizen met *"Momenteel geen beschikbaarheid"* hebben `price 0` en vallen automatisch af. Zodra zomer­inventaris in de verkoop gaat, verschijnen die reizen vanzelf in de feed.
4. Output: `feed_eog.*` en `feed_sng.*` + `last_run.txt` (tijdstip + aantallen + overgeslagen).

> **Waarom een headless browser en geen simpele HTTP-fetch?** De prijzen/beschikbaarheid en de `dataLayer` worden client-side (JavaScript) opgebouwd; een kale fetch krijgt lege/onvolledige data. Daarom Playwright. Dat draait gratis op GitHub Actions.

### ⚠️ Pixel-matching (belangrijk — zie ook §4)
`destination_id` in de feed = de interne **`item_id`** uit de `dataLayer` (bv. `440`). Dat is exact de waarde die de Meta-pixel als `content_ids` meestuurt. **Niet prefixen of wijzigen**, anders matcht de catalogus niet met de pixel-events en werkt retargeting/DPA niet.

---

## 2. Eenmalige setup (gratis)

### a. Repo aanmaken
1. Maak een (private) GitHub-repo, bv. `estivant-dpa-feed`.
2. Push de inhoud van deze map naar de repo (inclusief `.github/workflows/daily-feed.yml`).
3. Ga naar **Settings → Actions → General → Workflow permissions** en zet **Read and write permissions** aan (nodig om de feeds terug te committen).

### b. Eerste run
- **Actions**-tab → workflow *"Estivant DPA feed (daily)"* → **Run workflow**.
- Na ~1–2 min staan de bestanden in `output/`. Controleer `output/last_run.txt`.

### c. Feed-URL's voor Meta
De feeds zijn bereikbaar via de raw-URL van GitHub:
```
https://raw.githubusercontent.com/<org>/<repo>/main/output/feed_eog.xml
https://raw.githubusercontent.com/<org>/<repo>/main/output/feed_sng.xml
```
(Voor een private repo: zet ná de eerste run **GitHub Pages** aan op de `main`-branch → dan krijg je stabiele publieke `https://<org>.github.io/<repo>/output/feed_eog.xml`-URL's. Meta heeft een publiek bereikbare URL nodig.)

De cron staat op **05:10 UTC** (`.github/workflows/daily-feed.yml`) — pas aan naar wens.

---

## 3. Koppelen in Meta Commerce Manager

1. **Commerce Manager → Catalogs → Create catalog → Type: `Destinations` (Reizen)**. Maak er **twee**: `Estivant EOG` en `Estivant SNG` (of één catalogus met twee feeds — maar aparte catalogi houdt EOG/SNG-rapportage en product sets het schoonst).
2. In de catalogus: **Data sources → Add items → Use bulk upload → Scheduled feed**.
3. Plak de bijbehorende raw/Pages-URL, stel **dagelijks** in (net ná de crawl, bv. 06:00 NL), currency **EUR**.
4. Herhaal voor de tweede catalogus/feed.
5. Koppel elke catalogus aan de **Meta-pixel `555359731496980`** (Catalog → Settings → Connect data sources / events).

Daarna kun je **Advantage+ catalog-campagnes (DPA)** draaien op destination-sets, gesplitst per EOG/SNG.

---

## 4. Pixel-audit & vereiste GTM-controle

Bevindingen van de live site (geverifieerd 16-09-2026, read-only — niets gewijzigd):

- **Meta-pixel `555359731496980`**, **consent-gated via Cookiebot** (`cbid 1b197912-c29b-45e8-9473-8f71a5aa3615`, marketing standaard uit).
- **Server-side tracking via Taggrs** op eigen subdomein `sst.estivant.nl` (loader `tg=5GMTGBH4`). Meta-events lopen via **server-side GTM + Conversions API** — `fbevents.js` wordt client-side niet geladen. Ook Datatrics (personalisatie) aanwezig.
- Client-side voeding = GA4 `dataLayer.view_item` met `item_id`, `price`, `item_country`, `item_category`, `thema`.
- **`content_type` / `content_ids` worden in de Taggrs sGTM-container bepaald.** Die config is alleen zichtbaar met Taggrs/GTM-login (niet read-only vanaf de site te lezen). **Nog te bevestigen** in Taggrs of Meta Events Manager (zie tabel) — dit is bewust NIET aangepast.

**Actiepunt — verifieer/pas de Meta-tag in GTM aan** zodat destination-DPA werkt:

| Pixel-parameter | Moet zijn | Waarom |
|---|---|---|
| `content_type` | `destination` | Anders matcht de destinations-catalogus niet met de events (standaard e-commerce stuurt vaak `product`). |
| `content_ids` | `[ item_id ]` (bv. `["440"]`) | Moet exact gelijk zijn aan `destination_id` in de feed. |
| Events | `ViewContent` (detailpagina), `Search`, `Purchase`/`Lead` (boeking) | Voeding voor retargeting & optimalisatie. |

> Zonder `content_type: destination` + matchende `content_ids` toont Meta wel ads, maar **zonder personalisatie/retargeting** op basis van bekeken reizen. Dit is de belangrijkste te bevestigen stap vóór livegang. GTM-container: zie `CLAUDE.md`.

**Datakwaliteit-signaal:** meerdere *niet-boekbare* zomerpagina's delen nu een generieke `item_id` (bv. `425`/`428`) en `item_name` "Eenoudervakantie"/"Singlereis". Zolang die reizen `price 0` hebben, blijven ze uit de feed. Zodra ze in de verkoop gaan, moeten ze een **unieke `item_id`** krijgen — anders botsen ze in de catalogus én in de pixel-matching. Even checken met de webbouwer.

---

## 5. Google Sheet (mirror)

Er is al een gevulde Google Sheet aangemaakt met twee tabs (`EOG`, `SNG`), kolommen volgens de Meta destinations-conventie:

- **Sheet:** `Estivant DPA Feed — destinations (EOG + SNG)`
- **ID:** `1Vn9KwvySYEH0HOYtffg9aaiDCfhwRvR1AmP1qrsDP0A`
- **URL:** https://docs.google.com/spreadsheets/d/1Vn9KwvySYEH0HOYtffg9aaiDCfhwRvR1AmP1qrsDP0A/edit

Wil je de Sheet **dagelijks automatisch** laten bijwerken door de crawler (i.p.v. de XML-feeds), zet dan een service account op:

1. Maak in Google Cloud (gratis) een **service account** + JSON-key; zet de **Google Sheets API** aan.
2. Deel de Google Sheet met het service-account-e-mailadres (Bewerker).
3. Zet in GitHub **Settings → Secrets and variables → Actions** twee secrets:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — de volledige JSON-inhoud
   - `SPREADSHEET_ID` — het id uit de Sheet-URL
4. De workflow pikt ze automatisch op (staan al gemapt in `daily-feed.yml`).

De kolomkoppen in de Sheet/CSV volgen de Meta destinations-conventie (`destination_id`, `name`, `description`, `url`, `price`, `image[0].url`, `image[0].tag`, `address.city/region/country`, `neighborhood`, `types`, `custom_label_0-4`, `custom_number_0`, `product_tags`), zodat je desgewenst óók de Sheet zelf als scheduled feed aan Meta kunt koppelen.

---

## 6. Lokaal draaien / testen

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python main.py
# resultaat in output/
```

## 7. Onderhoud
- Komt er een nieuw land bij in de URL's? Voeg de slug toe aan `COUNTRY_NAME` in `main.py`.
- Verandert de sitewinkel-structuur of de `dataLayer`? Dan faalt de run met een duidelijke melding (0 reizen) i.p.v. stil verkeerde data te leveren.
- Als de **vertrekkalender** live gaat, kan de crawler uitgebreid worden met per-vertrek datums + actuele beschikbaarheid (nu nog "in aanbouw" op de site).

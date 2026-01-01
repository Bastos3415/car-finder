import re
import time
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# =========================
# CONFIG
# =========================
st.set_page_config(page_title="Import Allemagne → Revente France", layout="wide")
st.title("🚗 Import Allemagne → Revente France")
st.caption("Analyse automatique mobile.de – Playwright (local)")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# =========================
# PARAMÈTRES MÉTIER
# =========================
IMPORT_EXPORT_PLATES = 250
IMPORT_CT_FR = 80
IMPORT_CG_EST = 280
RISK_LOW = 300
RISK_MED = 600
RISK_HIGH = 900

TARGET_MODELS = {
    ("Volkswagen", "Golf"),
    ("Audi", "A3"),
    ("Peugeot", "308"),
    ("Renault", "Megane"),
    ("Skoda", "Octavia"),
    ("Ford", "Focus"),
    ("Volkswagen", "Polo"),
}

# =========================
# UTILS
# =========================
def _safe_int(x):
    try:
        return int(str(x).replace(".", "").replace(",", "").strip())
    except:
        return None

# =========================
# PRIX FR (MODELE SIMPLE)
# =========================
def estimate_fr_price(make, model, year, km, fuel):
    base_prices = {
        ("Volkswagen", "Golf"): 9800,
        ("Audi", "A3"): 10500,
        ("Peugeot", "308"): 8600,
        ("Renault", "Megane"): 8400,
        ("Skoda", "Octavia"): 9000,
        ("Ford", "Focus"): 8300,
        ("Volkswagen", "Polo"): 7800,
    }
    base = base_prices.get((make, model), 7500)

    age = 2026 - year
    price = base - age * 180

    if km > 150_000:
        price -= ((km - 150_000) // 10_000) * 200

    if fuel == "diesel":
        price += 300

    return max(int(price), 2000)

def estimate_import_costs(km):
    if km < 160_000:
        risk = RISK_LOW
    elif km < 200_000:
        risk = RISK_MED
    else:
        risk = RISK_HIGH
    return IMPORT_EXPORT_PLATES + IMPORT_CT_FR + IMPORT_CG_EST + risk

def liquidity_score(make, model, km, seller_type):
    score = 0
    if (make, model) in TARGET_MODELS:
        score += 40
    if km < 180_000:
        score += 20
    if seller_type == "professional":
        score += 20
    if km < 140_000:
        score += 10
    return min(score, 100)

# =========================
# PLAYWRIGHT FETCH
# =========================
@st.cache_data(ttl=600)
def fetch_detail_page(url):
    url = (
        url.replace("suchen.mobile.de", "m.mobile.de")
           .replace("www.mobile.de", "m.mobile.de")
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)

        page.goto(url, wait_until="networkidle", timeout=60000)

        # accepter cookies (CRUCIAL)
        try:
            page.locator("button:has-text('Accept')").first.click(timeout=3000)
        except:
            pass

        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()
        return html

# =========================
# PARSER ROBUSTE
# =========================
def parse_detail(html, url):
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    make = model = fuel = None
    year = km = price = None
    transmission = "manual"
    seller_type = "professional"

    # PRIX
    m = re.search(r"€\s?([\d\.\,]+)", text)
    if m:
        price = _safe_int(m.group(1))

    # KM
    m = re.search(r"(\d{1,3}[\.\,]?\d{3})\s?km", text, re.I)
    if m:
        km = _safe_int(m.group(1))

    # ANNEE
    m = re.search(r"(19\d{2}|20\d{2})", text)
    if m:
        year = int(m.group(1))

    # MARQUE / MODELE
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
        parts = title.split()
        if len(parts) >= 2:
            make = parts[0]
            model = parts[1]
    else:
        title = None

    # CARBURANT
    if "Diesel" in text:
        fuel = "diesel"
    elif "Benzin" in text or "Petrol" in text:
        fuel = "petrol"

    # BOITE
    if "Automatik" in text or "Automatic" in text:
        transmission = "automatic"

    # VENDEUR
    if "Privat" in text:
        seller_type = "private"

    return {
        "make": make,
        "model": model,
        "year": year,
        "km": km,
        "fuel": fuel,
        "transmission": transmission,
        "seller_type": seller_type,
        "price_de": price,
        "title": title,
        "url": url,
    }

# =========================
# ANALYSE
# =========================
def analyze(row):
    year = row["year"] or 2012
    km = row["km"] or 180_000
    price = row["price_de"]

    fr_price = estimate_fr_price(row["make"], row["model"], year, km, row["fuel"])
    costs = estimate_import_costs(km)
    margin = fr_price - (price + costs) if price else None
    liq = liquidity_score(row["make"], row["model"], km, row["seller_type"])

    score = 0
    if margin is not None:
        score = 0.6 * max(min(margin / 100, 100), -100) + 0.4 * liq

    return {
        **row,
        "fr_price_est": fr_price,
        "import_costs_est": costs,
        "margin_est": margin,
        "liq_score": liq,
        "final_score": round(score, 1),
    }

# =========================
# UI
# =========================
st.subheader("1) Colle des liens d’annonces mobile.de (1 par ligne)")

links_text = st.text_area(
    "Liens",
    height=180,
    placeholder="https://m.mobile.de/fahrzeuge/details.html?id=...\n..."
)

limit = st.slider("Nombre de liens à analyser", 1, 20, 5)

def clean_links(text):
    urls = []
    for l in text.splitlines():
        l = l.strip()
        if "mobile.de" in l and "details.html" in l:
            urls.append(
                l.replace("suchen.mobile.de", "m.mobile.de")
                 .replace("www.mobile.de", "m.mobile.de")
            )
    return list(dict.fromkeys(urls))

if st.button("Analyser"):
    urls = clean_links(links_text)[:limit]

    if not urls:
        st.error("Aucun lien valide.")
        st.stop()

    rows = []
    with st.spinner("Analyse en cours (Playwright)…"):
        for url in urls:
            html = fetch_detail_page(url)
            data = parse_detail(html, url)
            rows.append(analyze(data))
            time.sleep(0.5)

    df = pd.DataFrame(rows).sort_values("final_score", ascending=False)

    st.subheader("2) Résultats (triés par score)")
    st.dataframe(df, use_container_width=True)

    st.subheader("Top opportunités")
    for _, r in df.head(5).iterrows():
        st.markdown(
            f"- **{r.make} {r.model}** | {r.year} | {r.km} km | "
            f"DE: {r.price_de} € → marge: **{r.margin_est} €**  \n{r.url}"
        )

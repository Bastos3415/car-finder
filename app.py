import re
import json
import time
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

st.set_page_config(page_title="Import Allemagne -> Revente France", layout="wide")

st.title("🚗 Import Allemagne → Revente France")
st.caption("Mode PRO (Playwright) : analyse automatique d’annonces mobile.de")

# ---------------------------
# PARAMÈTRES
# ---------------------------
IMPORT_EXPORT_PLATES = 250
IMPORT_CT_FR = 80
IMPORT_CG_EST = 280
RISK_LOW = 300
RISK_MED = 600
RISK_HIGH = 900

TARGET_MODELS = {
    ("Volkswagen", "Golf"),
    ("Skoda", "Octavia"),
    ("Peugeot", "308"),
    ("Renault", "Megane"),
    ("Ford", "Focus"),
    ("Volkswagen", "Polo"),
    ("Audi", "A3"),
}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# ---------------------------
# OUTILS
# ---------------------------
def _safe_int(x):
    try:
        if x is None:
            return None
        s = str(x).replace(".", "").replace(",", "").strip()
        return int(s)
    except Exception:
        return None


# ---------------------------
# PRIX FR (MODELE SIMPLE)
# ---------------------------
def estimate_fr_price(make, model, year, km, fuel):
    base_prices = {
        ("Volkswagen", "Golf"): 9800,
        ("Skoda", "Octavia"): 9000,
        ("Peugeot", "308"): 8600,
        ("Renault", "Megane"): 8400,
        ("Ford", "Focus"): 8300,
        ("Volkswagen", "Polo"): 7800,
        ("Audi", "A3"): 10200,
    }
    base = base_prices.get((make, model), 7800)
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


# ---------------------------
# PLAYWRIGHT FETCH (LE CŒUR)
# ---------------------------
@st.cache_data(ttl=600)
def fetch_detail_page(url):
    url = url.replace("suchen.mobile.de", "m.mobile.de").replace("www.mobile.de", "m.mobile.de")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        page.goto(url, timeout=60000)
        page.wait_for_timeout(2000)
        html = page.content()
        browser.close()
        return html


# ---------------------------
# PARSE ANNONCE
# ---------------------------
def parse_detail(html, url):
    soup = BeautifulSoup(html, "lxml")

    title = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""

    make = model = fuel = "diesel"
    year = km = price = None
    seller_type = "professional"
    transmission = "manual"

    # JSON LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") in ("Car", "Vehicle"):
                brand = data.get("brand")
                if isinstance(brand, dict):
                    make = brand.get("name", make)
                model = data.get("model", model)
                year = _safe_int(data.get("productionDate"))
                mileage = data.get("mileageFromOdometer", {})
                if isinstance(mileage, dict):
                    km = _safe_int(mileage.get("value"))
                offers = data.get("offers", {})
                if isinstance(offers, dict):
                    price = _safe_int(offers.get("price"))
                ft = data.get("fuelType")
                if isinstance(ft, str):
                    fuel = ft.lower()
                break
        except Exception:
            pass

    text = soup.get_text(" ", strip=True)

    if price is None:
        m = re.search(r"€\s?([\d\.\,]+)", text)
        if m:
            price = _safe_int(m.group(1))

    if km is None:
        m = re.search(r"(\d{1,3}[\.\,]?\d{3})\s?km", text, re.I)
        if m:
            km = _safe_int(m.group(1))

    if year is None:
        m = re.search(r"\b(\d{4})\b", text)
        if m:
            year = _safe_int(m.group(1))

    if "Automatik" in text or "Automatic" in text:
        transmission = "automatic"

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


# ---------------------------
# ANALYSE
# ---------------------------
def analyze(row):
    year = row["year"] or 2012
    km = row["km"] or 180_000
    price = row["price_de"]

    fr_price = estimate_fr_price(row["make"], row["model"], year, km, row["fuel"])
    costs = estimate_import_costs(km)
    margin = fr_price - (price + costs) if price else None
    liq = liquidity_score(row["make"], row["model"], km, row["seller_type"])

    score = 0
    if margin:
        score = 0.6 * min(max(margin / 1000, -10), 10) * 10 + 0.4 * liq

    return {
        **row,
        "fr_price_est": fr_price,
        "import_costs_est": costs,
        "margin_est": margin,
        "liq_score": liq,
        "final_score": round(score, 1),
    }


# ---------------------------
# UI
# ---------------------------
st.subheader("1) Colle des liens d’annonces mobile.de (1 par ligne)")

links_text = st.text_area(
    "Liens",
    height=180,
    placeholder="https://m.mobile.de/fahrzeuge/details.html?id=...\n...",
)

limit = st.slider("Nombre de liens à analyser", 1, 20, 5)

def clean_links(text):
    urls = []
    for l in text.splitlines():
        l = l.strip()
        if "mobile.de" in l and "details.html" in l:
            urls.append(
                l.replace("suchen.mobile.de", "m.mobile.de").replace("www.mobile.de", "m.mobile.de")
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

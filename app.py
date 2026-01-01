import re
import json
import time
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Import Allemagne -> Revente France", layout="wide")

st.title("🚗 Import Allemagne -> Revente France")
st.caption("Mode liens : colle des URLs d'annonces mobile.de -> analyse prix FR / couts / marge / score.")

# ---------------------------
# Parametres (ajustables)
# ---------------------------
IMPORT_EXPORT_PLATES = 250
IMPORT_CT_FR = 80
IMPORT_CG_EST = 280  # estimation moyenne, a ajuster selon region/chevaux fiscaux
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
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# ---------------------------
# Modele simple prix FR
# ---------------------------
def estimate_fr_price(make: str, model: str, year: int, km: int, fuel: str) -> int:
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

    # decote age (plus realiste)
    age = 2026 - year
    price = base - (age * 180)

    # decote km
    if km > 150_000:
        price -= ((km - 150_000) // 10_000) * 200

    # bonus diesel
    if (fuel or "").lower() == "diesel":
        price += 300

    return int(max(price, 2000))


def estimate_risk_buffer(km: int, timing_belt_known: bool) -> int:
    if km < 160_000:
        risk = RISK_LOW
    elif km < 200_000:
        risk = RISK_MED
    else:
        risk = RISK_HIGH

    if not timing_belt_known:
        risk += 300  # distribution inconnue
    return int(risk)


def estimate_import_costs(km: int, timing_belt_known: bool) -> int:
    fixed = IMPORT_EXPORT_PLATES + IMPORT_CT_FR + IMPORT_CG_EST
    risk = estimate_risk_buffer(km, timing_belt_known)
    return int(fixed + risk)


def liquidity_score(make: str, model: str, km: int, seller_type: str) -> int:
    score = 0
    if (make, model) in TARGET_MODELS:
        score += 40
    if km < 180_000:
        score += 20
    if seller_type == "professional":
        score += 20
    if km < 140_000:
        score += 10
    return int(min(score, 100))


# ---------------------------
# Fetch page detail mobile.de
# ---------------------------
@st.cache_data(ttl=900)
def fetch_detail_page(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    return r.text


def _safe_int(x):
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return int(x)
        s = str(x).strip()
        s = s.replace(".", "").replace(",", "")
        return int(s)
    except Exception:
        return None


def parse_detail(html: str, url: str) -> dict:
    """
    Version robuste: on essaie d'abord le JSON structuré (application/ld+json).
    Fallback ensuite sur texte brut.
    """
    soup = BeautifulSoup(html, "lxml")

    title = soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else ""

    # ---------------------------
    # 1) JSON structuré (le plus fiable)
    # ---------------------------
    car_json = None
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string
        if not text:
            continue
        try:
            obj = json.loads(text)
        except Exception:
            continue

        # parfois c'est une liste, parfois un dict
        candidates = obj if isinstance(obj, list) else [obj]
        for c in candidates:
            if isinstance(c, dict) and c.get("@type") in ("Car", "Vehicle"):
                car_json = c
                break
        if car_json:
            break

    make = "Autre"
    model = "Autre"
    year = None
    km = None
    price = None
    fuel = "diesel"

    if car_json:
        # brand peut être string ou dict
        brand = car_json.get("brand")
        if isinstance(brand, dict):
            make = brand.get("name") or make
        elif isinstance(brand, str):
            make = brand

        model = car_json.get("model") or model

        # année
        prod_date = car_json.get("productionDate")
        year = _safe_int(prod_date)

        # km
        mileage = car_json.get("mileageFromOdometer")
        if isinstance(mileage, dict):
            km = _safe_int(mileage.get("value"))
        else:
            km = _safe_int(mileage)

        # fuelType (parfois list)
        ft = car_json.get("fuelType")
        if isinstance(ft, list) and ft:
            fuel = str(ft[0]).lower()
        elif isinstance(ft, str):
            fuel = ft.lower()

        # prix
        offers = car_json.get("offers")
        if isinstance(offers, dict):
            price = _safe_int(offers.get("price"))
        elif isinstance(offers, list) and offers:
            if isinstance(offers[0], dict):
                price = _safe_int(offers[0].get("price"))

    # ---------------------------
    # 2) Fallback texte brut si manques
    # ---------------------------
    full_text = soup.get_text(" ", strip=True)

    if price is None:
        m = re.search(r"€\s?([\d\.\,]+)", full_text)
        if not m:
            m = re.search(r"([\d\.\,]+)\s?€", full_text)
        if m:
            price = _safe_int(m.group(1))

    if km is None:
        m = re.search(r"(\d{1,3}[\.\,]?\d{3})\s?km", full_text, re.IGNORECASE)
        if m:
            km = _safe_int(m.group(1))

    if year is None:
        m = re.search(r"\b(\d{2})/(\d{4})\b", full_text)
        if m:
            year = _safe_int(m.group(2))

    # transmission / vendeur (best effort)
    transmission = "automatic" if ("Automatik" in full_text or "Automatic" in full_text) else "manual"
    seller_type = "professional" if ("Händler" in full_text or "Dealer" in full_text or "dealer" in full_text.lower()) else "private"

    # normalisation make/model si JSON n'a rien donné
    if make == "Autre":
        for mk in ["Volkswagen", "Skoda", "Peugeot", "Renault", "Ford", "Audi"]:
            if mk.lower() in full_text.lower():
                make = mk
                break
    if model == "Autre":
        for md in ["Golf", "Octavia", "308", "Megane", "Focus", "Polo", "A3"]:
            if re.search(rf"\b{re.escape(md)}\b", full_text, re.IGNORECASE):
                model = md
                break

    # normalise fuel
    if "diesel" not in (fuel or "").lower():
        if "Diesel" in full_text:
            fuel = "diesel"
        elif "Benzin" in full_text or "Petrol" in full_text:
            fuel = "essence"

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


def analyze_row(row: dict) -> dict:
    make = row.get("make") or "Autre"
    model = row.get("model") or "Autre"
    year = int(row.get("year") or 2012)
    km = int(row.get("km") or 180_000)
    fuel = row.get("fuel") or "diesel"
    seller_type = row.get("seller_type") or "professional"
    price_de = row.get("price_de")

    fr_est = estimate_fr_price(make, model, year, km, fuel)
    costs = estimate_import_costs(km, timing_belt_known=False)

    margin = None
    if isinstance(price_de, int) and price_de > 0:
        margin = fr_est - (price_de + costs)

    liq = liquidity_score(make, model, km, seller_type)

    if margin is None:
        margin_points = 0
    else:
        margin_points = max(min(margin / 1000, 10), -10) * 10  # -100..+100

    final_score = 0.6 * margin_points + 0.4 * liq

    return {
        **row,
        "fr_price_est": fr_est,
        "import_costs_est": costs,
        "margin_est": margin,
        "liq_score": liq,
        "final_score": round(final_score, 1),
    }


# ---------------------------
# UI - Mode liens d'annonces
# ---------------------------
st.subheader("1) Colle des liens d'annonces mobile.de (1 par ligne)")
st.write("Ouvre des annonces sur mobile.de, copie les liens (details.html?id=...), colle-les ici puis clique 'Analyser'.")

links_text = st.text_area(
    "Liens mobile.de",
    height=180,
    placeholder="https://m.mobile.de/fahrzeuge/details.html?id=...\nhttps://m.mobile.de/fahrzeuge/details.html?id=...\n...",
)

limit = st.slider("Nombre de liens a analyser", 1, 30, 10)


def clean_links(text: str) -> list[str]:
    urls = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "mobile.de" in line and "fahrzeuge/details.html" in line and "id=" in line:
            urls.append(line)

    # dedupe
    seen = set()
    uniq = []
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq


if st.button("Analyser"):
    urls = clean_links(links_text)

    if not urls:
        st.error("Colle au moins 1 lien d'annonce mobile.de (details.html?id=...)")
        st.stop()

    urls = urls[:limit]
    st.info(f"{len(urls)} lien(s) a analyser.")

    details = []
    with st.spinner("Lecture des annonces..."):
        for url in urls:
            try:
                dhtml = fetch_detail_page(url)
                d = parse_detail(dhtml, url)
                details.append(d)
                time.sleep(0.4)  # poli
            except Exception as e:
                details.append({"url": url, "error": str(e)})

    analyzed = []
    for d in details:
        if d.get("error"):
            analyzed.append({**d, "final_score": -999})
        else:
            analyzed.append(analyze_row(d))

    df = pd.DataFrame(analyzed)

    keep = [
        "final_score",
        "make",
        "model",
        "year",
        "km",
        "fuel",
        "transmission",
        "seller_type",
        "price_de",
        "fr_price_est",
        "import_costs_est",
        "margin_est",
        "liq_score",
        "url",
        "title",
    ]
    for k in keep:
        if k not in df.columns:
            df[k] = None

    df = df[keep].sort_values("final_score", ascending=False)

    st.subheader("2) Resultats (tries par score)")
    st.dataframe(df, use_container_width=True)

    st.write("### Top 5 (a ouvrir)")
    top = df.head(5).to_dict(orient="records")
    for row in top:
        st.markdown(
            f"- **{row.get('make')} {row.get('model')}** | {row.get('year')} | {row.get('km')} km | "
            f"DE: {row.get('price_de')} € | marge: {row.get('margin_est')} €  \n"
            f"{row.get('url')}"
        )

st.caption("MVP: si une annonce retourne encore des champs vides, envoie-moi le lien et je te renforce le parseur pour ce format.")

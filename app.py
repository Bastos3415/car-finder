import re
import time
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Import Allemagne -> Revente France", layout="wide")

st.title("Import Allemagne -> Revente France")
st.caption("Mode A: colle une URL de recherche mobile.de -> l'app recupere des annonces et calcule marge/score.")

# ---------------------------
# Parametres (ajustables)
# ---------------------------
IMPORT_EXPORT_PLATES = 250
IMPORT_CT_FR = 80
IMPORT_CG_EST = 280  # estimation moyenne
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

    # decote age plus realiste
    age = 2026 - year
    price = base - (age * 180)

    # decote km
    if km > 150_000:
        price -= ((km - 150_000) // 10_000) * 200

    if fuel.lower() == "diesel":
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
        risk += 300
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
# Scraping leger mobile.de via URL de recherche
# ---------------------------
@st.cache_data(ttl=900)
def fetch_search_page(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return r.text


def parse_listings_from_html(html: str) -> list[dict]:
    """
    Parsing "best effort". Le HTML peut changer.
    On cherche des liens vers /fahrzeuge/details.html?id=...
    et on tente d'extraire quelques infos proches.
    """
    soup = BeautifulSoup(html, "lxml")

    # 1) Trouver des URLs d'annonces
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "fahrzeuge/details.html" in href and "id=" in href:
            # rendre URL absolue
            if href.startswith("/"):
                href = "https://m.mobile.de" + href
            elif href.startswith("http") is False:
                href = "https://m.mobile.de/" + href.lstrip("/")
            links.append(href)

    # dedupe en gardant l'ordre
    seen = set()
    uniq = []
    for x in links:
        if x not in seen:
            uniq.append(x)
            seen.add(x)

    # 2) Tentative d'extraction simple: prix / titre / km / annee / fuel
    text = soup.get_text(" ", strip=True)

    # NOTE: on ne peut pas relier parfaitement prix<->lien sans structure stable.
    # Donc MVP: on retournera au minimum les liens, et on permettra l'analyse manuelle
    # OU on essaie d'extraire depuis chaque page détail (plus fiable).
    return [{"url": u} for u in uniq[:50]]


@st.cache_data(ttl=900)
def fetch_detail_page(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    return r.text


def parse_detail(html: str, url: str) -> dict:
    """
    Extraction best effort depuis une page détail mobile.de.
    """
    soup = BeautifulSoup(html, "lxml")
    full_text = soup.get_text(" ", strip=True)

    # Titre
    title = ""
    h = soup.find(["h1", "h2"])
    if h:
        title = h.get_text(" ", strip=True)

    # Prix
    price = None
    m = re.search(r"€\s?([\d\.\,]+)", full_text)
    if m:
        raw = m.group(1).replace(".", "").replace(",", "")
        if raw.isdigit():
            price = int(raw)

    # Km
    km = None
    m = re.search(r"(\d{1,3}[\.\,]?\d{3})\s?km", full_text, re.IGNORECASE)
    if m:
        raw = m.group(1).replace(".", "").replace(",", "")
        if raw.isdigit():
            km = int(raw)

    # Year (First registration)
    year = None
    m = re.search(r"(\d{2})/(\d{4})", full_text)
    if m:
        year = int(m.group(2))

    # Fuel
    fuel = "diesel" if "Diesel" in full_text else ("essence" if "Benzin" in full_text or "Petrol" in full_text else "diesel")

    # Transmission
    transmission = "manual"
    if "Automatic" in full_text or "Automatik" in full_text:
        transmission = "automatic"

    # Seller type (best effort)
    seller_type = "professional" if ("Händler" in full_text or "dealer" in full_text.lower()) else "private"

    # Guess make/model (très simple)
    make = "Autre"
    model = "Autre"
    makes = ["Volkswagen", "Skoda", "Peugeot", "Renault", "Ford", "Audi"]
    models = ["Golf", "Octavia", "308", "Megane", "Focus", "Polo", "A3"]
    for mk in makes:
        if mk.lower() in full_text.lower():
            make = mk
            break
    for md in models:
        if re.search(rf"\b{re.escape(md)}\b", full_text, re.IGNORECASE):
            model = md
            break

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
    # si infos manquantes, on met des valeurs neutres
    make = row.get("make") or "Autre"
    model = row.get("model") or "Autre"
    year = int(row.get("year") or 2012)
    km = int(row.get("km") or 180_000)
    fuel = row.get("fuel") or "diesel"
    seller_type = row.get("seller_type") or "professional"
    price_de = int(row.get("price_de") or 0)

    fr_est = estimate_fr_price(make, model, year, km, fuel)
    costs = estimate_import_costs(km, timing_belt_known=False)
    margin = fr_est - (price_de + costs) if price_de > 0 else None
    liq = liquidity_score(make, model, km, seller_type)

    # score final (indicatif)
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
# UI
# ---------------------------
st.subheader("1) Colle une URL de recherche mobile.de")

st.write("Astuce: fais ta recherche sur mobile.de (diesel, max 6300€, etc.), puis copie l'URL de la page de resultats et colle-la ici.")

search_url = st.text_input(
    "URL mobile.de (page de resultats)",
    value="",
    placeholder="https://m.mobile.de/auto/search.html?...",
)

limit = st.slider("Nombre d'annonces a analyser", 5, 30, 15)

if st.button("Chercher et analyser"):
    if not search_url.strip():
        st.error("Colle une URL mobile.de de resultats.")
        st.stop()

    with st.spinner("Recuperation des resultats..."):
        html = fetch_search_page(search_url)
        base_list = parse_listings_from_html(html)

    if not base_list:
        st.warning("Je n'ai pas trouve de liens d'annonces sur cette page. Essaye une autre URL (ou enleve des filtres).")
        st.stop()

    st.info(f"{len(base_list)} liens trouves. Analyse des {limit} premiers...")

    details = []
    with st.spinner("Lecture des pages d'annonces (detail)..."):
        for i, item in enumerate(base_list[:limit], start=1):
            try:
                dhtml = fetch_detail_page(item["url"])
                d = parse_detail(dhtml, item["url"])
                details.append(d)
                time.sleep(0.3)  # on reste poli
            except Exception as e:
                details.append({"url": item["url"], "error": str(e)})

    # Analyse
    analyzed = []
    for d in details:
        if d.get("error"):
            analyzed.append({**d, "final_score": -999})
        else:
            analyzed.append(analyze_row(d))

    df = pd.DataFrame(analyzed)

    # garder colonnes utiles
    keep = ["final_score", "make", "model", "year", "km", "fuel", "price_de", "fr_price_est", "import_costs_est", "margin_est", "liq_score", "url", "title"]
    for k in keep:
        if k not in df.columns:
            df[k] = None
    df = df[keep].sort_values("final_score", ascending=False)

    st.subheader("2) Resultats (tries par score)")
    st.dataframe(df, use_container_width=True)

    st.caption("Si certaines colonnes (prix/km/annee) sont vides, c'est que la page mobile.de n'affiche pas ces infos de maniere stable. Dans ce cas, on ajustera le parseur.")

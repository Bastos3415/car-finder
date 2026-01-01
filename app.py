import streamlit as st

st.set_page_config(page_title="Import Allemagne -> Revente France", layout="centered")

st.title("Import Allemagne -> Revente France")
st.caption("Analyse simple : estimation prix France, couts import, marge, score revente rapide.")

# ---------------------------
# Parametres (tu pourras ajuster)
# ---------------------------
IMPORT_EXPORT_PLATES = 250
IMPORT_CT_FR = 80
IMPORT_CG_EST = 280  # estimation moyenne, a ajuster selon region/chevaux fiscaux
RISK_LOW = 300
RISK_MED = 600
RISK_HIGH = 900


# ---------------------------
# Modele simple prix FR
# ---------------------------
def estimate_fr_price(make: str, model: str, year: int, km: int, fuel: str) -> int:
    # Bases "liquidites" (valeurs grossieres pour MVP)
    base_prices = {
        ("Volkswagen", "Golf"): 8500,
        ("Skoda", "Octavia"): 8200,
        ("Peugeot", "308"): 7800,
        ("Renault", "Megane"): 7600,
        ("Ford", "Focus"): 7500,
        ("Volkswagen", "Polo"): 7200,
        ("Audi", "A3"): 9000,
    }

    base = base_prices.get((make, model), 7200)

    # Decote age (simple)
    age = 2026 - year
    price = base - (age * 300)

    # Decote km (a partir de 150k)
    if km > 150_000:
        price -= ((km - 150_000) // 10_000) * 200

    # Bonus diesel (revente FR)
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
        risk += 300  # distribution inconnue

    return int(risk)


def estimate_import_costs(km: int, timing_belt_known: bool) -> int:
    fixed = IMPORT_EXPORT_PLATES + IMPORT_CT_FR + IMPORT_CG_EST
    risk = estimate_risk_buffer(km, timing_belt_known)
    return int(fixed + risk)


def liquidity_score(make: str, model: str, km: int, seller_type: str) -> int:
    score = 0
    liquid = {
        ("Volkswagen", "Golf"),
        ("Skoda", "Octavia"),
        ("Peugeot", "308"),
        ("Renault", "Megane"),
        ("Ford", "Focus"),
        ("Volkswagen", "Polo"),
        ("Audi", "A3"),
    }

    if (make, model) in liquid:
        score += 40
    if km < 180_000:
        score += 20
    if seller_type == "professional":
        score += 20

    # Bonus petit + si km vraiment clean
    if km < 140_000:
        score += 10

    return int(min(score, 100))


# ---------------------------
# UI - Formulaire
# ---------------------------
st.subheader("1) Renseigne l'annonce allemande")

col1, col2 = st.columns(2)

with col1:
    make = st.selectbox("Marque", ["Volkswagen", "Skoda", "Peugeot", "Renault", "Ford", "Audi", "Autre"])
    model = st.selectbox("Modele", ["Golf", "Octavia", "308", "Megane", "Focus", "Polo", "A3", "Autre"])
    fuel = st.selectbox("Carburant", ["diesel", "essence"])
    seller_type = st.selectbox("Vendeur", ["professional", "private"])

with col2:
    price_de = st.number_input("Prix Allemagne (EUR)", min_value=0, value=5490, step=50)
    year = st.number_input("Annee (1ere immat)", min_value=1995, max_value=2026, value=2011, step=1)
    km = st.number_input("Kilometrage (km)", min_value=0, value=177000, step=1000)
    timing_belt_known = st.checkbox("Distribution faite / connue", value=False)

st.divider()
st.subheader("2) Analyse")

if st.button("Analyser"):
    # Normalisation des "Autre"
    if make == "Autre":
        make = st.text_input("Tape la marque exacte (ex: Opel)").strip() or "Autre"
    if model == "Autre":
        model = st.text_input("Tape le modele exact (ex: Astra)").strip() or "Autre"

    fr_est = estimate_fr_price(make, model, int(year), int(km), fuel)
    costs = estimate_import_costs(int(km), timing_belt_known)
    margin = fr_est - (int(price_de) + costs)
    liq = liquidity_score(make, model, int(km), seller_type)

    # Score final (simple)
    # Marge convertie en points (cap pour eviter les valeurs extremes)
    margin_points = max(min(margin / 1000, 10), -10) * 10  # -100 a +100
    final_score = 0.6 * margin_points + 0.4 * liq

    st.success("Analyse terminee")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Prix FR estime", f"{fr_est} €")
    c2.metric("Couts import estimes", f"{costs} €")
    c3.metric("Marge estimee", f"{margin} €")
    c4.metric("Score revente", f"{liq}/100")

    st.write("### Verdict")
    if margin >= 1000 and liq >= 60:
        st.write("🟢 **Bonne affaire potentielle** : marge OK + revente plutot rapide.")
    elif margin >= 0 and liq >= 50:
        st.write("🟡 **Possible** : rentable ou presque, mais verifie bien l'historique et negocie.")
    else:
        st.write("🔴 **Pas ideal** : marge faible / risque trop haut. Cherche une meilleure annonce.")

    st.write("### Details (transparent)")
    st.write(f"- Prix Allemagne: **{int(price_de)} €**")
    st.write(f"- Estimation France: **{fr_est} €**")
    st.write(f"- Couts fixes (plaques+CT+CG): **{IMPORT_EXPORT_PLATES + IMPORT_CT_FR + IMPORT_CG_EST} €**")
    st.write(f"- Buffer risque: **{estimate_risk_buffer(int(km), timing_belt_known)} €**")
    st.write(f"- Score final (indicatif): **{final_score:.1f}**")

st.caption("Note: c'est un MVP. On pourra affiner avec options, region, boite auto, historique, etc.")

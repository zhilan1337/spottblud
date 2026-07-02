"""
Odświeża długożyjący token dostępu do Instagram Graph API (ważny 60 dni).

Uruchom co ok. 45-50 dni (np. ręcznie, albo jako cron/systemd timer), zanim
obecny token wygaśnie - w przeciwnym razie automatyczna publikacja przestanie
działać, aż wygenerujesz token od nowa przez Graph API Explorer.

Użycie:
    python refresh_ig_token.py

Wymaga w .env: META_APP_ID, META_APP_SECRET, IG_ACCESS_TOKEN (aktualny, jeszcze ważny)
"""

import os
import requests
from dotenv import load_dotenv, set_key

load_dotenv()

APP_ID = os.getenv("META_APP_ID")
APP_SECRET = os.getenv("META_APP_SECRET")
CURRENT_TOKEN = os.getenv("IG_ACCESS_TOKEN")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

if not all([APP_ID, APP_SECRET, CURRENT_TOKEN]):
    raise SystemExit(
        "Brak META_APP_ID / META_APP_SECRET / IG_ACCESS_TOKEN w .env. "
        "META_APP_ID i META_APP_SECRET znajdziesz w Meta for Developers -> "
        "Twoja aplikacja -> Ustawienia -> Podstawowe."
    )

resp = requests.get(
    "https://graph.facebook.com/v22.0/oauth/access_token",
    params={
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "fb_exchange_token": CURRENT_TOKEN,
    },
    timeout=30,
)
resp.raise_for_status()
data = resp.json()
new_token = data["access_token"]
expires_in_days = data.get("expires_in", 0) // 86400

set_key(ENV_PATH, "IG_ACCESS_TOKEN", new_token)
print(f"Nowy token zapisany do .env. Ważny jeszcze ~{expires_in_days} dni.")
print("Pamiętaj zrestartować aplikację (systemctl restart spotted), żeby wczytała nowy token.")

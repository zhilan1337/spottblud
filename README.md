# Spotted — instrukcja uruchomienia

## 1. Stwórz bota na Discordzie

1. Wejdź na https://discord.com/developers/applications → **New Application** → nazwij np. "Spotted".
2. Zakładka **Bot** (po lewej) → **Reset Token** → skopiuj token (wklejasz go potem do `.env`, nikomu nie pokazuj).
3. Tam samo włącz **Message Content Intent** nie jest wymagany — bot nie czyta wiadomości, tylko reakcje. Zostaw domyślne ustawienia.
4. Zakładka **OAuth2 → URL Generator**:
   - Scopes: zaznacz `bot`
   - Bot Permissions: `View Channels`, `Send Messages`, `Embed Links`, `Add Reactions`, `Manage Messages`, `Read Message History`
   - Skopiuj wygenerowany link, otwórz w przeglądarce, zaproś bota na swój serwer.

## 2. Skonfiguruj kanał moderacyjny

1. Na swoim serwerze Discord włącz **tryb developera** (Ustawienia użytkownika → Zaawansowane → Tryb Developera).
2. Stwórz prywatny kanał tekstowy (widoczny tylko dla Ciebie/moderatorów) np. `#moderacja-spotted`.
3. Kliknij prawym na kanał → **Kopiuj ID kanału**.

## 3. Skonfiguruj projekt

```bash
cp .env.example .env
nano .env
```

Uzupełnij:
- `DISCORD_TOKEN` – token bota z kroku 1
- `MOD_CHANNEL_ID` – ID kanału z kroku 2
- `MOD_ROLE_ID` – (opcjonalnie) ID roli moderatora, jeśli chcesz ograniczyć kto może klikać ✅/❌

## 4. Instalacja i test lokalny

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Wejdź na `http://localhost:5000` — wypełnij formularz, sprawdź czy zgłoszenie pojawia się na kanale moderacyjnym z reakcjami ✅ ❌.

## 5. Wdrożenie na Oracle Cloud VPS (24/7)

Po skonfigurowaniu VM (Ubuntu) w Oracle Cloud:

```bash
# na serwerze
sudo apt update && sudo apt install python3-venv python3-pip -y
git clone <twoje-repo>   # albo scp/rsync pliki na serwer
cd spotted
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env   # uzupełnij dane
```

### Uruchamianie jako usługa systemd (żeby działał w tle i restartował się sam)

Stwórz plik `/etc/systemd/system/spotted.service`:

```ini
[Unit]
Description=Spotted bot + web
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/spotted
ExecStart=/home/ubuntu/spotted/venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Potem:

```bash
sudo systemctl daemon-reload
sudo systemctl enable spotted
sudo systemctl start spotted
sudo systemctl status spotted   # sprawdź czy działa
```

### Otwórz port w Oracle Cloud

W konsoli Oracle Cloud: **VCN → Security Lists → Add Ingress Rule** → port `5000` (albo postaw nginx jako reverse proxy na porcie 80/443, jeśli chcesz podpiąć własną domenę + darmowy SSL z Certbot).

## Jak to działa na co dzień

1. Ktoś wchodzi na Twoją stronę, wpisuje anonimowy tekst, klika "Przypnij anonimowo".
2. Zgłoszenie leci na kanał moderacyjny jako embed z ✅ i ❌ pod spodem.
3. Klikasz ✅ (akceptuj) lub ❌ (odrzuć) — bot zapisuje decyzję i oznacza wiadomość kolorem.
4. Zaakceptowane treści wklejasz ręcznie na Instagrama (grafika w Canvie + tekst).

## Baza danych

Wszystko trzyma się w pliku `spotted.db` (SQLite) w folderze projektu — kopia zapasowa to po prostu skopiowanie tego pliku.

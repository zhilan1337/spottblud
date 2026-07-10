"""
Spotted - anonimowe zgłoszenia moderowane na Discordzie.

Jeden proces łączy:
- Flask (serwuje stronę + endpoint /submit)
- discord.py bota (wysyła zgłoszenia na kanał moderacyjny, nasłuchuje reakcji ✅/❌
  oraz komend /status i /wstaw)
- po zaakceptowaniu (✅): generuje grafikę i wrzuca zgłoszenie do kolejki karuzeli
- komenda /wstaw: bierze wszystko co jest w kolejce i publikuje jako jedną karuzelę
  na Instagramie (2-10 zdjęć; przy 1 zdjęciu publikuje zwykły pojedynczy post)
- komenda /status: pokazuje ile i jakie zgłoszenia czekają w kolejce

Uruchomienie: python app.py
Wymagane zmienne środowiskowe w pliku .env (patrz .env.example)
"""

import os
import sqlite3
import asyncio
import logging
import threading
from datetime import datetime

import discord
from discord import app_commands
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

from image_generator import generate_post_image
from instagram_publisher import publish_image, publish_carousel, get_permalink

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("spotted")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MOD_CHANNEL_ID = int(os.getenv("MOD_CHANNEL_ID", "0"))
MOD_ROLE_ID = os.getenv("MOD_ROLE_ID")  # opcjonalne - jeśli puste, każdy może moderować
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # opcjonalne - przyspiesza propagację slash-komend
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "spotted.db"))

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

CAROUSEL_MAX_ITEMS = 10

# --- Instagram ---
IG_USER_ID = os.getenv("IG_USER_ID")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
IG_CAPTION_EXTRA = os.getenv("IG_CAPTION_EXTRA", "")
IG_HASHTAGS = os.getenv(
    "IG_HASHTAGS",
    "#zyrardow #żyrardów #spotted #spottedzyrardow #polska"
).strip()
IG_ENABLED = bool(IG_USER_ID and IG_ACCESS_TOKEN and PUBLIC_BASE_URL)

if not IG_ENABLED:
    log.warning(
        "Publikacja na Instagramie WYŁĄCZONA - brak IG_USER_ID / IG_ACCESS_TOKEN / "
        "PUBLIC_BASE_URL w zmiennych środowiskowych. Zaakceptowane zgłoszenia będą "
        "generowały grafikę, ale /wstaw nie będzie działać."
    )

# ---------------------------------------------------------------------------
# Baza danych
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                discord_message_id TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                instagram_media_id TEXT,
                published_at TEXT,
                publish_error TEXT
            )
        """)
        # Dokładka dla baz założonych przed dodaniem kolumn IG
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
        for col in ("instagram_media_id", "published_at", "publish_error"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} TEXT")


init_db()

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = False
intents.reactions = True
intents.guilds = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
bot_loop = None  # ustawiane po starcie bota, do wywoływania z wątku Flaska


def _is_moderator_member(member) -> bool:
    if not MOD_ROLE_ID:
        return True
    if member is None:
        return False
    return any(str(r.id) == MOD_ROLE_ID for r in getattr(member, "roles", []))


@bot.event
async def on_ready():
    print(f"[bot] Zalogowano jako {bot.user}")
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild_obj)
            await tree.sync(guild=guild_obj)
            print(f"[bot] Slash-komendy zsynchronizowane dla serwera {GUILD_ID}")
        else:
            await tree.sync()
            print("[bot] Slash-komendy zsynchronizowane globalnie (propagacja może potrwać do ~1h)")
    except Exception:
        log.exception("Synchronizacja slash-komend nie powiodła się")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if str(payload.emoji) not in (APPROVE_EMOJI, REJECT_EMOJI):
        return
    if payload.channel_id != MOD_CHANNEL_ID:
        return

    # Sprawdź rolę moderatora, jeśli skonfigurowana
    if MOD_ROLE_ID:
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if not _is_moderator_member(member):
            return

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE discord_message_id = ?",
            (str(payload.message_id),),
        ).fetchone()

        if not row or row["status"] != "pending":
            return

        new_status = "queued" if str(payload.emoji) == APPROVE_EMOJI else "rejected"
        # Przy akceptacji ostateczny status ('queued') ustawiamy dopiero po wygenerowaniu
        # grafiki niżej — tu tylko zapisujemy decided_at i (dla odrzucenia) finalny status.
        conn.execute(
            "UPDATE submissions SET status = ?, decided_at = ? WHERE id = ?",
            (row["status"] if new_status == "queued" else new_status,
             datetime.utcnow().isoformat(), row["id"]),
        )

    channel = bot.get_channel(payload.channel_id)
    message = await channel.fetch_message(payload.message_id)
    embed = message.embeds[0]

    if new_status == "rejected":
        embed.color = discord.Color.red()
        embed.set_footer(text="❌ ODRZUCONE")
        await message.edit(embed=embed)
        await message.clear_reactions()
        return

    # --- Zaakceptowane: generujemy grafikę i wrzucamy do kolejki karuzeli ---
    await message.clear_reactions()  # blokuje ponowne klikanie w trakcie przetwarzania

    embed.color = discord.Color.gold()
    embed.set_footer(text="⏳ Generuję grafikę...")
    await message.edit(embed=embed)

    try:
        filename = await asyncio.to_thread(generate_post_image, row["id"], row["content"])
    except Exception:
        log.exception("Generowanie grafiki nie powiodło się (zgłoszenie #%s)", row["id"])
        embed.color = discord.Color.orange()
        embed.set_footer(text="⚠️ Zaakceptowane, ale generowanie grafiki nie powiodło się")
        await message.edit(embed=embed)
        return

    with db() as conn:
        conn.execute("UPDATE submissions SET status = 'queued' WHERE id = ?", (row["id"],))
        queued_count = conn.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE status = 'queued'"
        ).fetchone()["c"]

    embed.color = discord.Color.blue()
    if PUBLIC_BASE_URL:
        embed.set_image(url=f"{PUBLIC_BASE_URL}/static/generated/{filename}")
    embed.set_footer(text=f"📥 W KOLEJCE DO KARUZELI ({queued_count} w stosie) — użyj /wstaw")
    await message.edit(embed=embed)


async def send_to_discord(submission_id: int, content: str):
    channel = bot.get_channel(MOD_CHANNEL_ID)
    if channel is None:
        print("[bot] Nie znaleziono kanału moderacyjnego - sprawdź MOD_CHANNEL_ID")
        return

    embed = discord.Embed(
        title=f"Zgłoszenie #{submission_id}",
        description=content,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Oczekuje na moderację")

    message = await channel.send(embed=embed)
    await message.add_reaction(APPROVE_EMOJI)
    await message.add_reaction(REJECT_EMOJI)

    with db() as conn:
        conn.execute(
            "UPDATE submissions SET discord_message_id = ? WHERE id = ?",
            (str(message.id), submission_id),
        )


# ---------------------------------------------------------------------------
# Slash-komendy: /status i /wstaw
# ---------------------------------------------------------------------------

def _build_caption(base_caption: str = None) -> str:
    """
    Składa finalny podpis pod post: opcjonalny tekst od moderatora (albo IG_CAPTION_EXTRA
    jako domyślny), a na końcu stały zestaw hashtagów (IG_HASHTAGS). Całość przycięta
    do limitu 2200 znaków Instagrama — jeśli trzeba coś obciąć, obcinane są hashtagi
    jako ostatnie, żeby nie ucinać treści posta.
    """
    text = (base_caption or IG_CAPTION_EXTRA or "").strip()
    if not IG_HASHTAGS:
        return text[:2200]

    combined = f"{text}\n\n{IG_HASHTAGS}" if text else IG_HASHTAGS
    if len(combined) <= 2200:
        return combined

    # Nie mieści się razem z hashtagami — obetnij same hashtagi, zachowując treść posta
    room_for_tags = 2200 - len(text) - 2  # -2 na "\n\n"
    if room_for_tags <= 0:
        return text[:2200]
    trimmed_tags = IG_HASHTAGS[:room_for_tags].rsplit(" ", 1)[0]
    return f"{text}\n\n{trimmed_tags}" if trimmed_tags else text[:2200]


def _queued_rows():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM submissions WHERE status = 'queued' ORDER BY id"
        ).fetchall()


async def _check_mod_channel_and_role(interaction: discord.Interaction) -> bool:
    if interaction.channel_id != MOD_CHANNEL_ID:
        await interaction.response.send_message(
            "Ta komenda działa tylko na kanale moderacyjnym.", ephemeral=True
        )
        return False
    if not _is_moderator_member(interaction.user):
        await interaction.response.send_message("Brak uprawnień moderatora.", ephemeral=True)
        return False
    return True


@tree.command(name="status", description="Pokazuje ile zgłoszeń czeka w kolejce do karuzeli")
async def status_command(interaction: discord.Interaction):
    if not await _check_mod_channel_and_role(interaction):
        return

    rows = _queued_rows()
    if not rows:
        await interaction.response.send_message("📭 Kolejka jest pusta.")
        return

    embed = discord.Embed(
        title=f"📥 Kolejka do karuzeli — {len(rows)} zgłoszeń",
        color=discord.Color.blue(),
    )
    preview_rows = rows[:CAROUSEL_MAX_ITEMS]
    lines = []
    for r in preview_rows:
        snippet = r["content"].strip().replace("\n", " ")
        if len(snippet) > 60:
            snippet = snippet[:60] + "…"
        lines.append(f"**#{r['id']}** — {snippet}")
    embed.description = "\n".join(lines)

    if len(rows) > CAROUSEL_MAX_ITEMS:
        embed.set_footer(
            text=f"+ {len(rows) - CAROUSEL_MAX_ITEMS} kolejnych ponad limit karuzeli "
                 f"({CAROUSEL_MAX_ITEMS}) — zostaną w kolejce po /wstaw"
        )
    else:
        embed.set_footer(text="Użyj /wstaw, żeby opublikować to jako karuzelę")

    await interaction.response.send_message(embed=embed)


@tree.command(name="wstaw", description="Publikuje zestackowane zgłoszenia jako post/karuzelę na Instagramie")
@app_commands.describe(caption="Opcjonalny podpis pod post (domyślnie IG_CAPTION_EXTRA)")
async def wstaw_command(interaction: discord.Interaction, caption: str = None):
    if not await _check_mod_channel_and_role(interaction):
        return

    if not IG_ENABLED:
        await interaction.response.send_message(
            "Instagram nie jest skonfigurowany (brak IG_USER_ID / IG_ACCESS_TOKEN / PUBLIC_BASE_URL).",
            ephemeral=True,
        )
        return

    rows = _queued_rows()
    if not rows:
        await interaction.response.send_message("📭 Kolejka jest pusta — nie ma czego publikować.")
        return

    batch = rows[:CAROUSEL_MAX_ITEMS]
    leftover = rows[CAROUSEL_MAX_ITEMS:]
    final_caption = _build_caption(caption)
    image_urls = [f"{PUBLIC_BASE_URL}/static/generated/post_{r['id']}.jpg" for r in batch]

    await interaction.response.defer(thinking=True)

    try:
        if len(batch) == 1:
            media_id = await asyncio.to_thread(
                publish_image, IG_USER_ID, IG_ACCESS_TOKEN, image_urls[0], final_caption
            )
        else:
            media_id = await asyncio.to_thread(
                publish_carousel, IG_USER_ID, IG_ACCESS_TOKEN, image_urls, final_caption
            )
        permalink = await asyncio.to_thread(get_permalink, media_id, IG_ACCESS_TOKEN)
    except Exception as exc:
        log.exception("Publikacja nie powiodła się")
        ids = ", ".join(f"#{r['id']}" for r in batch)
        with db() as conn:
            conn.executemany(
                "UPDATE submissions SET publish_error = ? WHERE id = ?",
                [(str(exc), r["id"]) for r in batch],
            )
        await interaction.followup.send(f"⚠️ Publikacja nie powiodła się ({ids}): {exc}")
        return

    ids = [r["id"] for r in batch]
    with db() as conn:
        conn.executemany(
            "UPDATE submissions SET status = 'published', instagram_media_id = ?, published_at = ? WHERE id = ?",
            [(media_id, datetime.utcnow().isoformat(), sid) for sid in ids],
        )

    channel = bot.get_channel(MOD_CHANNEL_ID)
    for r in batch:
        if not r["discord_message_id"]:
            continue
        try:
            message = await channel.fetch_message(int(r["discord_message_id"]))
            embed = message.embeds[0]
            embed.color = discord.Color.green()
            label = "OPUBLIKOWANE" if len(batch) == 1 else "OPUBLIKOWANE W KARUZELI"
            embed.set_footer(text=f"✅ {label}")
            await message.edit(embed=embed)
        except Exception:
            log.exception("Nie udało się zaktualizować embeda dla zgłoszenia #%s", r["id"])

    kind = "post" if len(batch) == 1 else f"karuzelę ({len(batch)} zdjęć)"
    ids_text = ", ".join(f"#{i}" for i in ids)
    summary = f"✅ Opublikowano {kind}: {ids_text}"
    if permalink:
        summary += f"\n{permalink}"
    if leftover:
        summary += (
            f"\n\n📥 {len(leftover)} zgłoszeń zostało w kolejce (ponad limit "
            f"{CAROUSEL_MAX_ITEMS}) — użyj /wstaw ponownie, żeby je opublikować."
        )

    await interaction.followup.send(summary)


def run_bot():
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_until_complete(bot.start(DISCORD_TOKEN))


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)

MAX_LEN = 500


@app.route("/")
def index():
    return render_template("index.html", max_len=MAX_LEN)


@app.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()

    if not content:
        return jsonify({"error": "Treść nie może być pusta."}), 400
    if len(content) > MAX_LEN:
        return jsonify({"error": f"Maksymalnie {MAX_LEN} znaków."}), 400

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO submissions (content, status, created_at) VALUES (?, 'pending', ?)",
            (content, datetime.utcnow().isoformat()),
        )
        submission_id = cur.lastrowid

    if bot_loop is not None:
        asyncio.run_coroutine_threadsafe(send_to_discord(submission_id, content), bot_loop)

    return jsonify({"ok": True})


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

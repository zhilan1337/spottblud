"""
Spotted - anonimowe zgłoszenia moderowane na Discordzie.

Jeden proces łączy:
- Flask (serwuje stronę + endpoint /submit)
- discord.py bota (wysyła zgłoszenia na kanał moderacyjny, nasłuchuje reakcji ✅/❌)
- po zaakceptowaniu (✅): generuje grafikę i wrzuca ją do kolejki
- co jakiś czas (albo na żądanie przez /wstaw): publikuje zebrane zgłoszenia
  razem jako karuzelę na Instagramie

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
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "spotted.db"))

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

# --- Instagram ---
IG_USER_ID = os.getenv("IG_USER_ID")
IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
IG_CAPTION_EXTRA = os.getenv("IG_CAPTION_EXTRA", "")
IG_ENABLED = bool(IG_USER_ID and IG_ACCESS_TOKEN and PUBLIC_BASE_URL)

# --- Stackowanie w karuzele ---
CAROUSEL_MIN_ITEMS = int(os.getenv("CAROUSEL_MIN_ITEMS", "2"))
CAROUSEL_MAX_ITEMS = int(os.getenv("CAROUSEL_MAX_ITEMS", "10"))
CAROUSEL_CHECK_INTERVAL_MINUTES = int(os.getenv("CAROUSEL_CHECK_INTERVAL_MINUTES", "15"))
CAROUSEL_MAX_WAIT_MINUTES = int(os.getenv("CAROUSEL_MAX_WAIT_MINUTES", "120"))

if not IG_ENABLED:
    log.warning(
        "Publikacja na Instagramie WYŁĄCZONA - brak IG_USER_ID / IG_ACCESS_TOKEN / "
        "PUBLIC_BASE_URL w zmiennych środowiskowych. Zaakceptowane zgłoszenia będą "
        "tylko oznaczane, bez automatycznej publikacji."
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
                image_filename TEXT,
                instagram_media_id TEXT,
                published_at TEXT,
                publish_error TEXT
            )
        """)
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
        for col in ("image_filename", "instagram_media_id", "published_at", "publish_error"):
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
_carousel_task_started = False


def _is_moderator(member: discord.Member | None) -> bool:
    if not MOD_ROLE_ID:
        return True
    if member is None:
        return False
    return any(str(r.id) == MOD_ROLE_ID for r in member.roles)


@bot.event
async def on_ready():
    global _carousel_task_started
    print(f"[bot] Zalogowano jako {bot.user}")

    for guild in bot.guilds:
        try:
            await tree.sync(guild=guild)
        except Exception:
            log.exception("Nie udało się zsynchronizować slash-komend dla guildu %s", guild.id)

    if IG_ENABLED and not _carousel_task_started:
        _carousel_task_started = True
        bot.loop.create_task(carousel_publisher_loop())


@tree.command(name="wstaw", description="Publikuje zaległe zaakceptowane zgłoszenia na Instagramie (ręcznie, teraz).")
async def wstaw_command(interaction: discord.Interaction):
    if not _is_moderator(interaction.user if isinstance(interaction.user, discord.Member) else None):
        await interaction.response.send_message("Nie masz uprawnień do tej komendy.", ephemeral=True)
        return

    if not IG_ENABLED:
        await interaction.response.send_message("Publikacja na Instagramie jest wyłączona (brak konfiguracji).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await try_publish_queue(force=True)

    if result["status"] == "empty":
        await interaction.followup.send("Kolejka jest pusta - nic do opublikowania.", ephemeral=True)
    elif result["status"] == "published":
        extra = f" (karuzela, {result['count']} zdjęć)" if result["count"] > 1 else ""
        link = f"\n{result['permalink']}" if result.get("permalink") else ""
        await interaction.followup.send(f"Opublikowano{extra}.{link}", ephemeral=True)
    else:
        await interaction.followup.send(f"Publikacja nie powiodła się: {result.get('error', 'nieznany błąd')}", ephemeral=True)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if str(payload.emoji) not in (APPROVE_EMOJI, REJECT_EMOJI):
        return
    if payload.channel_id != MOD_CHANNEL_ID:
        return

    if MOD_ROLE_ID:
        guild = bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        if not member or not any(str(r.id) == MOD_ROLE_ID for r in member.roles):
            return

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM submissions WHERE discord_message_id = ?",
            (str(payload.message_id),),
        ).fetchone()

        if not row or row["status"] != "pending":
            return

        new_status = "approved" if str(payload.emoji) == APPROVE_EMOJI else "rejected"
        conn.execute(
            "UPDATE submissions SET status = ?, decided_at = ? WHERE id = ?",
            (new_status, datetime.utcnow().isoformat(), row["id"]),
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

    await message.clear_reactions()

    if not IG_ENABLED:
        embed.color = discord.Color.green()
        embed.set_footer(text="✅ ZAAKCEPTOWANE — Instagram nie skonfigurowany, wklej ręcznie")
        await message.edit(embed=embed)
        return

    embed.color = discord.Color.gold()
    embed.set_footer(text="⏳ Generuję grafikę...")
    await message.edit(embed=embed)

    await generate_and_queue(row["id"], row["content"], message, embed)


async def generate_and_queue(submission_id: int, content: str,
                              message: discord.Message, embed: discord.Embed):
    """Generuje grafikę i wrzuca zgłoszenie do kolejki na wspólny post karuzelowy."""
    try:
        filename = await asyncio.to_thread(generate_post_image, submission_id, content)

        with db() as conn:
            conn.execute(
                "UPDATE submissions SET status = 'queued_ig', image_filename = ? WHERE id = ?",
                (filename, submission_id),
            )

        image_url = f"{PUBLIC_BASE_URL}/static/generated/{filename}"
        embed.color = discord.Color.gold()
        embed.set_image(url=image_url)
        embed.set_footer(text="✅ Zaakceptowane — czeka w kolejce na wspólny post (użyj /wstaw, by opublikować teraz)")
        await message.edit(embed=embed)

    except Exception as exc:  # noqa: BLE001
        log.exception("Generowanie grafiki nie powiodło się (zgłoszenie #%s)", submission_id)
        with db() as conn:
            conn.execute(
                "UPDATE submissions SET publish_error = ? WHERE id = ?",
                (str(exc), submission_id),
            )
        embed.color = discord.Color.orange()
        embed.set_footer(text="⚠️ Zaakceptowane, ale generowanie grafiki nie powiodło się")
        embed.add_field(name="Błąd", value=str(exc)[:1000], inline=False)
        await message.edit(embed=embed)


async def carousel_publisher_loop():
    """W tle, co CAROUSEL_CHECK_INTERVAL_MINUTES, próbuje opublikować kolejkę."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await try_publish_queue()
        except Exception:
            log.exception("Błąd w pętli publikującej karuzele")
        await asyncio.sleep(CAROUSEL_CHECK_INTERVAL_MINUTES * 60)


async def try_publish_queue(force: bool = False) -> dict:
    """
    Sprawdza kolejkę i publikuje, jeśli warunki spełnione (albo force=True - wtedy
    publikuje natychmiast to, co jest w kolejce, niezależnie od progów).
    Zwraca dict z wynikiem, używany też przez komendę /wstaw do odpowiedzi.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM submissions WHERE status = 'queued_ig' ORDER BY decided_at ASC"
        ).fetchall()

    if not rows:
        return {"status": "empty"}

    if not force:
        oldest_wait_minutes = (
            datetime.utcnow() - datetime.fromisoformat(rows[0]["decided_at"])
        ).total_seconds() / 60
        if len(rows) < CAROUSEL_MIN_ITEMS and oldest_wait_minutes < CAROUSEL_MAX_WAIT_MINUTES:
            return {"status": "waiting"}

    batch = rows[:CAROUSEL_MAX_ITEMS]

    if len(batch) == 1:
        return await publish_single_from_queue(batch[0])
    return await publish_carousel_batch(batch)


async def publish_carousel_batch(rows) -> dict:
    image_urls = [f"{PUBLIC_BASE_URL}/static/generated/{r['image_filename']}" for r in rows]
    caption = "\n\n---\n\n".join(r["content"].strip() for r in rows)
    if IG_CAPTION_EXTRA:
        caption = f"{caption}\n\n{IG_CAPTION_EXTRA}"
    caption = caption[:2200]

    try:
        media_id = await asyncio.to_thread(
            publish_carousel, IG_USER_ID, IG_ACCESS_TOKEN, image_urls, caption
        )
        permalink = await asyncio.to_thread(get_permalink, media_id, IG_ACCESS_TOKEN)

        now = datetime.utcnow().isoformat()
        with db() as conn:
            for r in rows:
                conn.execute(
                    "UPDATE submissions SET status = 'published', instagram_media_id = ?, "
                    "published_at = ? WHERE id = ?",
                    (media_id, now, r["id"]),
                )

        await update_discord_messages(rows, success=True, permalink=permalink, batch_size=len(rows))
        return {"status": "published", "count": len(rows), "permalink": permalink}

    except Exception as exc:  # noqa: BLE001
        log.exception("Publikacja karuzeli nie powiodła się (zgłoszenia: %s)", [r["id"] for r in rows])
        with db() as conn:
            for r in rows:
                conn.execute(
                    "UPDATE submissions SET publish_error = ? WHERE id = ?",
                    (str(exc), r["id"]),
                )
        await update_discord_messages(rows, success=False, error=str(exc))
        return {"status": "error", "error": str(exc)}


async def publish_single_from_queue(row) -> dict:
    submission_id = row["id"]
    image_url = f"{PUBLIC_BASE_URL}/static/generated/{row['image_filename']}"
    caption = row["content"].strip()
    if IG_CAPTION_EXTRA:
        caption = f"{caption}\n\n{IG_CAPTION_EXTRA}"
    caption = caption[:2200]

    try:
        media_id = await asyncio.to_thread(
            publish_image, IG_USER_ID, IG_ACCESS_TOKEN, image_url, caption
        )
        permalink = await asyncio.to_thread(get_permalink, media_id, IG_ACCESS_TOKEN)

        now = datetime.utcnow().isoformat()
        with db() as conn:
            conn.execute(
                "UPDATE submissions SET status = 'published', instagram_media_id = ?, "
                "published_at = ? WHERE id = ?",
                (media_id, now, submission_id),
            )

        await update_discord_messages([row], success=True, permalink=permalink, batch_size=1)
        return {"status": "published", "count": 1, "permalink": permalink}

    except Exception as exc:  # noqa: BLE001
        log.exception("Publikacja nie powiodła się (zgłoszenie #%s)", submission_id)
        with db() as conn:
            conn.execute(
                "UPDATE submissions SET publish_error = ? WHERE id = ?",
                (str(exc), submission_id),
            )
        await update_discord_messages([row], success=False, error=str(exc))
        return {"status": "error", "error": str(exc)}


async def update_discord_messages(rows, success, permalink=None, error=None, batch_size=None):
    channel = bot.get_channel(MOD_CHANNEL_ID)
    if channel is None:
        return
    for r in rows:
        if not r["discord_message_id"]:
            continue
        try:
            message = await channel.fetch_message(int(r["discord_message_id"]))
        except discord.NotFound:
            continue
        embed = message.embeds[0]
        if success:
            embed.color = discord.Color.green()
            label = "✅ OPUBLIKOWANE" + (f" (karuzela, {batch_size} zdjęć)" if batch_size and batch_size > 1 else "")
            embed.set_footer(text=label)
            if permalink:
                embed.add_field(name="Link do posta", value=permalink, inline=False)
        else:
            embed.color = discord.Color.orange()
            embed.set_footer(text="⚠️ Publikacja nie powiodła się — sprawdź logi / wklej ręcznie")
            embed.add_field(name="Błąd", value=(error or "")[:1000], inline=False)
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

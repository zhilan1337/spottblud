"""
Spotted - anonimowe zgłoszenia moderowane na Discordzie.

Jeden proces łączy:
- Flask (serwuje stronę + endpoint /submit)
- discord.py bota (wysyła zgłoszenia na kanał moderacyjny, nasłuchuje reakcji ✅/❌)

Uruchomienie: python app.py
Wymagane zmienne środowiskowe w pliku .env (patrz .env.example)
"""

import os
import sqlite3
import asyncio
import threading
from datetime import datetime

import discord
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MOD_CHANNEL_ID = int(os.getenv("MOD_CHANNEL_ID", "0"))
MOD_ROLE_ID = os.getenv("MOD_ROLE_ID")  # opcjonalne - jeśli puste, każdy może moderować
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "spotted.db"))

APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"

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
                decided_at TEXT
            )
        """)


init_db()

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = False
intents.reactions = True
intents.guilds = True

bot = discord.Client(intents=intents)
bot_loop = None  # ustawiane po starcie bota, do wywoływania z wątku Flaska


@bot.event
async def on_ready():
    print(f"[bot] Zalogowano jako {bot.user}")


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

    label = "✅ ZAAKCEPTOWANE — wklej ręcznie na Instagrama" if new_status == "approved" else "❌ ODRZUCONE"
    embed = message.embeds[0]
    embed.color = discord.Color.green() if new_status == "approved" else discord.Color.red()
    embed.set_footer(text=label)
    await message.edit(embed=embed)
    await message.clear_reactions()


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

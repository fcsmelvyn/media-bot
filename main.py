import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("telegram-media-bot")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()

RADARR_URL = os.getenv("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")
SONARR_URL = os.getenv("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.getenv("SONARR_API_KEY", "")
SEERR_URL = os.getenv("SEERR_URL", "").rstrip("/")
SEERR_API_KEY = os.getenv("SEERR_API_KEY", "")
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOPICS_FILE = DATA_DIR / "topics.json"


def parse_ids(raw: str) -> set[int]:
    result = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                result.add(int(value))
            except ValueError:
                log.warning("ALLOWED_USER_IDS invalide: %s", value)
    return result


ALLOWED_USER_IDS = parse_ids(ALLOWED_USER_IDS_RAW)


def load_topics() -> dict:
    if TOPICS_FILE.exists():
        try:
            return json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Impossible de lire topics.json")
    return {}


def save_topics(data: dict) -> None:
    TOPICS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


topics = load_topics()


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    if TELEGRAM_CHAT_ID:
        try:
            if chat.id != int(TELEGRAM_CHAT_ID):
                return False
        except ValueError:
            pass

    # Vide = tous les membres du groupe peuvent utiliser les commandes.
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
        return False

    return True


async def deny(update: Update) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Tu n'es pas autorisé à utiliser ce bot.")


async def remember_topic(update: Update) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    thread_id = msg.message_thread_id
    if thread_id is None:
        return

    key = str(thread_id)
    current = topics.get(key, {})
    current["chat_id"] = chat.id
    current["thread_id"] = thread_id

    # Telegram ne fournit pas systématiquement le nom du topic avec chaque message.
    # /topicid permet donc de l'enregistrer proprement avec un nom.
    topics[key] = current
    save_topics(topics)


async def passive_topic_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await remember_topic(update)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    await remember_topic(update)
    await update.effective_message.reply_text(
        "🏠 Media Server Bot\n\n"
        "Commandes disponibles :\n"
        "/setup - afficher Chat ID et Topic ID\n"
        "/topicid nom - mémoriser le topic actuel\n"
        "/topics - afficher les topics mémorisés\n"
        "/status - état des services\n"
        "/film titre - rechercher dans Radarr\n"
        "/serie titre - rechercher dans Sonarr\n"
        "/help - aide"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    await remember_topic(update)

    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    await msg.reply_text(
        "🔧 Informations Telegram\n\n"
        f"Chat ID : {chat.id}\n"
        f"Ton User ID : {user.id}\n"
        f"Topic ID actuel : {msg.message_thread_id or 'aucun'}\n\n"
        "Mets TELEGRAM_CHAT_ID dans Coolify avec le Chat ID.\n"
        "Pour limiter le bot à certaines personnes, mets leurs User ID dans "
        "ALLOWED_USER_IDS séparés par des virgules."
    )


async def topicid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    msg = update.effective_message
    chat = update.effective_chat
    thread_id = msg.message_thread_id

    if thread_id is None:
        return await msg.reply_text(
            "⚠️ Lance cette commande à l'intérieur d'un Topic."
        )

    name = " ".join(context.args).strip()
    if not name:
        name = f"Topic {thread_id}"

    topics[str(thread_id)] = {
        "name": name,
        "chat_id": chat.id,
        "thread_id": thread_id,
    }
    save_topics(topics)

    await msg.reply_text(
        f"✅ Topic enregistré\n\n"
        f"Nom : {name}\n"
        f"Chat ID : {chat.id}\n"
        f"Topic ID : {thread_id}"
    )


async def topics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    if not topics:
        return await update.effective_message.reply_text(
            "Aucun topic mémorisé.\n"
            "Va dans chaque Topic et lance :\n"
            "/topicid Films"
        )

    lines = ["🗂️ Topics mémorisés", ""]
    for item in sorted(topics.values(), key=lambda x: x.get("thread_id", 0)):
        lines.append(
            f"• {item.get('name', 'Sans nom')} : {item.get('thread_id')}"
        )

    await update.effective_message.reply_text("\n".join(lines))


async def check_service(
    name: str,
    url: str,
    path: str = "",
    headers: Optional[dict] = None,
) -> str:
    if not url:
        return f"⚪ {name} — non configuré"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{url}{path}", headers=headers or {})
        if response.status_code < 500:
            return f"🟢 {name}"
        return f"🔴 {name} — HTTP {response.status_code}"
    except Exception:
        return f"🔴 {name} — inaccessible"


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    radarr_headers = {"X-Api-Key": RADARR_API_KEY} if RADARR_API_KEY else {}
    sonarr_headers = {"X-Api-Key": SONARR_API_KEY} if SONARR_API_KEY else {}
    seerr_headers = {"X-Api-Key": SEERR_API_KEY} if SEERR_API_KEY else {}
    jellyfin_headers = (
        {"X-Emby-Token": JELLYFIN_API_KEY} if JELLYFIN_API_KEY else {}
    )

    results = [
        await check_service("Jellyfin", JELLYFIN_URL, "/System/Info/Public", jellyfin_headers),
        await check_service("Radarr", RADARR_URL, "/api/v3/system/status", radarr_headers),
        await check_service("Sonarr", SONARR_URL, "/api/v3/system/status", sonarr_headers),
        await check_service("Seerr", SEERR_URL, "/api/v1/status", seerr_headers),
    ]

    await update.effective_message.reply_text(
        "🖥️ MEDIA SERVER\n\n" + "\n".join(results)
    )


async def film(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    query = " ".join(context.args).strip()
    if not query:
        return await update.effective_message.reply_text(
            "Utilisation : /film Dune"
        )

    if not RADARR_URL or not RADARR_API_KEY:
        return await update.effective_message.reply_text(
            "⚠️ Radarr n'est pas configuré."
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{RADARR_URL}/api/v3/movie/lookup",
                params={"term": query},
                headers={"X-Api-Key": RADARR_API_KEY},
            )
            r.raise_for_status()
            data = r.json()

        if not data:
            return await update.effective_message.reply_text(
                f"❌ Aucun film trouvé pour « {query} »."
            )

        lines = [f"🎬 Résultats pour « {query} »", ""]
        for movie in data[:5]:
            title = movie.get("title", "?")
            year = movie.get("year", "?")
            tmdb = movie.get("tmdbId", "?")
            lines.append(f"• {title} ({year}) — TMDb {tmdb}")

        await update.effective_message.reply_text("\n".join(lines))
    except Exception as e:
        log.exception("Erreur Radarr")
        await update.effective_message.reply_text(
            f"🔴 Erreur Radarr : {type(e).__name__}"
        )


async def serie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return await deny(update)

    query = " ".join(context.args).strip()
    if not query:
        return await update.effective_message.reply_text(
            "Utilisation : /serie Breaking Bad"
        )

    if not SONARR_URL or not SONARR_API_KEY:
        return await update.effective_message.reply_text(
            "⚠️ Sonarr n'est pas configuré."
        )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SONARR_URL}/api/v3/series/lookup",
                params={"term": query},
                headers={"X-Api-Key": SONARR_API_KEY},
            )
            r.raise_for_status()
            data = r.json()

        if not data:
            return await update.effective_message.reply_text(
                f"❌ Aucune série trouvée pour « {query} »."
            )

        lines = [f"📺 Résultats pour « {query} »", ""]
        for show in data[:5]:
            title = show.get("title", "?")
            year = show.get("year", "?")
            tvdb = show.get("tvdbId", "?")
            lines.append(f"• {title} ({year}) — TVDb {tvdb}")

        await update.effective_message.reply_text("\n".join(lines))
    except Exception as e:
        log.exception("Erreur Sonarr")
        await update.effective_message.reply_text(
            f"🔴 Erreur Sonarr : {type(e).__name__}"
        )


def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setup", setup))
    app.add_handler(CommandHandler("topicid", topicid))
    app.add_handler(CommandHandler("topics", topics_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("film", film))
    app.add_handler(CommandHandler("serie", serie))

    # Enregistre silencieusement les Topic IDs vus par le bot.
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, passive_topic_capture)
    )

    log.info("Démarrage du Telegram Media Bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
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
JELLYFIN_PUBLIC_URL = os.getenv("JELLYFIN_PUBLIC_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

DATA_DIR = Path("/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOPICS_FILE = DATA_DIR / "topics.json"
STATE_FILE = DATA_DIR / "state.json"
TMDB_POSTER_BASE = "https://image.tmdb.org/t/p/w500"


def parse_ids(raw: str) -> set[int]:
    result = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                result.add(int(value))
            except ValueError:
                pass
    return result


ALLOWED_USER_IDS = parse_ids(ALLOWED_USER_IDS_RAW)


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Impossible de lire %s", path)
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


topics = load_json(TOPICS_FILE, {})
state = load_json(STATE_FILE, {"initialized": False, "radarr_seen": [], "sonarr_seen": []})


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
    if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
        return False
    return True


async def deny(update: Update):
    if update.effective_message:
        await update.effective_message.reply_text("â Tu n'es pas autorisÃ© Ã  utiliser ce bot.")


async def remember_topic(update: Update):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or msg.message_thread_id is None:
        return
    key = str(msg.message_thread_id)
    current = topics.get(key, {})
    current["chat_id"] = chat.id
    current["thread_id"] = msg.message_thread_id
    topics[key] = current
    save_json(TOPICS_FILE, topics)


def find_topic_id(*names: str) -> Optional[int]:
    wanted = {n.strip().lower() for n in names}
    for item in topics.values():
        if str(item.get("name", "")).strip().lower() in wanted:
            try:
                return int(item["thread_id"])
            except Exception:
                pass
    fallback = {
        "annonces": 2, "films": 3, "film": 3, "sÃ©rie": 5, "series": 5, "sÃ©ries": 5, "serie": 5,
        "demande": 6, "demandes": 6, "jellyfin": 7, "serveur": 8, "server": 8, "gÃ©nÃ©ral": 9, "general": 9
    }
    for name in wanted:
        if name in fallback:
            return fallback[name]
    return None


async def send_to_topic(app: Application, topic_names, text: str):
    if not TELEGRAM_CHAT_ID:
        return
    thread_id = find_topic_id(*topic_names)
    if not thread_id:
        return
    await app.bot.send_message(
        chat_id=int(TELEGRAM_CHAT_ID),
        message_thread_id=thread_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def api_get(url, path, api_key="", params=None, timeout=10.0):
    headers = {"X-Api-Key": api_key} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{url}{path}", headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()


async def api_post(url, path, api_key="", payload=None, timeout=15.0):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{url}{path}", headers=headers, json=payload or {})
        r.raise_for_status()
        return r.json() if r.content else {}


def status_text(item):
    status = (item.get("mediaInfo") or {}).get("status")
    return {
        2: "ð¡ En attente",
        3: "ð  En traitement",
        4: "ð¢ Partiellement disponible",
        5: "â Disponible",
    }.get(status, "â Pas encore demandÃ©")


async def seerr_search(query: str, media_type: str):
    # IMPORTANT:
    # httpx encode normalement les paramÃ¨tres de formulaire avec "+" pour les espaces.
    # Seerr 3.4.x refuse ce format. On construit donc ici la query brute avec %20.
    encoded_query = quote(query, safe="")
    base = httpx.URL(f"{SEERR_URL}/api/v1/search")
    url = base.copy_with(
        query=f"query={encoded_query}&page=1".encode("ascii")
    )

    headers = {"X-Api-Key": SEERR_API_KEY}

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, headers=headers)

        if r.status_code >= 400:
            log.error(
                "Seerr search error HTTP %s | URL=%s | BODY=%s",
                r.status_code,
                r.url,
                r.text[:1000],
            )

        r.raise_for_status()
        data = r.json()

    results = data.get("results", data if isinstance(data, list) else [])
    return [x for x in results if x.get("mediaType") == media_type][:5]


async def seerr_tv_details(media_id: int):
    return await api_get(SEERR_URL, f"/api/v1/tv/{media_id}", SEERR_API_KEY)


async def seerr_request(media_type: str, media_id: int, seasons=None):
    payload = {"mediaType": media_type, "mediaId": media_id}
    if media_type == "tv":
        payload["seasons"] = seasons if seasons is not None else "all"
    return await api_post(SEERR_URL, "/api/v1/request", SEERR_API_KEY, payload)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    await remember_topic(update)
    await update.effective_message.reply_text(
        "ð  <b>Media Server Bot</b>\n\n"
        "/film Dune\n/serie Breaking Bad\n/status\n/setup\n/topicid Nom\n/topics",
        parse_mode=ParseMode.HTML,
    )


async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    await remember_topic(update)
    msg = update.effective_message
    await msg.reply_text(
        f"Chat ID : <code>{update.effective_chat.id}</code>\n"
        f"User ID : <code>{update.effective_user.id}</code>\n"
        f"Topic ID : <code>{msg.message_thread_id or 'aucun'}</code>",
        parse_mode=ParseMode.HTML,
    )


async def topicid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    msg = update.effective_message
    if msg.message_thread_id is None:
        return await msg.reply_text("â ï¸ Lance cette commande dans un Topic.")
    name = " ".join(context.args).strip() or f"Topic {msg.message_thread_id}"
    topics[str(msg.message_thread_id)] = {
        "name": name,
        "chat_id": update.effective_chat.id,
        "thread_id": msg.message_thread_id,
    }
    save_json(TOPICS_FILE, topics)
    await msg.reply_text(f"â {name} = {msg.message_thread_id}")


async def topics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    lines = ["ðï¸ <b>Topics mÃ©morisÃ©s</b>", ""]
    for item in sorted(topics.values(), key=lambda x: x.get("thread_id", 0)):
        lines.append(f"â¢ {item.get('name', 'Sans nom')} : <code>{item.get('thread_id')}</code>")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def check_service(name, url, path, api_key=""):
    if not url:
        return f"âª {name} â non configurÃ©"
    try:
        await api_get(url, path, api_key, timeout=5.0)
        return f"ð¢ {name}"
    except Exception:
        return f"ð´ {name} â inaccessible"


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    results = [
        await check_service("Jellyfin", JELLYFIN_URL, "/System/Info/Public"),
        await check_service("Radarr", RADARR_URL, "/api/v3/system/status", RADARR_API_KEY),
        await check_service("Sonarr", SONARR_URL, "/api/v3/system/status", SONARR_API_KEY),
        await check_service("Seerr", SEERR_URL, "/api/v1/status", SEERR_API_KEY),
    ]
    await update.effective_message.reply_text("ð¥ï¸ <b>MEDIA SERVER</b>\n\n" + "\n".join(results), parse_mode=ParseMode.HTML)


async def search_media(update: Update, context: ContextTypes.DEFAULT_TYPE, media_type: str):
    if not is_allowed(update):
        return await deny(update)
    query = " ".join(context.args).strip()
    if not query:
        return await update.effective_message.reply_text("Utilisation : /film Dune" if media_type == "movie" else "Utilisation : /serie Breaking Bad")
    try:
        results = await seerr_search(query, media_type)
        if not results:
            return await update.effective_message.reply_text("â Aucun rÃ©sultat.")
        for item in results:
            media_id = item.get("id")
            title = item.get("title") or item.get("name") or "Sans titre"
            date = item.get("releaseDate") or item.get("firstAirDate") or ""
            year = date[:4] if date else "?"
            overview = (item.get("overview") or "").strip()
            if len(overview) > 350:
                overview = overview[:347] + "..."
            s = (item.get("mediaInfo") or {}).get("status")
            rows = []

            if s == 5:
                if JELLYFIN_PUBLIC_URL:
                    rows.append([
                        InlineKeyboardButton("â¶ï¸ Ouvrir Jellyfin", url=JELLYFIN_PUBLIC_URL)
                    ])
                else:
                    rows.append([
                        InlineKeyboardButton("â DÃ©jÃ  disponible", callback_data="noop")
                    ])
            elif s in {2, 3, 4}:
                rows.append([
                    InlineKeyboardButton("ð DÃ©jÃ  demandÃ©", callback_data="noop")
                ])
            else:
                if media_type == "tv":
                    rows.append([
                        InlineKeyboardButton(
                            "ðº Choisir les saisons",
                            callback_data=f"tvseasons:{media_id}"
                        )
                    ])
                else:
                    rows.append([
                        InlineKeyboardButton(
                            "â Demander",
                            callback_data=f"request:movie:{media_id}"
                        )
                    ])

            caption = f"<b>{title}</b> ({year})\n{status_text(item)}"
            if overview:
                caption += f"\n\n{overview}"
            markup = InlineKeyboardMarkup(rows)
            poster = item.get("posterPath")
            if poster:
                try:
                    await update.effective_message.reply_photo(
                        photo=f"{TMDB_POSTER_BASE}{poster}",
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                    )
                    continue
                except Exception:
                    pass
            await update.effective_message.reply_text(caption, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception as e:
        log.exception("Erreur recherche Seerr")
        await update.effective_message.reply_text(f"ð´ Erreur Seerr : {type(e).__name__}")


async def film(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await search_media(update, context, "movie")


async def serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await search_media(update, context, "tv")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return

    if q.data == "noop":
        return await q.answer()

    if not is_allowed(update):
        return await q.answer("Non autorisÃ©", show_alert=True)

    await q.answer()

    # 1) Choix des saisons d'une sÃ©rie
    if q.data.startswith("tvseasons:"):
        try:
            media_id = int(q.data.split(":", 1)[1])
            details = await seerr_tv_details(media_id)

            seasons = []
            for season in details.get("seasons", []):
                number = season.get("seasonNumber")
                name = season.get("name") or f"Saison {number}"
                if isinstance(number, int) and number > 0:
                    seasons.append((number, name))

            if not seasons:
                return await q.answer("Aucune saison trouvÃ©e.", show_alert=True)

            rows = []
            current_row = []

            for number, _name in seasons:
                current_row.append(
                    InlineKeyboardButton(
                        f"Saison {number}",
                        callback_data=f"requestseason:{media_id}:{number}"
                    )
                )
                if len(current_row) == 2:
                    rows.append(current_row)
                    current_row = []

            if current_row:
                rows.append(current_row)

            rows.append([
                InlineKeyboardButton(
                    "ð Toutes les saisons",
                    callback_data=f"requestall:{media_id}"
                )
            ])

            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(rows)
            )
            return

        except Exception:
            log.exception("Erreur rÃ©cupÃ©ration saisons Seerr")
            return await q.answer(
                "Impossible de rÃ©cupÃ©rer les saisons.",
                show_alert=True
            )

    # 2) Demande d'une saison prÃ©cise
    if q.data.startswith("requestseason:"):
        try:
            _, media_id_raw, season_raw = q.data.split(":")
            media_id = int(media_id_raw)
            season = int(season_raw)

            await seerr_request("tv", media_id, [season])

            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"â Saison {season} demandÃ©e",
                        callback_data="noop"
                    )
                ]])
            )
            return

        except Exception:
            log.exception("Erreur demande saison Seerr")
            return await q.answer(
                "Impossible d'envoyer la demande.",
                show_alert=True
            )

    # 3) Demande de toutes les saisons
    if q.data.startswith("requestall:"):
        try:
            media_id = int(q.data.split(":", 1)[1])

            await seerr_request("tv", media_id, "all")

            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "â Toutes les saisons demandÃ©es",
                        callback_data="noop"
                    )
                ]])
            )
            return

        except Exception:
            log.exception("Erreur demande toutes saisons Seerr")
            return await q.answer(
                "Impossible d'envoyer la demande.",
                show_alert=True
            )

    # 4) Film
    if q.data.startswith("request:"):
        parts = q.data.split(":")
        if len(parts) != 3:
            return

        _, media_type, media_id_raw = parts

        try:
            media_id = int(media_id_raw)
            await seerr_request(media_type, media_id)

            await q.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "â Demande envoyÃ©e",
                        callback_data="noop"
                    )
                ]])
            )

        except Exception:
            log.exception("Erreur demande Seerr")
            await q.answer(
                "Impossible d'envoyer la demande.",
                show_alert=True
            )


async def get_imports(url, api_key, page_size=30):
    data = await api_get(url, "/api/v3/history", api_key, {
        "page": 1,
        "pageSize": page_size,
        "sortKey": "date",
        "sortDirection": "descending",
    })
    return [r for r in data.get("records", []) if r.get("eventType") == "downloadFolderImported"]


async def monitor_imports(app: Application):
    await asyncio.sleep(10)
    while True:
        try:
            radarr = await get_imports(RADARR_URL, RADARR_API_KEY, 30) if RADARR_URL and RADARR_API_KEY else []
            sonarr = await get_imports(SONARR_URL, SONARR_API_KEY, 40) if SONARR_URL and SONARR_API_KEY else []
            rid = [str(x.get("id")) for x in radarr if x.get("id") is not None]
            sid = [str(x.get("id")) for x in sonarr if x.get("id") is not None]

            if not state.get("initialized"):
                state["radarr_seen"] = rid
                state["sonarr_seen"] = sid
                state["initialized"] = True
                save_json(STATE_FILE, state)
            else:
                rseen = set(state.get("radarr_seen", []))
                sseen = set(state.get("sonarr_seen", []))

                for r in reversed(radarr):
                    if str(r.get("id")) in rseen:
                        continue
                    movie_id = r.get("movieId")
                    title = f"Film #{movie_id}"
                    try:
                        movie = await api_get(RADARR_URL, f"/api/v3/movie/{movie_id}", RADARR_API_KEY)
                        title = movie.get("title", title)
                    except Exception:
                        pass
                    quality = (((r.get("quality") or {}).get("quality") or {}).get("name") or "QualitÃ© inconnue")
                    await send_to_topic(app, ("Films", "Film"), f"ð¬ <b>Nouveau film disponible !</b>\n\n<b>{title}</b>\nðï¸ {quality}\n\nâ¶ï¸ Disponible prochainement dans Jellyfin.")

                for r in reversed(sonarr):
                    if str(r.get("id")) in sseen:
                        continue
                    series_id = r.get("seriesId")
                    title = f"SÃ©rie #{series_id}"
                    try:
                        show = await api_get(SONARR_URL, f"/api/v3/series/{series_id}", SONARR_API_KEY)
                        title = show.get("title", title)
                    except Exception:
                        pass
                    source = (r.get("data") or {}).get("sourceTitle") or "Nouvel Ã©pisode"
                    quality = (((r.get("quality") or {}).get("quality") or {}).get("name") or "QualitÃ© inconnue")
                    await send_to_topic(app, ("SÃ©rie", "SÃ©ries", "Serie", "Series"), f"ðº <b>Nouvel Ã©pisode disponible !</b>\n\n<b>{title}</b>\n{source}\nðï¸ {quality}\n\nâ¶ï¸ Disponible prochainement dans Jellyfin.")

                state["radarr_seen"] = rid[:100]
                state["sonarr_seen"] = sid[:150]
                save_json(STATE_FILE, state)

        except Exception:
            log.exception("Erreur monitoring")
        await asyncio.sleep(POLL_SECONDS)


async def passive_topic_capture(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await remember_topic(update)


async def post_init(app: Application):
    asyncio.create_task(monitor_imports(app))


def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("setup", setup))
    app.add_handler(CommandHandler("topicid", topicid))
    app.add_handler(CommandHandler("topics", topics_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("film", film))
    app.add_handler(CommandHandler("serie", serie))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, passive_topic_capture))
    log.info("DÃ©marrage Telegram Media Bot v2")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

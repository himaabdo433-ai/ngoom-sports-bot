import os
import sqlite3
import random
import logging
import asyncio
import json
import re
import html
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("ngoom-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "6809336258,6834497894").split(",") if x.strip()}
PAYMENT_NUMBER = os.getenv("PAYMENT_NUMBER", "01024689530")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW else None
WEEKLY_PRICE = os.getenv("WEEKLY_PRICE", "200")
MONTHLY_PRICE = os.getenv("MONTHLY_PRICE", "500")
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")

REACTIONS = ["🔥", "❤️", "👏", "⚽", "😍", "👍"]

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            expires_at TEXT, updated_at TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            plan TEXT, amount TEXT, created_at TEXT
        )""")
        con.execute("CREATE TABLE IF NOT EXISTS daily_gifts (gift_date TEXT PRIMARY KEY, code TEXT NOT NULL)")
        gift_columns = {row["name"] for row in con.execute("PRAGMA table_info(daily_gifts)").fetchall()}
        if "photo_file_id" not in gift_columns:
            con.execute("ALTER TABLE daily_gifts ADD COLUMN photo_file_id TEXT")
        if "caption" not in gift_columns:
            con.execute("ALTER TABLE daily_gifts ADD COLUMN caption TEXT")
        con.execute("CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS bot_visitors (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, start_count INTEGER NOT NULL DEFAULT 0
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS bot_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, event_key TEXT, created_at TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bot_events_created ON bot_events(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bot_events_key ON bot_events(event_key)")
        con.execute("CREATE TABLE IF NOT EXISTS daily_posts (post_date TEXT PRIMARY KEY, message_id INTEGER)")
        con.execute("""CREATE TABLE IF NOT EXISTS football_news_seen (
            link TEXT PRIMARY KEY, title TEXT NOT NULL, seen_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            match_date TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
            competition TEXT, kickoff TEXT, market TEXT NOT NULL, choice TEXT NOT NULL,
            created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result TEXT
        )""")
        con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_unique
            ON predictions(user_id, match_date, home_team, away_team, market)""")

def track_bot_event(user, event_type, event_key=None):
    if not user:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        con.execute("""INSERT INTO bot_visitors(user_id, username, full_name, first_seen, last_seen, start_count)
            VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, full_name=excluded.full_name,
            last_seen=excluded.last_seen,
            start_count=bot_visitors.start_count + excluded.start_count""",
            (user.id, user.username, user.full_name, now, now, 1 if event_type == "start" else 0))
        con.execute("INSERT INTO bot_events(user_id, event_type, event_key, created_at) VALUES(?,?,?,?)",
                    (user.id, event_type, event_key, now))


def get_saved_channel_id():
    if CHANNEL_ID is not None:
        return CHANNEL_ID
    with db() as con:
        row = con.execute("SELECT value FROM bot_settings WHERE key='channel_id'").fetchone()
    return int(row["value"]) if row else None


def save_channel_id(chat_id):
    with db() as con:
        con.execute(
            "INSERT INTO bot_settings(key, value) VALUES('channel_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(chat_id),)
        )


async def build_daily_matches_text():
    today = datetime.now(ZoneInfo("Africa/Cairo"))
    date_key = today.strftime("%Y-%m-%d")
    payload = await fetch_today_fixtures(date_key)
    if isinstance(payload, list):
        matches = payload
    elif isinstance(payload, dict):
        matches = payload.get("matches") or payload.get("fixtures") or payload.get("events")
        if matches is None and isinstance(payload.get("data"), list):
            matches = payload["data"]
        elif matches is None and isinstance(payload.get("data"), dict):
            data = payload["data"]
            matches = data.get("matches") or data.get("fixtures") or data.get("events")
    else:
        matches = None
    if not isinstance(matches, list):
        raise ValueError("Unexpected fixtures response")

    fixtures = []
    cairo_tz = ZoneInfo("Africa/Cairo")
    for match in matches:
        if not isinstance(match, dict):
            continue
        home_value = _pick_match_value(match, "home", "home_team", "homeTeam", "team_home")
        away_value = _pick_match_value(match, "away", "away_team", "awayTeam", "team_away")
        home = arabic_display_name(_team_label(home_value, "الفريق الأول"))
        away = arabic_display_name(_team_label(away_value, "الفريق الثاني"))
        if home == "الفريق الأول" or away == "الفريق الثاني":
            for team in (match.get("competitors") or []):
                if not isinstance(team, dict):
                    continue
                side = team.get("homeAway") or team.get("side")
                if side == "home":
                    home = arabic_display_name(_team_label(team.get("team") or team, home))
                elif side == "away":
                    away = arabic_display_name(_team_label(team.get("team") or team, away))
        raw_time = _pick_match_value(match, "time", "date", "utcDate", "kickoff", "start_time", "startTime")
        if isinstance(raw_time, dict):
            raw_time = raw_time.get("date") or raw_time.get("utc") or raw_time.get("timestamp")
        if not raw_time:
            continue
        try:
            kickoff = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            kickoff = kickoff.astimezone(cairo_tz)
        except (TypeError, ValueError):
            continue
        if kickoff.date() != today.date():
            continue
        competition = _pick_match_value(match, "competition", "league", "tournament")
        if isinstance(competition, dict):
            competition = competition.get("name") or competition.get("title")
        status = str(_pick_match_value(match, "status", "state") or "").lower()
        hs = _pick_match_value(match, "home_score", "homeScore")
        aws = _pick_match_value(match, "away_score", "awayScore")
        state = "لم تبدأ"
        if status in ("live", "in_progress", "in", "playing"):
            state = f"مباشر | {hs if hs is not None else 0} - {aws if aws is not None else 0}"
        elif status in ("finished", "complete", "completed", "ft", "post"):
            state = f"انتهت | {hs if hs is not None else '-'} - {aws if aws is not None else '-'}"
        fixtures.append((kickoff, str(competition or "مباريات كرة القدم"), home, away, state))

    fixtures.sort(key=lambda item: item[0])
    lines = ["⚽ أبرز مباريات اليوم", f"📆 {today.strftime('%d/%m/%Y')}", ""]
    if not fixtures:
        lines.append("مفيش مباريات مسجلة اليوم في مصدر البيانات.")
    else:
        for kickoff, competition, home, away, state in fixtures[:25]:
            lines.extend([f"🏆 {competition}", f"⚽ {home} × {away}",
                          f"🕒 {kickoff.strftime('%I:%M %p')} بتوقيت مصر",
                          f"📌 {state}", ""])
    lines.extend(["تابعوا نجوم الرياضة ⭐", "مصدر البيانات: SportScore"])
    return "\n".join(lines)


async def daily_channel_publisher(app):
    cairo = ZoneInfo("Africa/Cairo")
    while True:
        now = datetime.now(cairo)
        next_run = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep(max(1, (next_run - now).total_seconds()))
        channel_id = get_saved_channel_id()
        if not channel_id:
            log.warning("Daily publisher: channel ID not detected yet; waiting for a channel post.")
            continue
        post_date = datetime.now(cairo).date().isoformat()
        with db() as con:
            already_posted = con.execute("SELECT 1 FROM daily_posts WHERE post_date=?", (post_date,)).fetchone()
        if already_posted:
            continue
        try:
            message = await build_daily_matches_text()
            sent = await app.bot.send_message(chat_id=channel_id, text=message)
            with db() as con:
                con.execute("INSERT OR REPLACE INTO daily_posts(post_date, message_id) VALUES(?, ?)",
                            (post_date, sent.message_id))
            log.info("Daily fixtures posted to channel %s for %s", channel_id, post_date)
        except Exception:
            log.exception("Daily fixtures post failed for channel %s", channel_id)



# ELITE PRO promotional posts replace the former football-news feed.
ELITE_PROMO_TEXT = """👑 عايز تدخل عالم التوقعات الاحترافية؟

🔥 انضم إلى قناة النخبة من نجوم الرياضة.

⚽ توقعات مميزة
📊 تحليلات ومتابعة للمباريات
🎯 ترشيحات فردية وتراكمية وLIVE وقسائم محميه في حاله سقوط احداث

💎 لو عايز مستوى مختلف، مكانك معانا في النخبة.

📩 اشترك الآن ولا تفوّت الفرصة.
للاشتراك او لتجديد الاشتراك تواصل @ngoomredaabot

نجوم الرياضة | ELITE PRO"""

async def publish_elite_promo(app):
    channel_id = get_saved_channel_id()
    if not channel_id:
        log.warning("ELITE PRO promo: target channel is not set")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📩 اشترك أو جدّد اشتراكك", url="https://t.me/ngoomredaabot")
    ]])
    await app.bot.send_message(
        chat_id=channel_id,
        text=ELITE_PROMO_TEXT,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )
    log.info("ELITE PRO promotional post sent to channel %s", channel_id)

async def elite_promo_publisher(app):
    while True:
        try:
            if get_saved_channel_id():
                await publish_elite_promo(app)
        except Exception:
            log.exception("ELITE PRO scheduled promotion failed")
        await asyncio.sleep(4 * 60 * 60)

def home_keyboard(user_id=None):
    rows = [
        [InlineKeyboardButton("اشتراك أسبوعي", callback_data="plan:weekly"),
         InlineKeyboardButton("اشتراك شهري", callback_data="plan:monthly")],
        [InlineKeyboardButton("حالة اشتراكي", callback_data="status")],
        [InlineKeyboardButton("📅 أبرز مباريات اليوم", callback_data="matches_today")],
        [InlineKeyboardButton("🤖 اسأل عن مباراة", callback_data="ask_match")],
        [InlineKeyboardButton("🎯 سجل توقعاتك", callback_data="predictions")],
        [InlineKeyboardButton("💰 إدارة رأس المال", callback_data="capital_manager")],
        [InlineKeyboardButton("🎁 الهدية اليومية", callback_data="daily_gift")],
        [InlineKeyboardButton("📩 تواصل مع الإدارة", url="https://t.me/ahmed_saber90")]
    ]
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton("📊 إحصائيات البوت", callback_data="bot_stats")])
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_bot_event(update.effective_user, "start")
    msg = (
        "أهلاً بيك في جروب نجوم الرياضة النخبة ⭐\n"
        "أفضل وأقوى التوقعات الرياضية.\n\n"
        "تحب تشترك أسبوعي ولا شهري؟"
    )
    await update.effective_message.reply_text(msg, reply_markup=home_keyboard(update.effective_user.id))


async def fetch_today_fixtures(date_key):
    url = f"https://sportscore.com/api/v1/fixtures/?sport=football&date={date_key}&limit=200"
    request = Request(url, headers={
        "User-Agent": "NogoomSportsBot/1.0",
        "Accept": "application/json"
    })

    def load():
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(load)


def _pick_match_value(item, *keys):
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def _team_label(value, fallback):
    if isinstance(value, dict):
        return (value.get("name") or value.get("displayName")
                or value.get("shortName") or value.get("team_name") or fallback)
    if isinstance(value, str):
        return value
    return fallback

# Arabic display names for commonly covered clubs and competitions.
ARABIC_NAMES = {
    "real madrid": "ريال مدريد", "barcelona": "برشلونة", "atletico madrid": "أتلتيكو مدريد",
    "manchester city": "مانشستر سيتي", "manchester united": "مانشستر يونايتد",
    "liverpool": "ليفربول", "arsenal": "أرسنال", "chelsea": "تشيلسي",
    "tottenham": "توتنهام", "tottenham hotspur": "توتنهام", "newcastle united": "نيوكاسل يونايتد",
    "bayern munich": "بايرن ميونخ", "borussia dortmund": "بوروسيا دورتموند",
    "bayer leverkusen": "باير ليفركوزن", "rb leipzig": "لايبزيج", "paris saint germain": "باريس سان جيرمان",
    "paris saint-germain": "باريس سان جيرمان", "psg": "باريس سان جيرمان", "marseille": "مارسيليا",
    "inter": "إنتر ميلان", "inter milan": "إنتر ميلان", "ac milan": "إيه سي ميلان",
    "juventus": "يوفنتوس", "napoli": "نابولي", "roma": "روما", "lazio": "لاتسيو",
    "ajax": "أياكس", "benfica": "بنفيكا", "porto": "بورتو", "sporting cp": "سبورتنج لشبونة",
    "al ahly": "الأهلي", "al-ahly": "الأهلي", "zamalek": "الزمالك", "pyramids": "بيراميدز",
    "ismaily": "الإسماعيلي", "ceramica cleopatra": "سيراميكا كليوباترا",
    "al hilal": "الهلال", "al-hilal": "الهلال", "al nassr": "النصر", "al-nassr": "النصر",
    "al ittihad": "الاتحاد", "al-ittihad": "الاتحاد", "al ahli": "الأهلي السعودي",
    "aston villa": "أستون فيلا", "west ham": "وست هام", "brighton": "برايتون",
    "everton": "إيفرتون", "fulham": "فولهام", "crystal palace": "كريستال بالاس",
    "nottingham forest": "نوتنجهام فورست", "wolves": "وولفرهامبتون", "wolverhampton wanderers": "وولفرهامبتون",
    "premier league": "الدوري الإنجليزي الممتاز", "english premier league": "الدوري الإنجليزي الممتاز",
    "la liga": "الدوري الإسباني", "laliga": "الدوري الإسباني", "bundesliga": "الدوري الألماني",
    "serie a": "الدوري الإيطالي", "ligue 1": "الدوري الفرنسي", "uefa champions league": "دوري أبطال أوروبا",
    "champions league": "دوري أبطال أوروبا", "uefa europa league": "الدوري الأوروبي",
    "europa league": "الدوري الأوروبي", "egyptian premier league": "الدوري المصري الممتاز",
    "egypt premier league": "الدوري المصري الممتاز", "saudi pro league": "دوري روشن السعودي",
    "afc champions league": "دوري أبطال آسيا", "fa cup": "كأس الاتحاد الإنجليزي",
    "carabao cup": "كأس الرابطة الإنجليزية", "copa del rey": "كأس ملك إسبانيا",
}

def arabic_display_name(value):
    if not value:
        return value
    clean = " ".join(str(value).strip().split())
    return ARABIC_NAMES.get(clean.casefold(), clean)


async def matches_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("جاري تحميل مباريات اليوم...")
    today = datetime.now(ZoneInfo("Africa/Cairo"))
    date_key = today.strftime("%Y-%m-%d")

    try:
        payload = await fetch_today_fixtures(date_key)
    except Exception:
        log.exception("Could not fetch today's fixtures from SportScore")
        await q.message.reply_text("تعذر تحميل المباريات حاليًا. جرّب تاني بعد شوية.")
        return

    if isinstance(payload, list):
        matches = payload
    elif isinstance(payload, dict):
        matches = payload.get("matches") or payload.get("fixtures") or payload.get("events")
        if matches is None and isinstance(payload.get("data"), list):
            matches = payload["data"]
        elif matches is None and isinstance(payload.get("data"), dict):
            data = payload["data"]
            matches = data.get("matches") or data.get("fixtures") or data.get("events")
    else:
        matches = None

    if not isinstance(matches, list):
        log.error("Unexpected SportScore response format: %s", type(payload).__name__)
        await q.message.reply_text("مصدر المباريات رجّع بيانات غير متوقعة. هنعالج المشكلة.")
        return

    fixtures = []
    cairo_tz = ZoneInfo("Africa/Cairo")
    for match in matches:
        if not isinstance(match, dict):
            continue
        home_value = _pick_match_value(match, "home", "home_team", "homeTeam", "team_home")
        away_value = _pick_match_value(match, "away", "away_team", "awayTeam", "team_away")
        home = arabic_display_name(_team_label(home_value, "الفريق الأول"))
        away = arabic_display_name(_team_label(away_value, "الفريق الثاني"))
        if home == "الفريق الأول" or away == "الفريق الثاني":
            competitors = match.get("competitors") or []
            for team in competitors:
                if not isinstance(team, dict):
                    continue
                side = team.get("homeAway") or team.get("side")
                if side == "home":
                    home = arabic_display_name(_team_label(team.get("team") or team, home))
                elif side == "away":
                    away = arabic_display_name(_team_label(team.get("team") or team, away))

        raw_time = _pick_match_value(match, "time", "date", "utcDate", "kickoff", "start_time", "startTime")
        if isinstance(raw_time, dict):
            raw_time = raw_time.get("date") or raw_time.get("utc") or raw_time.get("timestamp")
        if not raw_time:
            continue
        try:
            kickoff = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            kickoff = kickoff.astimezone(cairo_tz)
        except (TypeError, ValueError):
            continue
        if kickoff.date() != today.date():
            continue

        competition = _pick_match_value(match, "competition", "league", "tournament")
        if isinstance(competition, dict):
            competition = competition.get("name") or competition.get("title")
        competition = arabic_display_name(competition or "مباريات كرة القدم")

        status = str(_pick_match_value(match, "status", "state") or "").lower()
        home_score = _pick_match_value(match, "home_score", "homeScore")
        away_score = _pick_match_value(match, "away_score", "awayScore")
        status_text = "لم تبدأ"
        if status in ("live", "in_progress", "in", "playing"):
            score = f"{home_score if home_score is not None else 0} - {away_score if away_score is not None else 0}"
            status_text = f"مباشر | {score}"
        elif status in ("finished", "complete", "completed", "ft", "post"):
            score = f"{home_score if home_score is not None else '-'} - {away_score if away_score is not None else '-'}"
            status_text = f"انتهت | {score}"
        elif home_score is not None or away_score is not None:
            status_text = f"{home_score or 0} - {away_score or 0}"

        fixtures.append((kickoff, str(competition), home, away, status_text))

    if not fixtures:
        await q.message.reply_text("⚽ مفيش مباريات مسجلة اليوم في مصدر البيانات.")
        return

    fixtures.sort(key=lambda item: item[0])
    lines = ["⚽ أبرز مباريات اليوم", f"📆 {today.strftime('%d/%m/%Y')}", ""]
    for kickoff, competition, home, away, status_text in fixtures[:25]:
        lines.extend([
            f"🏆 {competition}",
            f"⚽ {home} × {away}",
            f"🕒 {kickoff.strftime('%I:%M %p')} بتوقيت مصر",
            f"📌 {status_text}",
            ""
        ])
    lines.extend(["مصدر البيانات: SportScore", "https://sportscore.com/"])
    await q.message.reply_text("\n".join(lines))

def _extract_fixtures(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        matches = payload.get("matches") or payload.get("fixtures") or payload.get("events")
        if matches is None and isinstance(payload.get("data"), list):
            matches = payload["data"]
        elif matches is None and isinstance(payload.get("data"), dict):
            data = payload["data"]
            matches = data.get("matches") or data.get("fixtures") or data.get("events")
        return matches if isinstance(matches, list) else []
    return []


def _fixture_teams(item):
    home = _team_label(_pick_match_value(item, "home", "home_team", "homeTeam", "team_home"), "")
    away = _team_label(_pick_match_value(item, "away", "away_team", "awayTeam", "team_away"), "")
    competitors = item.get("competitors") or []
    if (not home or not away) and isinstance(competitors, list):
        for team in competitors:
            if not isinstance(team, dict):
                continue
            side = team.get("homeAway") or team.get("side")
            label = _team_label(team.get("team") or team, "")
            if side == "home":
                home = label
            elif side == "away":
                away = label
        if (not home or not away) and len(competitors) >= 2:
            home = home or _team_label(competitors[0].get("team") or competitors[0], "")
            away = away or _team_label(competitors[1].get("team") or competitors[1], "")
    return home, away


def _finished_score(item):
    status = str(_pick_match_value(item, "status", "state") or "").casefold()
    finished = status in ("finished", "complete", "completed", "ft", "post", "ended")
    hs = _pick_match_value(item, "home_score", "homeScore")
    aws = _pick_match_value(item, "away_score", "awayScore")
    if hs is None or aws is None:
        score = item.get("score")
        if isinstance(score, dict):
            hs = hs if hs is not None else _pick_match_value(score, "home", "home_score")
            aws = aws if aws is not None else _pick_match_value(score, "away", "away_score")
    try:
        hs, aws = int(hs), int(aws)
    except (TypeError, ValueError):
        return None
    if not finished:
        return None
    return hs, aws


async def _recent_team_results(team_names, days=14):
    today = datetime.now(ZoneInfo("Africa/Cairo")).date()
    date_keys = [(today - timedelta(days=offset)).isoformat() for offset in range(1, days + 1)]
    payloads = await asyncio.gather(
        *(fetch_today_fixtures(day) for day in date_keys),
        return_exceptions=True
    )
    wanted = {name.casefold() for name in team_names if name}
    results = {name: [] for name in wanted}
    seen = set()
    for date_key, payload in zip(date_keys, payloads):
        if isinstance(payload, Exception):
            continue
        for item in await _extract_fixtures(payload):
            if not isinstance(item, dict):
                continue
            home, away = _fixture_teams(item)
            score = _finished_score(item)
            if not home or not away or score is None:
                continue
            home_key, away_key = home.casefold(), away.casefold()
            match_key = (date_key, home_key, away_key, score)
            if match_key in seen:
                continue
            seen.add(match_key)
            hs, aws = score
            if home_key in wanted:
                results[home_key].append({"date": date_key, "gf": hs, "ga": aws, "win": hs > aws,
                                          "draw": hs == aws, "btts": hs > 0 and aws > 0})
            if away_key in wanted:
                results[away_key].append({"date": date_key, "gf": aws, "ga": hs, "win": aws > hs,
                                          "draw": hs == aws, "btts": hs > 0 and aws > 0})
    for rows in results.values():
        rows.sort(key=lambda row: row["date"], reverse=True)
        del rows[5:]
    return results


def _form_summary(rows):
    if not rows:
        return None
    count = len(rows)
    avg_for = sum(x["gf"] for x in rows) / count
    avg_against = sum(x["ga"] for x in rows) / count
    wins = sum(x["win"] for x in rows)
    btts = sum(x["btts"] for x in rows)
    return {
        "count": count, "avg_for": avg_for, "avg_against": avg_against,
        "wins": wins, "btts": btts,
        "over15": sum(x["gf"] + x["ga"] >= 2 for x in rows),
        "clean_sheets": sum(x["ga"] == 0 for x in rows)
    }


def _form_text(name, summary):
    if not summary:
        return f"• {name}: لم تتوفر نتائج حديثة كافية في المصدر."
    return (
        f"• {name} — آخر {summary['count']} مباريات: "
        f"{summary['wins']} فوز، متوسط تسجيل {summary['avg_for']:.1f} "
        f"واستقبال {summary['avg_against']:.1f} هدف/مباراة، "
        f"وسجل في {summary['btts']}/{summary['count']} مباراة."
    )


async def begin_match_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_match_analysis"] = True
    await update.effective_message.reply_text(
        "🤖 مساعد تحليل نجوم الرياضة المطوّر\n"
        "اكتب اسم الفريقين وسؤالك، مثال:\n"
        "الأهلي والزمالك - مين الأقرب للفوز؟\n"
        "ليفربول وأرسنال - هل الفريقان يسجلان؟\n"
        "اكتب (إلغاء) للرجوع."
    )


async def begin_capital_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_capital_amount"] = True
    context.user_data.pop("awaiting_match_analysis", None)
    await update.effective_message.reply_text(
        "💰 إدارة رأس المال الذكية\n"
        "اكتب إجمالي الميزانية التي خصصتها للترفيه فقط بالجنيه، مثال: 5000\n"
        "بعدها اختار طريقة التوزيع المناسبة. المبلغ المدخل مش معناه إنك لازم تستخدمه كله.\n"
        "اكتب (إلغاء) للرجوع."
    )


def _parse_capital_amount(raw):
    arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    value = (raw or "").translate(arabic_digits).replace(",", "").replace("٬", "").strip()
    if not value.isdigit():
        return None
    amount = int(value)
    if amount < 100 or amount > 10000000:
        return None
    return amount


async def handle_capital_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.effective_message.text or "").strip()
    context.user_data.pop("awaiting_capital_amount", None)
    if raw.casefold() in ("إلغاء", "الغاء", "cancel", "خروج"):
        await update.effective_message.reply_text("تم إلغاء إدارة رأس المال.")
        return
    amount = _parse_capital_amount(raw)
    if amount is None:
        context.user_data["awaiting_capital_amount"] = True
        await update.effective_message.reply_text(
            "اكتب مبلغًا صحيحًا بالجنيه من 100 إلى 10,000,000، مثال: 5000. أو اكتب (إلغاء)."
        )
        return

    context.user_data["capital_amount"] = amount
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 محافظ - 1%", callback_data="capital_mode:conservative")],
        [InlineKeyboardButton("🟡 متوازن - 2%", callback_data="capital_mode:balanced")],
        [InlineKeyboardButton("🔵 توزيع مستقل بدون تجميع - 1.5%", callback_data="capital_mode:singles")],
    ])
    await update.effective_message.reply_text(
        f"💵 الميزانية المسجلة: {amount:,} جنيه\n\n"
        "اختار أسلوب تنظيم الميزانية اليومية. النسب دي حدود قصوى مقترحة وليست هدفًا لازم تصرفه:",
        reply_markup=keyboard
    )


async def send_capital_plan(query, context, mode):
    amount = context.user_data.get("capital_amount")
    if not isinstance(amount, int) or amount < 100:
        await query.message.reply_text("ابدأ من جديد واضغط زر إدارة رأس المال.")
        return
    presets = {
        "conservative": ("محافظ", 0.01, 2),
        "balanced": ("متوازن", 0.02, 3),
        "singles": ("رهانات منفصلة بدون تجميع", 0.015, 3),
    }
    label, rate, match_count = presets[mode]
    daily_cap = max(1, int(amount * rate))
    match_count = min(match_count, daily_cap)
    base, remainder = divmod(daily_cap, match_count)
    stakes = [base + (1 if i < remainder else 0) for i in range(match_count)]
    reserve = amount - daily_cap
    lines = [
        "💰 خطة تنظيم الميزانية",
        f"💵 الميزانية: {amount:,} جنيه",
        f"⚙️ النظام: {label}",
        f"🎯 الحد الأقصى لميزانية اليوم: {daily_cap:,} جنيه ({rate * 100:g}%)",
        "",
        "تقسيم افتراضي متساوٍ، وكل اختيار مستقل عن الآخر:",
    ]
    for i, stake in enumerate(stakes, 1):
        lines.append(f"• الاختيار {i}: {stake:,} جنيه")
    lines.extend([
        "",
        f"🏦 المتبقي خارج ميزانية اليوم: {reserve:,} جنيه",
        "📌 مش لازم تستخدم الحد كله؛ لو مفيش اختيار مناسب، خليه احتياطي.",
        "⛔ لا تجمع اختيارات في كود واحد، ولا تزود المبلغ لتعويض خسارة.",
        "⛔ استخدم فقط مبلغًا تقدر تتحمل خسارته بالكامل، وليس فلوس المعيشة أو الديون.",
        "",
        "دي حاسبة لتنظيم حد الخسارة وليست توصية بالمراهنة أو ضمانًا للمكسب. لا تعتمد على التوقعات كمصدر دخل."
    ])
    context.user_data.pop("capital_amount", None)
    await query.message.reply_text("\n".join(lines))


async def handle_match_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data and context.user_data.get("awaiting_capital_amount"):
        await handle_capital_plan(update, context)
        return
    if not context.user_data or not context.user_data.get("awaiting_match_analysis"):
        return
    question = (update.effective_message.text or "").strip()
    if question.lower() in ("إلغاء", "الغاء", "cancel", "خروج"):
        context.user_data.pop("awaiting_match_analysis", None)
        await update.effective_message.reply_text("تم إلغاء التحليل.")
        return
    context.user_data.pop("awaiting_match_analysis", None)
    q = question.casefold()
    await update.effective_message.reply_text("⏳ بجمع نتائج آخر أسبوعين للمباراة...")
    today = datetime.now(ZoneInfo("Africa/Cairo"))
    try:
        todays_matches = await _extract_fixtures(
            await fetch_today_fixtures(today.strftime("%Y-%m-%d"))
        )
    except Exception:
        log.exception("Could not load today's fixtures for match analysis")
        todays_matches = []

    found = None
    for item in todays_matches:
        if not isinstance(item, dict):
            continue
        home, away = _fixture_teams(item)
        home_ar, away_ar = arabic_display_name(home), arabic_display_name(away)
        names = (home, away, home_ar, away_ar)
        if home and away and all(name.casefold() in q for name in (home_ar, away_ar)):
            found = (item, home, away)
            break
        if home and away and (home.casefold() in q and away.casefold() in q):
            found = (item, home, away)
            break

    if not found:
        await update.effective_message.reply_text(
            "مش لاقي المباراة ضمن جدول النهارده في مصدرنا. "
            "اكتب اسم الفريقين زي ما ظاهرين في جدول مباريات اليوم."
        )
        return

    item, home, away = found
    try:
        recent = await _recent_team_results((home, away), days=14)
    except Exception:
        log.exception("Recent results lookup failed")
        recent = {home.casefold(): [], away.casefold(): []}
    home_form = _form_summary(recent.get(home.casefold(), []))
    away_form = _form_summary(recent.get(away.casefold(), []))
    competition = _pick_match_value(item, "competition", "league", "tournament")
    if isinstance(competition, dict):
        competition = competition.get("name") or competition.get("title")
    market_corners = any(word in q for word in ("ركنيات", "ركنية", "corners"))
    market_btts = any(word in q for word in ("يسجل", "التسجيل", "هدف", "أهداف", "btts", "الفريقان"))
    market_win = any(word in q for word in ("يفوز", "الفائز", "الفوز", "انتصار", "الأقرب"))
    lines = [
        f"⚽ تحليل مباراة: {arabic_display_name(home)} × {arabic_display_name(away)}",
        f"🏆 البطولة: {arabic_display_name(competition or 'غير محددة')}",
        "📊 قراءة آخر النتائج المتاحة (حتى 5 مباريات لكل فريق):",
        _form_text(arabic_display_name(home), home_form),
        _form_text(arabic_display_name(away), away_form),
        ""
    ]
    if market_corners:
        lines.extend([
            "🚩 الركنيات: لا تتوفر إحصائيات ركنيات موثوقة في مصدرنا الحالي، لذلك مش هقدر أرشح عدد أو خط ركنيات."
        ])
    elif market_btts:
        if home_form and away_form:
            rate = (home_form["btts"] + away_form["btts"]) / (home_form["count"] + away_form["count"])
            lines.append(
                f"🎯 مؤشر تسجيل الفريقين تاريخيًا في العينة: {rate*100:.0f}% "
                f"({home_form['btts'] + away_form['btts']}/{home_form['count'] + away_form['count']} مشاركات فريق)."
            )
            lines.append("ده مؤشر وصفي من نتائج سابقة، مش احتمال مباشر للمباراة.")
        else:
            lines.append("🎯 لا توجد عينة نتائج كافية لتقييم تسجيل الفريقين.")
    elif market_win:
        if home_form and away_form:
            home_points = (home_form["wins"] * 3 + sum(x["draw"] for x in recent.get(home.casefold(), []))) / home_form["count"]
            away_points = (away_form["wins"] * 3 + sum(x["draw"] for x in recent.get(away.casefold(), []))) / away_form["count"]
            if home_points > away_points + 0.35:
                lean = f"المؤشرات التاريخية تميل نسبيًا إلى {arabic_display_name(home)}."
            elif away_points > home_points + 0.35:
                lean = f"المؤشرات التاريخية تميل نسبيًا إلى {arabic_display_name(away)}."
            else:
                lean = "المؤشرات متقاربة، ولا يوجد تفوق واضح من العينة."
            lines.append(f"🏅 مؤشر النقاط لكل مباراة: {home_points:.2f} مقابل {away_points:.2f}.")
            lines.append(lean)
        else:
            lines.append("🏅 لا توجد نتائج كافية للمقارنة.")
    else:
        lines.append("💡 اسأل عن الفوز أو تسجيل الفريقين أو الركنيات للحصول على قراءة مخصصة.")
    lines.extend([
        "",
        "⚠️ التحليل مبني على نتائج مسجلة فقط، ولا يشمل الغيابات أو التشكيل أو إحصائيات الركنيات. "
        "العينة قصيرة ولا تضمن نتيجة أو مكسب."
    ])
    await update.effective_message.reply_text("\n".join(lines))




async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db() as con:
        row = con.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row and row["expires_at"]:
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            if exp > datetime.now(timezone.utc):
                await update.effective_message.reply_text(
                    "اشتراكك نشط حتى: " + exp.strftime("%Y-%m-%d %H:%M UTC")
                )
                return
        except ValueError:
            pass
    await update.effective_message.reply_text("مفيش اشتراك نشط على حسابك حاليًا.", reply_markup=home_keyboard())

async def begin_predictions(query, context):
    today = datetime.now(ZoneInfo("Africa/Cairo"))
    date_key = today.date().isoformat()
    try:
        matches = _extract_fixtures(await fetch_today_fixtures(date_key))
    except Exception:
        log.exception("Could not load fixtures for predictions")
        await query.message.reply_text("تعذر تحميل مباريات اليوم حاليًا. جرّب بعد شوية.")
        return
    options = []
    for item in matches:
        if not isinstance(item, dict):
            continue
        home, away = _fixture_teams(item)
        if not home or not away:
            continue
        raw_time = _pick_match_value(item, "time", "date", "utcDate", "kickoff", "start_time", "startTime")
        if isinstance(raw_time, dict):
            raw_time = raw_time.get("date") or raw_time.get("utc") or raw_time.get("timestamp")
        if not raw_time:
            continue
        try:
            kickoff = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
            kickoff = kickoff.astimezone(ZoneInfo("Africa/Cairo"))
        except (TypeError, ValueError):
            continue
        status = str(_pick_match_value(item, "status", "state") or "").casefold()
        if kickoff <= today or status in ("live", "in_progress", "in", "playing", "finished", "complete", "completed", "ft", "post", "ended"):
            continue
        competition = _pick_match_value(item, "competition", "league", "tournament")
        if isinstance(competition, dict):
            competition = competition.get("name") or competition.get("title")
        options.append({"date": date_key, "home": home, "away": away,
                        "competition": str(competition or "كرة القدم"), "kickoff": kickoff.isoformat()})
    options.sort(key=lambda x: x["kickoff"])
    options = options[:10]
    if not options:
        await query.message.reply_text("مفيش مباريات قادمة متاحة للتوقع اليوم. جرّب وقت تاني.")
        return
    context.user_data["prediction_matches"] = options
    rows = [[InlineKeyboardButton(
        f'{arabic_display_name(item["home"])} × {arabic_display_name(item["away"])}'[:60],
        callback_data=f"pred_match:{idx}"
    )] for idx, item in enumerate(options)]
    rows.append([InlineKeyboardButton("📒 عرض سجل توقعاتي", callback_data="pred_history")])
    await query.message.reply_text("🎯 اختار مباراة لسه ما بدأتش:", reply_markup=InlineKeyboardMarkup(rows))


async def show_prediction_markets(query, context, index):
    matches = context.user_data.get("prediction_matches") or []
    if index < 0 or index >= len(matches):
        await query.message.reply_text("قائمة المباريات انتهت. افتح سجل التوقعات من جديد.")
        return
    context.user_data["current_prediction_match"] = matches[index]
    match = matches[index]
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 نتيجة المباراة", callback_data="pred_market:result")],
        [InlineKeyboardButton("⚽ الفريقان يسجلان", callback_data="pred_market:btts")],
        [InlineKeyboardButton("🥅 أكثر من 2.5 هدف", callback_data="pred_market:over25")],
    ])
    await query.message.reply_text(
        f'المباراة: {arabic_display_name(match["home"])} × {arabic_display_name(match["away"])}\nاختار نوع التوقع:',
        reply_markup=keyboard)


async def show_prediction_choices(query, context, market):
    match = context.user_data.get("current_prediction_match")
    if not match or market not in ("result", "btts", "over25"):
        await query.message.reply_text("اختار المباراة من جديد.")
        return
    context.user_data["current_prediction_market"] = market
    home, away = arabic_display_name(match["home"]), arabic_display_name(match["away"])
    if market == "result":
        buttons = [[InlineKeyboardButton(f"فوز {home}", callback_data="pred_pick:1")],
                   [InlineKeyboardButton("تعادل", callback_data="pred_pick:X")],
                   [InlineKeyboardButton(f"فوز {away}", callback_data="pred_pick:2")]]
    else:
        buttons = [[InlineKeyboardButton("نعم", callback_data="pred_pick:yes"),
                    InlineKeyboardButton("لا", callback_data="pred_pick:no")]]
    await query.message.reply_text("اختار توقعك:", reply_markup=InlineKeyboardMarkup(buttons))


async def save_prediction(query, context, choice):
    match = context.user_data.get("current_prediction_match")
    market = context.user_data.get("current_prediction_market")
    if not match or market not in ("result", "btts", "over25"):
        await query.message.reply_text("ابدأ من جديد واختار المباراة ونوع التوقع.")
        return
    with db() as con:
        con.execute("""INSERT OR IGNORE INTO predictions
            (user_id, match_date, home_team, away_team, competition, kickoff, market, choice, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (query.from_user.id, match["date"], match["home"], match["away"],
             match["competition"], match["kickoff"], market, choice, datetime.now(timezone.utc).isoformat()))
        row = con.execute("""SELECT id FROM predictions
            WHERE user_id=? AND match_date=? AND home_team=? AND away_team=? AND market=?""",
            (query.from_user.id, match["date"], match["home"], match["away"], market)).fetchone()
    context.user_data.pop("current_prediction_match", None)
    context.user_data.pop("current_prediction_market", None)
    await query.message.reply_text("✅ توقعك اتسجل! هنراجع النتيجة بعد نهاية المباراة." if row else "تعذر حفظ التوقع. جرّب تاني.")


def _grade_prediction(market, choice, score):
    hs, aws = score
    if market == "result":
        actual = "1" if hs > aws else ("2" if hs < aws else "X")
        return "correct" if choice == actual else "wrong"
    if market == "btts":
        actual = "yes" if hs > 0 and aws > 0 else "no"
    elif market == "over25":
        actual = "yes" if hs + aws >= 3 else "no"
    else:
        return "pending"
    return "correct" if choice == actual else "wrong"


async def update_pending_predictions():
    with db() as con:
        dates = [row["match_date"] for row in con.execute(
            "SELECT DISTINCT match_date FROM predictions WHERE status='pending'").fetchall()]
    if not dates:
        return
    payloads = await asyncio.gather(*(fetch_today_fixtures(day) for day in dates), return_exceptions=True)
    with db() as con:
        for day, payload in zip(dates, payloads):
            if isinstance(payload, Exception):
                log.warning("Prediction updater could not load fixtures for %s", day)
                continue
            fixtures = _extract_fixtures(payload)
            pending = con.execute("SELECT * FROM predictions WHERE match_date=? AND status='pending'",
                                  (day,)).fetchall()
            for row in pending:
                for fixture in fixtures:
                    if not isinstance(fixture, dict):
                        continue
                    home, away = _fixture_teams(fixture)
                    if home.casefold() != row["home_team"].casefold() or away.casefold() != row["away_team"].casefold():
                        continue
                    score = _finished_score(fixture)
                    if score is not None:
                        outcome = _grade_prediction(row["market"], row["choice"], score)
                        con.execute("UPDATE predictions SET status=?, result=? WHERE id=?",
                                    (outcome, f"{score[0]}-{score[1]}", row["id"]))
                    break


async def prediction_result_updater(app):
    while True:
        try:
            await update_pending_predictions()
        except Exception:
            log.exception("Automatic prediction result update failed")
        await asyncio.sleep(600)


async def show_prediction_history(query, context):
    user_id = query.from_user.id
    with db() as con:
        rows = con.execute("SELECT * FROM predictions WHERE user_id=? ORDER BY id DESC LIMIT 30",
                           (user_id,)).fetchall()
    if not rows:
        await query.message.reply_text("لسه معندكش توقعات مسجلة. اضغط 🎯 سجل توقعاتك وابدأ.")
        return
    dates = sorted({row["match_date"] for row in rows if row["status"] == "pending"})
    payloads = await asyncio.gather(*(fetch_today_fixtures(day) for day in dates), return_exceptions=True)
    by_date = {}
    for day, payload in zip(dates, payloads):
        if not isinstance(payload, Exception):
            by_date[day] = _extract_fixtures(payload)
    with db() as con:
        for row in rows:
            if row["status"] != "pending":
                continue
            for fixture in by_date.get(row["match_date"], []):
                if not isinstance(fixture, dict):
                    continue
                home, away = _fixture_teams(fixture)
                if home.casefold() != row["home_team"].casefold() or away.casefold() != row["away_team"].casefold():
                    continue
                score = _finished_score(fixture)
                if score is not None:
                    outcome = _grade_prediction(row["market"], row["choice"], score)
                    con.execute("UPDATE predictions SET status=?, result=? WHERE id=?",
                                (outcome, f"{score[0]}-{score[1]}", row["id"]))
                break
        rows = con.execute("SELECT * FROM predictions WHERE user_id=? ORDER BY id DESC LIMIT 30",
                           (user_id,)).fetchall()
    correct = sum(row["status"] == "correct" for row in rows)
    wrong = sum(row["status"] == "wrong" for row in rows)
    settled = correct + wrong
    rate = round(correct * 100 / settled) if settled else 0
    lines = ["📒 سجل توقعاتك", f"✅ صحيحة: {correct} | ❌ خاطئة: {wrong}",
             f"📊 نسبة النجاح: {rate}% ({settled} توقع محسوم)", ""]
    labels = {"result": "نتيجة المباراة", "btts": "الفريقان يسجلان", "over25": "أكثر من 2.5 هدف"}
    for row in rows[:12]:
        home, away = arabic_display_name(row["home_team"]), arabic_display_name(row["away_team"])
        choice = row["choice"]
        if row["market"] == "result":
            choice = {"1": f"فوز {home}", "X": "تعادل", "2": f"فوز {away}"}.get(choice, choice)
        else:
            choice = "نعم" if choice == "yes" else "لا"
        status = {"pending": "⏳ لم تُحسم", "correct": "✅ صح", "wrong": "❌ غلط"}.get(row["status"], row["status"])
        score = f' | النتيجة {row["result"]}' if row["result"] else ""
        lines.append(f'• {home} × {away}\n  {labels.get(row["market"], row["market"])}: {choice}\n  {status}{score}')
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎯 سجل توقع جديد", callback_data="predictions")]])
    await query.message.reply_text("\n".join(lines), reply_markup=keyboard)


async def show_bot_stats(message):
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()
    with db() as con:
        total_users = con.execute("SELECT COUNT(*) n FROM bot_visitors").fetchone()["n"]
        total_starts = con.execute("SELECT COALESCE(SUM(start_count),0) n FROM bot_visitors").fetchone()["n"]
        active_week = con.execute("SELECT COUNT(*) n FROM bot_visitors WHERE last_seen >= ?", (week_ago,)).fetchone()["n"]
        total_clicks = con.execute("SELECT COUNT(*) n FROM bot_events WHERE event_type='button'").fetchone()["n"]
        top_buttons = con.execute("""SELECT event_key, COUNT(*) n FROM bot_events
            WHERE event_type='button' AND event_key IS NOT NULL AND event_key != 'bot_stats'
            GROUP BY event_key ORDER BY n DESC LIMIT 5""").fetchall()
        active_subs = con.execute("SELECT COUNT(*) n FROM users WHERE expires_at > ?",
                                  (now.isoformat(),)).fetchone()["n"]
        pending_count = con.execute("SELECT COUNT(*) n FROM pending").fetchone()["n"]
    labels = {
        "plan:weekly": "اشتراك أسبوعي", "plan:monthly": "اشتراك شهري",
        "status": "حالة الاشتراك", "matches_today": "مباريات اليوم",
        "ask_match": "اسأل عن مباراة", "predictions": "سجل توقعاتك",
        "pred_history": "سجل التوقعات", "capital_manager": "إدارة رأس المال",
        "daily_gift": "الهدية اليومية"
    }
    lines = [
        "📊 إحصائيات بوت نجوم الرياضة",
        "",
        f"👥 إجمالي المستخدمين المسجلين: {total_users}",
        f"🚀 مرات الضغط على /start: {total_starts}",
        f"🟢 المستخدمون النشطون آخر 7 أيام: {active_week}",
        f"🖱️ إجمالي ضغطات الأزرار: {total_clicks}",
        f"💎 الاشتراكات النشطة: {active_subs}",
        f"⏳ طلبات الاشتراك قيد المراجعة: {pending_count}",
        "",
        "🔥 أكثر الأزرار استخدامًا:"
    ]
    if top_buttons:
        lines.extend([f"{i}. {labels.get(row['event_key'], row['event_key'])}: {row['n']} ضغطة"
                      for i, row in enumerate(top_buttons, 1)])
    else:
        lines.append("لسه مفيش ضغطات مسجلة.")
    lines.extend(["", "📌 الإحصائيات بدأت تتسجل من وقت تفعيل الميزة، ومش بتشمل الزيارات القديمة."])
    await message.reply_text("\n".join(lines))


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("الخاصية دي للإدارة فقط.")
        return
    await show_bot_stats(update.effective_message)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    track_bot_event(q.from_user, "button", data)
    if data == "bot_stats":
        if q.from_user.id not in ADMIN_IDS:
            await q.message.reply_text("الخاصية دي للإدارة فقط.")
            return
        await show_bot_stats(q.message)
        return
    if data == "daily_gift_publish":
        if q.from_user.id not in ADMIN_IDS:
            await q.message.reply_text("الخاصية دي للإدارة فقط.")
            return
        photo_id = context.user_data.pop("daily_gift_photo_id", None)
        caption = context.user_data.pop("daily_gift_caption", None)
        code = context.user_data.pop("daily_gift_code", None)
        if not photo_id or not caption:
            await q.message.reply_text("الصورة أو الكود مش موجودين. ابعت /setgift CODE وبعدها الصورة من جديد.")
            return
        if not code:
            code = caption
        today = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
        with db() as con:
            con.execute("""INSERT INTO daily_gifts(gift_date, code, photo_file_id, caption)
                VALUES(?,?,?,?) ON CONFLICT(gift_date) DO UPDATE SET
                code=excluded.code, photo_file_id=excluded.photo_file_id, caption=excluded.caption""",
                (today, code, photo_id, caption))
        await q.message.reply_text("✅ تم حفظ صورة ونص هدية النهارده. الأعضاء هيستلموهم لما يضغطوا 🎁 الهدية اليومية.")
        return
    elif data == "daily_gift_cancel":
        context.user_data.pop("daily_gift_photo_id", None)
        context.user_data.pop("daily_gift_caption", None)
        context.user_data.pop("daily_gift_code", None)
        context.user_data.pop("pending_daily_gift_code", None)
        await q.message.reply_text("تم إلغاء إعداد الهدية اليومية.")
        return
    if data == "gift_paid_publish":
        if q.from_user.id not in ADMIN_IDS:
            await q.message.reply_text("الخاصية دي للإدارة فقط.")
            return
        photo_id = context.user_data.pop("gift_paid_photo_id", None)
        channel_id = get_saved_channel_id()
        if not photo_id:
            await q.message.reply_text("مفيش صورة جاهزة للنشر. ابعت الصورة من جديد.")
            return
        if not channel_id:
            await q.message.reply_text("القناة مش محفوظة في البوت. ابعت منشورًا مُعاد توجيهه من القناة للأدمن أولًا.")
            return
        caption = (
            "🎉🏆 تم دفع جائزة كود الهدية بنجاح!\n\n"
            "مبروك للفائزين ❤️\n\n"
            "تابعونا باستمرار، فالهدايا والمفاجآت مستمرة مع نجوم الرياضة 👑\n\n"
            "نجوم الرياضة | ELITE PRO"
        )
        try:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎁 ادخل بوت نجوم الرياضة", url="https://t.me/ngoomredaabot")
            ]])
            await context.bot.send_photo(
                chat_id=channel_id,
                photo=photo_id,
                caption=caption,
                reply_markup=keyboard
            )
            await q.message.reply_text("✅ تم نشر صورة إعلان دفع جائزة كود الهدية على القناة، ومعاها زر دخول البوت.")
        except Exception:
            log.exception("Could not publish gift-paid announcement")
            await q.message.reply_text("تعذر نشر الصورة. تأكد إن البوت أدمن في القناة وعنده صلاحية النشر.")
        return
    elif data == "gift_paid_cancel":
        context.user_data.pop("gift_paid_photo_id", None)
        await q.message.reply_text("تم إلغاء نشر إعلان الجائزة.")
        return
    if data.startswith("plan:"):
        plan = data.split(":", 1)[1]
        if plan == "weekly":
            amount, label = WEEKLY_PRICE, "أسبوعي"
        else:
            amount, label = MONTHLY_PRICE, "شهري"
        context.user_data["pending_plan"] = plan
        await q.message.reply_text(
            f"اخترت الاشتراك {label}.\nقيمة الاشتراك: {amount} جنيه.\n\n"
            f"حوّل المبلغ على فودافون كاش: {PAYMENT_NUMBER}\n"
            "وبعد التحويل ابعت صورة الإيصال هنا في المحادثة."
        )
    elif data == "predictions":
        await begin_predictions(q, context)
    elif data.startswith("pred_match:"):
        await show_prediction_markets(q, context, int(data.split(":", 1)[1]))
    elif data.startswith("pred_market:"):
        await show_prediction_choices(q, context, data.split(":", 1)[1])
    elif data.startswith("pred_pick:"):
        await save_prediction(q, context, data.split(":", 1)[1])
    elif data == "pred_history":
        await show_prediction_history(q, context)
    elif data == "matches_today":
        await matches_today(update, context)
    elif data == "ask_match":
        await begin_match_analysis(update, context)
    elif data == "capital_manager":
        await begin_capital_manager(update, context)
    elif data.startswith("capital_mode:"):
        await send_capital_plan(q, context, data.split(":", 1)[1])
    elif data == "daily_gift":
        today = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
        with db() as con:
            row = con.execute("SELECT code, photo_file_id, caption FROM daily_gifts WHERE gift_date=?", (today,)).fetchone()
        if row:
            if row["photo_file_id"]:
                await q.message.reply_photo(photo=row["photo_file_id"], caption=row["caption"] or row["code"])
            else:
                await q.message.reply_text("🎁 كود هديتك اليومية:\\n" + row["code"])
        else:
            await q.message.reply_text("🎁 مفيش كود هدية متاح النهارده، تابعنا قريبًا.")
    elif data == "status":
        user_id = q.from_user.id
        with db() as con:
            row = con.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row and row["expires_at"]:
            try:
                exp = datetime.fromisoformat(row["expires_at"])
                if exp > datetime.now(timezone.utc):
                    await q.message.reply_text("اشتراكك نشط حتى: " + exp.strftime("%Y-%m-%d %H:%M UTC"))
                    return
            except ValueError:
                pass
        await q.message.reply_text("مفيش اشتراك نشط حاليًا.", reply_markup=home_keyboard())
    elif data.startswith("send_group_link:"):
        if q.from_user.id not in ADMIN_IDS:
            await q.message.reply_text("الخاصية دي للإدارة فقط.")
            return
        pending_id = int(data.split(":", 1)[1])
        with db() as con:
            row = con.execute("SELECT user_id FROM pending WHERE id=?", (pending_id,)).fetchone()
        if not row:
            await q.message.reply_text("طلب الاشتراك غير موجود أو تم التعامل معه بالفعل.")
            return
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text="أهلاً بيك ❤️\nده لينك دخول جروب نجوم الرياضة النخبة:\nhttps://t.me/+ri52W-OfZks4NDE8\n\nنورتنا 👑"
            )
            await q.message.reply_text("✅ تم إرسال لينك الجروب للعضو في الخاص.")
        except Exception:
            log.exception("Could not send group invite link to user %s", row["user_id"])
            await q.message.reply_text("❌ مقدرتش أبعتله اللينك. غالبًا العضو ما بدأش البوت أو عامل حظر. خليه يفتح البوت ويضغط Start الأول.")
        return
    elif data.startswith("approve:") or data.startswith("reject:"):
        if q.from_user.id not in ADMIN_IDS:
            return
        action, raw_id = data.split(":", 1)
        pending_id = int(raw_id)
        with db() as con:
            row = con.execute("SELECT * FROM pending WHERE id=?", (pending_id,)).fetchone()
            if not row:
                await q.message.reply_text("الطلب غير موجود أو تم التعامل معه.")
                return
            if action == "approve":
                days = 7 if row["plan"] == "weekly" else 30
                now = datetime.now(timezone.utc)
                old = con.execute("SELECT expires_at FROM users WHERE user_id=?", (row["user_id"],)).fetchone()
                start_at = now
                if old and old["expires_at"]:
                    try:
                        previous = datetime.fromisoformat(old["expires_at"])
                        if previous > now:
                            start_at = previous
                    except ValueError:
                        pass
                expires = start_at + timedelta(days=days)
                con.execute("""INSERT INTO users(user_id, expires_at, updated_at)
                    VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                    expires_at=excluded.expires_at, updated_at=excluded.updated_at""",
                    (row["user_id"], expires.isoformat(), now.isoformat()))
                con.execute("DELETE FROM pending WHERE id=?", (pending_id,))
                await context.bot.send_message(row["user_id"], "تم تأكيد الدفع وتفعيل اشتراكك بنجاح ⭐")
                await q.message.reply_text("تم قبول الدفع وتفعيل الاشتراك.")
            else:
                con.execute("DELETE FROM pending WHERE id=?", (pending_id,))
                await context.bot.send_message(row["user_id"], "الإيصال لم يتم اعتماده. تواصل مع الإدارة للمراجعة.")
                await q.message.reply_text("تم رفض الطلب.")

async def gift_paid_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id not in ADMIN_IDS:
        return
    if not message.photo:
        return
    # If /setgift CODE was sent first, pair that code with the next admin photo.
    pending_code = context.user_data.pop("pending_daily_gift_code", None)
    if pending_code:
        context.user_data["daily_gift_photo_id"] = message.photo[-1].file_id
        context.user_data["daily_gift_code"] = pending_code
        context.user_data["daily_gift_caption"] = f"🎁 كود هديتك اليومية:\\n{pending_code}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ حفظ هدية اليوم", callback_data="daily_gift_publish"),
            InlineKeyboardButton("❌ إلغاء", callback_data="daily_gift_cancel")
        ]])
        await message.reply_text(
            f"وصلت الصورة وكود الهدية ({pending_code}) يا أدمن 👑\\nراجعهم واضغط حفظ هدية اليوم.",
            reply_markup=keyboard
        )
        return
    # Also accept an image with its code written in the photo caption.
    if message.caption and message.caption.strip():
        context.user_data["daily_gift_photo_id"] = message.photo[-1].file_id
        context.user_data["daily_gift_code"] = message.caption.strip()
        context.user_data["daily_gift_caption"] = f"🎁 كود هديتك اليومية:\\n{message.caption.strip()}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ حفظ هدية اليوم", callback_data="daily_gift_publish"),
            InlineKeyboardButton("❌ إلغاء", callback_data="daily_gift_cancel")
        ]])
        await message.reply_text(
            "وصلت صورة كود الهدية والنص يا أدمن 👑\\nراجعهم واضغط حفظ هدية اليوم.",
            reply_markup=keyboard
        )
        return
    context.user_data["gift_paid_photo_id"] = message.photo[-1].file_id
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأكيد النشر على القناة", callback_data="gift_paid_publish"),
        InlineKeyboardButton("❌ إلغاء", callback_data="gift_paid_cancel")
    ]])
    await message.reply_text(
        "وصلت الصورة يا أدمن 👑\nراجعها، واضغط تأكيد النشر عشان أنزلها على قناة نجوم الرياضة بالنص المعتمد.",
        reply_markup=keyboard
    )


async def receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id in ADMIN_IDS:
        return
    plan = context.user_data.get("pending_plan")
    if not plan:
        await update.effective_message.reply_text("اختار نوع الاشتراك الأول من /start.")
        return
    user = update.effective_user
    amount = WEEKLY_PRICE if plan == "weekly" else MONTHLY_PRICE
    with db() as con:
        cur = con.execute(
            "INSERT INTO pending(user_id, plan, amount, created_at) VALUES(?,?,?,?)",
            (user.id, plan, amount, datetime.now(timezone.utc).isoformat())
        )
        pending_id = cur.lastrowid
    context.user_data.pop("pending_plan", None)
    caption = (
        f"طلب اشتراك جديد رقم #{pending_id}\n"
        f"الاسم: {user.full_name}\n"
        f"المستخدم: @{user.username or 'بدون'}\n"
        f"الخطة: {'أسبوعي' if plan == 'weekly' else 'شهري'}\n"
        f"المبلغ: {amount} جنيه"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("قبول وتفعيل", callback_data=f"approve:{pending_id}"),
            InlineKeyboardButton("رفض", callback_data=f"reject:{pending_id}")
        ],
        [InlineKeyboardButton("📩 إرسال لينك الجروب", callback_data=f"send_group_link:{pending_id}")]
    ])
    for admin_id in ADMIN_IDS:
        try:
            if update.effective_message.photo:
                await context.bot.send_photo(admin_id, update.effective_message.photo[-1].file_id,
                                             caption=caption, reply_markup=keyboard)
            else:
                await context.bot.send_document(admin_id, update.effective_message.document.file_id,
                                                caption=caption, reply_markup=keyboard)
        except Exception:
            log.exception("Could not send receipt to admin %s", admin_id)
    await update.effective_message.reply_text("وصل الإيصال للإدارة للمراجعة. هنبعتلك النتيجة هنا.")

async def setgift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("الأمر ده للإدارة فقط.")
        return
    if not context.args:
        await update.effective_message.reply_text("استخدم الأمر كده:\n/setgift CODE\nوبعدها ابعت صورة الهدية.")
        return
    code = " ".join(context.args).strip()
    context.user_data["pending_daily_gift_code"] = code
    await update.effective_message.reply_text(
        "تمام 👑 كود الهدية اتسجل مؤقتًا. ابعت صورة الهدية دلوقتي في رسالة منفصلة، "
        "وهعرضهالك للتأكيد قبل الحفظ."
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("الأمر ده للإدارة فقط.")
        return
    with db() as con:
        pending_count = con.execute("SELECT COUNT(*) n FROM pending").fetchone()["n"]
        active_count = con.execute(
            "SELECT COUNT(*) n FROM users WHERE expires_at > ?",
            (datetime.now(timezone.utc).isoformat(),)
        ).fetchone()["n"]
    await update.effective_message.reply_text(
        f"لوحة الإدارة ⭐\nطلبات قيد المراجعة: {pending_count}\nاشتراكات نشطة: {active_count}"
    )


async def capture_forwarded_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id not in ADMIN_IDS:
        return
    origin = getattr(message, "forward_origin", None)
    channel = getattr(origin, "chat", None) if origin else None
    if not channel or getattr(origin, "type", None) != "channel":
        await message.reply_text("ابعتلي رسالة مُعاد توجيهها من القناة نفسها، مش صورة أو نص منسوخ.")
        return
    save_channel_id(channel.id)
    log.info("Target channel ID saved from admin forwarded post: %s", channel.id)
    await message.reply_text("تمام ⭐ تم حفظ القناة بنجاح. هبدأ نشر إعلان قناة النخبة كل 4 ساعات.")
    asyncio.create_task(publish_elite_promo(context.application))

async def channel_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return
    if CHANNEL_ID is None and get_saved_channel_id() is None:
        save_channel_id(post.chat_id)
        log.info("Detected and saved target channel ID: %s", post.chat_id)
        asyncio.create_task(publish_elite_promo(context.application))
    if get_saved_channel_id() is not None and post.chat_id != get_saved_channel_id():
        return
    try:
        chosen = random.sample(REACTIONS, k=random.randint(1, 3))
        await context.bot.set_message_reaction(
            chat_id=post.chat_id,
            message_id=post.message_id,
            reaction=[ReactionTypeEmoji(emoji=e) for e in chosen],
            is_big=False
        )
    except Exception:
        log.exception("Could not react to channel post. Check bot admin permissions and Telegram reaction settings.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("للاشتراك استخدم /start، ولمراجعة اشتراكك اضغط حالة اشتراكي.")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("setgift", setgift))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(on_button))
    # Handle forwarded channel posts before generic text/photo handlers.
    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, capture_forwarded_channel))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, gift_paid_photo), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_match_analysis))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, receipt), group=1)
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_reactions))

    async def post_init(application):
        application.bot_data["daily_publisher_task"] = asyncio.create_task(daily_channel_publisher(application))
        application.bot_data["prediction_updater_task"] = asyncio.create_task(prediction_result_updater(application))
        application.bot_data["elite_promo_task"] = asyncio.create_task(elite_promo_publisher(application))
    app.post_init = post_init
    log.info("Nogoom Sports bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

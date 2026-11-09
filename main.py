import os
import sqlite3
import random
import logging
import asyncio
import json
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

def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("اشتراك أسبوعي", callback_data="plan:weekly"),
         InlineKeyboardButton("اشتراك شهري", callback_data="plan:monthly")],
        [InlineKeyboardButton("حالة اشتراكي", callback_data="status")],
        [InlineKeyboardButton("📅 أبرز مباريات اليوم", callback_data="matches_today")],
        [InlineKeyboardButton("🎁 الهدية اليومية", callback_data="daily_gift")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "أهلاً بيك في جروب نجوم الرياضة النخبة ⭐\n"
        "أفضل وأقوى التوقعات الرياضية.\n\n"
        "تحب تشترك أسبوعي ولا شهري؟"
    )
    await update.effective_message.reply_text(msg, reply_markup=home_keyboard())


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
        home = _team_label(home_value, "الفريق الأول")
        away = _team_label(away_value, "الفريق الثاني")
        if home == "الفريق الأول" or away == "الفريق الثاني":
            competitors = match.get("competitors") or []
            for team in competitors:
                if not isinstance(team, dict):
                    continue
                side = team.get("homeAway") or team.get("side")
                if side == "home":
                    home = _team_label(team.get("team") or team, home)
                elif side == "away":
                    away = _team_label(team.get("team") or team, away)

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
        competition = competition or "مباريات كرة القدم"

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

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
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
    elif data == "matches_today":
        await matches_today(update, context)
    elif data == "daily_gift":
        today = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
        with db() as con:
            row = con.execute("SELECT code FROM daily_gifts WHERE gift_date=?", (today,)).fetchone()
        if row:
            await q.message.reply_text("🎁 كود هديتك اليومية:\n" + row["code"])
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

async def receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("قبول وتفعيل", callback_data=f"approve:{pending_id}"),
        InlineKeyboardButton("رفض", callback_data=f"reject:{pending_id}")
    ]])
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
        await update.effective_message.reply_text("استخدم الأمر كده:\n/setgift CODE")
        return
    code = " ".join(context.args).strip()
    today = datetime.now(ZoneInfo("Africa/Cairo")).date().isoformat()
    with db() as con:
        con.execute("INSERT INTO daily_gifts(gift_date, code) VALUES(?, ?) ON CONFLICT(gift_date) DO UPDATE SET code=excluded.code", (today, code))
    await update.effective_message.reply_text("تم ضبط هدية النهارده بنجاح 🎁")

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

async def channel_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return
    if CHANNEL_ID is not None and post.chat_id != CHANNEL_ID:
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
    app.add_handler(CommandHandler("setgift", setgift))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, receipt))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_reactions))
    log.info("Nogoom Sports bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

import os
import sqlite3
import random
import logging
from datetime import datetime, timedelta, timezone

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

def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("اشتراك أسبوعي", callback_data="plan:weekly"),
         InlineKeyboardButton("اشتراك شهري", callback_data="plan:monthly")],
        [InlineKeyboardButton("حالة اشتراكي", callback_data="status")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "أهلاً بيك في جروب نجوم الرياضة النخبة ⭐\n"
        "أفضل وأقوى التوقعات الرياضية.\n\n"
        "تحب تشترك أسبوعي ولا شهري؟"
    )
    await update.effective_message.reply_text(msg, reply_markup=home_keyboard())

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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, receipt))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_reactions))
    log.info("Nogoom Sports bot is starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

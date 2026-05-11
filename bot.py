import time
import sqlite3
import logging
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler, ConversationHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8299742874:AAGobOHUA6eTLNgAvmAWDBkJGOz98RBFJEE"
ADMIN_CHAT_ID = -1003627227272  # переконайтеся, що ID правильний

CHOOSING_ACTION, CHOOSING_TYPE, PRIVACY, TYPING = range(4)

# --- База даних ---
conn = sqlite3.connect("tickets.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT,
    text TEXT,
    priority TEXT,
    status TEXT,
    anonymous INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

# --- Клавіатури ---
menu_markup = ReplyKeyboardMarkup([["➕ Нове звернення", "📦 Мої звернення"]], resize_keyboard=True)
type_markup = ReplyKeyboardMarkup([["💡Пропозиція", "❓Питання"], ["⚠️ Проблема"]], resize_keyboard=True)
privacy_markup = ReplyKeyboardMarkup([["🙈 Анонімно", "👤 Відкрито"]], resize_keyboard=True)

# --- Пам'ять ---
tickets = {}                # {ticket_id: ticket_data}
admin_reply_state = {}      # {admin_id: ticket_id}
user_last_msg = {}          # спам-захист

def is_spam(uid):
    now = time.time()
    if now - user_last_msg.get(uid, 0) < 5:
        return True
    user_last_msg[uid] = now
    return False

def get_priority(t):
    return {"Проблема": "🔴 Високий", "Питання": "🟡 Середній"}.get(t, "🟢 Низький")

def get_admin_name(user):
    """Повертає зручне ім'я адміна: @username або first_name"""
    if user.username:
        return f"@{user.username}"
    return user.first_name

async def show_menu(update: Update):
    await update.message.reply_text("Оберіть дію:", reply_markup=menu_markup)

async def show_my_tickets(update: Update, user_id: int):
    rows = cursor.execute(
        "SELECT id, type, status, priority FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (user_id,)
    ).fetchall()
    if not rows:
        await update.message.reply_text("📭 Немає звернень.")
        return
    text = "📦 Ваші звернення:\n\n"
    for tid, ttype, status, prio in rows:
        text += f"#{tid}\n{ttype}\n{prio}\n{status}\n\n"
    await update.message.reply_text(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_menu(update)
    return CHOOSING_ACTION

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "➕ Нове звернення":
        await update.message.reply_text("Оберіть тип:", reply_markup=type_markup)
        return CHOOSING_TYPE
    elif txt == "📦 Мої звернення":
        await show_my_tickets(update, update.message.from_user.id)
        await show_menu(update)
        return CHOOSING_ACTION
    else:
        await show_menu(update)
        return CHOOSING_ACTION

async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt not in ["💡Пропозиція", "❓Питання", "⚠️ Проблема"]:
        await update.message.reply_text("Оберіть з кнопок.", reply_markup=type_markup)
        return CHOOSING_TYPE
    context.user_data["type"] = txt
    await update.message.reply_text("Як надіслати?", reply_markup=privacy_markup)
    return PRIVACY

async def choose_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["anon"] = (update.message.text == "🙈 Анонімно")
    context.user_data["ready"] = True
    await update.message.reply_text("Напишіть повідомлення ✍️")
    return TYPING

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("ready"):
        return
    user = update.message.from_user
    if is_spam(user.id):
        await update.message.reply_text("⏳ Не спам!")
        return CHOOSING_ACTION

    uid = user.id
    ttype = context.user_data["type"]
    anon = context.user_data["anon"]
    text = update.message.text
    prio = get_priority(ttype)
    tag = "Анонімно" if anon else (f"@{user.username}" if user.username else str(uid))

    # Зберігаємо в БД (id автоматично)
    cursor.execute("""
        INSERT INTO tickets (user_id, type, text, priority, status, anonymous)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (uid, ttype, text, prio, "🆕 Нове", int(anon)))
    conn.commit()
    tid = cursor.lastrowid

    tickets[tid] = {
        "user_id": uid,
        "type": ttype,
        "text": text,
        "priority": prio,
        "status": "🆕 Нове",
        "anonymous": anon
    }

    admin_text = f"📩 #{tid}\n{ttype}\n{prio}\n\n{text}\n\n👤 {tag}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📂 Відкрити", callback_data=f"open_{tid}"),
        InlineKeyboardButton("💬 Відповісти", callback_data=f"reply_{tid}"),
        InlineKeyboardButton("⚙️ В роботу", callback_data=f"work_{tid}"),
        InlineKeyboardButton("✅ Закрити", callback_data=f"close_{tid}")
    ]])

    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Помилка надсилання в адмін-чат: {e}")
        await update.message.reply_text("❌ Помилка надсилання адмінам. Звернення збережено, але сповіщення не доставлено.")
    else:
        await update.message.reply_text(f"✅ Дякую! Звернення #{tid} отримано.", reply_markup=menu_markup)
        return CHOOSING_ACTION

    await update.message.reply_text(f"✅ Звернення #{tid} збережено.", reply_markup=menu_markup)
    return CHOOSING_ACTION

# --- Адмін: обробка відповідей (тільки після натискання "Відповісти") ---
async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in admin_reply_state:
        # Якщо адмін не натискав "Відповісти", ігноруємо повідомлення
        return
    tid = admin_reply_state.pop(uid)
    t = tickets.get(tid)
    if not t:
        await update.message.reply_text("❌ Звернення не знайдено.")
        return
    if t["status"] == "✅ Закрито":
        await update.message.reply_text("❌ Це звернення вже закрите. Відповідь неможлива.")
        return

    try:
        await context.bot.send_message(
            chat_id=t["user_id"],
            text=f"💬 Відповідь на звернення #{tid}:\n\n{update.message.text}"
        )
    except Exception as e:
        logger.error(f"Не вдалося надіслати відповідь користувачу: {e}")
        await update.message.reply_text("❌ Не вдалося надіслати відповідь користувачу.")
        return

    t["status"] = "💬 Відповідь"
    cursor.execute("UPDATE tickets SET status=? WHERE id=?", (t["status"], tid))
    conn.commit()
    await update.message.reply_text("✅ Відповідь надіслано користувачеві.")

# --- Обробка всіх inline-кнопок ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    act, tid = query.data.split("_")
    tid = int(tid)
    t = tickets.get(tid)
    if not t:
        await query.edit_message_text("❌ Звернення не знайдено.")
        return

    admin_name = get_admin_name(query.from_user)

    if act == "reply":
        # Встановлюємо стан, щоб наступне повідомлення адміна пішло як відповідь
        admin_reply_state[query.from_user.id] = tid
        await query.message.reply_text(f"✍️ Введіть текст відповіді для #{tid}:")
        return

    elif act == "work":
        if t["status"] == "✅ Закрито":
            await query.answer("Звернення вже закрите!", show_alert=True)
            return
        t["status"] = "⚙️ В роботі"
        cursor.execute("UPDATE tickets SET status=? WHERE id=?", ("⚙️ В роботі", tid))
        conn.commit()
        # Оновлюємо повідомлення в адмін-чаті, додаючи інформацію про адміна
        current_text = query.message.text
        if "Взяв у роботу:" not in current_text:
            new_text = current_text + f"\n\n👨‍💼 Взяв у роботу: {admin_name}"
            await query.edit_message_text(new_text, reply_markup=query.message.reply_markup)
        # Показуємо деталі (як при відкритті)
        await query.edit_message_text(
            f"#{tid}\n{t['type']}\n{t['priority']}\n\n{t['text']}\n\nСтатус: {t['status']}",
            reply_markup=query.message.reply_markup
        )

    elif act == "close":
        if t["status"] == "✅ Закрито":
            await query.answer("Вже закрито!", show_alert=True)
            return
        t["status"] = "✅ Закрито"
        cursor.execute("UPDATE tickets SET status=? WHERE id=?", ("✅ Закрито", tid))
        conn.commit()
        # Сповіщаємо користувача
        try:
            await context.bot.send_message(t["user_id"], f"✅ Ваше звернення #{tid} закрито адміном {admin_name}.")
        except:
            pass
        # Оновлюємо повідомлення в адмін-чаті
        current_text = query.message.text
        new_text = current_text + f"\n\n🔒 Закрито: {admin_name}"
        await query.edit_message_text(new_text, reply_markup=query.message.reply_markup)
        # Показуємо деталі
        await query.edit_message_text(
            f"#{tid}\n{t['type']}\n{t['priority']}\n\n{t['text']}\n\nСтатус: {t['status']}",
            reply_markup=query.message.reply_markup
        )

    elif act == "open":
        # Просто показуємо деталі (без зміни статусу)
        await query.edit_message_text(
            f"#{tid}\n{t['type']}\n{t['priority']}\n\n{t['text']}\n\nСтатус: {t['status']}",
            reply_markup=query.message.reply_markup
        )

# --- Перевірка доступу до адмін-чату при старті ---
async def test_admin_chat(app):
    try:
        await app.bot.send_chat_action(chat_id=ADMIN_CHAT_ID, action="typing")
        logger.info(f"✅ Доступ до адмін-чату {ADMIN_CHAT_ID} підтверджено.")
    except Exception as e:
        logger.error(f"❌ НЕМАЄ ДОСТУПУ: {e}")
        logger.error("Перевірте ID групи, додавання бота та права.")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.post_init = test_admin_chat

    # Розмова з користувачем
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
            CHOOSING_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_type)],
            PRIVACY: [MessageHandler(filters.TEXT & ~filters.COMMAND, choose_privacy)],
            TYPING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)],
        },
        fallbacks=[CommandHandler("start", start)]
    )
    app.add_handler(conv)

    # Обробник відповідей адміна (тільки якщо є стан)
    app.add_handler(MessageHandler(filters.TEXT & filters.Chat(ADMIN_CHAT_ID), admin_reply))
    # Обробник всіх callback-кнопок
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.run_polling()

if __name__ == "__main__":
    main()
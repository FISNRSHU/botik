import os
import time
import sqlite3
import logging
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler, CallbackQueryHandler, ConversationHandler
from profanity_filter import ProfanityFilter

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не задано в .env")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip()]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHOOSING_ACTION, CHOOSING_TYPE, PRIVACY, TYPING = range(4)   # Без BROADCAST

# --- База даних ---
conn = sqlite3.connect("tickets.db", check_same_thread=False)
cursor = conn.cursor()

# Таблиця tickets
cursor.execute("PRAGMA table_info(tickets)")
columns = [col[1] for col in cursor.fetchall()]
if "last_updated" not in columns:
    cursor.execute("ALTER TABLE tickets ADD COLUMN last_updated TIMESTAMP")
if "taken_by" not in columns:
    cursor.execute("ALTER TABLE tickets ADD COLUMN taken_by TEXT DEFAULT NULL")
conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT,
    text TEXT,
    priority TEXT,
    status TEXT,
    anonymous INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP,
    taken_by TEXT DEFAULT NULL
)
""")
conn.commit()

# Таблиця replies (історія відповідей)
cursor.execute("""
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER,
    admin_name TEXT,
    reply_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (ticket_id) REFERENCES tickets (id)
)
""")
conn.commit()

cursor.execute("UPDATE tickets SET last_updated = created_at WHERE last_updated IS NULL")
conn.commit()

# --- Клавіатури ---
menu_markup = ReplyKeyboardMarkup([["➕ Нове звернення", "📦 Мої звернення"]], resize_keyboard=True)
type_markup = ReplyKeyboardMarkup(
    [["💡Пропозиція", "❓Питання"], ["⚠️ Проблема", "◀️ Назад"]],
    resize_keyboard=True
)
privacy_markup = ReplyKeyboardMarkup(
    [["🙈 Анонімно", "👤 Відкрито"], ["◀️ Назад"]],
    resize_keyboard=True
)

# --- Пам'ять ---
tickets = {}
admin_reply_state = {}
user_last_msg = {}
broadcast_sessions = {}   # {user_id: {"media": [], "caption": ""}}

# --- Цензура ---
profanity_filter = ProfanityFilter()

def is_spam(uid):
    now = time.time()
    if now - user_last_msg.get(uid, 0) < 5:
        return True
    user_last_msg[uid] = now
    return False

def get_priority(t):
    return {"Проблема": "🔴 Високий", "Питання": "🟡 Середній"}.get(t, "🟢 Низький")

def get_admin_name(user):
    return f"@{user.username}" if user.username else user.first_name

def auto_upgrade_priority(ticket):
    if ticket["status"] == "🔒 Закрито":
        return False
    cursor.execute("SELECT created_at, last_updated FROM tickets WHERE id=?", (ticket["id"],))
    row = cursor.fetchone()
    if not row:
        return False
    created_at = datetime.fromisoformat(row[0]) if isinstance(row[0], str) else row[0]
    last_updated = datetime.fromisoformat(row[1]) if row[1] and isinstance(row[1], str) else (created_at if row[1] is None else row[1])
    now = datetime.now()
    if now - created_at > timedelta(hours=24) and now - last_updated > timedelta(hours=24):
        old = ticket["priority"]
        new = None
        if old == "🟢 Низький":
            new = "🟡 Середній"
        elif old == "🟡 Середній":
            new = "🔴 Високий"
        if new:
            ticket["priority"] = new
            cursor.execute("UPDATE tickets SET priority=?, last_updated=? WHERE id=?", (new, now.isoformat(), ticket["id"]))
            conn.commit()
            return True
    return False

# ----------------- КОРИСТУВАЦЬКІ ФУНКЦІЇ -----------------
async def show_menu(update: Update):
    await update.message.reply_text("Оберіть дію:", reply_markup=menu_markup)

async def show_my_tickets(update: Update, user_id: int, page: int = 0):
    """Показує звернення з пагінацією та історією відповідей"""
    limit = 5
    offset = page * limit
    rows = cursor.execute("""
        SELECT id, type, status, priority, created_at
        FROM tickets 
        WHERE user_id=? 
        ORDER BY id DESC 
        LIMIT ? OFFSET ?
    """, (user_id, limit, offset)).fetchall()
    
    if not rows:
        if page == 0:
            await update.message.reply_text("📭 Немає звернень.")
        else:
            await update.message.reply_text("❌ Більше немає звернень.")
        return

    for tid, ttype, status, prio, created in rows:
        # Отримуємо відповіді
        replies = cursor.execute("""
            SELECT admin_name, reply_text, created_at
            FROM replies
            WHERE ticket_id=?
            ORDER BY created_at ASC
        """, (tid,)).fetchall()
        
        # Текст звернення
        if tid in tickets:
            ticket_text = tickets[tid]['text']
        else:
            cursor.execute("SELECT text FROM tickets WHERE id=?", (tid,))
            row_text = cursor.fetchone()
            ticket_text = row_text[0] if row_text else "..."
        
        text = f"📌 *Звернення #{tid}*\n"
        text += f"📅 Створено: {created[:10]}\n"
        text += f"📂 Тип: {ttype}\n"
        text += f"⚙️ Пріоритет: {prio}\n"
        text += f"🔖 Статус: {status}\n"
        text += f"📝 Текст:\n{ticket_text}\n"
        
        if replies:
            text += "\n💬 *Історія відповідей:*\n"
            for admin, reply, reply_date in replies:
                text += f"👨‍💼 {admin} [{reply_date[:10]}]:\n{reply}\n\n"
        else:
            text += "\n_(Відповідей поки що немає)_\n"
        
        await update.message.reply_text(text, parse_mode="Markdown")
    
    # Пагінація
    total_count = cursor.execute("SELECT COUNT(*) FROM tickets WHERE user_id=?", (user_id,)).fetchone()[0]
    if total_count > (page + 1) * limit:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("➡️ Наступні", callback_data=f"mytickets_{page+1}")
        ]])
        await update.message.reply_text("Щоб побачити більше звернень, натисніть кнопку:", reply_markup=kb)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Очищаємо можливий режим розсилки
    user_id = update.message.from_user.id
    if user_id in broadcast_sessions:
        del broadcast_sessions[user_id]
    await show_menu(update)
    return CHOOSING_ACTION

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "➕ Нове звернення":
        await update.message.reply_text("Оберіть тип:", reply_markup=type_markup)
        return CHOOSING_TYPE
    elif txt == "📦 Мої звернення":
        await show_my_tickets(update, update.message.from_user.id, 0)
        await show_menu(update)
        return CHOOSING_ACTION
    else:
        await show_menu(update)
        return CHOOSING_ACTION

async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "◀️ Назад":
        await show_menu(update)
        return CHOOSING_ACTION
    if txt not in ["💡Пропозиція", "❓Питання", "⚠️ Проблема"]:
        await update.message.reply_text("Оберіть з кнопок.", reply_markup=type_markup)
        return CHOOSING_TYPE
    context.user_data["type"] = txt
    await update.message.reply_text("Як надіслати?", reply_markup=privacy_markup)
    return PRIVACY

async def choose_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "◀️ Назад":
        await update.message.reply_text("Оберіть тип:", reply_markup=type_markup)
        return CHOOSING_TYPE
    if txt not in ["🙈 Анонімно", "👤 Відкрито"]:
        await update.message.reply_text("Оберіть кнопку:", reply_markup=privacy_markup)
        return PRIVACY
    context.user_data["anon"] = (txt == "🙈 Анонімно")
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

    if profanity_filter.contains_profanity(text):
        await update.message.reply_text("❌ Ваше повідомлення містить нецензурну лексику або образи. Будь ласка, переформулюйте звернення ввічливо.")
        return CHOOSING_ACTION

    prio = get_priority(ttype)
    tag = "Анонімно" if anon else (f"@{user.username}" if user.username else str(uid))

    cursor.execute("""
        INSERT INTO tickets (user_id, type, text, priority, status, anonymous, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (uid, ttype, text, prio, "🆕 Нове", int(anon), datetime.now().isoformat()))
    conn.commit()
    tid = cursor.lastrowid

    tickets[tid] = {
        "id": tid,
        "user_id": uid,
        "type": ttype,
        "text": text,
        "priority": prio,
        "status": "🆕 Нове",
        "anonymous": anon,
        "taken_by": None
    }

    admin_text = f"📩 #{tid}\n{ttype}\n{prio}\n\n{text}\n\n👤 {tag}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Взяти в роботу", callback_data=f"take_{tid}")],
        [InlineKeyboardButton("💬 Відповісти", callback_data=f"reply_{tid}"),
         InlineKeyboardButton("📊 Пріоритет", callback_data=f"priority_{tid}"),
         InlineKeyboardButton("✅ Закрити", callback_data=f"close_{tid}")]
    ])

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

# ----------------- АДМІН ФУНКЦІЇ -----------------
async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in admin_reply_state:
        return
    tid = admin_reply_state.pop(uid)
    t = tickets.get(tid)
    if not t:
        await update.message.reply_text("❌ Звернення не знайдено.")
        return
    if t["status"] == "🔒 Закрито":
        await update.message.reply_text("❌ Це звернення вже закрите. Відповідь неможлива.")
        return
    if t.get("taken_by") and t["taken_by"] != get_admin_name(update.message.from_user):
        await update.message.reply_text(f"❌ Це звернення взяв у роботу {t['taken_by']}. Ви не можете відповідати.")
        return

    reply_text = update.message.text
    admin_name = get_admin_name(update.message.from_user)

    try:
        await context.bot.send_message(t["user_id"], f"💬 Відповідь на звернення #{tid}:\n\n{reply_text}")
        cursor.execute("""
            INSERT INTO replies (ticket_id, admin_name, reply_text, created_at)
            VALUES (?, ?, ?, ?)
        """, (tid, admin_name, reply_text, datetime.now().isoformat()))
        conn.commit()
    except Exception as e:
        logger.error(f"Не вдалося надіслати відповідь: {e}")
        await update.message.reply_text("❌ Не вдалося надіслати відповідь користувачу.")
        return

    t["status"] = "💬 Відповідь"
    cursor.execute("UPDATE tickets SET status=?, last_updated=? WHERE id=?", (t["status"], datetime.now().isoformat(), tid))
    conn.commit()
    await update.message.reply_text("✅ Відповідь надіслано користувачеві та збережено в історії.")

# ---------- РОЗСИЛКА (без ConversationHandler) ----------
def get_all_users():
    cursor.execute("SELECT DISTINCT user_id FROM tickets")
    return [row[0] for row in cursor.fetchall()]

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У вас немає прав для цієї команди.")
        return
    broadcast_sessions[user_id] = {"media": [], "caption": ""}
    await update.message.reply_text(
        "📢 *Режим створення розсилки*\n"
        "Надішліть **текст** (буде підписом до медіа) та **до 4 фото** (одне за одним) або **одне відео**.\n"
        "Після додавання всього контенту натисніть кнопку **'✅ Готово'**.\n"
        "Для скасування – /cancel",
        parse_mode="Markdown"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово", callback_data="broadcast_finish")]])
    await update.message.reply_text("Коли закінчите – натисніть кнопку:", reply_markup=kb)

# Загальний обробник для тексту, фото, відео (перевіряє, чи користувач у режимі розсилки)
async def broadcast_collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in broadcast_sessions:
        return
    session = broadcast_sessions[user_id]
    msg = update.message

    # Якщо вже є відео – більше не додаємо
    if any(m["type"] == "video" for m in session["media"]):
        await msg.reply_text("❌ Ви вже додали відео. Натисніть 'Готово' для розсилки.")
        return

    if msg.photo:
        if len(session["media"]) >= 4:
            await msg.reply_text("❌ Максимум 4 фото. Натисніть 'Готово'.")
            return
        file_id = msg.photo[-1].file_id
        session["media"].append({"type": "photo", "file_id": file_id})
        await msg.reply_text(f"✅ Фото {len(session['media'])}/4 додано. Надішліть ще або натисніть 'Готово'.")
    elif msg.video:
        if session["media"]:
            await msg.reply_text("❌ Відео має бути єдиним файлом. Почніть заново (/cancel).")
            return
        session["media"].append({"type": "video", "file_id": msg.video.file_id})
        if msg.caption:
            session["caption"] = msg.caption
        await msg.reply_text("✅ Відео додано. Натисніть 'Готово'.")
    elif msg.text and not session["caption"]:
        session["caption"] = msg.text
        await msg.reply_text("✅ Текст додано. Тепер можете додати медіа або натиснути 'Готово'.")
    else:
        await msg.reply_text("Надішліть фото, відео або текст (капшен).")

    broadcast_sessions[user_id] = session

# ---------- ОСНОВНИЙ CALLBACK ХЕНДЛЕР ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ----- Пагінація "Мої звернення" -----
    if data.startswith("mytickets_"):
        page = int(data.split("_")[1])
        await show_my_tickets(update, query.from_user.id, page)
        return

    # ----- Скидання статистики -----
    if data.startswith("reset_"):
        user_id = query.from_user.id
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔️ Немає прав.")
            return
        if data == "reset_yes":
            cursor.execute("DELETE FROM tickets")
            conn.commit()
            tickets.clear()
            await query.edit_message_text("✅ Вся статистика успішно очищена.")
            logger.info(f"Адмін {user_id} очистив всю статистику")
        else:
            await query.edit_message_text("❌ Очищення скасовано.")
        return

    # ----- Завершення збору розсилки -----
    if data == "broadcast_finish":
        user_id = query.from_user.id
        if user_id not in broadcast_sessions:
            await query.answer("Немає активної сесії розсилки.", show_alert=True)
            return
        session = broadcast_sessions.pop(user_id)
        media_list = session["media"]
        caption = session["caption"]
        users = get_all_users()
        if not users:
            await query.edit_message_text("❌ Немає жодного користувача для розсилки.")
            return
        context.user_data["broadcast_data"] = {"media": media_list, "caption": caption, "users_count": len(users)}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Так, розіслати", callback_data="broadcast_confirm")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="broadcast_cancel")]
        ])
        await query.edit_message_text(
            f"⚠️ **Підтвердження розсилки**\n"
            f"Отримувачів: {len(users)}\n"
            f"Текст: {caption or '—'}\n"
            f"Медіа: {len(media_list)} шт.\n\n"
            f"Розпочати?",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return

    if data == "broadcast_confirm":
        bdata = context.user_data.get("broadcast_data")
        if not bdata:
            await query.edit_message_text("❌ Дані розсилки не знайдено. Почніть заново /broadcast.")
            return
        users = get_all_users()
        success = 0
        fail = 0
        prefix = "📢 *РОЗСИЛКА:*\n\n"
        for uid in users:
            try:
                if not bdata["media"]:
                    await context.bot.send_message(chat_id=uid, text=prefix + (bdata["caption"] or " "))
                else:
                    if len(bdata["media"]) == 1:
                        m = bdata["media"][0]
                        if m["type"] == "photo":
                            await context.bot.send_photo(chat_id=uid, photo=m["file_id"], caption=prefix + (bdata["caption"] or ""))
                        elif m["type"] == "video":
                            await context.bot.send_video(chat_id=uid, video=m["file_id"], caption=prefix + (bdata["caption"] or ""))
                    else:
                        media_group = [InputMediaPhoto(m["file_id"]) for m in bdata["media"]]
                        if bdata["caption"]:
                            media_group[0].caption = prefix + bdata["caption"]
                        await context.bot.send_media_group(chat_id=uid, media=media_group)
                success += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Помилка надсилання {uid}: {e}")
                fail += 1
        await query.edit_message_text(f"✅ Розсилка завершена.\nУспішно: {success}\nПомилок: {fail}")
        context.user_data.pop("broadcast_data", None)
        return

    if data == "broadcast_cancel":
        context.user_data.pop("broadcast_data", None)
        await query.edit_message_text("❌ Розсилку скасовано.")
        return

    # ----- Зміна пріоритету -----
    if data.startswith("set_priority_"):
        parts = data.split("_")
        if len(parts) >= 4:
            tid = int(parts[2])
            level = parts[3]
            t = tickets.get(tid)
            if not t:
                cursor.execute("SELECT user_id, type, text, priority, status, anonymous, taken_by FROM tickets WHERE id=?", (tid,))
                row = cursor.fetchone()
                if row:
                    t = {
                        "id": tid,
                        "user_id": row[0],
                        "type": row[1],
                        "text": row[2],
                        "priority": row[3],
                        "status": row[4],
                        "anonymous": row[5],
                        "taken_by": row[6]
                    }
                    tickets[tid] = t
                else:
                    await query.edit_message_text("❌ Звернення не знайдено.")
                    return
            mapping = {"high": "🔴 Високий", "medium": "🟡 Середній", "low": "🟢 Низький"}
            new_prio = mapping.get(level)
            if new_prio:
                t["priority"] = new_prio
                cursor.execute("UPDATE tickets SET priority=?, last_updated=? WHERE id=?", (new_prio, datetime.now().isoformat(), tid))
                conn.commit()
                await show_ticket_details(query, t)
        return

    # ----- Звичайні дії з тикетами -----
    try:
        act, tid_str = data.split("_", 1)
        tid = int(tid_str)
    except ValueError:
        await query.edit_message_text("❌ Невідома дія.")
        return

    t = tickets.get(tid)
    if not t:
        cursor.execute("SELECT user_id, type, text, priority, status, anonymous, taken_by FROM tickets WHERE id=?", (tid,))
        row = cursor.fetchone()
        if row:
            t = {
                "id": tid,
                "user_id": row[0],
                "type": row[1],
                "text": row[2],
                "priority": row[3],
                "status": row[4],
                "anonymous": row[5],
                "taken_by": row[6]
            }
            tickets[tid] = t
        else:
            await query.edit_message_text("❌ Звернення не знайдено.")
            return

    admin_name = get_admin_name(query.from_user)
    is_taken_by_me = (t.get("taken_by") == admin_name)
    is_taken = t.get("taken_by") is not None and t.get("taken_by") != ""

    if act in ["take", "priority"]:
        auto_upgrade_priority(t)

    if act == "take":
        if is_taken:
            await query.answer("Це звернення вже хтось взяв!", show_alert=True)
            return
        t["taken_by"] = admin_name
        t["status"] = "⚙️ В роботі"
        cursor.execute("UPDATE tickets SET taken_by=?, status=?, last_updated=? WHERE id=?", (admin_name, "⚙️ В роботі", datetime.now().isoformat(), tid))
        conn.commit()
        new_text = query.message.text + f"\n\n👨‍💼 Взяв у роботу: {admin_name}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Відповісти", callback_data=f"reply_{tid}"),
             InlineKeyboardButton("📊 Пріоритет", callback_data=f"priority_{tid}"),
             InlineKeyboardButton("✅ Закрити", callback_data=f"close_{tid}")]
        ])
        await query.edit_message_text(new_text, reply_markup=kb)
        return

    if is_taken and not is_taken_by_me:
        await query.answer(f"Це звернення взяв {t['taken_by']}. Ви не можете ним керувати.", show_alert=True)
        return

    if act == "reply":
        admin_reply_state[query.from_user.id] = tid
        await query.message.reply_text(f"✍️ Введіть текст відповіді для #{tid}:")
        return

    elif act == "priority":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 Високий", callback_data=f"set_priority_{tid}_high")],
            [InlineKeyboardButton("🟡 Середній", callback_data=f"set_priority_{tid}_medium")],
            [InlineKeyboardButton("🟢 Низький", callback_data=f"set_priority_{tid}_low")],
            [InlineKeyboardButton("◀️ Назад", callback_data=f"back_{tid}")]
        ])
        await query.edit_message_text(f"Оберіть пріоритет для #{tid}:", reply_markup=kb)
        return

    elif act == "close":
        if t["status"] == "🔒 Закрито":
            await query.answer("Вже закрито!", show_alert=True)
            return
        t["status"] = "🔒 Закрито"
        cursor.execute("UPDATE tickets SET status=?, last_updated=? WHERE id=?", ("🔒 Закрито", datetime.now().isoformat(), tid))
        conn.commit()
        current_text = query.message.text
        if "🔒 Закрито:" not in current_text:
            new_text = current_text + f"\n\n🔒 Закрито: {admin_name}"
        else:
            new_text = current_text
        await query.edit_message_text(new_text, reply_markup=None)
        return

    elif act == "back":
        await show_ticket_details(query, t)

async def show_ticket_details(query, t):
    buttons = []
    if t["status"] != "🔒 Закрито":
        if not t.get("taken_by"):
            buttons.append([InlineKeyboardButton("⚙️ Взяти в роботу", callback_data=f"take_{t['id']}")])
        row = [
            InlineKeyboardButton("💬 Відповісти", callback_data=f"reply_{t['id']}"),
            InlineKeyboardButton("📊 Пріоритет", callback_data=f"priority_{t['id']}"),
            InlineKeyboardButton("✅ Закрити", callback_data=f"close_{t['id']}")
        ]
        buttons.append(row)
    kb = InlineKeyboardMarkup(buttons) if buttons else None
    text = f"#{t['id']}\n{t['type']}\n{t['priority']}\n\n{t['text']}\n\nСтатус: {t['status']}"
    if t.get("taken_by"):
        text += f"\n👨‍💼 В роботі: {t['taken_by']}"
    await query.edit_message_text(text, reply_markup=kb)

# ----------------- СТАТИСТИКА -----------------
async def get_stats_data(since_date: datetime):
    cursor.execute("""
        SELECT type, priority, status, created_at, last_updated
        FROM tickets
        WHERE created_at >= ?
    """, (since_date.isoformat(),))
    rows = cursor.fetchall()
    if not rows:
        return None
    total = len(rows)
    types = defaultdict(int)
    priorities = defaultdict(int)
    statuses = defaultdict(int)
    closed_tickets = 0
    total_time = 0.0

    for typ, prio, stat, created, updated in rows:
        types[typ] += 1
        priorities[prio] += 1
        statuses[stat] += 1
        if stat == "🔒 Закрито":
            closed_tickets += 1
            try:
                created_dt = datetime.fromisoformat(created)
                updated_dt = datetime.fromisoformat(updated) if updated else datetime.now()
                days = (updated_dt - created_dt).total_seconds() / 86400
                total_time += max(0, days)
            except:
                pass
    avg_close_days = total_time / closed_tickets if closed_tickets > 0 else 0
    return {
        "total": total,
        "types": types,
        "priorities": priorities,
        "statuses": statuses,
        "closed": closed_tickets,
        "avg_days": avg_close_days
    }

def format_stats_report(data: dict, period_name: str, since_date: datetime) -> str:
    if data is None:
        return f"📊 Статистика за {period_name} (з {since_date.strftime('%d.%m.%Y')}) відсутня."
    report = f"📊 *Статистика використання за {period_name} (з {since_date.strftime('%d.%m.%Y')})*\n"
    report += f"Всього звернень: {data['total']}\n\n"
    report += "*За типами:*\n"
    for t, count in data['types'].items():
        report += f"  {t}: {count}\n"
    report += "\n*За пріоритетами:*\n"
    for p, count in data['priorities'].items():
        report += f"  {p}: {count}\n"
    report += "\n*За статусами:*\n"
    for s, count in data['statuses'].items():
        emoji = "🆕" if "Нове" in s else "⚙️" if "роботі" in s else "💬" if "Відповідь" in s else "✅" if "Закрито" in s else "❓"
        report += f"  {emoji} {s}: {count}\n"
    if data['closed'] > 0:
        report += f"\n🕒 Середній час закриття: {data['avg_days']:.1f} днів"
    return report

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У вас немає прав для цієї команди.")
        return
    args = context.args
    period = args[0].lower() if args else "week"
    now = datetime.now()
    if period == "week":
        since = now - timedelta(days=7)
        period_name = "тиждень"
    elif period == "month":
        since = now - timedelta(days=30)
        period_name = "місяць"
    elif period == "year":
        since = now - timedelta(days=365)
        period_name = "рік"
    elif period == "all":
        since = datetime(2020, 1, 1)
        period_name = "весь час"
    else:
        await update.message.reply_text("❌ Використання: /stats [week|month|year|all]")
        return
    data = await get_stats_data(since)
    report = format_stats_report(data, period_name, since)
    await update.message.reply_text(report, parse_mode="Markdown")

async def cmd_reset_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У вас немає прав для цієї команди.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Ні, скасувати", callback_data="reset_no"),
         InlineKeyboardButton("✅ Так, очистити все", callback_data="reset_yes")]
    ])
    await update.message.reply_text(
        "⚠️ *Ви впевнені, що хочете очистити всю статистику?*\n"
        "Всі звернення будуть безповоротно видалені!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

# ---------- Автоматичні звіти ----------
last_report_date = {"week": None, "month": None, "year": None}

async def send_scheduled_report(app, period: str):
    now = datetime.now()
    if period == "week":
        if now.weekday() == 0 and now.hour == 9:
            if last_report_date["week"] is None or (now - last_report_date["week"]).days >= 7:
                since = now - timedelta(days=7)
                data = await get_stats_data(since)
                report = format_stats_report(data, "тиждень", since)
                await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report, parse_mode="Markdown")
                last_report_date["week"] = now
    elif period == "month":
        if now.day == 1 and now.hour == 10:
            if last_report_date["month"] is None or (now - last_report_date["month"]).days >= 28:
                since = now - timedelta(days=30)
                data = await get_stats_data(since)
                report = format_stats_report(data, "місяць", since)
                await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report, parse_mode="Markdown")
                last_report_date["month"] = now
    elif period == "year":
        if now.month == 1 and now.day == 1 and now.hour == 11:
            if last_report_date["year"] is None or (now - last_report_date["year"]).days >= 365:
                since = now - timedelta(days=365)
                data = await get_stats_data(since)
                report = format_stats_report(data, "рік", since)
                await app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=report, parse_mode="Markdown")
                last_report_date["year"] = now

async def stats_scheduler(app):
    while True:
        await send_scheduled_report(app, "week")
        await send_scheduled_report(app, "month")
        await send_scheduled_report(app, "year")
        await asyncio.sleep(3600)

# ----------------- ЗАПУСК -----------------
async def test_admin_chat(app):
    try:
        await app.bot.send_chat_action(chat_id=ADMIN_CHAT_ID, action="typing")
        logger.info(f"✅ Доступ до адмін-чату {ADMIN_CHAT_ID} підтверджено.")
    except Exception as e:
        logger.error(f"❌ НЕМАЄ ДОСТУПУ: {e}")

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    
    async def combined_post_init(app):
        await test_admin_chat(app)
        asyncio.create_task(stats_scheduler(app))
    app.post_init = combined_post_init

    # Основний діалог (звернення)
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

    # Обробник розсилки (без ConversationHandler) – перевіряє, чи користувач у режимі
    app.add_handler(MessageHandler((filters.PHOTO | filters.VIDEO | filters.TEXT) & ~filters.COMMAND, broadcast_collector), group=0)

    # Інші обробники
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.TEXT & filters.Chat(ADMIN_CHAT_ID), admin_reply))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("reset_stats", cmd_reset_stats))
    app.add_handler(CommandHandler("cancel", start))   # /cancel скидає стан

    app.run_polling()

if __name__ == "__main__":
    main()
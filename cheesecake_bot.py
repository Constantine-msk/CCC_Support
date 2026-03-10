import os
import logging
import httpx
import xml.etree.ElementTree as ET
import time
import datetime
import holidays

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# =====================
# НАСТРОЙКИ
# =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"

# Ваши ID менеджеров
MANAGER_IDS = [1321630636]

MAX_HISTORY = 10       
BOT_DATA_TTL = 3600    

# =====================
# ЛОГИ И КАЛЕНДАРЬ
# =====================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ru_holidays = holidays.RU()

def is_non_working_day(date: datetime.date = None) -> bool:
    d = date or datetime.date.today()
    return d.weekday() >= 5 or d in ru_holidays

def next_working_day() -> datetime.date:
    d = datetime.date.today() + datetime.timedelta(days=1)
    while is_non_working_day(d):
        d += datetime.timedelta(days=1)
    return d

# =====================
# КЭШ ПРОДУКТОВ
# =====================

products_cache = {"offers": [], "menu_text": "", "last_update": 0}

# =====================
# SYSTEM PROMPT (Ask Cheez)
# =====================

def build_system_prompt(menu_text: str = "") -> str:
    today = datetime.date.today()
    weekday_ru = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    
    if not is_non_working_day():
        delivery_status = "✅ Сегодня рабочий день, доставка работает."
    else:
        nwd = next_working_day()
        delivery_status = f"❌ Сегодня выходной. Ближайшая доставка: {nwd.strftime('%d.%m.%Y')}."

    return f"""Ты — Ask Cheez, ИИ-помощник Cheesecake Club. 
Сегодня: {today.strftime('%d.%m.%Y')}, {weekday_ru[today.weekday()]}.
{delivery_status}

ПРАВИЛА:
- Заказ от 3000 ₽ — доставка бесплатно.
- Заказ до 14:00 — доставка сегодня.
- В выходные только самовывоз (Рябиновая, 32).
- Дарим кусочек чизкейка к каждому заказу!

КАТАЛОГ:
{menu_text if menu_text else "Меню обновляется..." }
"""

# =====================
# КЛАВИАТУРЫ
# =====================

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("🔍 Найти чизкейк", callback_data="search"),
         InlineKeyboardButton("🔥 Популярные", callback_data="popular")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="manager")],
    ])

def end_chat_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Вернуться к Ask Cheez", callback_data="end_chat")]])

def manager_end_kb(client_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Завершить диалог", callback_data=f"m_end:{client_id}")]])

# =====================
# РАБОТА С YML
# =====================

async def load_products():
    now = time.time()
    if now - products_cache["last_update"] < 900: return
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(YML_URL, timeout=20)
            root = ET.fromstring(res.content)
            offers = []
            for o in root.findall(".//offer"):
                offers.append({
                    "name": o.find("name").text if o.find("name") is not None else "",
                    "price": o.find("price").text if o.find("price") is not None else "",
                    "picture": o.find("picture").text if o.find("picture") is not None else None,
                    "url": o.attrib.get("url", ""),
                    "description": o.find("description").text if o.find("description") is not None else ""
                })
            products_cache["offers"] = offers
            products_cache["last_update"] = now
            products_cache["menu_text"] = "\n".join([f"• {x['name']} — {x['price']} ₽" for x in offers[:30]])
    except Exception as e: logger.error(f"YML error: {e}")

async def send_cakes(update, context, cakes):
    for cake in cakes:
        cap = f"🍰 *{cake['name']}*\n💰 {cake['price']} ₽"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Купить на сайте", url=cake["url"])]])
        if cake["picture"]:
            await context.bot.send_photo(update.effective_chat.id, cake["picture"], caption=cap, parse_mode="Markdown", reply_markup=kb)
        else:
            await context.bot.send_message(update.effective_chat.id, cap, parse_mode="Markdown", reply_markup=kb)

# =====================
# ОБРАБОТЧИК СООБЩЕНИЙ
# =====================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id

    # 1. МЕНЕДЖЕР ОТВЕЧАЕТ (Reply на сообщение или уведомление)
    if user_id in MANAGER_IDS and update.message.reply_to_message:
        reply_to = update.message.reply_to_message
        # Ищем ID в памяти или парсим из текста уведомления
        record = context.bot_data.get(f"msg:{reply_to.message_id}")
        client_id = record["client_id"] if record else None
        
        if not client_id and reply_to.text and "id: " in reply_to.text:
            try: client_id = int(reply_to.text.split("id: ")[1].split(")")[0])
            except: pass

        if client_id:
            await context.bot.copy_message(client_id, user_id, update.message.message_id, reply_markup=end_chat_kb())
            await update.message.reply_text(f"✅ Отправлено клиенту {client_id}", reply_markup=manager_end_kb(client_id))
        return

    # 2. КЛИЕНТ В РЕЖИМЕ МЕНЕДЖЕРА
    if context.user_data.get("mode") == "manager" or not update.message.text:
        for m_id in MANAGER_IDS:
            fwd = await update.message.forward(m_id)
            context.bot_data[f"msg:{fwd.message_id}"] = {"client_id": user_id, "ts": time.time()}
        if not update.message.text: await update.message.reply_text("📎 Файл передан менеджеру.")
        return

    # 3. ПОИСК И AI
    text = update.message.text
    if context.user_data.pop("awaiting_search", False):
        await load_products()
        res = [o for o in products_cache["offers"] if text.lower() in o["name"].lower()][:3]
        if res: await send_cakes(update, context, res)
        else: await update.message.reply_text("Ничего не нашел 😔", reply_markup=main_kb())
        return

    msg = await update.message.reply_text("⏳")
    hist = context.user_data.setdefault("history", [])
    await load_products()
    
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama-3.3-70b-versatile", 
                      "messages": [{"role": "system", "content": build_system_prompt(products_cache["menu_text"])}] + hist[-MAX_HISTORY:] + [{"role": "user", "content": text}], 
                      "temperature": 0.5}, timeout=30)
            ans = r.json()["choices"][0]["message"]["content"]
            hist.append({"role": "user", "content": text})
            hist.append({"role": "assistant", "content": ans})
            await msg.delete()
            await update.message.reply_text(ans, reply_markup=main_kb())
    except:
        await msg.edit_text("Не удалось связаться с ИИ. Позвать менеджера?", 
                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👨‍💼 Менеджер", callback_data="manager")]]))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "manager":
        context.user_data["mode"] = "manager"
        for m_id in MANAGER_IDS: 
            await context.bot.send_message(m_id, f"🚨 Клиент {query.from_user.full_name} (id: {query.from_user.id}) просит помощи!")
        await query.message.reply_text("👨‍💼 Переключаю на менеджера. Пишите!", reply_markup=end_chat_kb())
    elif query.data == "end_chat":
        context.user_data["mode"] = "ai"
        await query.edit_message_text("✅ Вы вернулись к Ask Cheez.", reply_markup=main_kb())
    elif query.data.startswith("m_end:"):
        cid = int(query.data.split(":")[1])
        if cid in context.application.user_data: context.application.user_data[cid]["mode"] = "ai"
        await context.bot.send_message(cid, "🤝 Менеджер завершил диалог. Я снова на связи!", reply_markup=main_kb())
        await query.edit_message_text(f"✅ Диалог {cid} завершен.")
    elif query.data == "search":
        context.user_data["awaiting_search"] = True
        await query.message.reply_text("🔍 Что ищем? (например: клубника)")
    elif query.data == "popular":
        await load_products()
        await send_cakes(update, context, products_cache["offers"][:3])

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🍰 Привет! Я Ask Cheez.", reply_markup=main_kb())))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.run_polling()

if __name__ == "__main__":
    main()

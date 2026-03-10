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

MANAGER_IDS = [1321630636]

# =====================
# ЛОГИ
# =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

# =====================
# КАЛЕНДАРЬ РФ
# =====================

ru_holidays = holidays.RU()

def is_non_working_day():

    today = datetime.date.today()

    if today.weekday() >= 5:
        return True

    if today in ru_holidays:
        return True

    return False

# =====================
# ПАМЯТЬ БОТА
# =====================

msg_to_client = {}

products_cache = {
    "offers": [],
    "menu_text": "",
    "last_update": 0
}

# =====================
# SYSTEM PROMPT
# =====================

SYSTEM_PROMPT = """
Ты — эксперт Cheesecake Club.

Правила:

1. Доставка только в будни
2. Бесплатная доставка от 3000 ₽
3. Если заказ до 14:00 — доставка сегодня
4. В выходные доставки нет
5. Самовывоз: Рябиновая 32
6. К заказу дарим кусочек чизкейка
7. Заказ можно оформить на сайте
"""

# =====================
# КНОПКИ
# =====================

def main_keyboard():

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("🔥 Популярные чизкейки", callback_data="popular")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="manager")]
    ])

# =====================
# ЗАГРУЗКА YML
# =====================

async def load_products():

    now = time.time()

    if now - products_cache["last_update"] < 900:
        return products_cache["offers"]

    try:

        async with httpx.AsyncClient() as client:

            response = await client.get(YML_URL, timeout=20)

            root = ET.fromstring(response.content)

            offers = []

            for offer in root.findall(".//offer"):

                name = offer.find("name")
                price = offer.find("price")
                picture = offer.find("picture")

                url = offer.attrib.get("url")

                if name is not None and price is not None:

                    offers.append({
                        "name": name.text,
                        "price": price.text,
                        "picture": picture.text if picture is not None else None,
                        "url": url
                    })

            products_cache["offers"] = offers
            products_cache["last_update"] = now

            products_cache["menu_text"] = "\n".join(
                [f"• {o['name']} — {o['price']} ₽" for o in offers[:25]]
            )

    except Exception as e:

        logger.error(f"YML error: {e}")

    return products_cache["offers"]

# =====================
# ОТПРАВКА КАРТОЧЕК
# =====================

async def send_cakes(update, context, cakes):

    for cake in cakes:

        text = f"🍰 {cake['name']}\n💰 {cake['price']} ₽"

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыть в каталоге", url=cake["url"])]
        ])

        if cake["picture"]:

            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=cake["picture"],
                caption=text,
                reply_markup=buttons
            )

        else:

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=buttons
            )

# =====================
# РЕКОМЕНДАЦИИ
# =====================

async def get_recommendations():

    offers = await load_products()

    if not offers:
        return None

    return offers[:3]

def wants_recommendation(text):

    text = text.lower()

    triggers = [
        "посовет",
        "рекомен",
        "что взять",
        "что вкусн",
        "что попробовать",
        "какой чизкейк"
    ]

    return any(t in text for t in triggers)

# =====================
# AI
# =====================

async def ask_ai(message, history):

    menu = products_cache["menu_text"]

    messages = [{
        "role": "system",
        "content": f"{SYSTEM_PROMPT}\n\nМеню:\n{menu}"
    }]

    messages.extend(history[-6:])

    messages.append({
        "role": "user",
        "content": message
    })

    try:

        async with httpx.AsyncClient() as client:

            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": messages,
                    "max_tokens": 500
                },
                timeout=30
            )

            response.raise_for_status()

            data = response.json()

            return data["choices"][0]["message"]["content"]

    except Exception as e:

        logger.error(f"AI error: {e}")

        return "Извините, сейчас я не могу ответить."

# =====================
# START
# =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["mode"] = "ai"
    context.user_data["history"] = []

    await update.message.reply_text(
        "🍰 Привет! Я помощник Cheesecake Club.\n\n"
        "Могу подсказать по десертам и доставке.",
        reply_markup=main_keyboard()
    )

# =====================
# СООБЩЕНИЯ
# =====================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text
    user_id = update.effective_user.id

    if context.user_data.get("mode") == "manager":

        for manager in MANAGER_IDS:

            msg = await context.bot.send_message(
                manager,
                f"💬 Клиент {user_id}:\n\n{text}"
            )

            msg_to_client[f"{manager}:{msg.message_id}"] = user_id

        await update.message.reply_text("Сообщение передано менеджеру")

        return

    if wants_recommendation(text):

        recs = await get_recommendations()

        if recs:

            await update.message.reply_text(
                "🍰 Вот несколько популярных чизкейков:"
            )

            await send_cakes(update, context, recs)

            return

    if "доставк" in text.lower():

        if is_non_working_day():

            await update.message.reply_text(
                "🚚 Доставка работает только в будни.\n\n"
                "Но вы можете забрать заказ сами на Рябиновой 32 по договоренности.",
                reply_markup=main_keyboard()
            )

        else:

            await update.message.reply_text(
                "🚚 Доставка в будни. Бесплатно от 3000 ₽.",
                reply_markup=main_keyboard()
            )

        return

    history = context.user_data.setdefault("history", [])

    history.append({"role": "user", "content": text})

    answer = await ask_ai(text, history)

    history.append({"role": "assistant", "content": answer})

    await update.message.reply_text(
        answer,
        reply_markup=main_keyboard()
    )

# =====================
# МЕДИА
# =====================

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    for manager in MANAGER_IDS:

        await context.bot.forward_message(
            manager,
            chat_id=update.message.chat_id,
            message_id=update.message.message_id
        )

    await update.message.reply_text("Файл отправлен менеджеру")

# =====================
# КНОПКИ
# =====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    if query.data == "manager":

        context.user_data["mode"] = "manager"

        for manager in MANAGER_IDS:

            await context.bot.send_message(
                manager,
                f"🔔 Клиент {query.from_user.id} вызывает менеджера"
            )

        await query.message.reply_text(
            "Менеджер скоро подключится"
        )

    if query.data == "popular":

        recs = await get_recommendations()

        if recs:

            await query.message.reply_text(
                "🔥 Популярные чизкейки:"
            )

            await send_cakes(update, context, recs)

# =====================
# ОТВЕТ МЕНЕДЖЕРА
# =====================

async def manager_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    if user_id not in MANAGER_IDS:
        return

    if not update.message.reply_to_message:
        return

    key = f"{user_id}:{update.message.reply_to_message.message_id}"

    client_id = msg_to_client.get(key)

    if client_id:

        await context.bot.send_message(
            client_id,
            f"👨‍💼 Менеджер:\n\n{update.message.text}"
        )

# =====================
# MAIN
# =====================

def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.add_handler(MessageHandler(filters.PHOTO | filters.Document, media_handler))

    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, manager_reply))

    logger.info("Bot started")

    app.run_polling()

if __name__ == "__main__":
    main()

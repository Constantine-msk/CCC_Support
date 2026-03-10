import os
import logging
import httpx
import xml.etree.ElementTree as ET
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"
MANAGER_IDS = [1321630636]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Оперативная память (сбрасывается при деплое)
active_chats = {}
msg_to_client = {}
products_cache = {"text": "", "last_update": 0}

SYSTEM_PROMPT = """Ты — эксперт Cheesecake Club. Твоя задача — консультировать клиентов.
ГЛАВНЫЕ ПРАВИЛА:
1. Доставка: Будни, от 3000р БЕСПЛАТНО. Заказ до 14:00 — привезем сегодня.
2. ВЫХОДНЫЕ: Доставки нет. НО ОБЯЗАТЕЛЬНО ГОВОРИ: "Но вы можете забрать заказ сами на Рябиновой, 32, по договоренности с нами".
3. ПОДАРОК: К каждому заказу — 1 кусочек чизкейка (на наше усмотрение).
4. КАТАЛОГ: Используй названия и цены только из предоставленного меню.
5. Если клиент ругается или хочет отменить заказ — зови менеджера."""

# --- СЕРВИСНЫЕ ФУНКЦИИ ---

async def get_menu():
    now = time.time()
    if now - products_cache["last_update"] < 900 and products_cache["text"]:
        return products_cache["text"]
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res = await client.get(YML_URL, timeout=15)
            root = ET.fromstring(res.content)
            offers = [f"• {o.find('name').text} — {o.find('price').text} руб." 
                      for o in root.findall(".//offer") if o.find('name') is not None]
            products_cache["text"] = "\n".join(offers[:40])
            products_cache["last_update"] = now
    except Exception as e:
        logger.error(f"YML Error: {e}")
    return products_cache["text"] or "Актуальное меню на сайте: cheesecakeclub.ru/shop"

async def ask_ai(message, history):
    menu = await get_menu()
    messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nМЕНЮ:\n{menu}"}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": message})
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama-3.3-70b-versatile", "messages": messages},
                timeout=25
            )
            return res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "🍰 Немного засмотрелся на витрину... Повторите вопрос или позовите менеджера."

# --- ОБРАБОТЧИКИ ---

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="contact_manager")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    context.user_data["mode"] = "ai"
    await update.message.reply_text("Привет! 🍰 Я помощник Cheesecake Club. Что вам подсказать?", reply_markup=main_kb())

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message: return

    # 1. ЛОГИКА МЕНЕДЖЕРА (Ответ через Reply)
    if user.id in MANAGER_IDS and update.message.reply_to_message:
        key = f"{user.id}:{update.message.reply_to_message.message_id}"
        client_id = msg_to_client.get(key)
        if client_id:
            try:
                caption = f"💬 <b>Менеджер:</b> {update.message.caption or ''}"
                if update.message.photo:
                    await context.bot.send_photo(client_id, update.message.photo[-1].file_id, caption=caption, parse_mode="HTML")
                elif update.message.document:
                    await context.bot.send_document(client_id, update.message.document.file_id, caption=caption, parse_mode="HTML")
                elif

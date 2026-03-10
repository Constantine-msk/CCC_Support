import os
import logging
import httpx
import xml.etree.ElementTree as ET
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, filters, ContextTypes
)

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"
MANAGER_IDS = [1321630636]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Хранилище связей и состояний
msg_to_client = {} 
products_cache = {"text": "", "last_update": 0}

SYSTEM_PROMPT = """Ты — эксперт Cheesecake Club.
ПРАВИЛА:
1. Доставка: Будни, от 3000р БЕСПЛАТНО. Заказ до 14:00 — сегодня.
2. ВЫХОДНЫЕ: Доставки нет. ГОВОРИ: "Но вы можете забрать заказ сами на Рябиновой, 32, по договоренности с нами".
3. ПОДАРОК: К каждому заказу — 1 кусочек чизкейка на наше усмотрение.
4. Если клиент просит позвать человека — используй кнопку или скажи, что менеджер скоро подключится."""

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
    except: pass
    return products_cache["text"] or "Меню на сайте: cheesecakeclub.ru/shop"

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
    except: return "🍰 Извините, я задумался. Попробуйте еще раз."

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="contact_manager")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "ai"
    context.user_data["history"] = []
    await update.message.reply_text("Привет! 🍰 Я ИИ-помощник Cheesecake Club.", reply_markup=main_kb())

async def end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение чата с менеджером и возврат к ИИ"""
    user_id = update.effective_user.id
    # Если пишет менеджер в ответ на сообщение
    if user_id in MANAGER_IDS and update.message.reply_to_message:
        key = f"{user_id}:{update.message.reply_to_message.message_id}"
        client_id = msg_to_client.get(key)
        if client_id:
            await context.bot.send_message(client_id, "🤝 Менеджер завершил диалог. Если у вас появятся вопросы, я снова к вашим услугам! 🍰", reply_markup=main_kb())
            # Сбрасываем режим у клиента через bot_data или передаем через context
            await update.message.reply_text(f"✅ Чат с клиентом {client_id} завершен.")
            return

    # Если пишет сам клиент
    context.user_data["mode"] = "ai"
    await update.message.reply_text("✅ Чат с менеджером завершен. Теперь я (ИИ) снова готов отвечать на ваши вопросы!", reply_markup=main_kb())

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

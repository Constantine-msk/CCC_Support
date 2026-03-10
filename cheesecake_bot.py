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

# Настройки из Railway
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "ВАШ_КЛЮЧ")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"
MANAGER_IDS = [1321630636]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальные переменные (хранятся в RAM Railway)
active_chats = {}
msg_to_client = {}
products_cache = {"text": "", "last_update": 0}

SYSTEM_PROMPT = """Ты — эксперт-консультант Cheesecake Club.
Твоя задача: помогать выбирать чизкейки и отвечать на вопросы о сервисе.

=== ПРАВИЛА ===
- Используй только цены и названия из раздела "АКТУАЛЬНОЕ МЕНЮ".
- Если клиент спрашивает совета, предлагай 2-3 варианта из меню.
- Доставка: от 3000р БЕСПЛАТНО. Заказ до 14:00 — привезем сегодня.
- Выходные: доставки нет, но возможен самовывоз (Рябиновая, 32) по договоренности.
- ПОДАРОК: К каждому заказу мы прилагаем подарок — один кусочек чизкейка на наше усмотрение.
- Если вопрос о жалобе или возврате — переключай на менеджера."""

# --- ФУНКЦИЯ ЗАГРУЗКИ ТОВАРОВ ---

async def get_actual_menu():
    now = time.time()
    # Обновляем кэш раз в 15 минут (900 сек)
    if now - products_cache["last_update"] < 900 and products_cache["text"]:
        return products_cache["text"]

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(YML_URL, timeout=10)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            
            offers = []
            for offer in root.findall(".//offer"):
                name = offer.find("name").text
                price = offer.find("price").text
                offers.append(f"• {name} — {price} руб.")
            
            menu_text = "\n".join(offers[:40]) # Берем первые 40 позиций
            products_cache["text"] = menu_text
            products_cache["last_update"] = now
            return menu_text
        except Exception as e:
            logger.error(f"YML Error: {e}")
            return products_cache["text"] or "Меню доступно на сайте: cheesecakeclub.ru/shop"

# --- AI CORE ---

async def ask_groq(user_message: str, history: list) -> str:
    menu = await get_actual_menu()
    full_prompt = f"{SYSTEM_PROMPT}\n\nАКТУАЛЬНОЕ МЕНЮ И ЦЕНЫ:\n{menu}"
    
    messages = [{"role": "system", "content": full_prompt}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": "llama-3.3-70b-versatile", "messages": messages, "temperature": 0.6},
                timeout=25
            )
            return res.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Groq Error: {e}")
            return "🍰 Ой, я немного отвлекся на дегустацию чизкейка. Пожалуйста, позовите менеджера, если я долго не отвечаю!"

# --- ИНТЕРФЕЙС И ЛОГИКА ---

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Магазин", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("👨‍💼 Менеджер", callback_data="contact_manager")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("Привет! 🍰 Я ИИ-помощник Cheesecake Club. Знаю всё о наших десертах. Что подсказать?", reply_markup=main_kb())

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Логика менеджера (ответы клиентам)
    if user.id in MANAGER_IDS and update.message.reply_to_message:
        key = f"{user.id}:{update.message.reply_to_message.message_id}"
        client_id = msg_to_client.get(key)
        if client_id:
            try:
                if update.message.photo:
                    await context.bot.send_photo(client_id, update.message.photo[-1].file_id, caption=f"💬 <b>Менеджер:</b> {update.message.caption or ''}", parse_mode="HTML")
                else:
                    await context.bot.send_message(client_id, f"💬 <b>Менеджер:</b> {update.message.text}", parse_mode="HTML")
                await update.message.reply_text("✅ Отправлено")
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    # Логика клиента (чат с менеджером)
    mode = context.user_data.get("mode", "ai")
    if user.id in active_chats or mode == "manager_wait":
        context.user_data["mode"] = "manager_active"
        # Пересылка менеджеру
        text = update.message.text or update.message.caption or "[Файл]"
        for m_id in MANAGER_IDS:
            try:
                msg = await context.bot.send_message(m_id, f"📩 <b>От: {user.full_name}</b>\n<code>{user.id}</code>\n\n{text}", parse_mode="HTML")
                msg_to_client[f"{m_id}:{msg.message_id}"] = user.id
            except: pass
        if mode == "manager_wait":
            await update.message.reply_text("✅ Передал менеджеру. Скоро ответим!")
        return

    # Логика ИИ
    if not update.message.text: return
    thinking = await update.message.reply_text("⏳ Ищу в меню...")
    history = context.user_data.get("history", [])
    reply = await ask_groq(update.message.text, history)
    
    history.append({"role": "user", "content": update.message.text})
    history.append({"role": "assistant", "content": reply})
    context.user_data["history"] = history[-10:]
    
    await thinking.delete()
    await update.message.reply_text(reply, reply_

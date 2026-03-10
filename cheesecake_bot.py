import os
import logging
import httpx
import xml.etree.ElementTree as ET
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Чтение переменных из Railway
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"
MANAGER_IDS = [1321630636]

# Проверка токенов при старте
if not BOT_TOKEN or not GROQ_API_KEY:
    logger.error("ОШИБКА: Переменные BOT_TOKEN или GROQ_API_KEY не найдены в Railway Variables!")

# Глобальные хранилища (сотрутся при перезагрузке Railway)
active_chats = {}
msg_to_client = {}
products_cache = {"text": "", "last_update": 0}

SYSTEM_PROMPT = """Ты — эксперт Cheesecake Club. Помогай выбирать чизкейки. 
Доставка от 3000р БЕСПЛАТНО. Заказ до 14:00 — доставка сегодня. 
В выходные доставки нет. К каждому заказу — один кусочек чизкейка в подарок (на наше усмотрение).
Если вопрос сложный — зови менеджера."""

async def get_actual_menu():
    now = time.time()
    if now - products_cache["last_update"] < 900 and products_cache["text"]:
        return products_cache["text"]
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res = await client.get(YML_URL, timeout=15)
            if res.status_code == 200:
                root = ET.fromstring(res.content)
                offers = [f"• {o.find('name').text} — {o.find('price').text} руб." 
                          for o in root.findall(".//offer") if o.find('name') is not None]
                products_cache["text"] = "\n".join(offers[:40])
                products_cache["last_update"] = now
    except Exception as e:
        logger.error(f"YML parsing error: {e}")
    return products_cache["text"] or "Меню доступно на сайте: cheesecakeclub.ru/shop"

async def ask_groq(user_message, history):
    menu = await get_actual_menu()
    messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nАКТУАЛЬНОЕ МЕНЮ:\n{menu}"}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": user_message})
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
        logger.error(f"Groq API Error: {e}")
        return "🍰 Извините, я немного задумался. Попробуйте еще раз или позовите менеджера."

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="contact_manager")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    context.user_data["mode"] = "ai"
    await update.message.reply_text("Привет! 🍰 Я ИИ-помощник Cheesecake Club. Чем могу помочь?", reply_markup=main_kb())

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message: return

    # 1. Ответ менеджера
    if user.id in MANAGER_IDS and update.message.reply_to_message:
        key = f"{user.id}:{update.message.reply_to_message.message_id}"
        client_id = msg_to_client.get(key)
        if client_id:
            try:
                if update.message.photo:
                    await context.bot.send_photo(client_id, update.message.photo[-1].file_id, 
                                               caption=f"💬 <b>Менеджер:</b> {update.message.caption or ''}", parse_mode="HTML")
                else:
                    await context.bot.send_message(client_id, f"💬 <b>Менеджер:</b> {update.message.text}", parse_mode="HTML")
                await update.message.reply_text("✅ Отправлено клиенту")
            except Exception as e:
                logger.error(f"Reply error: {e}")
        return

    # 2. Сообщение клиента менеджеру
    mode = context.user_data.get("mode", "ai")
    if user.id in active_chats or mode == "manager_wait":
        context.user_data["mode"] = "manager_active"
        text = update.message.text or update.message.caption or "[Файл/Фото]"
        for m_id in MANAGER_IDS:
            try:
                msg = await context.bot.send_message(m_id, f"📩 <b>Запрос от {user.full_name}</b>\nID: {user.id}\n\n{text}", parse_mode="HTML")
                msg_to_client[f"{m_id}:{msg.message_id}"] = user.id
            except Exception as e:
                logger.error(f"Notify manager error: {e}")
        if mode == "manager_wait":
            await update.message.reply_text("✅ Передал ваш вопрос менеджеру. Пожалуйста, ожидайте.")
        return

    # 3. Общение с ИИ
    if update.message.text:
        thinking = await update.message.reply_text("⏳")
        history = context.user_data.get("history", [])
        reply = await ask_groq(update.message.text, history)
        history.append({"role": "user", "content": update.message.text})
        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-10:]
        await thinking.delete()
        await update.message.reply_text(reply, reply_markup=main_kb())

async def handle_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "contact_manager":
        context.user_data["mode"] = "manager_wait"
        await query.message.reply_text("👨‍💼 Напишите ваш вопрос, и менеджер ответит вам в этом чате.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_btn))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    logger.info("Бот запускается...")
    app.run_polling()

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

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"
MANAGER_IDS = [1321630636]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Память бота (очищается при перезагрузке Railway)
msg_to_client = {}
products_cache = {"text": "", "last_update": 0}

SYSTEM_PROMPT = """Ты — эксперт Cheesecake Club. Твоя задача — консультировать клиентов.
ГЛАВНЫЕ ПРАВИЛА:
1. Доставка: Будни, от 3000р БЕСПЛАТНО. Заказ до 14:00 — привезем сегодня.
2. ВЫХОДНЫЕ: Доставки нет. НО ОБЯЗАТЕЛЬНО ГОВОРИ: "Но вы можете забрать заказ сами на Рябиновой, 32, по договоренности с нами".
3. ПОДАРОК: К каждому заказу — 1 кусочек чизкейка (на наше усмотрение).
4. КАТАЛОГ: Используй только те названия и цены, которые есть в меню ниже.
5. Если клиент хочет отменить заказ или жалуется — зови менеджера."""

# --- СЛУЖЕБНЫЕ ФУНКЦИИ ---

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
    except Exception as e:
        logger.error(f"AI Error: {e}")
        return "🍰 Извините, я немного отвлекся. Повторите вопрос или позовите менеджера."

# --- ЛОГИКА ТЕЛЕГРАМ ---

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="contact_manager")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    context.user_data["mode"] = "ai"
    await update.message.reply_text("Привет! 🍰 Я помощник Cheesecake Club. Чем могу помочь?", reply_markup=main_kb())

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message: return

    # 1. ОТВЕТ МЕНЕДЖЕРА КЛИЕНТУ (через Reply)
    if user.id in MANAGER_IDS and update.message.reply_to_message:
        key = f"{user.id}:{update.message.reply_to_message.message_id}"
        client_id = msg_to_client.get(key)
        
        if client_id:
            try:
                caption = f"💬 <b>Менеджер:</b> {update.message.caption or ''}"
                if update.message.photo:
                    # Берем самое качественное фото из списка
                    file_id = update.message.photo[-1].file_id
                    await context.bot.send_photo(chat_id=client_id, photo=file_id, caption=caption, parse_mode="HTML")
                elif update.message.document:
                    file_id = update.message.document.file_id
                    await context.bot.send_document(chat_id=client_id, document=file_id, caption=caption, parse_mode="HTML")
                elif update.message.text:
                    await context.bot.send_message(chat_id=client_id, text=f"💬 <b>Менеджер:</b> {update.message.text}", parse_mode="HTML")
                
                await update.message.reply_text("✅ Отправлено клиенту")
            except Exception as e:
                logger.error(f"Send to client error: {e}")
                await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    # 2. ПЕРЕСЫЛКА ОТ КЛИЕНТА МЕНЕДЖЕРУ
    mode = context.user_data.get("mode", "ai")
    if mode in ["manager_wait", "manager_active"]:
        context.user_data["mode"] = "manager_active"
        for m_id in MANAGER_IDS:
            try:
                # Если клиент шлет фото/файл
                if update.message.photo or update.message.document:
                    msg = await update.message.forward(chat_id=m_id)
                    # Доп. уведомление, чтобы менеджер видел ID
                    header = await context.bot.send_message(m_id, f"📩 <b>Медиа от {user.full_name}</b>\nID: <code>{user.id}</code>", parse_mode="HTML")
                    msg_to_client[f"{m_id}:{msg.message_id}"] = user.id
                    msg_to_client[f"{m_id}:{header.message_id}"] = user.id
                else:
                    # Если текст
                    msg = await context.bot.send_message(m_id, f"📩 <b>Запрос от {user.full_name}</b>\nID: <code>{user.id}</code>\n\n{update.message.text}", parse_mode="HTML")
                    msg_to_client[f"{m_id}:{msg.message_id}"] = user.id
            except Exception as e:
                logger.error(f"Forward to manager error: {e}")
        
        if mode == "manager_wait":
            await update.message.reply_text("✅ Передал менеджеру! Скоро ответим.")
        return

    # 3. ОБЩЕНИЕ С ИИ
    if update.message.text:
        thinking = await update.message.reply_text("⏳")
        history = context.user_data.get("history", [])
        reply = await ask_ai(update.message.text, history)
        
        history.append({"role": "user", "content": update.message.text})
        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-10:]
        
        await thinking.delete()
        await update.message.reply_text(reply, reply_markup=main_kb())

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "contact_manager":
        context.user_data["mode"] = "manager_wait"
        await query.message.reply_text("👨‍💼 Напишите ваш вопрос, и менеджер ответит вам в этом чате.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.run_polling()

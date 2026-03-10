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

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"
MANAGER_IDS = [1321630636]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальное хранилище для связей (сбрасывается при перезагрузке)
msg_to_client = {} 
products_cache = {"text": "", "last_update": 0}

SYSTEM_PROMPT = """Ты — эксперт Cheesecake Club. 
ТВОИ ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
1. Доставка: Только в будни. От 3000р БЕСПЛАТНО. Заказ до 14:00 — доставка сегодня.
2. ВЫХОДНЫЕ И ПРАЗДНИКИ: Доставки НЕТ. Если спрашивают про выходные, ОБЯЗАТЕЛЬНО отвечай: "Но вы можете забрать заказ сами на Рябиновой, 32, по договоренности с нами".
3. ПОДАРОК: К каждому заказу дарим 1 кусочек чизкейка (на наше усмотрение).
4. Если клиент хочет позвать человека — используй кнопку в меню."""

# --- ФУНКЦИИ МЕНЮ И ИИ ---

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
    return products_cache["text"] or "Меню доступно на сайте: cheesecakeclub.ru/shop"

async def ask_ai(message, history):
    menu = await get_menu()
    messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nАКТУАЛЬНОЕ МЕНЮ:\n{menu}"}]
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
    except: return "🍰 Извините, я немного задумался. Попробуйте еще раз или позовите менеджера."

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="contact_manager")]
    ])

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "ai"
    context.user_data["history"] = []
    await update.message.reply_text("Привет! 🍰 Я ИИ-помощник Cheesecake Club. Чем могу помочь?", reply_markup=main_kb())

async def end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /end для выхода из режима менеджера"""
    user_id = update.effective_user.id
    
    # Если завершает менеджер через Reply
    if user_id in MANAGER_IDS and update.message.reply_to_message:
        key = f"{user_id}:{update.message.reply_to_message.message_id}"
        client_id = msg_to_client.get(key)
        if client_id:
            await context.bot.send_message(client_id, "🤝 Менеджер завершил диалог. Я снова на связи! 🍰", reply_markup=main_kb())
            await update.message.reply_text(f"✅ Чат с клиентом {client_id} закрыт.")
            return

    # Если завершает сам клиент
    context.user_data["mode"] = "ai"
    await update.message.reply_text("✅ Вы вернулись к общению с ИИ. Чем еще я могу вам помочь?", reply_markup=main_kb())

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message: return

    # 1. МЕНЕДЖЕР ОТВЕЧАЕТ КЛИЕНТУ
    if user.id in MANAGER_IDS and update.message.reply_to_message:
        key = f"{user.id}:{update.message.reply_to_message.message_id}"
        client_id = msg_to_client.get(key)
        if client_id:
            # Копируем сообщение (текст, фото, файл) один в один
            await context.bot.copy_message(chat_id=client_id, from_chat_id=user.id, message_id=update.message.message_id)
            await update.message.reply_text("✅ Доставлено. Используйте /end для закрытия чата.")
        return

    # 2. РЕЖИМ ПРЯМОГО ЧАТА (ИИ ОТКЛЮЧЕН)
    mode = context.user_data.get("mode", "ai")
    if mode == "manager":
        for m_id in MANAGER_IDS:
            try:
                # Пересылаем сообщение менеджерам
                fwd = await update.message.forward(chat_id=m_id)
                msg_to_client[f"{m_id}:{fwd.message_id}"] = user.id
                # Уведомление с ID для удобства
                info = await context.bot.send_message(m_id, f"☝️ Сообщение от {user.full_name} (ID: <code>{user.id}</code>)", parse_mode="HTML")
                msg_to_client[f"{m_id}:{info.message_id}"] = user.id
            except: pass
        return

    # 3. РЕЖИМ ИИ
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
        context.user_data["mode"] = "manager"
        await query.message.reply_text("👨‍💼 Переключаю на менеджера. ИИ временно отключен. Чтобы вернуть ИИ, напишите /end")
        for m_id in MANAGER_IDS:
            await context.bot.send_message(m_id, f"🚨 <b>Внимание:</b> Клиент {query.from_user.full_name} просит менеджера!", parse_mode="HTML")

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("end", end_chat))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.run_polling()

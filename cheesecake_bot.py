import os
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes
)

# Настройки (Рекомендуется использовать переменные окружения)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "ВАШ_КЛЮЧ")
MANAGER_IDS = [1321630636]  # Список ID менеджеров

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Хранилище активных сессий (в продакшене лучше использовать БД)
active_chats = {}  # client_id -> manager_id
msg_to_client = {}  # f"{manager_id}:{msg_id}" -> client_id

SYSTEM_PROMPT = """Ты — вежливый и дружелюбный ИИ-помощник Cheesecake Club.
Отвечай кратко и только по делу. Если вопрос сложный — предлагай позвать менеджера.
Сайт: cheesecakeclub.ru. Адрес: Москва, ул. Рябиновая, 32.
Никогда не выдумывай цены. Отвечай на русском."""

# --- AI ЛОГИКА ---

async def ask_groq(user_message: str, history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-6:])
    messages.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": messages,
                    "max_tokens": 512,
                    "temperature": 0.6
                },
                timeout=20
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error(f"Groq Error: {e}")
            return "Извините, я немного задумался. Пожалуйста, попробуйте позже или свяжитесь с менеджером. 🍰"

# --- КЛАВИАТУРЫ ---

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("🚚 Доставка", url="https://www.cheesecakeclub.ru/deliveryandpayment")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="contact_manager")]
    ])

def manager_action_kb(client_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✋ Взять чат", callback_data=f"take_chat:{client_id}"),
         InlineKeyboardButton("🔚 Закрыть", callback_data=f"end_chat:{client_id}")]
    ])

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data["history"] = []
    context.user_data["mode"] = "ai"
    active_chats.pop(user.id, None)
    
    await update.message.reply_text(
        f"Привет, {user.first_name}! 🎂\nЯ ИИ-помощник Cheesecake Club. Могу рассказать о наших тортах или позвать человека.",
        reply_markup=main_menu_kb()
    )

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "contact_manager":
        context.user_data["mode"] = "manager_wait"
        await query.message.reply_text("Напишите ваш вопрос, и я передам его человеку. Чтобы вернуться к ИИ, нажмите /cancel")

    elif data.startswith("take_chat:"):
        client_id = int(data.split(":")[1])
        active_chats[client_id] = query.from_user.id
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔚 Завершить диалог", callback_data=f"end_chat:{client_id}")]
        ]))
        await context.bot.send_message(client_id, "✅ Менеджер подключился к чату.")

    elif data.startswith("end_chat:"):
        client_id = int(data.split(":")[1])
        active_chats.pop(client_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Чат завершен.")
        await context.bot.send_message(client_id, "Менеджер завершил диалог. Я снова готов отвечать на ваши вопросы!", reply_markup=main_menu_kb())

async def notify_managers(update: Update, context: ContextTypes.DEFAULT_TYPE, client_id: int):
    user = update.effective_user
    # Собираем текст сообщения (даже если это подпись к фото)
    text = update.message.text or update.message.caption or "[Медиа-файл]"
    
    msg_text = (
        f"📩 <b>Новый запрос</b>\n"
        f"От: {user.full_name} (@{user.username})\n"
        f"ID: <code>{client_id}</code>\n\n"
        f"Текст: {text}\n\n"
        f"<i>↩️ Ответьте на ЭТО сообщение (Reply), чтобы клиент получил ответ.</i>"
    )

    for m_id in MANAGER_IDS:
        try:
            if update.message.photo:
                sent = await context.bot.send_photo(
                    m_id, update.message.photo[-1].file_id, 
                    caption=msg_text, parse_mode="HTML", reply_markup=manager_action_kb(client_id)
                )
            else:
                sent = await context.bot.send_message(
                    m_id, msg_text, parse_mode="HTML", reply_markup=manager_action_kb(client_id)
                )
            msg_to_client[f"{m_id}:{sent.message_id}"] = client_id
        except Exception as e:
            logger.error(f"Send to manager error: {e}")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text
    
    # 1. Логика для МЕНЕДЖЕРА
    if user.id in MANAGER_IDS:
        if update.message.reply_to_message:
            key = f"{user.id}:{update.message.reply_to_message.message_id}"
            client_id = msg_to_client.get(key)
            if client_id:
                await context.bot.send_message(client_id, f"<b>Менеджер:</b> {text}", parse_mode="HTML")
                await update.message.reply_text("✅ Отправлено")
                return
        return

    # 2. Логика для КЛИЕНТА
    mode = context.user_data.get("mode", "ai")
    
    # Если уже в чате с менеджером или ждет его
    if user.id in active_chats or mode == "manager_wait":
        context.user_data["mode"] = "manager_active"
        await notify_managers(update, context, user.id)
        if mode == "manager_wait":
            await update.message.reply_text("Запрос отправлен! Менеджер скоро ответит.")
        return

    # 3. Режим ИИ
    thinking = await update.message.reply_text("⏳ Чизкейк-секунду...")
    history = context.user_data.get("history", [])
    
    ai_reply = await ask_groq(text, history)
    
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": ai_reply})
    context.user_data["history"] = history[-10:] # Храним чуть больше для памяти

    await thinking.delete()
    await update.message.reply_text(ai_reply, reply_markup=main_menu_kb())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "ai"
    active_chats.pop(update.effective_user.id, None)
    await update.message.reply_text("Возвращаемся в режим ИИ. Чем еще помочь?", reply_markup=main_menu_kb())

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(handle_button))
    # Обрабатываем и текст, и фото
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, on_message))
    
    print("🚀 Бот запущен...")
    app.run_polling()

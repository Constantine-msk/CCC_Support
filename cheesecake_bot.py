import os
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "ВАШ_КЛЮЧ")
MANAGER_IDS = [1321630636]

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

active_chats = {}   # client_id -> manager_id
msg_to_client = {}  # f"{manager_id}:{msg_id}" -> client_id

SYSTEM_PROMPT = """Ты — вежливый и дружелюбный ИИ-помощник Cheesecake Club.
Отвечай кратко и только по делу. Если вопрос сложный — предлагай позвать менеджера.
Сайт: cheesecakeclub.ru. Адрес: Москва, ул. Рябиновая, 32.
Никогда не выдумывай цены. Отвечай на русском."""


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
            return "Извините, я немного задумался. Попробуйте позже или свяжитесь с менеджером. 🍰"


# --- КЛАВИАТУРЫ ---

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("🚚 Доставка", url="https://www.cheesecakeclub.ru/deliveryandpayment")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="contact_manager")]
    ])

def client_in_chat_kb():
    """Кнопка выхода из чата — показывается клиенту"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Завершить чат с менеджером", callback_data="client_exit")]
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
        f"Привет, {user.first_name}! 🎂\n"
        f"Я ИИ-помощник Cheesecake Club. Могу рассказать о наших тортах или позвать менеджера.",
        reply_markup=main_menu_kb()
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user

    if data == "contact_manager":
        context.user_data["mode"] = "manager_wait"
        await query.message.reply_text(
            "👨‍💼 Напишите ваш вопрос — менеджер ответит в ближайшее время.",
            reply_markup=client_in_chat_kb()
        )

    elif data == "client_exit":
        # Клиент сам выходит из чата
        manager_id = active_chats.pop(user.id, None)
        context.user_data["mode"] = "ai"
        if manager_id:
            try:
                await context.bot.send_message(
                    manager_id,
                    f"ℹ️ Клиент {user.full_name} (@{user.username or user.id}) завершил чат."
                )
            except Exception:
                pass
        await query.message.reply_text(
            "✅ Чат с менеджером завершён. Чем ещё могу помочь?",
            reply_markup=main_menu_kb()
        )

    elif data.startswith("take_chat:"):
        client_id = int(data.split(":")[1])
        active_chats[client_id] = user.id
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔚 Завершить диалог", callback_data=f"end_chat:{client_id}")]
        ]))
        await context.bot.send_message(
            client_id,
            "✅ Менеджер подключился! Продолжайте писать.",
            reply_markup=client_in_chat_kb()
        )

    elif data.startswith("end_chat:"):
        client_id = int(data.split(":")[1])
        active_chats.pop(client_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Чат завершён.")
        try:
            await context.bot.send_message(
                client_id,
                "💬 Менеджер завершил диалог. Если остались вопросы — напишите снова!",
                reply_markup=main_menu_kb()
            )
        except Exception:
            pass


async def notify_managers(update: Update, context: ContextTypes.DEFAULT_TYPE, client_id: int):
    user = update.effective_user
    text = update.message.text or update.message.caption or "[Медиа-файл]"

    msg_text = (
        f"📩 <b>Новый запрос</b>\n"
        f"От: {user.full_name} (@{user.username or 'нет'})\n"
        f"ID: <code>{client_id}</code>\n\n"
        f"💬 {text}\n\n"
        f"<i>↩️ Нажмите Reply на это сообщение чтобы ответить клиенту</i>"
    )

    for m_id in MANAGER_IDS:
        try:
            if update.message.photo:
                sent = await context.bot.send_photo(
                    m_id, update.message.photo[-1].file_id,
                    caption=msg_text, parse_mode="HTML",
                    reply_markup=manager_action_kb(client_id)
                )
            else:
                sent = await context.bot.send_message(
                    m_id, msg_text, parse_mode="HTML",
                    reply_markup=manager_action_kb(client_id)
                )
            msg_to_client[f"{m_id}:{sent.message_id}"] = client_id
        except Exception as e:
            logger.error(f"Send to manager error: {e}")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    # 1. Менеджер отвечает через Reply
    if user.id in MANAGER_IDS:
        if update.message.reply_to_message:
            key = f"{user.id}:{update.message.reply_to_message.message_id}"
            client_id = msg_to_client.get(key)
            if client_id:
                await context.bot.send_message(
                    client_id,
                    f"💬 <b>Менеджер:</b> {text}",
                    parse_mode="HTML"
                )
                await update.message.reply_text("✅ Отправлено")
                return
        await update.message.reply_text(
            "↩️ Нажмите <b>Reply</b> на сообщение клиента чтобы ответить.",
            parse_mode="HTML"
        )
        return

    # 2. Клиент в чате с менеджером или ждёт
    mode = context.user_data.get("mode", "ai")

    if user.id in active_chats or mode in ("manager_wait", "manager_active"):
        context.user_data["mode"] = "manager_active"
        await notify_managers(update, context, user.id)
        if mode == "manager_wait":
            await update.message.reply_text(
                "✅ Запрос отправлен! Менеджер скоро ответит.",
                reply_markup=client_in_chat_kb()
            )
        return

    # 3. Режим ИИ
    thinking = await update.message.reply_text("⏳ Чизкейк-секунду...")
    history = context.user_data.get("history", [])

    ai_reply = await ask_groq(text, history)

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": ai_reply})
    context.user_data["history"] = history[-10:]

    await thinking.delete()
    await update.message.reply_text(ai_reply, reply_markup=main_menu_kb())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manager_id = active_chats.pop(update.effective_user.id, None)
    context.user_data["mode"] = "ai"
    await update.message.reply_text(
        "Возвращаемся в главное меню. Чем ещё помочь?",
        reply_markup=main_menu_kb()
    )


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, on_message))

    print("🚀 Бот запущен...")
    app.run_polling()

import os
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН_ЗДЕСЬ")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "ВАШ_GROQ_КЛЮЧ_ЗДЕСЬ")

MANAGER_IDS = [125741486, 1320920569, 6025401601]

logging.basicConfig(level=logging.INFO)

SYSTEM_PROMPT = """Ты — вежливый и дружелюбный ИИ-помощник интернет-магазина Cheesecake Club.

О магазине:
- Продаём торты и чизкейки на заказ с доставкой по Москве
- Сайт: https://www.cheesecakeclub.ru/shop
- Каталог: https://www.cheesecakeclub.ru/shop
- Доставка и оплата: https://www.cheesecakeclub.ru/deliveryandpayment
- FAQ: https://www.cheesecakeclub.ru/faq
- Адрес: Москва, ул. Рябиновая, 32
- Телефон: +7 (905) 792-02-22
- Email: info@cheesecakeclub.ru

Правила:
- Отвечай только на вопросы связанные с магазином
- Будь кратким и дружелюбным
- Если не знаешь точного ответа — честно скажи и предложи связаться с менеджером
- Если вопрос требует живого человека (жалоба, сложный заказ, возврат) — предложи нажать кнопку "Связаться с менеджером"
- Отвечай на русском языке
- Никогда не выдумывай цены или условия которых не знаешь"""


async def ask_groq(user_message: str, history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-6:])  # последние 3 пары сообщений
    messages.append({"role": "user", "content": user_message})

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
                "max_tokens": 500,
                "temperature": 0.7
            },
            timeout=30
        )
        data = response.json()
        return data["choices"][0]["message"]["content"]


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://www.cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("🚚 Доставка и оплата", url="https://www.cheesecakeclub.ru/deliveryandpayment")],
        [InlineKeyboardButton("❓ FAQ", url="https://www.cheesecakeclub.ru/faq")],
        [InlineKeyboardButton("👨‍💼 Связаться с менеджером", callback_data="contact_manager")],
    ])


def manager_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👨‍💼 Связаться с менеджером", callback_data="contact_manager")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    context.user_data["mode"] = "ai"
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        f"Добро пожаловать в <b>Cheesecake Club</b> 🎂\n"
        f"Торты и чизкейки на заказ с доставкой по Москве.\n\n"
        f"Задайте любой вопрос — я постараюсь помочь!",
        parse_mode="HTML",
        reply_markup=main_menu()
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if query.data == "contact_manager":
        context.user_data["mode"] = "manager"
        await query.message.reply_text(
            "👨‍💼 Напишите ваш вопрос — менеджер ответит в ближайшее время.\n\n"
            "Для отмены — /cancel"
        )

    elif query.data == "main_menu":
        context.user_data["mode"] = "ai"
        await query.message.reply_text(
            "🏠 Главное меню. Чем могу помочь?",
            reply_markup=main_menu()
        )

    elif query.data.startswith("take_chat:"):
        client_id = int(query.data.split(":")[1])
        manager = query.from_user
        await query.edit_message_text(
            f"✅ @{manager.username or manager.first_name} взял чат\n\n"
            f"Отвечайте: <code>/reply {client_id} текст</code>",
            parse_mode="HTML"
        )
        await context.bot.send_message(
            chat_id=client_id,
            text="✅ Менеджер подключился и скоро ответит!"
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "ai"
    await update.message.reply_text(
        "Возвращаемся в главное меню 🏠",
        reply_markup=main_menu()
    )


async def reply_to_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in MANAGER_IDS:
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Использование: /reply <client_id> <текст>")
        return
    try:
        client_id = int(args[0])
        text = " ".join(args[1:])
        await context.bot.send_message(
            chat_id=client_id,
            text=f"💬 <b>Менеджер:</b> {text}",
            parse_mode="HTML"
        )
        await update.message.reply_text("✅ Ответ отправлен")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    # Менеджеры
    if user.id in MANAGER_IDS:
        await update.message.reply_text(
            "Для ответа клиенту:\n<code>/reply client_id текст</code>",
            parse_mode="HTML"
        )
        return

    mode = context.user_data.get("mode", "ai")

    # Режим живого менеджера
    if mode == "manager":
        context.user_data["mode"] = "ai"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✋ Взять чат", callback_data=f"take_chat:{user.id}")]
        ])
        msg = (
            f"💬 <b>Клиент хочет поговорить с менеджером</b>\n\n"
            f"👤 {user.full_name} (@{user.username or 'нет'})\n"
            f"🆔 <code>{user.id}</code>\n\n"
            f"📩 {text}\n\n"
            f"Ответить: <code>/reply {user.id} текст</code>"
        )
        for manager_id in MANAGER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=manager_id,
                    text=msg,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            except Exception:
                pass

        await update.message.reply_text(
            "✅ Ваш запрос передан менеджеру!\n"
            "Ответим в течение 30 минут в рабочее время.",
            reply_markup=main_menu()
        )
        return

    # Режим ИИ
    thinking = await update.message.reply_text("⏳ Думаю...")
    if "history" not in context.user_data:
        context.user_data["history"] = []

    try:
        ai_reply = await ask_groq(text, context.user_data["history"])
        context.user_data["history"].append({"role": "user", "content": text})
        context.user_data["history"].append({"role": "assistant", "content": ai_reply})
        await thinking.delete()
        await update.message.reply_text(ai_reply, reply_markup=manager_button())
    except Exception as e:
        await thinking.delete()
        await update.message.reply_text(
            "😔 Произошла ошибка. Попробуйте позже или свяжитесь с менеджером.",
            reply_markup=manager_button()
        )


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("reply", reply_to_client))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Cheesecake Club AI бот запущен!")
    app.run_polling()

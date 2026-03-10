import os
import logging
import httpx
import xml.etree.ElementTree as ET
import time
import datetime
import holidays

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# =====================
# НАСТРОЙКИ (Переменные окружения в Amvera)
# =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"

# ID менеджеров (Константин)
MANAGER_IDS = [1321630636]

MAX_HISTORY = 10       

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# =====================
# КАЛЕНДАРЬ И ДОСТАВКА
# =====================
ru_holidays = holidays.RU()

def is_non_working_day(date: datetime.date = None) -> bool:
    d = date or datetime.date.today()
    return d.weekday() >= 5 or d in ru_holidays

def next_working_day() -> datetime.date:
    d = datetime.date.today() + datetime.timedelta(days=1)
    while is_non_working_day(d):
        d += datetime.timedelta(days=1)
    return d

products_cache = {"offers": [], "menu_text": "", "last_update": 0}

# =====================
# SYSTEM PROMPT (Ask Cheez AI)
# =====================
def build_system_prompt(menu_text: str = "") -> str:
    today = datetime.date.today()
    weekday_ru = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    
    if not is_non_working_day():
        status = "✅ Сегодня рабочий день, доставка работает."
    else:
        nwd = next_working_day()
        status = f"❌ Сегодня выходной. Ближайшая доставка возможна с {nwd.strftime('%d.%m.%Y')}."

    return f"""Ты — Ask Cheez AI, виртуальный помощник Cheesecake Club Support. 
Сегодня: {today.strftime('%d.%m.%Y')}, {weekday_ru[today.weekday()]}.
{status}

ПРАВИЛА И АКЦИИ:
- Мы дарим бесплатный кусочек чизкейка к каждому заказу! 🍰
- Заказы от 3000 ₽ доставляем бесплатно в пределах МКАД.
- Заказы до 14:00 доставляем в тот же день (в будни).
- В выходные возможен только самовывоз.

КАТАЛОГ:
{menu_text if menu_text else "Загружаю меню..."}
"""

# =====================
# КЛАВИАТУРЫ
# =====================
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог на сайте", url="https://cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("🔍 Найти", callback_data="search"), 
         InlineKeyboardButton("🔥 Популярные", callback_data="popular")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="manager")],
    ])

def end_chat_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Вернуться к Ask Cheez AI", callback_data="end_chat")]])

def manager_end_kb(client_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Завершить диалог", callback_data=f"m_end:{client_id}")]])

# =====================
# РАБОТА С КАТАЛОГОМ (YML)
# =====================
async def load_products():
    now = time.time()
    if now - products_cache["last_update"] < 900: return
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(YML_URL, timeout=20)
            root = ET.fromstring(res.content)
            offers = []
            for o in root.findall(".//offer"):
                offers.append({
                    "name": o.find("name").text if o.find("name") is not None else "Без названия",
                    "price": o.find("price").text if o.find("price") is not None else "0",
                    "picture": o.find("picture").text if o.find("picture") is not None else None,
                    "url": o.attrib.get("url", "https://cheesecakeclub.ru"),
                })
            products_cache.update({
                "offers": offers, 
                "last_update": now, 
                "menu_text": "\n".join([f"• {x['name']} — {x['price']} ₽" for x in offers[:30]])
            })
    except Exception as e: 
        logger.error(f"YML error: {e}")

async def send_cakes(update, context, cakes):
    for cake in cakes:
        cap = f"🍰 *{cake['name']}*\n💰 {cake['price']} ₽"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Купить на сайте", url=cake["url"])]])
        try:
            if cake["picture"]:
                await context.bot.send_photo(update.effective_chat.id, cake["picture"], caption=cap, parse_mode="Markdown", reply_markup=kb)
            else:
                await context.bot.send_message(update.effective_chat.id, cap, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.error(f"Error sending cake: {e}")

# =====================
# ОБРАБОТКА СООБЩЕНИЙ
# =====================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    user_id = update.effective_user.id

    # 1. ОТВЕТ МЕНЕДЖЕРА
    if user_id in MANAGER_IDS and update.message.reply_to_message:
        reply_to = update.message.reply_to_message
        client_id = context.bot_data.get(f"msg:{reply_to.message_id}")
        
        # Если нет в памяти, ищем ID в тексте уведомления
        if not client_id and reply_to.text and "id: " in reply_to.text:
            try: client_id = int(reply_to.text.split("id: ")[1].split(")")[0])
            except: pass

        if client_id:
            await context.bot.copy_message(client_id, user_id, update.message.message_id, reply_markup=end_chat_kb())
            await update.message.reply_text(f"✅ Ответ отправлен клиенту {client_id}", reply_markup=manager_end_kb(client_id))
        return

    # 2. РЕЖИМ МЕНЕДЖЕРА (Клиент пишет)
    if context.user_data.get("mode") == "manager" or not update.message.text:
        for m_id in MANAGER_IDS:
            fwd = await update.message.forward(m_id)
            context.bot_data[f"msg:{fwd.message_id}"] = user_id
        if not update.message.text: 
            await update.message.reply_text("📎 Файл/картинка переданы менеджеру.")
        return

    # 3. ПОИСК ЧИЗКЕЙКОВ
    if context.user_data.pop("awaiting_search", False):
        await load_products()
        query = update.message.text.lower()
        res = [o for o in products_cache["offers"] if query in o["name"].lower()][:3]
        if res:
            await send_cakes(update, context, res)
        else:
            await update.message.reply_text("Ничего не нашлось 😔", reply_markup=main_kb())
        return

    # 4. AI ДИАЛОГ (Ask Cheez AI)
    msg = await update.message.reply_text("⏳")
    hist = context.user_data.setdefault("history", [])
    await load_products()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "system", "content": build_system_prompt(products_cache["menu_text"])}] + hist[-MAX_HISTORY:] + [{"role": "user", "content": update.message.text}],
                    "temperature": 0.5
                }, 
                timeout=30
            )
            ans = r.json()["choices"][0]["message"]["content"]
            hist.append({"role": "user", "content": update.message.text})
            hist.append({"role": "assistant", "content": ans})
            await msg.delete()
            await update.message.reply_text(ans, reply_markup=main_kb())
    except:
        await msg.edit_text("Не удалось связаться с ИИ. Позвать менеджера?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👨‍💼 Менеджер", callback_data="manager")]]))

# =====================
# ОБРАБОТЧИК КНОПОК
# =====================
async def button_handler

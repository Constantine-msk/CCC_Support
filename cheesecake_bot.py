import os
import re
import logging
import httpx
import xml.etree.ElementTree as ET
import time
import datetime
import holidays

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# =====================
# НАСТРОЙКИ
# =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"

MANAGER_IDS = [1321630636]

MAX_HISTORY = 10       # Максимум сообщений в истории
BOT_DATA_TTL = 3600    # Секунд до очистки msg_to_client (1 час)

# =====================
# ЛОГИ
# =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# =====================
# КАЛЕНДАРЬ РФ
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

# =====================
# КЭШ ПРОДУКТОВ
# =====================

products_cache = {
    "offers": [],
    "menu_text": "",
    "last_update": 0
}

# bot_data структура:
# "msg:{msg_id}" -> {"client_id": int, "ts": float}

# =====================
# SYSTEM PROMPT (динамический — вызывается при каждом запросе)
# =====================

def build_system_prompt(menu_text: str = "") -> str:
    today = datetime.date.today()
    weekday_ru = ["понедельник","вторник","среда","четверг","пятница","суббота","воскресенье"]
    day_name = weekday_ru[today.weekday()]
    is_holiday = today in ru_holidays
    holiday_note = f" (праздник: {ru_holidays.get(today)})" if is_holiday else ""

    if not is_non_working_day():
        delivery_status = "✅ Сегодня рабочий день, доставка работает."
    else:
        nwd = next_working_day()
        delivery_status = (
            f"❌ Сегодня выходной/праздник — доставки нет. "
            f"Ближайший рабочий день: {nwd.strftime('%d.%m.%Y')}."
        )

    return f"""Ты — виртуальный помощник «Ask Cheeze» магазина Cheesecake Club.
При первом приветствии представляйся: «Я виртуальный помощник Ask Cheeze».
При приветствии упомяни: статус доставки на сегодня, минимальную сумму заказа (3000 ₽) и подарок — кусочек чизкейка к заказу.
Не упоминай бесплатную доставку в приветствии — только минимальную сумму заказа.

СЕГОДНЯ: {today.strftime('%d.%m.%Y')}, {day_name}{holiday_note}.
{delivery_status}

ПРАВИЛА ЗАКАЗА:
- Минимальная сумма заказа: 3000 ₽ (и для доставки, и для самовывоза)
- Если сумма меньше 3000 ₽: возможен самовывоз или можно прислать своего курьера, но нужно предварительно связаться с нами
- Доставка работает только в будни (пн–пт), без праздников РФ
- Доставка бесплатная при заказе от 3000 ₽
- Заказ до 14:00 — доставка сегодня, после 14:00 — следующий рабочий день
- В выходные/праздники: только самовывоз на Рябиновой, 32 (по договорённости)

РАБОТА С АДРЕСАМИ ДОСТАВКИ:
- Зона доставки: Москва, Внуково, Коммунарка, Одинцово
- Если клиент называет адрес в зоне доставки — подтверди что доставим, и направь оформлять заказ на сайт
- Если адрес вне зоны доставки — вежливо сообщи что пока не доставляем туда, предложи оформить самовывоз на Рябиновой, 32 — приехать самостоятельно или прислать своего курьера (нужно связаться заранее)
- Если рабочий день и адрес в зоне — скажи: "Отлично! Для оформления доставки по адресу [адрес] перейдите на сайт или уточните у менеджера"
- Никогда не обещай конкретное время доставки самостоятельно — это уточняет менеджер

ПРОЧЕЕ:
- К каждому заказу дарим кусочек чизкейка
- Заказ оформляется на сайте cheesecakeclub.ru/shop
- Если не знаешь ответа — предложи позвать менеджера
- Отвечай кратко и по делу

КАТАЛОГ:
{menu_text if menu_text else "Меню временно недоступно."}
"""

# =====================
# КЛАВИАТУРЫ
# =====================

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎂 Каталог", url="https://cheesecakeclub.ru/shop")],
        [InlineKeyboardButton("🔍 Найти чизкейк", callback_data="search"),
         InlineKeyboardButton("🔥 Популярные", callback_data="popular")],
        [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="manager")],
    ])

def end_chat_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Вернуться к ИИ-помощнику", callback_data="end_chat")]
    ])

def manager_end_kb(client_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏁 Завершить диалог с клиентом", callback_data=f"m_end:{client_id}")]
    ])

# =====================
# ЗАГРУЗКА YML
# =====================

async def load_products() -> list:
    now = time.time()
    if now - products_cache["last_update"] < 900:
        return products_cache["offers"]

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(YML_URL, timeout=20)
            root = ET.fromstring(response.content)
            offers = []
            for offer in root.findall(".//offer"):
                name = offer.find("name")
                price = offer.find("price")
                picture = offer.find("picture")
                description = offer.find("description")
                url = offer.attrib.get("url", "https://cheesecakeclub.ru/shop")
                if name is not None and price is not None:
                    offers.append({
                        "name": name.text or "",
                        "price": price.text or "",
                        "picture": picture.text if picture is not None else None,
                        "description": description.text if description is not None else "",
                        "url": url
                    })

        products_cache["offers"] = offers
        products_cache["last_update"] = now
        products_cache["menu_text"] = "\n".join(
            [f"• {o['name']} — {o['price']} ₽" for o in offers[:40]]
        )
        logger.info(f"YML загружен: {len(offers)} позиций")

    except Exception as e:
        logger.error(f"YML error: {e}")

    return products_cache["offers"]

# =====================
# ПОИСК ПО КАТАЛОГУ
# =====================

def search_products(query: str) -> list:
    q = query.lower()
    results = []
    for offer in products_cache["offers"]:
        name = (offer["name"] or "").lower()
        desc = (offer["description"] or "").lower()
        if q in name or q in desc:
            results.append(offer)
    return results[:5]

# =====================
# ОТПРАВКА КАРТОЧЕК
# =====================

async def send_cakes(update: Update, context: ContextTypes.DEFAULT_TYPE, cakes: list):
    for cake in cakes:
        text = f"🍰 *{cake['name']}*\n💰 {cake['price']} ₽"
        if cake.get("description"):
            text += f"\n_{cake['description'][:120]}_"

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("Открыть в каталоге", url=cake["url"])]
        ])

        try:
            if cake.get("picture"):
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=cake["picture"],
                    caption=text,
                    parse_mode="Markdown",
                    reply_markup=buttons
                )
            else:
                raise ValueError("no picture")
        except Exception:
            # Fallback без фото
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=buttons
                )
            except Exception as e:
                logger.error(f"send_cakes fallback error '{cake['name']}': {e}")

# =====================
# AI
# =====================

async def ask_ai(message: str, history: list) -> str | None:
    await load_products()
    menu = products_cache["menu_text"]

    messages = [{"role": "system", "content": build_system_prompt(menu)}]
    messages.extend(history[-(MAX_HISTORY):])
    messages.append({"role": "user", "content": message})

    try:
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
                    "temperature": 0.5
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

    except Exception as e:
        logger.error(f"AI error: {e}")
        return None  # None = сигнал для fallback

# =====================
# ОЧИСТКА bot_data
# =====================

def cleanup_bot_data(bot_data: dict):
    now = time.time()
    stale = [
        k for k, v in bot_data.items()
        if k.startswith("msg:") and isinstance(v, dict)
        and now - v.get("ts", 0) > BOT_DATA_TTL
    ]
    for k in stale:
        del bot_data[k]
    if stale:
        logger.info(f"Очищено {len(stale)} устаревших записей bot_data")

# =====================
# РАСПОЗНАВАНИЕ АДРЕСА
# =====================

# Зона доставки
DELIVERY_ZONES = ["москва", "внуково", "коммунарка", "одинцово"]

# Паттерн для адресов:
# "ул. Ленина 5", "Тверская 10 кв 3", "пр-т Мира, д. 15", "б-р Яна Райниса 3" и т.п.
ADDRESS_PATTERN = re.compile(
    r"""
    (?:
        ул(?:ица|\.)?|пр(?:-т|\.)?|проспект|пр-д|проезд|
        б-р|бульвар|пл(?:ощадь|\.)?|ш(?:оссе|\.)?|
        пер(?:еулок|\.)?|наб(?:ережная|\.)?|
        туп(?:ик|\.)?|аллея|линия
    )
    [\s\.]?
    [А-ЯЁа-яё\w\s\-\.]{2,40}?
    ,?\s*
    (?:д(?:ом|\.)?[\s\.]?)?
    \d{1,4}
    (?:[\/\-]\d{1,4})?
    (?:\s*(?:кв|квартира|оф|офис)\.?\s*\d{1,4})?
    """,
    re.VERBOSE | re.IGNORECASE
)

ADDRESS_PATTERN_SHORT = re.compile(
    r'\b[А-ЯЁ][а-яё]{3,}\s+\d{1,4}(?:[\/\-]\d{1,4})?\b'
)

def extract_address(text: str) -> str | None:
    """Извлекает адрес из текста сообщения."""
    m = ADDRESS_PATTERN.search(text)
    if m:
        return m.group(0).strip()
    m = ADDRESS_PATTERN_SHORT.search(text)
    if m:
        return m.group(0).strip()
    return None

def in_delivery_zone(text: str) -> bool:
    """Проверяет, упоминается ли в тексте зона доставки."""
    text_lower = text.lower()
    # Если город не упомянут явно — считаем Москву по умолчанию
    other_cities = ["петербург", "спб", "краснодар", "екатеринбург", "новосибирск",
                    "казань", "нижний", "самара", "ростов", "уфа", "пермь",
                    "воронеж", "волгоград", "красноярск", "саратов"]
    if any(c in text_lower for c in other_cities):
        return False
    return any(z in text_lower for z in DELIVERY_ZONES) or not any(
        c in text_lower for c in other_cities
    )

def is_delivery_request(text: str) -> bool:
    """Проверяет, похоже ли сообщение на запрос о доставке по адресу."""
    text_lower = text.lower()
    delivery_keywords = ["доставьте", "доставка", "привезите", "заказ", "доставить", "адрес", "привезти"]
    has_keyword = any(k in text_lower for k in delivery_keywords)
    has_address = extract_address(text) is not None
    return has_address or (has_keyword and any(c.isdigit() for c in text))

# =====================
# START
# =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = "ai"
    context.user_data["history"] = []
    context.user_data.pop("awaiting_search", None)
    await load_products()
    await update.message.reply_text(
        "🍰 Привет! Я помощник *Cheesecake Club*.\n\n"
        "Помогу выбрать десерт, расскажу про доставку или позову менеджера.",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

# =====================
# ЕДИНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ
# =====================

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id

    # --- 1. МЕНЕДЖЕР ОТВЕЧАЕТ (reply на пересланное сообщение) ---
    if user_id in MANAGER_IDS and update.message.reply_to_message:
        reply_msg_id = update.message.reply_to_message.message_id
        record = context.bot_data.get(f"msg:{reply_msg_id}")

        if record:
            client_id = record["client_id"]
            try:
                await context.bot.copy_message(
                    chat_id=client_id,
                    from_chat_id=update.message.chat_id,
                    message_id=update.message.message_id,
                )
                await context.bot.send_message(
                    chat_id=client_id,
                    text="Если вопросов больше нет — можете вернуться к ИИ-помощнику.",
                    reply_markup=end_chat_kb()
                )
                await update.message.reply_text(
                    f"✅ Отправлено клиенту {client_id}",
                    reply_markup=manager_end_kb(client_id)
                )
            except Exception as e:
                logger.error(f"Ошибка отправки менеджером: {e}")
                await update.message.reply_text("❌ Не удалось отправить сообщение клиенту.")
        else:
            # Reply есть, но сообщение не от клиента — просто игнорируем
            pass
        return

    # --- 1б. МЕНЕДЖЕР ПИШЕТ БЕЗ REPLY ---
    if user_id in MANAGER_IDS:
        # Пропускаем если ждём стикер
        if context.user_data.get("awaiting_sticker"):
            pass
        else:
            # Проверяем есть ли активные клиенты
            active_clients = [
                v["client_id"] for k, v in context.bot_data.items()
                if k.startswith("msg:") and isinstance(v, dict)
            ]
            if active_clients:
                await update.message.reply_text(
                    "⚠️ Чтобы ответить клиенту, используйте *Reply* (кнопка «Ответить») "
                    "на пересланном сообщении от клиента.\n\n"
                    "Ваше сообщение клиенту *не отправлено*.",
                    parse_mode="Markdown"
                )
            return

    # --- 2. КЛИЕНТ В РЕЖИМЕ МЕНЕДЖЕРА ---
    if context.user_data.get("mode") == "manager":
        cleanup_bot_data(context.bot_data)
        for m_id in MANAGER_IDS:
            try:
                fwd = await update.message.forward(chat_id=m_id)
                context.bot_data[f"msg:{fwd.message_id}"] = {
                    "client_id": user_id,
                    "ts": time.time()
                }
            except Exception as e:
                logger.error(f"Ошибка пересылки менеджеру: {e}")
        return  # Молчим — менеджер сам ответит

    # --- 3. РЕЖИМ ИИ ---

    # Медиа в режиме ИИ — пересылаем менеджеру
    if not update.message.text:
        cleanup_bot_data(context.bot_data)
        for m_id in MANAGER_IDS:
            try:
                fwd = await update.message.forward(chat_id=m_id)
                context.bot_data[f"msg:{fwd.message_id}"] = {
                    "client_id": user_id,
                    "ts": time.time()
                }
            except Exception as e:
                logger.error(f"Ошибка пересылки медиа: {e}")
        await update.message.reply_text("📎 Файл передан менеджеру, он скоро свяжется.")
        return

    text = update.message.text

    # Режим ожидания поискового запроса
    if context.user_data.pop("awaiting_search", False):
        await handle_search_query(update, context, text)
        return

    # Быстрый поиск через команду /find
    if text.startswith("/find "):
        await handle_search_query(update, context, text[6:].strip())
        return

    # Распознавание адреса доставки
    address = extract_address(text)
    if address:
        if not in_delivery_zone(text):
            await update.message.reply_text(
                f"📍 Вижу адрес: *{address}*\n\n"
                "😔 К сожалению, в этот район мы пока не доставляем.\n\n"
                "Доставляем по: Москва, Внуково, Коммунарка, Одинцово.\n\n"
                "Но вы можете оформить самовывоз на *Рябиновой, 32* — "
                "приехать самостоятельно или прислать своего курьера.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍💼 Уточнить у менеджера", callback_data="manager")]
                ])
            )
            return
        elif is_non_working_day():
            await update.message.reply_text(
                f"📍 Вижу адрес: *{address}*\n\n"
                "❌ Сегодня выходной — доставка не работает.\n"
                f"Ближайшая доставка: *{next_working_day().strftime('%d.%m.%Y')}*\n\n"
                "Можете оформить заказ на сайте заранее!",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛍 Оформить заказ", url="https://cheesecakeclub.ru/shop")],
                    [InlineKeyboardButton("👨‍💼 Спросить менеджера", callback_data="manager")]
                ])
            )
            return
        else:
            # Рабочий день, зона доставки — передаём адрес в AI как контекст
            text = f"{text}\n[Клиент указал адрес доставки: {address}]"

    # Обычный диалог с AI
    thinking = await update.message.reply_text("⏳")

    history = context.user_data.setdefault("history", [])
    history.append({"role": "user", "content": text})

    # Ограничиваем размер истории
    if len(history) > MAX_HISTORY * 2:
        context.user_data["history"] = history[-(MAX_HISTORY * 2):]
        history = context.user_data["history"]

    answer = await ask_ai(text, history[:-1])

    await thinking.delete()

    if answer is None:
        # Fallback: AI не ответил — убираем сообщение из истории и предлагаем менеджера
        history.pop()
        await update.message.reply_text(
            "😔 Не могу ответить прямо сейчас. Позвать менеджера?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👨‍💼 Позвать менеджера", callback_data="manager")]
            ])
        )
        return

    history.append({"role": "assistant", "content": answer})
    await update.message.reply_text(answer, reply_markup=main_kb())

# =====================
# ПОИСК (хелпер)
# =====================

async def handle_search_query(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    if not query:
        await update.message.reply_text("Введите название или вкус для поиска.", reply_markup=main_kb())
        return

    await load_products()
    results = search_products(query)

    if not results:
        await update.message.reply_text(
            f"🔍 По запросу «{query}» ничего не найдено.\n\nПопробуйте другое слово или посмотрите весь каталог.",
            reply_markup=main_kb()
        )
        return

    await update.message.reply_text(f"🔍 Нашёл {len(results)} вариант(а) по запросу «{query}»:")
    await send_cakes(update, context, results)

# =====================
# КНОПКИ
# =====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "manager":
        context.user_data["mode"] = "manager"
        context.user_data["history"] = []
        context.user_data.pop("awaiting_search", None)
        for m_id in MANAGER_IDS:
            try:
                await context.bot.send_message(
                    m_id,
                    f"🚨 Клиент {query.from_user.full_name} (id: {user_id}) просит помощи!"
                )
            except Exception as e:
                logger.error(f"Уведомление менеджеру не отправлено: {e}")
        await query.message.reply_text(
            "👨‍💼 Переключаю на менеджера. ИИ отключён.\n\nПишите — передам!",
            reply_markup=end_chat_kb()
        )

    elif data == "end_chat":
        context.user_data["mode"] = "ai"
        context.user_data["history"] = []
        await query.edit_message_text("✅ Вы вернулись к ИИ-помощнику.", reply_markup=main_kb())

    elif data.startswith("m_end:"):
        try:
            client_id = int(data.split(":")[1])
            # Сбрасываем состояние клиента напрямую через application.user_data
            client_data = context.application.user_data.get(client_id)
            if client_data is not None:
                client_data["mode"] = "ai"
                client_data["history"] = []
            await context.bot.send_message(
                client_id,
                "🤝 Менеджер завершил диалог. Если появятся вопросы — я здесь!",
                reply_markup=main_kb()
            )
            await query.edit_message_text(f"✅ Диалог с клиентом {client_id} завершён.")
        except Exception as e:
            logger.error(f"m_end error: {e}")
            await query.edit_message_text("❌ Не удалось завершить диалог.")

    elif data == "popular":
        await load_products()
        offers = products_cache["offers"]
        if offers:
            await query.message.reply_text("🔥 Популярные чизкейки:")
            await send_cakes(update, context, offers[:3])
        else:
            await query.message.reply_text("😔 Каталог временно недоступен.", reply_markup=main_kb())

    elif data == "search":
        context.user_data["awaiting_search"] = True
        await query.message.reply_text(
            "🔍 Напишите название или вкус чизкейка:\n"
            "_(например: «клубника», «шоколад», «манго»)_",
            parse_mode="Markdown"
        )

# =====================
# MAIN
# =====================

async def getsticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Временная команда для получения file_id стикера."""
    if update.effective_user.id not in MANAGER_IDS:
        return
    await update.message.reply_text("Отправьте стикер следующим сообщением.")
    context.user_data["awaiting_sticker"] = True

async def sticker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получает file_id отправленного стикера."""
    if update.effective_user.id not in MANAGER_IDS:
        return
    if context.user_data.pop("awaiting_sticker", False):
        file_id = update.message.sticker.file_id
        await update.message.reply_text(
            f"✅ file_id стикера:\n\n`{file_id}`",
            parse_mode="Markdown"
        )

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getsticker", getsticker))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Sticker.ALL, sticker_handler))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.VOICE)
        & ~filters.COMMAND,
        on_message
    ))

    logger.info("Бот запущен ✅")
    app.run_polling()

if __name__ == "__main__":
    main()

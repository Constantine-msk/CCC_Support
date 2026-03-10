import os, logging, httpx, time, datetime, holidays
import xml.etree.ElementTree as ET
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

# Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YML_URL = "https://cheesecakeclub.ru/tstore/yml/3b6b91d4c8f9e05c6c8a43e5f3d47476.yml"
MANAGER_IDS = [1321630636]
products_cache = {"offers": [], "menu_text": "", "last_update": 0}

# Календарь
def get_status():
    d = datetime.date.today()
    is_work = d.weekday() < 5 and d not in holidays.RU()
    return "✅ Работаем" if is_work else "❌ Выходной"

# ТОВАРЫ
async def load_products():
    if time.time() - products_cache["last_update"] < 900: return
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(YML_URL, timeout=20)
            root = ET.fromstring(r.content)
            offers = []
            for o in root.findall(".//offer"):
                offers.append({
                    "name": o.find("name").text if o.find("name") is not None else "Чизкейк",
                    "price": o.find("price").text if o.find("price") is not None else "0",
                    "picture": o.find("picture").text if o.find("picture") is not None else None,
                    "url": o.attrib.get("url", "https://cheesecakeclub.ru")
                })
            products_cache.update({"offers": offers, "last_update": time.time(), "menu_text": "\n".join([o["name"] for o in offers[:20]])})
    except: pass

async def send_cakes(update, context, cakes):
    for c in cakes:
        cap = f"🍰 *{c['name']}*\n💰 {c['price']} ₽"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Купить", url=c["url"])]])
        if c["picture"]: await context.bot.send_photo(update.effective_chat.id, c["picture"], caption=cap, parse_mode="Markdown", reply_markup=kb)
        else: await context.bot.send_message(update.effective_chat.id, cap, parse_mode="Markdown", reply_markup=kb)

# ЛОГИКА
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    uid, text = update.effective_user.id, update.message.text

    if uid in MANAGER_IDS and update.message.reply_to_message:
        rid = context.bot_data.get(f"m:{update.message.reply_to_message.message_id}")
        if rid:
            await context.bot.copy_message(rid, uid, update.message.message_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ask Cheez AI", callback_data="end_chat")]]))
        return

    if context.user_data.get("m") == 1:
        for m in MANAGER_IDS:
            f = await update.message.forward(m)
            context.bot_data[f"m:{f.message_id}"] = uid
        return

    if context.user_data.pop("s", 0):
        await load_products()
        res = [o for o in products_cache["offers"] if text.lower() in o["name"].lower()][:3]
        await (send_cakes(update, context, res) if res else update.message.reply_text("Нет в наличии"))
        return

    # AI
    h = context.user_data.setdefault("h", [])
    await load_products()
    try:
        async with httpx.AsyncClient() as cl:
            prompt = f"Ты Ask Cheez AI. {get_status()}. Акция: кусочек в подарок! Меню: {products_cache['menu_text']}"
            r = await cl.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json={"model": "llama-3.3-70b-versatile", "messages": [{"role":"system","content":prompt}] + h[-10:] + [{"role":"user","content":text}]}, timeout=30)
            ans = r.json()["choices"][0]["message"]["content"]
            h.append({"role":"user","content":text}); h.append({"role":"assistant","content":ans})
            await update.message.reply_text(ans, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Найти", callback_data="search"), InlineKeyboardButton("🔥 Хиты", callback_data="popular")],[InlineKeyboardButton("👨‍💼 Менеджер", callback_data="manager")]]))
    except: await update.message.reply_text("Ошибка ИИ. Зову менеджера.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👨‍💼 Менеджер", callback_data="manager")]]))

async def btn(update, context):
    q = update.callback_query; await q.answer()
    if q.data == "manager":
        context.user_data["m"] = 1
        for m in MANAGER_IDS: await context.bot.send_message(m, f"🚨 Помощь! id: {q.from_user.id}")
        await q.message.reply_text("👨‍💼 Менеджер на связи. Пишите!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Ask Cheez AI", callback_data="end_chat")]]))
    elif q.data == "end_chat":
        context.user_data.update({"m": 0, "h": []})
        await q.message.reply_text("✅ Ask Cheez AI на связи!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Найти", callback_data="search"), InlineKeyboardButton("🔥 Хиты", callback_data="popular")]]))
    elif q.data == "search":
        context.user_data["s"] = 1
        await q.message.reply_text("🔍 Что ищем?")
    elif q.data == "popular":
        await load_products()
        keys = ["нью-йорк", "дубай", "себастьян"]
        hits = [o for o in products_cache["offers"] if any(k in o["name"].lower() for k in keys)]
        await (send_cakes(update, context, hits[:4]) if hits else send_cakes(update, context, products_cache["offers"][:3]))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🍰 Cheesecake Club Support", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Найти", callback_data="search"), InlineKeyboardButton("🔥 Хиты", callback_data="popular")]]))))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    app.run_polling()

if __name__ == "__main__": main()

import asyncio
import html
import json
import logging
import re
import time
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                           Message, WebAppInfo)
from aiohttp import web

import config
import db
from auth import check_init_data

log = logging.getLogger("cafe")
WEB_DIR = Path(__file__).parent / "webapp"
UPLOAD_DIR = Path(config.UPLOAD_DIR)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
IMG_URL_RE = re.compile(r"^/uploads/[0-9a-f]{32}\.(jpg|png|webp)$")
TG_NAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if config.BOT_TOKEN else None
dp = Dispatcher()
esc = html.escape


def money(n: float) -> str:
    s = str(int(n)) if float(n).is_integer() else f"{n:.2f}".replace(".", ",")
    return f"{s} {config.CURRENCY}"


def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


def client_no(o: dict) -> int:
    """Номер заказа для клиента: порядковый номер именно у этого клиента (1, 2, 3 …).
    Если в записи нет user_seq (старая база / старый db.py), считаем по порядку заказов клиента."""
    n = o.get("user_seq")
    if n:
        return n
    return db.conn().execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND id<=?", (o["user_id"], o["id"])).fetchone()[0]


STATUS_LABEL = {
    "new": "🕐 Ожидает подтверждения",
    "accepted": "✅ Принят, готовится",
    "ready": "🍽 Готов / передан",
    "rejected": "❌ Отклонён",
}


# ======================= тексты и клавиатуры =======================
def order_text(o: dict) -> str:
    items = json.loads(o["items_json"])
    kind = o.get("object_type") or ""
    kind_ico = {"Квартира": "🏠", "Помещение": "🏢"}.get(kind, "📦")
    # админу показываем общий номер (по нему идёт работа) и номер заказа у клиента
    lines = [f"🧾 <b>Заказ №{o['id']}</b> — {STATUS_LABEL[o['status']]}",
             f"<i>Для клиента это заказ №{client_no(o)}</i>", ""]
    # крупно, чтобы сразу было видно, куда едет заказ
    if o["type"] == "delivery":
        lines.append(f"🚚 <b>ДОСТАВКА → {kind_ico} {esc(kind.upper())}</b>" if kind else "🚚 <b>ДОСТАВКА</b>")
    else:
        lines.append("🏃 <b>САМОВЫВОЗ</b>")
    lines.append("")
    for i in items:
        lines.append(f"• {esc(i['name'])} × {i['qty']} — {money(i['price'] * i['qty'])}")
    lines += ["", f"💰 <b>Итого: {money(o['total'])}</b>", ""]
    if o["type"] == "delivery":
        lines.append("📍 <b>Адрес</b>: " + esc(o.get("address") or ""))
        if o.get("place_name"): lines.append("🏷 Название помещения: " + esc(o["place_name"]))
        if o.get("unit_number"): lines.append(f"🔢 {esc(kind or 'Квартира/помещение')} №: " + esc(o["unit_number"]))
        if o.get("entrance"): lines.append("🚪 Подъезд: " + esc(o["entrance"]))
        if o.get("floor"): lines.append("🛗 Этаж: " + esc(o["floor"]))
        if o.get("intercom"): lines.append("🔐 Домофон: " + esc(o["intercom"]))
        if o.get("latitude") is not None and o.get("longitude") is not None:
            lines.append(f'🗺 <a href="https://www.google.com/maps?q={o["latitude"]},{o["longitude"]}">Открыть точку на карте</a>')
        else:
            lines.append("🗺 Точка на карте не указана — ориентируйтесь по адресу")
    lines.append(f"👤 {esc(o['name'])}" + (f" (@{esc(o['username'])})" if o.get("username") else ""))
    lines.append(f"📞 {esc(o['phone'])}")
    if o["comment"]:
        lines.append(f"💬 {esc(o['comment'])}")
    lines.append("\n<i>Ответьте (Reply) на это сообщение — ответ уйдёт клиенту.</i>")
    return "\n".join(lines)


def order_kb(o: dict, with_write: bool = True) -> InlineKeyboardMarkup:
    oid, st = o["id"], o["status"]
    rows = []
    if st == "new":
        rows.append([InlineKeyboardButton(text="✅ Принять", callback_data=f"o:{oid}:acc"),
                     InlineKeyboardButton(text="❌ Отклонить", callback_data=f"o:{oid}:rej")])
    elif st == "accepted":
        label = "🚗 Передан курьеру" if o["type"] == "delivery" else "🍽 Готов к выдаче"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"o:{oid}:rdy")])
        rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"o:{oid}:rej")])
    # Кнопка «Написать клиенту» только по публичному нику: ссылка tg://user?id=… у многих клиентов
    # запрещена настройками приватности, и тогда Telegram отклоняет сообщение целиком.
    uname = o.get("username") or ""
    if with_write and TG_NAME_RE.match(uname):
        rows.append([InlineKeyboardButton(text="💬 Написать клиенту", url=f"https://t.me/{uname}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def client_text(o: dict) -> str:
    n = client_no(o)
    delivery = o["type"] == "delivery"
    return {
        "new": f"🕐 <b>Заказ №{n} оформлен!</b>\nАдминистратор получил его и скоро подтвердит. Мы пришлём сообщение.",
        "accepted": f"✅ <b>Заказ №{n} подтверждён!</b>\nУже готовим. "
                    + ("Привезём по адресу: " + esc(o["address"]) if delivery else "Ждём вас на самовывоз."),
        "ready": f"🍽 <b>Заказ №{n} готов!</b>\n" + ("Курьер уже выехал к вам 🚗" if delivery else "Можно забирать, приятного аппетита!"),
        "rejected": f"❌ <b>К сожалению, заказ №{n} не удалось принять.</b>\n"
                    f"Свяжитесь с администратором: напишите сюда или позвоните {esc(config.CAFE_PHONE)}.",
    }[o["status"]]


def admin_link() -> str:
    return f"https://t.me/{config.ADMIN_USERNAME}" if TG_NAME_RE.match(config.ADMIN_USERNAME) else ""


def main_kb(uid: int) -> InlineKeyboardMarkup:
    rows = []
    if config.WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="🍽 Открыть меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/"))])
    rows.append([InlineKeyboardButton(text="💬 Чат с администратором", callback_data="contact")])
    rows.append([InlineKeyboardButton(text="📞 Позвонить в кафе", callback_data="call")])
    if admin_link():
        rows.append([InlineKeyboardButton(text="✈️ Написать администратору", url=admin_link())])
    if is_admin(uid) and config.WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="⚙️ Управление меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/?admin=1"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def contact_text() -> str:
    lines = ["💬 <b>Связь с кафе</b>", "",
             "• <b>Чат:</b> напишите вопрос одним сообщением прямо здесь — я передам его администратору, "
             "ответ придёт в этот чат.",
             f"• <b>Звонок:</b> {esc(config.CAFE_PHONE)} (нажмите на номер, чтобы позвонить)"]
    if config.ADMIN_USERNAME:
        lines.append(f"• <b>Telegram администратора:</b> @{esc(config.ADMIN_USERNAME)}")
    return "\n".join(lines)


def fallback_contacts() -> str:
    s = f"Позвоните нам: {esc(config.CAFE_PHONE)}"
    if config.ADMIN_USERNAME:
        s += f" или напишите @{esc(config.ADMIN_USERNAME)}"
    return s


# ======================= логика заказов =======================
async def safe_send(chat_id, text, **kw):
    if not bot:
        log.info("[dev] -> %s: %s", chat_id, text)
        return None
    try:
        return await bot.send_message(chat_id, text, **kw)
    except Exception as e:  # пользователь мог заблокировать бота и т.п.
        log.warning("send to %s failed: %s", chat_id, e)
        return None


async def notify_admins_new_order(o: dict) -> int:
    """Шлёт заказ всем админам. Возвращает, скольким удалось доставить."""
    msgs = []
    for admin in config.ADMIN_IDS:
        m = await safe_send(admin, order_text(o), reply_markup=order_kb(o))
        if not m:  # запасной вариант: без кнопки «Написать клиенту»
            m = await safe_send(admin, order_text(o), reply_markup=order_kb(o, with_write=False))
        if m:
            msgs.append([admin, m.message_id])
            # админ может ответить (Reply) на карточку заказа — ответ уйдёт клиенту
            db.add_relay(admin, m.message_id, o["user_id"])
    db.set_admin_msgs(o["id"], msgs)
    if not msgs:
        log.error("Заказ №%s не доставлен ни одному админу (проверьте ADMIN_IDS и что админ нажал /start)", o["id"])
    return len(msgs)


async def apply_status(order_id: int, new: str):
    allowed = {"accepted": {"new"}, "rejected": {"new", "accepted"}, "ready": {"accepted"}}
    o = db.get_order(order_id)
    if not o or o["status"] not in allowed[new]:
        return None
    db.set_order_status(order_id, new)
    o = db.get_order(order_id)
    if bot:
        for chat, mid in json.loads(o["admin_msgs"]):
            try:
                await bot.edit_message_text(order_text(o), chat_id=chat, message_id=mid, reply_markup=order_kb(o))
            except Exception as e:
                log.warning("edit failed: %s", e)
    await safe_send(o["user_id"], client_text(o))
    return o


async def relay_to_admins(m: Message) -> int:
    """Пересылает сообщение клиента всем админам. Возвращает число админов, которым дошло."""
    u = m.from_user
    head = f"💬 <b>{esc(u.full_name)}</b>" + (f" (@{esc(u.username)})" if u.username else "") + \
           "\n<i>Ответьте на это сообщение (Reply), и ответ уйдёт клиенту.</i>"
    sent = 0
    for admin in config.ADMIN_IDS:
        try:
            h = await bot.send_message(admin, head)
            c = await bot.copy_message(admin, m.chat.id, m.message_id)
            db.add_relay(admin, h.message_id, u.id)
            db.add_relay(admin, c.message_id, u.id)
            sent += 1
        except Exception as e:
            log.warning("relay to %s failed: %s", admin, e)
    return sent


# ======================= хендлеры бота =======================
@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    if command.args == "contact":
        await m.answer(contact_text())
        return
    await m.answer(
        f"Добро пожаловать в <b>{esc(config.CAFE_NAME)}</b> 🌿\n"
        "Откройте меню, выберите блюда и оформите заказ.\n\n"
        "Если кнопка меню не открылась, отправьте /start ещё раз.",
        reply_markup=main_kb(m.from_user.id))


@dp.message(Command("contact"))
async def cmd_contact(m: Message):
    await m.answer(contact_text())


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id) or not config.WEBAPP_URL:
        return
    await m.answer("⚙️ Панель администратора: меняйте меню на день, ставьте блюда на стоп.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                       text="⚙️ Управление меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/?admin=1"))]]))


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")


@dp.callback_query(F.data == "contact")
async def cb_contact(c: CallbackQuery):
    await c.message.answer(contact_text())
    await c.answer()


@dp.callback_query(F.data == "call")
async def cb_call(c: CallbackQuery):
    await c.message.answer(f"📞 Телефон кафе: {esc(config.CAFE_PHONE)}\nНажмите на номер, чтобы позвонить.")
    await c.answer()


@dp.callback_query(F.data.startswith("o:"))
async def cb_order(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    _, oid, act = c.data.split(":")
    new = {"acc": "accepted", "rej": "rejected", "rdy": "ready"}[act]
    o = await apply_status(int(oid), new)
    await c.answer("Готово ✓" if o else "Статус уже изменён")


# админ отвечает (reply) на сообщение клиента или на карточку заказа -> уходит клиенту
@dp.message(F.chat.type == "private", F.reply_to_message, F.func(lambda m: is_admin(m.from_user.id)))
async def admin_reply(m: Message):
    uid = db.get_relay(m.chat.id, m.reply_to_message.message_id)
    if not uid:
        await m.answer("Не понял, какому клиенту это адресовано. Ответьте (Reply) на сообщение клиента "
                       "или на карточку его заказа.")
        return
    try:
        await bot.send_message(uid, "💬 <b>Ответ администратора:</b>")
        await bot.copy_message(uid, m.chat.id, m.message_id)
    except Exception as e:
        log.warning("reply to %s failed: %s", uid, e)
        await m.answer("⚠️ Не удалось отправить: клиент мог заблокировать бота.")
        return
    await m.answer("✅ Отправлено клиенту")


# любое сообщение клиента -> всем админам
@dp.message(F.chat.type == "private")
async def client_message(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("Чтобы ответить клиенту, ответьте (Reply) на его сообщение.")
        return
    if await relay_to_admins(m):
        await m.answer("✅ Передали администратору. Ответ придёт сюда.")
    else:
        await m.answer("⚠️ Не получилось передать сообщение администратору. " + fallback_contacts())


# ======================= HTTP API для мини-приложения =======================
def json_err(msg, status=400):
    return web.json_response({"error": msg}, status=status)


def get_user(request: web.Request):
    init = request.headers.get("X-Init-Data", "")
    user = check_init_data(init, config.BOT_TOKEN)
    if user:
        return user
    if config.DEV_MODE and not init:
        return {"id": config.DEV_USER_ID, "first_name": "Тест", "username": "tester"}
    return None


def require_admin(request):
    u = get_user(request)
    return u if u and (is_admin(u["id"]) or (config.DEV_MODE and u["id"] == config.DEV_USER_ID)) else None


async def api_menu(request):
    items = db.list_items()
    return web.json_response({
        "categories": db.grouped(items),
        "today": db.today(),
        "orders_open": db.get_setting("orders_open", "1") == "1",
        "cafe": config.CAFE_NAME, "phone": config.CAFE_PHONE, "admin_username": config.ADMIN_USERNAME,
        "min_delivery": config.MIN_DELIVERY, "bot_username": request.app.get("bot_username", ""),
        "currency": config.CURRENCY, "map": [config.MAP_LAT, config.MAP_LON],
    })


async def api_me(request):
    u = get_user(request)
    if not u:
        return json_err("auth", 401)
    return web.json_response({"id": u["id"], "name": u.get("first_name", ""), "is_admin": bool(require_admin(request))})


def order_public(o):
    return {"id": o["id"], "num": client_no(o), "status": o["status"], "type": o["type"], "total": o["total"],
            "created": o["created"], "items": json.loads(o["items_json"]), "address": o["address"],
            "object_type": o.get("object_type", ""), "place_name": o.get("place_name", ""),
            "unit_number": o.get("unit_number", ""),
            "entrance": o.get("entrance", ""), "floor": o.get("floor", ""),
            "intercom": o.get("intercom", ""), "latitude": o.get("latitude"),
            "longitude": o.get("longitude"), "map_address": o.get("map_address", "")}


async def api_order_create(request):
    user = get_user(request)
    if not user:
        return json_err("Откройте приложение через Telegram", 401)
    if db.get_setting("orders_open", "1") != "1":
        return json_err("Сейчас приём заказов закрыт")
    try:
        d = await request.json()
        if not isinstance(d, dict):
            raise ValueError
    except Exception:
        return json_err("Некорректный запрос")
    typ = d.get("type")
    name = str(d.get("name", "")).strip()[:60]
    phone = str(d.get("phone", "")).strip()[:30]
    address = str(d.get("address", "")).strip()[:250]
    if typ not in ("delivery", "pickup") or not name or len(phone) < 5:
        return json_err("Заполните имя и телефон")
    lat = lon = None
    if typ == "delivery":
        if not address:
            return json_err("Укажите адрес доставки")
        if d.get("object_type") not in ("Квартира", "Помещение"):
            return json_err("Выберите тип объекта")
        if not str(d.get("unit_number", "")).strip():
            return json_err("Укажите номер квартиры или помещения")
        # точка на карте необязательна: если её нет, курьер ориентируется по адресу
        if d.get("latitude") is not None and d.get("longitude") is not None:
            try:
                lat, lon = float(d["latitude"]), float(d["longitude"])
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
            except (TypeError, ValueError):
                return json_err("Некорректная точка на карте")
    raw_items = d.get("items")
    if not isinstance(raw_items, list):
        return json_err("Корзина пуста")
    items, total = [], 0.0
    for it in raw_items[:60]:
        try:
            row = db.get_item(int(it.get("id", 0)))
            qty = int(it.get("qty", 0))
        except (TypeError, ValueError, AttributeError):
            return json_err("Некорректные данные корзины")
        if not row or not row["available"] or not 1 <= qty <= 50:
            return json_err("Некоторые блюда уже недоступны, обновите меню")
        items.append({"id": row["id"], "name": row["name"], "price": row["price"], "qty": qty})
        total += row["price"] * qty
    total = round(total, 2)
    if not items:
        return json_err("Корзина пуста")
    if typ == "delivery" and total < config.MIN_DELIVERY:
        return json_err(f"Минимальная сумма доставки — {money(config.MIN_DELIVERY)}")
    obj_type = str(d.get("object_type", ""))[:20] if typ == "delivery" else ""
    payload = {
        "type": typ, "name": name, "phone": phone, "address": address,
        "comment": str(d.get("comment", "")).strip()[:300],
        "object_type": obj_type,
        "place_name": str(d.get("place_name", "")).strip()[:80] if obj_type == "Помещение" else "",
        "unit_number": str(d.get("unit_number", "")).strip()[:30],
        "entrance": str(d.get("entrance", "")).strip()[:20],
        "floor": str(d.get("floor", "")).strip()[:20],
        "intercom": str(d.get("intercom", "")).strip()[:40],
        "latitude": lat, "longitude": lon,
        "map_address": str(d.get("map_address", "")).strip()[:250] if lat is not None else "",
    }
    oid = db.create_order(user, payload, items, total)
    o = db.get_order(oid)
    notified = await notify_admins_new_order(o)
    await safe_send(user["id"], client_text(o))
    resp = order_public(o)
    resp["notified"] = notified > 0  # False -> заказ сохранён, но админу уведомление не дошло
    return web.json_response(resp)


async def api_order_get(request):
    user = get_user(request)
    try:
        o = db.get_order(int(request.match_info["id"]))
    except ValueError:
        o = None
    if not user or not o or (o["user_id"] != user["id"] and not require_admin(request)):
        return json_err("not found", 404)
    return web.json_response(order_public(o))


# ---- админ ----
async def api_admin_menu(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    return web.json_response({
        "items": db.list_items(), "orders_open": db.get_setting("orders_open", "1") == "1",
        "updated": db.get_setting("menu_updated", ""), "today": db.today()})


async def api_admin_item(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    d = await request.json()
    try:
        d["category"], d["name"] = str(d["category"]).strip()[:60], str(d["name"]).strip()[:120]
        d["desc"] = str(d.get("desc", "")).strip()[:200]
        d["image"] = str(d.get("image", "") or "")
        if d["image"] and not IMG_URL_RE.match(d["image"]):
            raise ValueError
        if not d["category"] or not d["name"] or float(d["price"]) < 0:
            raise ValueError
    except (KeyError, ValueError):
        return json_err("Проверьте поля")
    old = db.get_item(int(d["id"])) if d.get("id") else None
    db.upsert_item(d)
    if old and old["image"] and old["image"] != d["image"]:
        remove_image(old["image"])
    return web.json_response({"ok": True})


async def api_admin_toggle(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    d = await request.json()
    db.set_available(int(d["id"]), bool(d["available"]))
    return web.json_response({"ok": True})


async def api_admin_delete(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    item_id = int((await request.json())["id"])
    it = db.get_item(item_id)
    db.delete_item(item_id)
    if it and it["image"]:
        remove_image(it["image"])
    return web.json_response({"ok": True})


def remove_image(url: str):
    """Удаляет файл фото, если им больше не пользуется ни одно блюдо."""
    if not IMG_URL_RE.match(url or ""):
        return
    used = db.conn().execute("SELECT COUNT(*) FROM items WHERE image=?", (url,)).fetchone()[0]
    if not used:
        (UPLOAD_DIR / url.rsplit("/", 1)[1]).unlink(missing_ok=True)


def cleanup_uploads():
    """Удаляет старые загруженные файлы, на которые не ссылается ни одно блюдо."""
    used = {r[0] for r in db.conn().execute("SELECT image FROM items WHERE image!=''")}
    for p in UPLOAD_DIR.iterdir():
        if f"/uploads/{p.name}" not in used and time.time() - p.stat().st_mtime > 86400:
            p.unlink(missing_ok=True)


def sniff_ext(b: bytes):
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    return None


async def api_admin_upload(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "file":
        return json_err("Файл не получен")
    data = await part.read(decode=False)
    ext = sniff_ext(data)
    if not ext or len(data) > 6 * 1024 * 1024:
        return json_err("Нужна картинка JPG/PNG/WebP до 6 МБ")
    name = f"{uuid.uuid4().hex}.{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    return web.json_response({"image": f"/uploads/{name}"})


async def api_admin_item_image(request):
    """Привязать/убрать фото у существующего блюда: {id, image}"""
    if not require_admin(request):
        return json_err("forbidden", 403)
    d = await request.json()
    img = str(d.get("image") or "")
    it = db.get_item(int(d["id"]))
    if not it or (img and not IMG_URL_RE.match(img)):
        return json_err("Проверьте поля")
    db.set_item_image(it["id"], img)
    if it["image"] and it["image"] != img:
        remove_image(it["image"])
    return web.json_response({"ok": True})


async def api_my_orders(request):
    user = get_user(request)
    if not user:
        return json_err("auth", 401)
    return web.json_response([order_public(o) for o in db.list_user_orders(user["id"])])


async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


async def api_admin_import(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    menu = db.parse_menu_text(str((await request.json()).get("text", "")))
    if not menu:
        return json_err("Не удалось распознать меню. Формат: строка-категория, затем «Блюдо — цена».")
    db.replace_menu(menu)
    return web.json_response({"ok": True, "count": sum(len(v) for v in menu.values())})


async def api_admin_open(request):
    if not require_admin(request):
        return json_err("forbidden", 403)
    db.set_setting("orders_open", "1" if (await request.json())["open"] else "0")
    return web.json_response({"ok": True})


@web.middleware
async def nocache(request, handler):
    resp = await handler(request)
    resp.headers["Cache-Control"] = (
        "public, max-age=2592000, immutable" if request.path.startswith("/uploads/") else "no-store")
    return resp


def build_app() -> web.Application:
    app = web.Application(middlewares=[nocache], client_max_size=8 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_static("/static", WEB_DIR, append_version=False)
    app.router.add_get("/api/orders", api_my_orders)
    app.router.add_post("/api/admin/upload", api_admin_upload)
    app.router.add_post("/api/admin/item_image", api_admin_item_image)
    app.router.add_get("/api/menu", api_menu)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/order", api_order_create)
    app.router.add_get("/api/order/{id}", api_order_get)
    app.router.add_get("/api/admin/menu", api_admin_menu)
    app.router.add_post("/api/admin/item", api_admin_item)
    app.router.add_post("/api/admin/toggle", api_admin_toggle)
    app.router.add_post("/api/admin/delete", api_admin_delete)
    app.router.add_post("/api/admin/import", api_admin_import)
    app.router.add_post("/api/admin/open", api_admin_open)
    # фото блюд (имена файлов — случайные, кэшируем надолго)
    app.router.add_static("/uploads", UPLOAD_DIR, append_version=False)
    return app


async def main():
    logging.basicConfig(level=logging.INFO)
    db.init()
    cleanup_uploads()
    app = build_app()
    if bot:
        me = await bot.get_me()
        app["bot_username"] = me.username  # нужен мини-приложению для кнопки «Чат с администратором»
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("Web server on :%s", config.PORT)
    if bot:
        try:
            await bot.set_my_commands([BotCommand(command="start", description="Меню"),
                                       BotCommand(command="contact", description="Связаться с кафе")])
        except Exception as e:
            log.warning("set_my_commands failed: %s", e)
        if config.WEBAPP_URL:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text="Меню", web_app=WebAppInfo(url=config.WEBAPP_URL + "/")))
        await dp.start_polling(bot)
    else:
        log.warning("BOT_TOKEN не задан — работает только веб-часть (DEV_MODE).")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())

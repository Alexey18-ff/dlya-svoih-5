import json
import re
import sqlite3
import time
from datetime import datetime

import config

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def today() -> str:
    return datetime.now(config.TZ).date().isoformat()


SEED = {
    "Салаты": [
        ("Салат оливье с курицей", 5.5), ("Салат с курицей", 7.5), ("Салат винегрет овощной", 4.5),
        ("Маринованное ассорти из овощей", 4), ("Салат витаминный", 4.5),
    ],
    "Супы": [("Суп куриный с фрикадельками", 6.5), ("Рассольник", 5.5), ("Уха из сёмги", 7)],
    "Горячее": [
        ("Филе индейки под грибным соусом", 13), ("Филе хека под сырным соусом", 14),
        ("Куриное филе в соусе терияки", 9.5), ("Куриное филе в сливочном соусе с грибами", 9.5),
        ("Медальоны из вырезки свинины на гриле под соусом BBQ", 10.5),
    ],
    "Гарниры": [("Картофель отварной с маслом", 3.5), ("Булгур с овощами", 4), ("Греча с маслом", 3.5)],
}


def init():
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL, name TEXT NOT NULL, descr TEXT DEFAULT '',
            price REAL NOT NULL, available INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, user_name TEXT, username TEXT,
            type TEXT, name TEXT, phone TEXT, address TEXT, comment TEXT,
            items_json TEXT, total REAL, status TEXT DEFAULT 'new',
            created INTEGER, admin_msgs TEXT DEFAULT '[]');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS relay(admin_chat INTEGER, msg_id INTEGER, user_id INTEGER,
            PRIMARY KEY(admin_chat, msg_id));
        """
    )
    # Безопасная миграция существующей базы: добавляем новые поля, не удаляя старые заказы.
    existing = {row["name"] for row in c.execute("PRAGMA table_info(orders)")}
    for col, sql_type in (
        ("object_type", "TEXT DEFAULT ''"), ("unit_number", "TEXT DEFAULT ''"),
        ("entrance", "TEXT DEFAULT ''"), ("floor", "TEXT DEFAULT ''"),
        ("intercom", "TEXT DEFAULT ''"), ("latitude", "REAL"),
        ("longitude", "REAL"), ("map_address", "TEXT DEFAULT ''"),
        ("place_name", "TEXT DEFAULT ''"), ("user_seq", "INTEGER"),
    ):
        if col not in existing:
            c.execute(f"ALTER TABLE orders ADD COLUMN {col} {sql_type}")
    existing_items = {row["name"] for row in c.execute("PRAGMA table_info(items)")}
    for col, sql_type in (("image", "TEXT DEFAULT ''"), ("hit", "INTEGER DEFAULT 0")):
        if col not in existing_items:
            c.execute(f"ALTER TABLE items ADD COLUMN {col} {sql_type}")
    _backfill_user_seq(c)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_user_seq ON orders(user_id, user_seq)")
    if c.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0:
        replace_menu(SEED)
    c.commit()


def _backfill_user_seq(c):
    """Старым заказам проставляет номер «внутри клиента» (1, 2, 3 …) по порядку создания."""
    rows = c.execute("SELECT id, user_id FROM orders WHERE user_seq IS NULL ORDER BY id").fetchall()
    if not rows:
        return
    last = {}
    for uid in {r["user_id"] for r in rows}:
        last[uid] = c.execute("SELECT COALESCE(MAX(user_seq), 0) FROM orders WHERE user_id=?", (uid,)).fetchone()[0]
    for r in rows:
        last[r["user_id"]] += 1
        c.execute("UPDATE orders SET user_seq=? WHERE id=?", (last[r["user_id"]], r["id"]))


def get_setting(key, default=""):
    r = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    conn().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))
    conn().commit()


def touch_menu():
    set_setting("menu_updated", today())


def _row(r):
    return {"id": r["id"], "category": r["category"], "name": r["name"], "desc": r["descr"],
            "price": r["price"], "available": bool(r["available"]),
            "image": r["image"] or "", "hit": bool(r["hit"])}


def list_items(only_available=False):
    q = "SELECT * FROM items"
    if only_available:
        q += " WHERE available=1"
    rows = [_row(r) for r in conn().execute(q + " ORDER BY sort, id")]
    return rows


def grouped(items):
    cats: dict[str, list] = {}
    for it in items:
        cats.setdefault(it["category"], []).append(it)
    return [{"name": k, "items": v} for k, v in cats.items()]


def get_item(item_id):
    r = conn().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    return _row(r) if r else None


def upsert_item(d):
    c = conn()
    price = round(float(d["price"]), 2)
    if d.get("id"):
        c.execute("UPDATE items SET category=?, name=?, descr=?, price=?, available=?, image=?, hit=? WHERE id=?",
                  (d["category"], d["name"], d.get("desc", ""), price, int(d.get("available", True)),
                   d.get("image", ""), int(bool(d.get("hit", False))), d["id"]))
    else:
        existing = c.execute("SELECT MAX(sort) m FROM items WHERE category=?", (d["category"],)).fetchone()["m"]
        if existing is None:
            existing = (c.execute("SELECT MAX(sort) m FROM items").fetchone()["m"] or 0)
        # вставляем в конец своей категории: сдвигаем всё что дальше
        c.execute("UPDATE items SET sort = sort + 1 WHERE sort > ?", (existing,))
        c.execute("INSERT INTO items(category,name,descr,price,available,sort,image,hit) VALUES(?,?,?,?,?,?,?,?)",
                  (d["category"], d["name"], d.get("desc", ""), price, int(d.get("available", True)), existing + 1,
                   d.get("image", ""), int(bool(d.get("hit", False)))))
    c.commit()
    touch_menu()


def set_item_image(item_id, image):
    conn().execute("UPDATE items SET image=? WHERE id=?", (image, item_id))
    conn().commit()
    touch_menu()


def list_user_orders(user_id, limit=30):
    rows = conn().execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
    return [dict(r) for r in rows]


def set_available(item_id, available):
    conn().execute("UPDATE items SET available=? WHERE id=?", (int(available), item_id))
    conn().commit()
    touch_menu()


def delete_item(item_id):
    conn().execute("DELETE FROM items WHERE id=?", (item_id,))
    conn().commit()
    touch_menu()


def replace_menu(menu: dict):
    """menu: {категория: [(название, цена[, описание]), ...]}"""
    c = conn()
    # сохраняем фото и «хит» у блюд с тем же названием
    keep = {r["name"].lower(): (r["image"], r["hit"]) for r in c.execute("SELECT name,image,hit FROM items")}
    c.execute("DELETE FROM items")
    n = 0
    for cat, items in menu.items():
        for it in items:
            n += 1
            name, price = it[0], it[1]
            desc = it[2] if len(it) > 2 else ""
            img, hit = keep.get(name.lower(), ("", 0))
            c.execute("INSERT INTO items(category,name,descr,price,available,sort,image,hit) VALUES(?,?,?,?,1,?,?,?)",
                      (cat, name, desc, price, n, img or "", hit or 0))
    c.commit()
    touch_menu()


_PRICE_RE = re.compile(r"^(.*?)[\s.:\-–—]*?(\d+(?:[.,]\d{1,2})?)\s*(?:руб\.?|р\.?|byn|br)?\s*$", re.I)


def parse_menu_text(text: str) -> dict:
    """
    Формат:
        Салаты                      <- строка без цены = категория
        Оливье с курицей — 5.5
        Винегрет 4,5 | со свёклой   <- после | необязательное описание
    """
    menu: dict[str, list] = {}
    cat = None
    for raw in text.splitlines():
        line = raw.strip().lstrip("•*-– ").strip()
        if not line:
            continue
        desc = ""
        if "|" in line:
            line, desc = [x.strip() for x in line.split("|", 1)]
        m = _PRICE_RE.match(line)
        if m and m.group(1).strip() and cat:
            name = m.group(1).strip()
            menu[cat].append((name, float(m.group(2).replace(",", ".")), desc))
        else:
            cat = line.rstrip(":").strip()
            menu.setdefault(cat, [])
    return {k: v for k, v in menu.items() if v}


def create_order(user, d, items, total):
    """Создаёт заказ. id — общий номер в системе, user_seq — порядковый номер заказа у этого клиента."""
    c = conn()
    seq = c.execute("SELECT COALESCE(MAX(user_seq), 0) + 1 FROM orders WHERE user_id=?", (user["id"],)).fetchone()[0]
    cur = c.execute(
        "INSERT INTO orders(user_id,user_name,username,type,name,phone,address,comment,items_json,total,created,"
        "object_type,unit_number,entrance,floor,intercom,latitude,longitude,map_address,place_name,user_seq) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], user.get("first_name", ""), user.get("username", ""), d["type"], d["name"], d["phone"],
         d.get("address", ""), d.get("comment", ""), json.dumps(items, ensure_ascii=False), total, int(time.time()),
         d.get("object_type", ""), d.get("unit_number", ""), d.get("entrance", ""), d.get("floor", ""),
         d.get("intercom", ""), d.get("latitude"), d.get("longitude"), d.get("map_address", ""),
         d.get("place_name", ""), seq))
    c.commit()
    return cur.lastrowid


def get_order(order_id):
    r = conn().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(r) if r else None


def set_order_status(order_id, status):
    conn().execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn().commit()


def set_admin_msgs(order_id, msgs):
    conn().execute("UPDATE orders SET admin_msgs=? WHERE id=?", (json.dumps(msgs), order_id))
    conn().commit()


def add_relay(admin_chat, msg_id, user_id):
    conn().execute("INSERT OR REPLACE INTO relay VALUES(?,?,?)", (admin_chat, msg_id, user_id))
    conn().commit()


def get_relay(admin_chat, msg_id):
    r = conn().execute("SELECT user_id FROM relay WHERE admin_chat=? AND msg_id=?", (admin_chat, msg_id)).fetchone()
    return r["user_id"] if r else None

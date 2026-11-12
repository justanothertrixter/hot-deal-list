#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOT DEAL LIST — горячие предложения недвижимости list.am (Армения).

Сервис по подписке: автоматически собирает объявления о продаже недвижимости
с list.am (раздел «Продажа», категория 62 — все регионы Армении), извлекает
историю изменения цен с каждой страницы объявления, находит те, у которых
текущая цена упала, и показывает их подписчикам с фото и фильтрами.

Стек: Python 3.10+, curl_cffi + BeautifulSoup4, SQLite, Flask, schedule.
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from curl_cffi import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

try:
    import schedule
except ImportError:  # pragma: no cover - schedule ставится из requirements.txt
    schedule = None

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("HOTDEAL_DATA_DIR", BASE_DIR)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    DATA_DIR = BASE_DIR
DB_PATH = os.path.join(DATA_DIR, "hot_deals.db")
LOG_PATH = os.path.join(DATA_DIR, "collector.log")

LANG = "am"                 # армянский интерфейс (приоритет), структура не зависит от языка
CATEGORY = 62               # «Продажа» (вся недвижимость: квартиры, дома, земля, коммерция...)
LIST_URL = f"https://www.list.am/{LANG}/category/{CATEGORY}"

TARGET = int(os.environ.get("HOTDEAL_TARGET", "30"))  # минимум квалифицированных объявлений
DEEP = os.environ.get("HOTDEAL_DEEP") == "1"         # глубокий скан по умолчанию (для CLI)
MIN_LISTINGS = 200          # минимум собранных объявлений до первого «снятия» строгого порога
MAX_PAGES = 20              # защитный предел страниц списка
MAX_LISTINGS_POOL = int(os.environ.get("HOTDEAL_POOL", "800"))  # уникальных объявлений за проход
RECENT_MINUTES = 120        # не пересобирать объявления, собранные недавно (инкрементальность)
PREVIEW_COUNT = 3           # сколько объявлений видно без подписки (бесплатный превью)

# Пороги (drop_from_first, drop_from_prev) в порядке приоритета — от строгого к мягкому.
# По ТЗ снижается только drop_from_first (0.15 -> 0.10 -> 0.05); drop_from_prev фиксирован на 0.10.
THRESHOLD_STEPS = [
    (0.15, 0.10),   # строгий порог из ТЗ
    (0.10, 0.10),   # снижен drop_from_first до 0.10
    (0.05, 0.10),   # снижен drop_from_first до 0.05
]

# Реальные User-Agent Chrome, согласованные с impersonate-профилями curl_cffi.
IMPERSONATE_POOL = [
    ("chrome", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    ("chrome124", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    ("chrome131", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
]

CUR_SYMBOL = {"USD": "$", "AMD": "֏", "EUR": "€", "RUB": "₽", "GBP": "£"}
CUR_PREFIX = {"USD", "EUR", "GBP"}

# Тарифы подписки HOT DEAL LIST (USD).
# Минимальный срок — 3 месяца; годовой тариф пока не продаём.
PLANS = {
    "3m": {"label": "3 месяца", "price": 297, "months": 3, "period": "3 мес", "billing": "3m"},
    "6m": {"label": "6 месяцев", "price": 473, "months": 6, "period": "6 мес", "billing": "6m"},
}

# Секрет для Flask-сессий (в проде — через переменную окружения).
SECRET_KEY = os.environ.get("HOTDEAL_SECRET", "hot-deal-list-dev-secret-change-me")

# Тип недвижимости по ID категории list.am (breadcrumb на странице объявления).
CATEGORY_TYPES = {
    "60": "Квартиры",
    "1386": "Дома",
    "199": "Коммерческая",
    "55": "Земля",
    "173": "Гаражи",
    "61": "Другое",
}

# Эвристика типа по заголовку (fallback, пока не распарсен точный breadcrumb).
TYPE_KEYWORDS = [
    ("Земля", ["հողատարածք", "հողամաս", "հող", "земел", "участок", "land"]),
    ("Гаражи", ["ավտոտնակ", "ավտոկայանատեղ", "ավտոկայան", "гараж", "парков", "garage", "parking"]),
    ("Квартиры", ["բնակարան", "квартир", "apartment", "ստուդիո", "студи", "բնակարանի"]),
    ("Дома", ["առանձնատուն", "քոթեջ", "տուն", "дом", "особняк", "дача", "таунхаус",
              "house", "cottage", "villa", "վիլլա", "դաչա"]),
    ("Коммерческая", ["գրասենյակ", "խանութ", "տարածք", "առևտրային", "արտադրական",
                      "սրահ", "սրճարան", "ռեստորան", "հյուրանոց", "офис", "магазин",
                      "помещ", "коммер", "производ", "склад", "гостиниц", "hote",
                      "office", "shop", "commercial"]),
]


def guess_type(title):
    """Возвращает тип недвижимости по заголовку объявления."""
    t = (title or "").lower()
    for typ, keywords in TYPE_KEYWORDS:
        for kw in keywords:
            if kw in t:
                return typ
    return "Другое"

MONTHS = {
    # армянский
    "հունվար": 1, "փետրվար": 2, "մարտ": 3, "ապրիլ": 4, "մայիս": 5, "հունիս": 6,
    "հուլիս": 7, "օգոստոս": 8, "սեպտեմբեր": 9, "հոկտեմբեր": 10, "նոյեմբեր": 11, "դեկտեմբեր": 12,
    # русский (именительный и родительный падеж)
    "январь": 1, "января": 1, "февраль": 2, "февраля": 2, "март": 3, "марта": 3,
    "апрель": 4, "апреля": 4, "май": 5, "мая": 5, "июнь": 6, "июня": 6,
    "июль": 7, "июля": 7, "август": 8, "августа": 8, "сентябрь": 9, "сентября": 9,
    "октябрь": 10, "октября": 10, "ноябрь": 11, "ноября": 11, "декабрь": 12, "декабря": 12,
    # английский
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")],
)
log = logging.getLogger("hotdeals")

# Единый часовой пояс (иначе Render работает в UTC и время/инкрементальность плывут).
os.environ.setdefault("TZ", "Asia/Yerevan")
try:
    time.tzset()
except AttributeError:  # Windows
    pass

# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            image_url TEXT,
            category TEXT,
            area REAL,
            current_price REAL,
            first_price REAL,
            previous_price REAL,
            currency TEXT,
            region TEXT,
            has_history INTEGER DEFAULT 0,
            drop_from_first REAL,
            drop_from_prev REAL,
            qualified INTEGER DEFAULT 0,
            last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT,
            date TEXT,
            date_iso TEXT,
            price REAL,
            currency TEXT,
            UNIQUE(listing_id, date, price)
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            plan TEXT,
            amount REAL,
            currency TEXT DEFAULT 'USD',
            status TEXT DEFAULT 'active',
            created_at TEXT,
            expires_at TEXT
        );
        """
    )

    # Миграция существующей БД: добавляем недостающие колонки.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    migrations = {
        "image_url": "ALTER TABLE listings ADD COLUMN image_url TEXT",
        "category": "ALTER TABLE listings ADD COLUMN category TEXT",
        "area": "ALTER TABLE listings ADD COLUMN area REAL",
    }
    for col, ddl in migrations.items():
        if col not in existing:
            conn.execute(ddl)
            log.info("Миграция БД: добавлена колонка listings.%s", col)

    conn.commit()
    conn.close()


def set_meta(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_meta(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def get_all_meta():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def count_collected():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM listings").fetchone()
    conn.close()
    return row["c"]


def count_qualified(tf, tp):
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM listings "
        "WHERE has_history=1 AND (drop_from_first>=? OR drop_from_prev>=?)",
        (tf, tp),
    ).fetchone()
    conn.close()
    return row["c"]


def apply_qualification(tf, tp):
    conn = get_conn()
    conn.execute(
        "UPDATE listings SET qualified = "
        "CASE WHEN has_history=1 AND (drop_from_first>=? OR drop_from_prev>=?) "
        "THEN 1 ELSE 0 END",
        (tf, tp),
    )
    conn.commit()
    conn.close()


def save_listing(lid, url, title, current, first, previous, currency, region,
                 has_history, drop_first, drop_prev, image_url=None, category=None,
                 area=None):
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO listings(id, url, title, image_url, category, area, current_price,
                             first_price, previous_price, currency, region,
                             has_history, drop_from_first, drop_from_prev,
                             qualified, last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            image_url=excluded.image_url,
            category=excluded.category,
            area=COALESCE(excluded.area, listings.area),
            current_price=excluded.current_price,
            first_price=excluded.first_price,
            previous_price=excluded.previous_price,
            currency=excluded.currency,
            region=excluded.region,
            has_history=excluded.has_history,
            drop_from_first=excluded.drop_from_first,
            drop_from_prev=excluded.drop_from_prev,
            qualified=0,
            last_seen=excluded.last_seen
        """,
        (lid, url, title, image_url, category, area, current, first, previous,
         currency, region, has_history, drop_first, drop_prev, now),
    )
    conn.commit()
    conn.close()


def save_card_metadata(e):
    """Обновляет метаданные карточки (фото/заголовок/регион/тип) у уже известных объявлений.

    Новые объявления сюда не добавляем: они попадают в БД только после разбора их
    страницы (фаза B), чтобы не раздувать базу «пустыми» записями без истории цен.
    """
    lid = e["id"]
    conn = get_conn()
    existing = conn.execute(
        "SELECT category, area FROM listings WHERE id=?", (lid,)).fetchone()
    if existing is not None:
        # category не затираем точным значением из breadcrumb (если он уже есть).
        category = existing["category"] or guess_type(e["title"])
        area = existing["area"] if existing["area"] else e.get("area")
        conn.execute(
            "UPDATE listings SET image_url=?, title=?, region=?, category=?, "
            "area=COALESCE(?, area) WHERE id=?",
            (e["image"], e["title"], e["region"], category, area, lid),
        )
    conn.commit()
    conn.close()


def save_history(lid, history):
    """history: список dict {date, date_iso, price, currency}, newest-first."""
    conn = get_conn()
    conn.execute("DELETE FROM price_history WHERE listing_id=?", (lid,))
    for h in history:
        conn.execute(
            "INSERT OR IGNORE INTO price_history(listing_id, date, date_iso, price, currency) "
            "VALUES(?,?,?,?,?)",
            (lid, h.get("date"), h.get("date_iso"), h.get("price"), h.get("currency")),
        )
    conn.commit()
    conn.close()


def get_history(lid):
    conn = get_conn()
    rows = conn.execute(
        "SELECT date_iso, price FROM price_history WHERE listing_id=? ORDER BY id ASC",
        (lid,),
    ).fetchall()
    conn.close()
    # В таблицу пишем newest-first; разворачиваем в хронологический порядок.
    out = []
    for r in reversed(rows):
        out.append({"date": r["date_iso"], "price": r["price"]})
    return out


# ----------------------------------------------------------------------------
# Парсинг
# ----------------------------------------------------------------------------

def parse_price(text):
    """Возвращает (amount: float|None, currency: str|None) из строки цены."""
    if not text:
        return None, None
    currency = None
    for code, sym in CUR_SYMBOL.items():
        if sym in text:
            currency = code
            break
    m = re.search(r"\d[\d\s,]*(?:\.\d+)?", text)
    if not m:
        return None, currency
    num = m.group(0).replace(",", "").replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    try:
        return float(num), currency
    except ValueError:
        return None, currency


def parse_date(text):
    """Разбирает 'Սեպտեմբեր 03, 2026' / 'Ноябрь 09, 2026' -> 'YYYY-MM-DD' или None."""
    if not text:
        return None
    t = text.strip()
    m = re.match(r"^\s*([^\d]+?)\s+(\d{1,2}),?\s*(\d{4})\s*$", t)
    if m:
        month_name = m.group(1).strip().lower().strip(".,;: ")
        month = MONTHS.get(month_name)
        day = int(m.group(2))
        year = int(m.group(3))
        if month and 1 <= day <= 31:
            try:
                return f"{year:04d}-{month:02d}-{day:02d}"
            except ValueError:
                return None
    # числовой fallback: YYYY-MM-DD или DD.MM.YYYY
    m2 = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$", t)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    m3 = re.match(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s*$", t)
    if m3:
        return f"{m3.group(3)}-{m3.group(2)}-{m3.group(1)}"
    return None


_AREA_UNITS = (
    r"(քմ|քառ\.?\s*մ|քառակուսի\s*մետր|кв\.?\s*м|кв\.?\s*метр|м²|m²|m2|sq\.?\s*m|sqm|հա|га|ha|sotka|սոտք|соток|сотки|сотка|сот\.?)")
_AREA_RE = re.compile(r"([\d][\d\s.,]*?)\s*" + _AREA_UNITS + r"(?![а-яёa-z])", re.IGNORECASE)


def parse_area(text):
    """Извлекает площадь из строки и приводит к м² (поддержка քմ/кв.м/m², հա/га, соток)."""
    if not text:
        return None
    m = _AREA_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip().replace(" ", "").replace("\u00a0", "").replace("\u202f", "").rstrip(".,")
    unit = m.group(2).lower()
    try:
        if raw.count(",") >= 1 and raw.count(".") == 0:
            parts = raw.split(",")
            if len(parts) == 2 and len(parts[1]) == 3:
                num = float("".join(parts))        # 44,000 -> 44000 (тысячи)
            else:
                num = float(raw.replace(",", "."))  # 82,5 -> 82.5 (дробь)
        else:
            num = float(raw.replace(",", ""))
    except ValueError:
        return None
    if unit.startswith(("հա", "га", "ha")):
        num *= 10000
    elif unit.startswith(("սոտ", "сот", "сотк")):
        num *= 100
    if num <= 0:
        return None
    return num


def parse_list_page(html):
    """Извлекает карточки со страницы списка. Возвращает список dict."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    seen = set()
    for a in soup.select("a[href*='/item/']"):
        m = re.search(r"/item/(\d+)", a.get("href", ""))
        if not m:
            continue
        lid = m.group(1)
        if lid in seen:
            continue
        seen.add(lid)

        title_el = a.select_one("div.l")
        title = title_el.get_text(" ", strip=True) if title_el else ""

        amt_el = a.select_one("span.category-data-list-card__amount")
        amt_text = amt_el.get_text(" ", strip=True) if amt_el else ""
        price, currency = parse_price(amt_text)

        loc_el = a.select_one("div.at.category-data-list-card__location")
        region = loc_el.get_text(" ", strip=True) if loc_el else ""

        attr_el = a.select_one("div.at:not(.category-data-list-card__location)")
        attr_text = attr_el.get_text(" ", strip=True) if attr_el else ""
        area = parse_area(attr_text) or parse_area(title)

        img_el = a.select_one("img[data-post-card-photo='1']") or a.select_one("img")
        image = img_el.get("src") if img_el else None
        if image:
            if image.startswith("//"):
                image = "https:" + image
            elif image.startswith("/"):
                image = "https://www.list.am" + image

        out.append({
            "id": lid,
            "url": f"https://www.list.am/{LANG}/item/{lid}",
            "title": title,
            "price": price,
            "currency": currency,
            "region": region,
            "image": image,
            "area": area,
        })
    return out


def parse_item_page(html):
    """Извлекает историю цен, заголовок и тип недвижимости со страницы объявления."""
    soup = BeautifulSoup(html, "lxml")
    history = []
    ph = soup.find("div", class_="price_history")
    if ph:
        for tr in ph.select("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.select("td")]
            if len(tds) < 2:
                continue
            price, currency = parse_price(tds[1])
            history.append({
                "date": tds[0],
                "date_iso": parse_date(tds[0]),
                "price": price,
                "currency": currency,
            })
    h1 = soup.select_one("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    area = parse_area(title)
    if area is None:
        attr = soup.select_one("div.attr")
        if attr:
            area = parse_area(attr.get_text(" ", strip=True))

    # Основное фото со страницы объявления.
    image = None
    img_el = soup.select_one("img[itemprop='image']") or soup.select_one(
        "img[data-testid^='listing-gallery-image']")
    if img_el and img_el.get("src"):
        src = img_el.get("src")
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://www.list.am" + src
        image = src

    # Тип недвижимости из breadcrumb (первый известный ID категории продажи).
    category = None
    for a in soup.select("a[href*='/category/']"):
        m = re.search(r"/category/(\d+)", a.get("href", ""))
        if m and m.group(1) in CATEGORY_TYPES:
            category = CATEGORY_TYPES[m.group(1)]
            break

    return {"history": history, "title": title, "category": category,
            "image": image, "area": area}


def compute_drops(history):
    """
    history: newest-first список записей.
    Возвращает dict {current, first, previous, currency, drop_from_first, drop_from_prev}
    или None, если меньше двух сопоставимых (одной валюты) записей цены.
    """
    if not history:
        return None
    cur_currency = history[0].get("currency")
    rows = [h for h in history
            if h.get("price") is not None and h.get("currency") == cur_currency]
    if len(rows) < 2:
        return None
    current = rows[0]["price"]
    previous = rows[1]["price"]
    first = rows[-1]["price"]
    if not (first > 0 and previous > 0 and current > 0):
        return None
    return {
        "current": current,
        "first": first,
        "previous": previous,
        "currency": cur_currency,
        "drop_from_first": (first - current) / first,
        "drop_from_prev": (previous - current) / previous,
    }


# ----------------------------------------------------------------------------
# HTTP-клиент с имитацией браузера и адаптацией к блокировкам
# ----------------------------------------------------------------------------

class AdaptiveClient:
    BASE_MIN, BASE_MAX = 2.0, 4.0

    def __init__(self):
        self.idx = 0
        self.delay_min = self.BASE_MIN
        self.delay_max = self.BASE_MAX
        self.success_streak = 0
        self.session = requests.Session()
        self._apply()

    def _apply(self):
        impersonate, ua = IMPERSONATE_POOL[self.idx % len(IMPERSONATE_POOL)]
        self.impersonate = impersonate
        self.session.headers.update({
            "User-Agent": ua,
            "Accept-Language": "hy-AM,hy;q=0.9,ru;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        })

    def _rotate(self):
        self.idx = (self.idx + 1) % len(IMPERSONATE_POOL)
        self.delay_min = min(self.delay_min * 1.5, 15.0)
        self.delay_max = min(self.delay_max * 1.5, 20.0)
        self._apply()
        log.info("Смена отпечатка/User-Agent -> %s (задержка %.1f-%.1f с)",
                 self.impersonate, self.delay_min, self.delay_max)

    def _ease(self):
        # после серии успешных запросов постепенно возвращаемся к базовой задержке
        self.delay_min = max(self.BASE_MIN, self.delay_min * 0.85)
        self.delay_max = max(self.BASE_MAX, self.delay_max * 0.85)

    def sleep(self):
        time.sleep(random.uniform(self.delay_min, self.delay_max))

    def get(self, url, retries=4):
        """Возвращает HTML-текст или None при неудаче. Адаптируется к 403/429."""
        for attempt in range(retries):
            self.sleep()
            try:
                r = self.session.get(url, impersonate=self.impersonate, timeout=30)
                if r.status_code == 200:
                    self.success_streak += 1
                    if self.success_streak >= 12:
                        self._ease()
                        self.success_streak = 0
                    return r.text
                if r.status_code in (403, 429):
                    self.success_streak = 0
                    log.warning("%s -> %s, переключение отпечатка (попытка %d)",
                                r.status_code, url, attempt + 1)
                    self._rotate()
                    continue
                log.warning("%s -> HTTP %s (пропуск)", url, r.status_code)
                return None
            except Exception as e:
                self.success_streak = 0
                log.warning("Ошибка запроса %s: %s", url, e)
                time.sleep(random.uniform(2, 4))
        return None


# ----------------------------------------------------------------------------
# Сбор данных
# ----------------------------------------------------------------------------

_collect_lock = threading.Lock()
_collecting = {"active": False}


def _is_recent(lid):
    """True, если объявление уже собрано недавно (можно пропустить пересбор)."""
    conn = get_conn()
    row = conn.execute("SELECT last_seen FROM listings WHERE id=?", (lid,)).fetchone()
    conn.close()
    if not row or not row["last_seen"]:
        return False
    try:
        ts = datetime.fromisoformat(row["last_seen"])
    except ValueError:
        return False
    return (datetime.now() - ts).total_seconds() < RECENT_MINUTES * 60


def _collect_impl(deep=False):
    init_db()
    set_meta("status", "collecting")
    set_meta("error", "")
    set_meta("started_at", datetime.now().isoformat(timespec="seconds"))

    client = AdaptiveClient()

    # --- Фаза A: собираем уникальные объявления со страниц списка. ---
    # Сначала быстро проходим пагинацию (иначе живой поток list.am «уезжает»,
    # и поздние страницы списка пересекаются с ранними).
    entries_by_id = {}
    page = 1
    while page <= MAX_PAGES and len(entries_by_id) < MAX_LISTINGS_POOL:
        list_url = LIST_URL if page == 1 else f"{LIST_URL}/{page}"
        list_html = client.get(list_url)
        if list_html is None:
            log.warning("Страница списка %d недоступна, пропуск", page)
            page += 1
            continue
        entries = parse_list_page(list_html)
        if not entries:
            log.info("Пустая страница списка, конец пагинации")
            break
        for e in entries:
            entries_by_id.setdefault(e["id"], e)
        log.info("Страница списка %d: %d объявлений (уникальных всего %d)",
                 page, len(entries), len(entries_by_id))
        page += 1

    log.info("Фаза A завершена: %d уникальных объявлений", len(entries_by_id))

    # Обновляем метаданные карточек (фото/заголовок/регион/тип) для всех объявлений
    # списка — это дёшево и заодно бэкфиллит фото/тип у уже собранных записей.
    for e in entries_by_id.values():
        save_card_metadata(e)
    log.info("Метаданные карточек обновлены для %d объявлений", len(entries_by_id))

    # --- Фаза B: посещаем страницы объявлений и собираем историю цен. ---
    items_scanned = 0          # успешно распарсенные страницы объявлений
    items_with_history = 0     # из них с непустой историей цен
    skipped_recent = 0
    structure_error = None
    final = None

    for e in entries_by_id.values():
        if structure_error:
            break

        # Инкрементальность: не пересобираем то, что уже собрано недавно.
        if _is_recent(e["id"]):
            skipped_recent += 1
            continue

        item_html = client.get(e["url"])
        if item_html is None:
            # сеть/блокировка — сохраняем данные карточки без истории
            save_listing(e["id"], e["url"], e["title"], e["price"], None, None,
                         e["currency"], e["region"], 0, None, None,
                         e["image"], guess_type(e["title"]), e.get("area"))
        else:
            items_scanned += 1
            parsed = parse_item_page(item_html)
            history = parsed["history"]
            title = parsed["title"] or e["title"]
            category = parsed.get("category") or guess_type(title)
            image = parsed.get("image") or e["image"]
            area = parsed.get("area") or e.get("area")

            if history:
                items_with_history += 1

            # Требование 4: после 50 распарсенных объявлений без единой истории — стоп.
            if items_scanned >= 50 and items_with_history == 0:
                structure_error = "structure_mismatch"
                snippet_path = os.path.join(BASE_DIR, "structure_error.html")
                try:
                    with open(snippet_path, "w", encoding="utf-8") as f:
                        f.write(item_html[:6000])
                except OSError:
                    snippet_path = "(не удалось сохранить)"
                set_meta("error",
                         f"Структура страницы не соответствует ожиданиям: за {items_scanned} "
                         f"объявлений не найдено ни одной истории цен. Фрагмент HTML: {snippet_path}")
                set_meta("status", "error")
                log.error("СТОП: нет истории цен в %d объявлениях", items_scanned)
                break

            comp = compute_drops(history)
            if comp:
                save_listing(e["id"], e["url"], title, comp["current"], comp["first"],
                             comp["previous"], comp["currency"], e["region"],
                             1, comp["drop_from_first"], comp["drop_from_prev"],
                             image, category, area)
                save_history(e["id"], history)
            else:
                # одна запись цены или валюты не сопоставимы — сохраняем, но без фильтрации
                cur = history[0]["price"] if history else e["price"]
                cur_cur = history[0]["currency"] if history else e["currency"]
                save_listing(e["id"], e["url"], title, cur, None, None,
                             cur_cur, e["region"], 0, None, None, image, category, area)
                save_history(e["id"], history)

        # Периодическое обновление прогресса в meta.
        if items_scanned and items_scanned % 10 == 0:
            set_meta("collected_count", count_collected())
            set_meta("items_with_history", items_with_history)
            set_meta("skipped_recent", skipped_recent)

        # В глубоком режиме периодически пересчитываем квалификацию — каталог растёт на глазах.
        if deep and items_scanned and items_scanned % 25 == 0:
            dtf, dtp = THRESHOLD_STEPS[-1]
            apply_qualification(dtf, dtp)
            set_meta("qualified_count", count_qualified(dtf, dtp))

        # Максимизируем каталог: используем самый мягкий порог (5%/10%), как только
        # он даёт >= TARGET. Точную «скидку» пользователь сузит фильтром min_drop.
        if not deep and count_collected() >= MIN_LISTINGS:
            dtf, dtp = THRESHOLD_STEPS[-1]
            if count_qualified(dtf, dtp) >= TARGET:
                final = (dtf, dtp)
        if final:
            break

    if structure_error:
        return

    # Если порог не определён (мало данных) — берём самый мягкий.
    if final is None:
        final = THRESHOLD_STEPS[-1]
    tf, tp = final
    apply_qualification(tf, tp)
    q = count_qualified(tf, tp)

    set_meta("th_first", tf)
    set_meta("th_prev", tp)
    set_meta("qualified_count", q)
    set_meta("collected_count", count_collected())
    set_meta("items_with_history", items_with_history)
    set_meta("skipped_recent", skipped_recent)
    set_meta("thresholds_relaxed", 0 if (tf, tp) == THRESHOLD_STEPS[0] else 1)
    set_meta("last_updated", datetime.now().isoformat(timespec="seconds"))
    set_meta("status", "done")
    log.info("Сбор завершён: собрано=%d, квалифицировано=%d, пороги=(%.2f, %.2f), "
             "пропущено недавних=%d", count_collected(), q, tf, tp, skipped_recent)


def collect_data(deep=False):
    """Запускает сбор в фоне; возвращает {'status': ...}. deep=True — без раннего выхода."""
    if not _collect_lock.acquire(blocking=False):
        return {"status": "already_running"}
    _collecting["active"] = True
    try:
        _collect_impl(deep=deep)
    except Exception as e:  # noqa: BLE001
        log.exception("Критическая ошибка сбора")
        set_meta("status", "error")
        set_meta("error", f"Ошибка сбора: {e}")
    finally:
        _collecting["active"] = False
        _collect_lock.release()
    return {"status": "done"}


def backfill_deals_metadata():
    """Дозаполняет фото и тип у квалифицированных сделок (заходим на их страницы)."""
    init_db()
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, url FROM listings WHERE qualified=1 AND "
        "(image_url IS NULL OR image_url='' OR category IS NULL OR area IS NULL)").fetchall()
    conn.close()
    if not rows:
        log.info("Дозаполнение не нужно: у всех сделок есть фото, тип и площадь")
        return {"done": 0, "total": 0}

    log.info("Дозаполнение метаданных для %d сделок", len(rows))
    client = AdaptiveClient()
    done = 0
    for r in rows:
        html = client.get(r["url"])
        if html is None:
            continue
        parsed = parse_item_page(html)
        image = parsed.get("image")
        category = parsed.get("category") or guess_type(parsed.get("title") or "")
        area = parsed.get("area")
        if image or category or area:
            conn = get_conn()
            conn.execute(
                "UPDATE listings SET image_url=COALESCE(image_url, ?), "
                "category=COALESCE(category, ?), area=COALESCE(area, ?) WHERE id=?",
                (image, category, area, r["id"]))
            conn.commit()
            conn.close()
            done += 1
    log.info("Дозаполнено сделок: %d из %d", done, len(rows))
    return {"done": done, "total": len(rows)}


# ----------------------------------------------------------------------------
# Презентация
# ----------------------------------------------------------------------------

def fmt_price(amount, currency):
    if amount is None:
        return "—"
    s = f"{amount:,.0f}".replace(",", " ")
    sym = CUR_SYMBOL.get(currency, "")
    if currency in CUR_PREFIX:
        return f"{sym}{s}"
    return f"{s} {sym}"


def fmt_sqm(amount, currency):
    """Цена за м²: до 2 знаков для дешёвых участков, целые для дорогих."""
    if amount is None:
        return None
    if abs(amount) < 10:
        s = f"{amount:,.2f}"
    elif abs(amount) < 100:
        s = f"{amount:,.1f}"
    else:
        s = f"{amount:,.0f}"
    s = s.replace(",", " ")
    sym = CUR_SYMBOL.get(currency, "")
    if currency in CUR_PREFIX:
        return f"{sym}{s}"
    return f"{s} {sym}"


def fmt_pct(x):
    if x is None:
        return None
    return round(x * 100, 1)


def build_deal_dict(r):
    history = get_history(r["id"])
    badge = fmt_pct(r["drop_from_first"])
    if badge is None:
        badge = fmt_pct(r["drop_from_prev"])

    # Цена за квадратный метр и её падение.
    area = r["area"]
    price_sqm = first_sqm = drop_sqm_abs = drop_sqm_pct = None
    if area and area > 0 and r["current_price"] and r["first_price"]:
        price_sqm = r["current_price"] / area
        first_sqm = r["first_price"] / area
        drop_sqm_abs = first_sqm - price_sqm
        if first_sqm > 0:
            drop_sqm_pct = round((drop_sqm_abs / first_sqm) * 100, 1)

    return {
        "id": r["id"],
        "url": r["url"],
        "title": r["title"],
        "region": r["region"],
        "category": r["category"] or "Другое",
        "currency": r["currency"],
        "image": r["image_url"],
        "current_label": fmt_price(r["current_price"], r["currency"]),
        "first_label": fmt_price(r["first_price"], r["currency"]),
        "badge": badge,
        "drop_first": r["drop_from_first"],
        "drop_prev": r["drop_from_prev"],
        "drop_first_label": fmt_pct(r["drop_from_first"]),
        "drop_prev_label": fmt_pct(r["drop_from_prev"]),
        "has_history": len(history) >= 2,
        "history": [[h["date"] or "", h["price"]] for h in history],
        # — цена за м² —
        "area": area,
        "has_area": bool(area and area > 0),
        "price_sqm": fmt_sqm(price_sqm, r["currency"]) if price_sqm else None,
        "first_sqm": fmt_sqm(first_sqm, r["currency"]) if first_sqm else None,
        "drop_sqm_abs": fmt_sqm(drop_sqm_abs, r["currency"]) if drop_sqm_abs else None,
        "drop_sqm_pct": drop_sqm_pct,
    }


def query_deals(filters):
    """Возвращает строки объявлений (qualified=1) с учётом фильтров."""
    where = ["qualified=1"]
    params = []
    f = filters or {}

    if f.get("q"):
        where.append("title LIKE ?")
        params.append(f"%{f['q']}%")
    if f.get("category"):
        where.append("category = ?")
        params.append(f["category"])
    if f.get("region"):
        where.append("region = ?")
        params.append(f["region"])
    if f.get("currency"):
        where.append("currency = ?")
        params.append(f["currency"])
    if f.get("min_price"):
        where.append("current_price >= ?")
        params.append(float(f["min_price"]))
    if f.get("max_price"):
        where.append("current_price <= ?")
        params.append(float(f["max_price"]))
    if f.get("min_drop"):
        d = float(f["min_drop"]) / 100.0
        where.append("(drop_from_first >= ? OR drop_from_prev >= ?)")
        params.extend([d, d])

    sql = ("SELECT * FROM listings WHERE " + " AND ".join(where) +
           " ORDER BY COALESCE(drop_from_first, 0) DESC, COALESCE(drop_from_prev, 0) DESC")
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


# ----------------------------------------------------------------------------
# Подписка
# ----------------------------------------------------------------------------

def is_subscribed():
    return bool(session.get("sub_id"))


def subscription_info():
    sub_id = session.get("sub_id")
    if not sub_id:
        return None
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    conn.close()
    return row


# ----------------------------------------------------------------------------
# Flask-приложение
# ----------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=400)

# Под gunicorn main() не вызывается — схему БД гарантируем при импорте.
init_db()


@app.route("/")
def index():
    meta = get_all_meta()
    th_first = float(meta.get("th_first", THRESHOLD_STEPS[0][0]))
    th_prev = float(meta.get("th_prev", THRESHOLD_STEPS[0][1]))
    relaxed = meta.get("thresholds_relaxed", "0") == "1"
    collecting = _collecting["active"] or meta.get("status") == "collecting"

    last_updated_display = "—"
    if meta.get("last_updated"):
        try:
            last_updated_display = datetime.fromisoformat(
                meta["last_updated"]).strftime("%d.%m.%Y %H:%M:%S")
        except ValueError:
            last_updated_display = meta["last_updated"]

    sub = subscription_info()

    return render_template(
        "index.html",
        plans=PLANS,
        meta=meta,
        th_first=th_first,
        th_prev=th_prev,
        relaxed=relaxed,
        collecting=collecting,
        error=meta.get("error", ""),
        target=TARGET,
        last_updated_display=last_updated_display,
        is_subscribed=is_subscribed(),
        subscription=sub,
        qualified_count=int(meta.get("qualified_count", 0) or 0),
        collected_count=int(meta.get("collected_count", 0) or 0),
    )


@app.route("/api/deals")
def api_deals():
    filters = {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", "").strip(),
        "region": request.args.get("region", "").strip(),
        "currency": request.args.get("currency", "").strip(),
        "min_price": request.args.get("min_price", "").strip(),
        "max_price": request.args.get("max_price", "").strip(),
        "min_drop": request.args.get("min_drop", "").strip(),
    }
    rows = query_deals(filters)
    total = len(rows)

    if is_subscribed():
        deals = [build_deal_dict(r) for r in rows]
        return jsonify({"subscribed": True, "locked": False, "total": total, "deals": deals})

    preview = [build_deal_dict(r) for r in rows[:PREVIEW_COUNT]]
    return jsonify({
        "subscribed": False,
        "locked": True,
        "total": total,
        "preview": preview,
        "locked_count": max(0, total - len(preview)),
    })


@app.route("/api/filters")
def api_filters():
    conn = get_conn()
    regions = [r["region"] for r in conn.execute(
        "SELECT DISTINCT region FROM listings WHERE qualified=1 AND region != '' "
        "ORDER BY region").fetchall()]
    currencies = [r["currency"] for r in conn.execute(
        "SELECT DISTINCT currency FROM listings WHERE qualified=1 "
        "AND currency IS NOT NULL ORDER BY currency").fetchall()]
    categories = [r["category"] for r in conn.execute(
        "SELECT DISTINCT category FROM listings WHERE qualified=1 "
        "AND category IS NOT NULL ORDER BY category").fetchall()]
    conn.close()
    return jsonify({"regions": regions, "currencies": currencies, "categories": categories})


@app.route("/api/me")
def api_me():
    sub = subscription_info()
    if sub:
        return jsonify({
            "subscribed": True,
            "plan": sub["plan"],
            "plan_label": PLANS.get(sub["plan"], {}).get("label", sub["plan"]),
            "email": sub["email"],
            "amount": sub["amount"],
            "created_at": sub["created_at"],
            "expires_at": sub["expires_at"],
        })
    return jsonify({"subscribed": False})


@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    data = request.get_json(silent=True) or {}
    plan = data.get("plan", "monthly")
    if plan not in PLANS:
        plan = "monthly"
    email = (data.get("email") or "").strip() or None
    p = PLANS[plan]
    now = datetime.now().isoformat(timespec="seconds")
    expires = (datetime.now() + timedelta(days=30 * p["months"])).isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO subscriptions(email, plan, amount, currency, status, created_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (email, plan, p["price"], "USD", "active", now, expires),
    )
    sub_id = cur.lastrowid
    conn.commit()
    conn.close()

    session.permanent = True
    session["sub_id"] = sub_id
    session["plan"] = plan
    log.info("Новая подписка: id=%s plan=%s email=%s (ДЕМО-режим)", sub_id, plan, email)
    return jsonify({"ok": True, "plan": plan, "demo": True, "sub_id": sub_id})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    sub_id = session.get("sub_id")
    if sub_id:
        conn = get_conn()
        conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (sub_id,))
        conn.commit()
        conn.close()
    session.pop("sub_id", None)
    session.pop("plan", None)
    return jsonify({"ok": True})


@app.route("/refresh", methods=["POST"])
def refresh():
    if _collecting["active"]:
        return jsonify({"started": False, "collecting": True})
    threading.Thread(target=collect_data, daemon=True).start()
    return jsonify({"started": True, "collecting": True})


@app.route("/api/status")
def api_status():
    meta = get_all_meta()
    return jsonify({
        "collecting": _collecting["active"] or meta.get("status") == "collecting",
        "status": meta.get("status", "idle"),
        "last_updated": meta.get("last_updated", ""),
        "collected_count": int(meta.get("collected_count", 0) or 0),
        "qualified_count": int(meta.get("qualified_count", 0) or 0),
        "items_with_history": int(meta.get("items_with_history", 0) or 0),
        "error": meta.get("error", ""),
    })


def scheduler_loop():
    if schedule is None:
        log.error("Библиотека schedule не установлена — планировщик отключён")
        return
    # Каждые 6 часов — глубокое обогащение каталога (без раннего выхода на 30).
    schedule.every(6).hours.do(lambda: collect_data(deep=True))
    log.info("Планировщик запущен: глубокое обновление каждые 6 часов")
    while True:
        schedule.run_pending()
        time.sleep(30)


def startup_collect():
    time.sleep(1)  # даём Flask-серверу подняться
    collect_data()  # быстрый старт: гарантируем >= 30 сделок сразу


def main():
    parser = argparse.ArgumentParser(description="HOT DEAL LIST — горячие предложения list.am")
    parser.add_argument("--collect", action="store_true",
                        help="однократно собрать данные и выйти (без веб-сервера)")
    parser.add_argument("--deep", action="store_true",
                        help="глубокий скан (без раннего выхода), использовать с --collect")
    parser.add_argument("--backfill", action="store_true",
                        help="дозаполнить фото/тип у квалифицированных сделок и выйти")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")))
    args = parser.parse_args()

    init_db()

    if args.collect:
        print(json.dumps(collect_data(deep=args.deep or DEEP), ensure_ascii=False))
        return

    if args.backfill:
        print(json.dumps(backfill_deals_metadata(), ensure_ascii=False))
        return

    # Первый сбор при старте сервера + фоновая задача каждые 6 часов.
    threading.Thread(target=startup_collect, daemon=True).start()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    print(f"\n🔥 Горячие предложения list.am: http://{args.host}:{args.port}\n", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

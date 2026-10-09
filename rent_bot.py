"""
Бот, що шукає нові оголошення про довгострокову оренду квартир у Рівному
на OLX, DIM.RIA та Rieltor.ua і надсилає їх у Telegram.

Запуск: python rent_bot.py
Потрібні змінні середовища: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests

# ─── НАЛАШТУВАННЯ ПОШУКУ (можна змінювати) ───────────────────────────────────
PRICE_MIN = 15000      # грн / міс
PRICE_MAX = 25000      # грн / міс
AREA_MIN = 50          # м², загальна площа
USD_RATE = 41.5        # для оголошень у $ на Rieltor (приблизний курс)
EUR_RATE = 48.0
# Надіслати всі актуальні оголошення під час першого запуску (True),
# чи лише ті, що з'являться після запуску (False)
SEND_EXISTING_ON_FIRST_RUN = True
# ─────────────────────────────────────────────────────────────────────────────

STATE_FILE = Path(__file__).with_name("seen.json")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

session = requests.Session(impersonate="chrome")


# ─── Джерела ─────────────────────────────────────────────────────────────────

def fetch_olx():
    ads = []
    for offset in (0, 50):
        params = {
            "offset": offset, "limit": 50,
            "category_id": 1760,          # Довгострокова оренда квартир
            "region_id": 14, "city_id": 124,  # Рівне
            "currency": "UAH", "sort_by": "created_at:desc",
            "filter_float_price:from": PRICE_MIN,
            "filter_float_price:to": PRICE_MAX,
            "filter_float_total_area:from": AREA_MIN,
        }
        r = session.get("https://www.olx.ua/api/v1/offers/", params=params, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
        for o in data:
            p = {x["key"]: (x.get("value") or {}) for x in o.get("params", [])}
            loc = o.get("location") or {}
            district = (loc.get("district") or {}).get("name", "")
            photo = ""
            if o.get("photos"):
                photo = o["photos"][0]["link"].replace("{width}", "1000").replace("{height}", "750")
            ads.append({
                "id": f"olx:{o['id']}",
                "source": "OLX",
                "title": o.get("title", ""),
                "price": p.get("price", {}).get("label", ""),
                "area": p.get("total_area", {}).get("label", ""),
                "rooms": p.get("number_of_rooms_string", {}).get("label", ""),
                "floor": p.get("floor", {}).get("label", ""),
                "where": district,
                "who": "Бізнес / агент" if o.get("business") else "Приватна особа",
                "url": o["url"],
                "photo": photo,
            })
        if len(data) < 50:
            break
    return ads


def fetch_domria(seen):
    params = {
        "category": 1, "realty_type": 2, "operation": 3,  # квартири, довгострокова оренда
        "state_id": 9, "city_id": 9,                       # Рівне
        "limit": 100, "page": 0,
        "characteristic[234][from]": PRICE_MIN,
        "characteristic[234][to]": PRICE_MAX,
        "characteristic[214][from]": AREA_MIN,
    }
    r = session.get("https://dom.ria.com/node/searchEngine/v2/", params=params, timeout=30)
    r.raise_for_status()
    ads = []
    for rid in r.json().get("items", []):
        key = f"domria:{rid}"
        if key in seen:
            continue  # деталі тягнемо лише для нових
        d = session.get(f"https://dom.ria.com/realty/data/{rid}?lang_id=4", timeout=30).json()
        photo = ""
        if d.get("main_photo"):
            photo = "https://cdn.riastatic.com/photos/" + d["main_photo"].replace(".jpg", "fl.jpg")
        floor = f"{d.get('floor', '')} з {d.get('floors_count', '')}" if d.get("floor") else ""
        street = " ".join(x for x in [d.get("street_name_uk") or d.get("street_name", ""),
                                      str(d.get("building_number_str") or "")] if x)
        ads.append({
            "id": key,
            "source": "DIM.RIA",
            "title": (d.get("description_uk") or d.get("description") or "")[:90],
            "price": (d.get("priceArr") or {}).get("3", "") + " грн",
            "area": f"{d.get('total_square_meters', '')} м²",
            "rooms": f"{d.get('rooms_count', '')} кімн.",
            "floor": floor,
            "where": ", ".join(x for x in [d.get("district_name_uk", ""), street] if x),
            "who": "Рієлтор" if d.get("realtorVerified") or d.get("agencyVerified") else "",
            "url": "https://dom.ria.com/uk/" + d.get("beautiful_url", ""),
            "photo": photo,
        })
        time.sleep(0.3)
    return ads


def _to_uah(label):
    num = float(re.sub(r"[^\d.]", "", label.replace(",", ".")) or 0)
    if "$" in label:
        return num * USD_RATE
    if "€" in label:
        return num * EUR_RATE
    return num


def fetch_rieltor():
    ads = []
    for page in (1, 2):
        url = (f"https://rieltor.ua/rovno/flats-rent/?price_min={PRICE_MIN}"
               f"&price_max={PRICE_MAX}&page={page}")
        r = session.get(url, timeout=30)
        r.raise_for_status()
        for card in r.text.split('<div class="catalog-card ')[1:]:
            m_id = re.search(r'data-catalog-item-id="(\d+)"', card)
            m_url = re.search(r'href="(https://rieltor\.ua/rovno/flats-rent/view/\d+/)"', card)
            if not (m_id and m_url):
                continue
            label = html.unescape(re.search(r'data-label="([^"]*)"', card).group(1))
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", card)))
            m_area = re.search(r"([\d.]+)\s*/\s*[\d.]+\s*/\s*[\d.]+\s*м²|([\d.]+)\s*м²", text)
            area = float((m_area.group(1) or m_area.group(2))) if m_area else 0
            price_uah = _to_uah(label)
            if area and area < AREA_MIN:
                continue
            if not (PRICE_MIN * 0.95 <= price_uah <= PRICE_MAX * 1.05):
                continue
            m_rooms = re.search(r"(\d+)\s*кімнат", text)
            m_floor = re.search(r"поверх\s*(\d+\s*з\s*\d+)", text)
            m_addr = re.search(r"/міс\s+(.+?)\s+Рівне", text)
            m_photo = re.search(r'src="(https://market-images\.lunstatic\.net/[^"]+\.jpg)"', card)
            ads.append({
                "id": f"rieltor:{m_id.group(1)}",
                "source": "Rieltor.ua",
                "title": "",
                "price": label + ("" if "грн" in label else f" (≈{price_uah:,.0f} грн)".replace(",", " ")),
                "area": f"{area:g} м²" if area else "",
                "rooms": f"{m_rooms.group(1)} кімн." if m_rooms else "",
                "floor": m_floor.group(1) if m_floor else "",
                "where": m_addr.group(1) if m_addr else "",
                "who": "Рієлтор (уточніть комісію)",
                "url": m_url.group(1),
                "photo": m_photo.group(1).replace("/480/360/", "/960/720/") if m_photo else "",
            })
    return ads


# ─── Telegram ────────────────────────────────────────────────────────────────

def tg(method, **payload):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=30)
    if r.status_code == 429:  # занадто часто — чекаємо, скільки просить Telegram
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        return tg(method, **payload)
    return r


def send_ad(ad):
    e = html.escape
    lines = [f"🏠 <b>{e(ad['price'])}</b> · {e(ad['area'])} · {e(ad['rooms'])}"]
    if ad["where"]:
        lines.append(f"📍 {e(ad['where'])}")
    if ad["floor"]:
        lines.append(f"🏢 Поверх {e(ad['floor'])}")
    if ad["title"]:
        lines.append(f"📝 {e(ad['title'])}")
    if ad["who"]:
        lines.append(f"👤 {e(ad['who'])}")
    lines.append(f'🔗 <a href="{e(ad["url"])}">Відкрити на {e(ad["source"])}</a>')
    text = "\n".join(lines)

    if ad["photo"]:
        r = tg("sendPhoto", chat_id=TG_CHAT, photo=ad["photo"], caption=text, parse_mode="HTML")
        if r.ok:
            return True
    r = tg("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML")
    if not r.ok:
        print("Telegram error:", r.status_code, r.text[:300])
    return r.ok


# ─── Головний цикл ───────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen": [], "failures": {}}


def save_state(state):
    state["seen"] = state["seen"][-5000:]  # не даємо файлу рости безкінечно
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=0))


def main():
    if not TG_TOKEN or not TG_CHAT:
        sys.exit("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID")

    first_run = not STATE_FILE.exists()
    state = load_state()
    seen = set(state["seen"])

    sources = {
        "OLX": fetch_olx,
        "DIM.RIA": lambda: fetch_domria(seen),
        "Rieltor.ua": fetch_rieltor,
    }
    new_ads = []
    for name, fetch in sources.items():
        try:
            ads = fetch()
            print(f"{name}: знайдено {len(ads)}")
            new_ads += [a for a in ads if a["id"] not in seen]
            state["failures"][name] = 0
        except Exception as exc:  # один сайт впав — інші працюють далі
            print(f"{name}: помилка {exc!r}")
            n = state["failures"].get(name, 0) + 1
            state["failures"][name] = n
            if n == 6:  # ~30 хв поспіль не працює — повідомляємо один раз
                tg("sendMessage", chat_id=TG_CHAT,
                   text=f"⚠️ Не вдається отримати оголошення з {name} уже кілька спроб поспіль. "
                        f"Інші сайти працюють.")

    if first_run:
        tg("sendMessage", chat_id=TG_CHAT,
           text=f"✅ Бот запущено! Шукаю квартири в Рівному: {PRICE_MIN}–{PRICE_MAX} грн, "
                f"від {AREA_MIN} м².\n" +
                (f"Зараз надішлю {len(new_ads)} актуальних оголошень, далі — лише нові."
                 if SEND_EXISTING_ON_FIRST_RUN else "Надсилатиму лише нові оголошення."))

    for ad in new_ads:
        if not first_run or SEND_EXISTING_ON_FIRST_RUN:
            if not send_ad(ad):
                continue  # не позначаємо як побачене — спробуємо наступного разу
            time.sleep(1.1)
        state["seen"].append(ad["id"])
        seen.add(ad["id"])

    save_state(state)
    print(f"Нових оголошень: {len(new_ads)}")


if __name__ == "__main__":
    main()

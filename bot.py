import os
import re
import json
import time
import hashlib
import shutil
import tempfile
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import pymupdf
from bs4 import BeautifulSoup
import telebot
from flask import Flask, request

# Cloudflare va anti-bot himoyasini aylanib o'tish uchun
from curl_cffi import requests

logging.basicConfig(level=logging.INFO)
telebot.logger.setLevel(logging.INFO)

# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi!")

PORT = int(os.getenv("PORT", "10000"))
MAX_WORKERS = 8
MAX_PARALLEL_JOBS = 2          # bir vaqtda nechta chapter ishlanadi
MIN_IMAGE_BYTES = 20 * 1024    # bundan kichik rasmlar reklama deb tashlanadi
MAX_PDF_MB = 49.5
MAX_PAGE_HEIGHT = 14000        # PDF sahifa balandligi chegarasi (pt)

# Webhook uchun maxfiy kalit (token'dan avtomatik hosil qilinadi)
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)
JOB_SEM = threading.Semaphore(MAX_PARALLEL_JOBS)

PROTECTED_MSG = (
    "🔒 Bu maxfiy sayt.\n"
    "Rasmlarni yuklab bo'lmaydi (sayt himoyalangan)."
)

# =========================================================
# XATO TURLARI
# =========================================================

class ProtectedSite(Exception):
    pass

class SkipImage(Exception):
    pass

# =========================================================
# HTTP SESSION
# =========================================================

def make_session(referer=None):
    # impersonate brauzerning TLS izini va headerlarini o'zi qo'yadi
    session = requests.Session(impersonate="chrome120")
    session.headers.update({"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"})
    if referer:
        session.headers.update({"Referer": referer})
    return session

# =========================================================
# PDF TAYYORLASH
# =========================================================

def images_to_pdf(image_paths, output_path):
    doc = pymupdf.open()
    pages = 0
    try:
        for image_path in image_paths:
            try:
                img_doc = pymupdf.open(image_path)
                rect = img_doc[0].rect
                img_doc.close()

                w, h = rect.width, rect.height
                if h > MAX_PAGE_HEIGHT:
                    scale = MAX_PAGE_HEIGHT / h
                    w, h = w * scale, MAX_PAGE_HEIGHT

                page = doc.new_page(width=w, height=h)
                page.insert_image(page.rect, filename=image_path)
                pages += 1
            except Exception as e:
                logging.error(f"Rasm PDFga o'tmadi ({image_path}): {e}")

        if pages:
            doc.save(output_path, deflate=True, garbage=4)
    finally:
        doc.close()
    return pages

# =========================================================
# SAHIFADAN RASMLARNI AJRATISH (UNIVERSAL)
# =========================================================

BAD_NAME_RE = re.compile(
    r"(logo|banner|avatar|icon|favicon|sprite|advert|promo|emoji|"
    r"placeholder|loading|nelzya|stop|block)",
    re.IGNORECASE,
)
IMG_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp)(?:\?|$)", re.IGNORECASE)


def clean_url(u, base):
    if not isinstance(u, str):
        return None
    u = u.strip().replace("\\/", "/").replace("&amp;", "&")
    if not u or u.startswith("data:") or u.startswith("javascript:"):
        return None
    return urljoin(base, u)


def is_bad_name(url):
    name = os.path.basename(urlparse(url).path)
    return bool(BAD_NAME_RE.search(name))


def fetch_page(url):
    session = make_session(referer=url)
    try:
        response = session.get(url, timeout=30)
    finally:
        session.close()

    if response.status_code in (401, 403, 429, 503):
        raise ProtectedSite(f"HTTP {response.status_code}")
    response.raise_for_status()

    html = response.text
    head = html[:5000].lower()
    if "just a moment" in head or "cf-chl" in head or "attention required" in head:
        raise ProtectedSite("Anti-bot sahifasi")
    return html


def get_page_info(page_url):
    html = fetch_page(page_url)
    soup = BeautifulSoup(html, "html.parser")

    title = soup.find("h1")
    if title and title.get_text(strip=True):
        name = title.get_text(" ", strip=True)
    else:
        title_tag = soup.find("title")
        name = title_tag.get_text(" ", strip=True) if title_tag else "Manhwa"

    found = []

    # 1. JS ichidagi massivlar: pages = [...], images = [...]
    m = re.search(
        r'(?:pages|images|chapter_images|files|imgArr|page_image)\s*[=:]\s*(\[.*?\])\s*[;,}]',
        html, re.DOTALL,
    )
    if m:
        raw = m.group(1)
        try:
            parsed = json.loads(raw.replace("'", '"'))
            for p in parsed:
                if isinstance(p, str):
                    found.append(clean_url(p, page_url))
                elif isinstance(p, dict):
                    for key in ("url", "src", "image", "img"):
                        if key in p:
                            found.append(clean_url(p[key], page_url))
                            break
        except Exception:
            for u in re.findall(r'https?:(?:\\?/){2}[^\s"\'<>\\]+', raw):
                found.append(clean_url(u, page_url))

    # 2. <img> teglari (lazy-load atributlari bilan)
    if not [u for u in found if u]:
        for img in soup.find_all("img"):
            src = None
            for attr in ("data-src", "data-lazy-src", "data-original", "data-url", "src"):
                v = img.get(attr)
                if v and not v.strip().startswith("data:"):
                    src = v
                    break
            if not src and img.get("srcset"):
                src = img["srcset"].split(",")[0].strip().split(" ")[0]
            if src:
                found.append(clean_url(src, page_url))

    # 3. Oxirgi chora: HTML ichidagi barcha rasm havolalari
    if not [u for u in found if u]:
        text = html.replace("\\/", "/")
        for u in re.findall(
            r'https?://[^\s"\'<>\\]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>\\]*)?',
            text, re.IGNORECASE,
        ):
            found.append(clean_url(u, page_url))

    unique, seen = [], set()
    for u in found:
        if not u or u in seen or is_bad_name(u):
            continue
        seen.add(u)
        unique.append(u)

    return name, unique

# =========================================================
# RASMLARNI YUKLASH
# =========================================================

CT_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def download_one(item):
    index, url, folder, page_url = item
    last_err = None

    for attempt in range(3):
        session = make_session(referer=page_url)
        try:
            response = session.get(url, timeout=45)

            if response.status_code in (401, 403):
                raise ProtectedSite(f"HTTP {response.status_code}")
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower().split(";")[0].strip()
            if content_type and not content_type.startswith("image/"):
                raise SkipImage("rasm emas")

            if len(response.content) < MIN_IMAGE_BYTES:
                raise SkipImage("juda kichik (reklama/poster)")

            ext = os.path.splitext(urlparse(url).path)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = CT_EXT.get(content_type, ".jpg")

            path = os.path.join(folder, f"{index:05d}{ext}")
            with open(path, "wb") as f:
                f.write(response.content)
            return path

        except (ProtectedSite, SkipImage):
            raise
        except Exception as e:
            last_err = e
            time.sleep(1)
        finally:
            session.close()

    raise last_err


def download_images(image_urls, folder, page_url):
    jobs = [(i + 1, url, folder, page_url) for i, url in enumerate(image_urls)]
    paths, errors = [], []
    blocked = skipped = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(download_one, job): job for job in jobs}
        for future in as_completed(future_map):
            job = future_map[future]
            try:
                paths.append(future.result())
            except ProtectedSite:
                blocked += 1
            except SkipImage:
                skipped += 1
            except Exception as error:
                errors.append(f"{job[0]}-rasm: {error}")

    paths.sort()
    return paths, errors, blocked, skipped


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name[:80] if name else "manhwa"

# =========================================================
# URL PROCESSOR
# =========================================================

def process_url(chat_id, url):
    temp_dir = None
    try:
        if not JOB_SEM.acquire(blocking=False):
            bot.send_message(chat_id, "⏳ Bot band, navbatda turibsiz...")
            JOB_SEM.acquire()

        try:
            bot.send_message(chat_id, "🔎 Sahifa tekshirilmoqda...")

            try:
                name, image_urls = get_page_info(url)
            except ProtectedSite:
                bot.send_message(chat_id, PROTECTED_MSG)
                return

            if not image_urls:
                bot.send_message(chat_id, PROTECTED_MSG)
                return

            temp_dir = tempfile.mkdtemp(prefix="manhwa_")

            bot.send_message(
                chat_id,
                f"📚 {name}\n\n🖼 {len(image_urls)} ta rasm topildi.\n⚡ Yuklanmoqda...",
            )

            paths, errors, blocked, skipped = download_images(image_urls, temp_dir, url)

            if not paths:
                bot.send_message(chat_id, PROTECTED_MSG)
                return

            bot.send_message(chat_id, f"📄 {len(paths)} ta sahifa yuklandi.\n📕 PDF tayyorlanmoqda...")

            pdf_path = os.path.join(temp_dir, "chapter.pdf")
            pages = images_to_pdf(paths, pdf_path)

            if not pages:
                bot.send_message(chat_id, "❌ Rasmlarni PDFga aylantirib bo'lmadi.")
                return

            file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            if file_size_mb > MAX_PDF_MB:
                bot.send_message(
                    chat_id,
                    f"⚠️ PDF hajmi juda katta ({file_size_mb:.1f} MB).\nTelegram limitidan oshib ketdi.",
                )
                return

            caption = f"📚 {name}\n📄 {pages} ta sahifa"
            if errors:
                caption += f"\n⚠️ {len(errors)} ta rasm yuklanmadi"

            bot.send_message(chat_id, "✅ PDF tayyor!\n📤 Yuborilmoqda...")

            with open(pdf_path, "rb") as file:
                bot.send_document(
                    chat_id,
                    file,
                    visible_file_name=f"{safe_filename(name)}.pdf",
                    caption=caption[:1000],
                )
        finally:
            JOB_SEM.release()

    except Exception as error:
        logging.error(f"PROCESS ERROR: {error}")
        try:
            bot.send_message(chat_id, f"❌ Xatolik yuz berdi:\n\n{str(error)[:1000]}")
        except Exception:
            pass
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

# =========================================================
# BOT HANDLERS
# =========================================================

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(
        message,
        "👋 Salom!\n\n"
        "Menga manhwa chapter havolasini (URL) yuboring, men PDF qilib beraman.\n\n"
        "Masalan:\n"
        "https://a.zazaza.me/legenda_o_regressii/vol1/192\n\n"
        "Boshqa saytlar ham ishlaydi. Himoyalangan saytlar uchun 'maxfiy sayt' deb yozaman.",
    )


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_url(message):
    match = re.search(r"https?://\S+", message.text or "")
    if not match:
        bot.reply_to(message, "❌ Havola topilmadi.\n\nChapter URL yuboring (https://...).")
        return

    url = match.group(0)
    bot.reply_to(message, "✅ URL qabul qilindi.\n⏳ Ishlash boshlandi...")

    threading.Thread(target=process_url, args=(message.chat.id, url), daemon=True).start()

# =========================================================
# FLASK / HEALTH CHECK / WEBHOOK
# =========================================================

@app.get("/")
def home():
    return "Telegram Manhwa Bot ishlayapti.", 200


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.post("/webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "Forbidden", 403

    if (request.headers.get("content-type") or "").startswith("application/json"):
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Bad request", 400


def setup_webhook():
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN")
    public_url = os.getenv("WEBHOOK_URL") or (f"https://{domain}" if domain else None)
    if not public_url:
        logging.warning("Public URL topilmadi (RAILWAY_PUBLIC_DOMAIN yo'q).")
        return

    webhook_url = public_url.rstrip("/") + "/webhook"
    try:
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
        logging.info(f"Webhook o'rnatildi: {webhook_url}")
    except Exception as error:
        logging.error(f"Webhook ERROR: {error}")


setup_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
  

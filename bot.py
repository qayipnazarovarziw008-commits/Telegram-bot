import os
import re
import json
import shutil
import tempfile
import threading
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import pymupdf
import requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask, request

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

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)


# =========================================================
# ZAZAZA URL PATTERN
# =========================================================

ZAZAZA_RE = re.compile(
    r"^https?://a\.zazaza\.me/[^/]+/vol\d+/\d+/?$",
    re.IGNORECASE
)


# =========================================================
# HTTP SESSION CONFIG
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def make_session(referer=None):
    session = requests.Session()
    session.headers.update(HEADERS)
    if referer:
        session.headers.update({"Referer": referer})
    return session


# =========================================================
# PDF TAYYORLASH
# =========================================================

def images_to_pdf(image_paths, output_path):
    doc = pymupdf.open()
    try:
        for image_path in image_paths:
            try:
                img_doc = pymupdf.open(image_path)
                img_page = img_doc[0]
                rect = img_page.rect

                page = doc.new_page(width=rect.width, height=rect.height)
                page.insert_image(page.rect, filename=image_path)
                img_doc.close()
            except Exception as e:
                logging.error(f"Rasm PDFga o'tmadi ({image_path}): {e}")
                continue

        doc.save(output_path, deflate=True, garbage=4)
    finally:
        doc.close()


# =========================================================
# CHAPTER HTML VA JAVASCRIPT DAN RASMLARNI AJRATISH
# =========================================================

def get_page_info(chapter_url):
    session = make_session(referer=chapter_url)
    response = session.get(chapter_url, timeout=30)
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    title = soup.find("h1")
    if title:
        name = title.get_text(" ", strip=True)
    else:
        title_tag = soup.find("title")
        name = title_tag.get_text(" ", strip=True) if title_tag else "Manhwa"

    image_urls = []

    # 1. ZazaZa va shunga o'xshash reader'lar ichida JS o'zgaruvchilarni izlash (masalan, images = [...], pages = [...])
    js_images_match = re.search(r'(?:pages|images|chapter_images|files)\s*=\s*(\[.*?\]);', html, re.DOTALL)
    
    if js_images_match:
        try:
            raw_json = js_images_match.group(1).replace("'", '"')
            parsed_paths = json.loads(raw_json)
            for p in parsed_paths:
                if isinstance(p, str):
                    image_urls.append(urljoin(chapter_url, p))
                elif isinstance(p, dict) and 'url' in p:
                    image_urls.append(urljoin(chapter_url, p['url']))
        except Exception as e:
            logging.warning(f"JS JSON parsing xatosi: {e}")

    # 2. Agar JS ichidan topilmasa, xususiy CDN / Storage hostlaridan keluvchi rasmlarni izlash
    if not image_urls:
        all_found = re.findall(r'https?://[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)', html, re.IGNORECASE)
        
        # Anti-bot/reklama rasmlarini chiqarib tashlash
        IGNORE_PATTERNS = ["block", "stop", "nelzya", "logo", "banner", "avatar", "icon", "advert", "ads", "promo", "telegram"]
        
        for url in all_found:
            low = url.lower()
            if not any(bad in low for bad in IGNORE_PATTERNS):
                if url not in image_urls:
                    image_urls.append(url)

    # Duplikatlarni tozalash
    unique_urls = []
    seen = set()
    for u in image_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    return name, unique_urls


# =========================================================
# NATURAL SORT
# =========================================================

def natural_key(path):
    filename = os.path.basename(path)
    numbers = re.findall(r"\d+", filename)
    if numbers:
        return [int(n) for n in numbers]
    return [10**12, filename]


# =========================================================
# BIRTA RASMNI YUKLASH (FILTR VA HEADERLAR BILAN)
# =========================================================

def download_one(item):
    index, url, folder, chapter_url = item
    session = make_session(referer=chapter_url)

    parsed = urlparse(url)
    ext = os.path.splitext(parsed.path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    filename = f"{index:05d}{ext}"
    path = os.path.join(folder, filename)

    response = session.get(url, timeout=45)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and not content_type.startswith("image/"):
        raise RuntimeError(f"URL rasm emas: {url}")

    # Shubhali kichik rasmlar va posterlarni tashlab yuborish (50 KB dan kichiklari)
    if len(response.content) < 50 * 1024:
        raise RuntimeError("Rasm hajm jihatidan juda kichik (xavfsizlik posteri yoki reklama)")

    with open(path, "wb") as f:
        f.write(response.content)

    return path


# =========================================================
# RASMLARNI PARALLEL YUKLASH
# =========================================================

def download_images(image_urls, folder, chapter_url):
    jobs = [(i + 1, url, folder, chapter_url) for i, url in enumerate(image_urls)]
    paths = []
    errors = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(download_one, job): job for job in jobs}

        for future in as_completed(future_map):
            job = future_map[future]
            try:
                path = future.result()
                paths.append(path)
            except Exception as error:
                errors.append(f"{job[0]}-sahifa: {error}")

    paths.sort(key=natural_key)
    return paths, errors


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name[:80] if name else "manhwa"


# =========================================================
# URL PROCESSOR
# =========================================================

def process_url(chat_id, url):
    temp_dir = None
    try:
        bot.send_message(chat_id, "🔎 Chapter tekshirilmoqda...")

        name, image_urls = get_page_info(url)

        if not image_urls:
            bot.send_message(chat_id, "❌ Asl manhwa rasmlari topilmadi. Sayt JS orqali rasmlarni to'liq yashirgan bo'lishi mumkin.")
            return

        temp_dir = tempfile.mkdtemp(prefix="manhwa_")

        bot.send_message(
            chat_id,
            f"📚 **{name}**\n\n🖼 {len(image_urls)} ta havola tahlil qilinmoqda...\n⚡ Yuklanmoqda...",
            parse_mode="Markdown"
        )

        paths, errors = download_images(image_urls, temp_dir, url)

        if not paths:
            bot.send_message(chat_id, "❌ Manhwa sahifalari yuklanmadi. Barcha yuklangan fayllar rasm filtridan o'tmadi.")
            return

        bot.send_message(chat_id, f"📄 {len(paths)} ta asl manhwa sahifasi yuklandi.\n📕 PDF tayyorlanmoqda...")

        pdf_path = os.path.join(temp_dir, "chapter.pdf")
        images_to_pdf(paths, pdf_path)

        file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        if file_size_mb > 49.5:
            bot.send_message(
                chat_id,
                f"⚠️ PDF hajmi juda katta ({file_size_mb:.1f} MB).\nTelegram limitidan oshib ketdi."
            )
            return

        filename = f"{safe_filename(name)}.pdf"

        bot.send_message(chat_id, "✅ PDF tayyor!\n📤 Telegramga yuborilmoqda...")

        with open(pdf_path, "rb") as file:
            bot.send_document(
                chat_id,
                file,
                visible_file_name=filename,
                caption=f"📚 {name}"
            )

    except Exception as error:
        logging.error(f"PROCESS ERROR: {error}")
        try:
            bot.send_message(chat_id, f"❌ Xatolik yuz berdi:\n\n`{str(error)[:1000]}`", parse_mode="Markdown")
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
        "Menga ZazaZa chapter URL yuboring.\n\n"
        "Masalan:\n"
        "https://a.zazaza.me/legenda_o_regressii/vol1/192"
    )


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_url(message):
    url = message.text.strip()

    if not ZAZAZA_RE.match(url):
        bot.reply_to(message, "❌ URL formati noto‘g‘ri.\n\nZazaZa chapter URL yuboring.")
        return

    bot.reply_to(message, "✅ URL qabul qilindi.\n⏳ Ishlash boshlandi...")

    threading.Thread(
        target=process_url,
        args=(message.chat.id, url),
        daemon=True
    ).start()


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
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Bad request", 400


def setup_webhook():
    public_url = os.getenv("RENDER_EXTERNAL_URL")
    if not public_url:
        logging.warning("RENDER_EXTERNAL_URL topilmadi.")
        return

    webhook_url = public_url.rstrip("/") + "/webhook"
    try:
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook o'rnatildi: {webhook_url}")
    except Exception as error:
        logging.error(f"Webhook ERROR: {error}")


setup_webhook()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)

import os
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import pymupdf
import requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask, request


# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi!")

PORT = int(os.getenv("PORT", "10000"))
MAX_WORKERS = 8

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


# =========================================================
# ZAZAZA URL
# =========================================================

ZAZAZA_RE = re.compile(
    r"^https?://a\.zazaza\.me/[^/]+/vol\d+/\d+/?$",
    re.IGNORECASE
)


# =========================================================
# HTTP
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Mobile Safari/537.36"
    )
}


def make_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


# =========================================================
# PDF
# =========================================================

def images_to_pdf(image_paths, output_path):

    doc = pymupdf.open()

    try:
        for image_path in image_paths:

            img_doc = pymupdf.open(image_path)

            try:
                img_page = img_doc[0]
                rect = img_page.rect

                page = doc.new_page(
                    width=rect.width,
                    height=rect.height
                )

                page.insert_image(
                    page.rect,
                    filename=image_path
                )

            finally:
                img_doc.close()

        doc.save(
            output_path,
            deflate=True,
            garbage=3
        )

    finally:
        doc.close()


# =========================================================
# CHAPTER HTML
# =========================================================

def get_page_info(chapter_url):

    session = make_session()

    response = session.get(
        chapter_url,
        timeout=30
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    # -----------------------------------------------------
    # NOM
    # -----------------------------------------------------

    title = soup.find("h1")

    if title:
        name = title.get_text(
            " ",
            strip=True
        )
    else:
        title_tag = soup.find("title")

        if title_tag:
            name = title_tag.get_text(
                " ",
                strip=True
            )
        else:
            name = "Manhwa"

    # -----------------------------------------------------
    # RASMLAR
    # -----------------------------------------------------

    urls = []

    attributes = [
        "src",
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-url"
    ]

    for tag in soup.find_all(
        ["img", "source"]
    ):

        for attr in attributes:

            value = tag.get(attr)

            if not value:
                continue

            value = value.strip()

            if value.startswith("data:image"):
                continue

            full_url = urljoin(
                chapter_url,
                value
            )

            urls.append(full_url)

        srcset = (
            tag.get("srcset")
            or tag.get("data-srcset")
        )

        if srcset:

            for item in srcset.split(","):

                value = (
                    item.strip()
                    .split(" ")[0]
                )

                if value:
                    urls.append(
                        urljoin(
                            chapter_url,
                            value
                        )
                    )

    # -----------------------------------------------------
    # DUPLIKATLARNI OLIB TASHLASH
    # -----------------------------------------------------

    image_urls = []
    seen = set()

    for url in urls:

        low = url.lower()

        is_image = (
            ".jpg" in low
            or ".jpeg" in low
            or ".png" in low
            or ".webp" in low
        )

        if not is_image:
            continue

        if url in seen:
            continue

        seen.add(url)
        image_urls.append(url)

    return name, image_urls


# =========================================================
# NATURAL SORT
# =========================================================

def natural_key(path):

    filename = os.path.basename(path)

    numbers = re.findall(
        r"\d+",
        filename
    )

    if numbers:
        return [
            int(number)
            for number in numbers
        ]

    return [
        10**12,
        filename
    ]


# =========================================================
# BIRTA RASM
# =========================================================

def download_one(item):

    index, url, folder = item

    session = make_session()

    parsed = urlparse(url)

    extension = os.path.splitext(
        parsed.path
    )[1].lower()

    if extension not in (
        ".jpg",
        ".jpeg",
        ".png",
        ".webp"
    ):
        extension = ".jpg"

    filename = (
        f"{index:05d}"
        f"{extension}"
    )

    path = os.path.join(
        folder,
        filename
    )

    response = session.get(
        url,
        timeout=60
    )

    response.raise_for_status()

    content_type = (
        response.headers
        .get("Content-Type", "")
        .lower()
    )

    if content_type and not content_type.startswith(
        "image/"
    ):
        raise RuntimeError(
            f"URL rasm qaytarmadi: {url}"
        )

    with open(
        path,
        "wb"
    ) as file:
        file.write(
            response.content
        )

    return path


# =========================================================
# RASMLARNI YUKLASH
# =========================================================

def download_images(
    image_urls,
    folder
):

    jobs = [
        (
            index + 1,
            url,
            folder
        )
        for index, url
        in enumerate(image_urls)
    ]

    paths = []
    errors = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        future_map = {
            executor.submit(
                download_one,
                job
            ): job
            for job in jobs
        }

        for future in as_completed(
            future_map
        ):

            job = future_map[future]

            try:
                path = future.result()
                paths.append(path)

            except Exception as error:

                index = job[0]

                errors.append(
                    f"{index}: {error}"
                )

    paths.sort(
        key=natural_key
    )

    return paths, errors


# =========================================================
# FILENAME
# =========================================================

def safe_filename(name):

    name = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        name
    )

    name = name.strip()

    if not name:
        name = "manhwa"

    return name[:80]


# =========================================================
# URLNI ISHLASH
# =========================================================

def process_url(
    chat_id,
    url
):

    temp_dir = None

    try:

        bot.send_message(
            chat_id,
            "🔎 Chapter tekshirilmoqda..."
        )

        name, image_urls = get_page_info(
            url
        )

        if not image_urls:

            bot.send_message(
                chat_id,
                "❌ Rasmlar topilmadi.\n\n"
                "ZazaZa reader rasmlarni "
                "JavaScript orqali yuklayotgan "
                "bo‘lishi mumkin."
            )

            return

        temp_dir = tempfile.mkdtemp(
            prefix="manhwa_"
        )

        bot.send_message(
            chat_id,
            f"📚 {name}\n\n"
            f"🖼 {len(image_urls)} ta sahifa topildi.\n"
            "⚡ Yuklanmoqda..."
        )

        paths, errors = download_images(
            image_urls,
            temp_dir
        )

        if not paths:

            bot.send_message(
                chat_id,
                "❌ Hech qanday rasm yuklanmadi."
            )

            return

        bot.send_message(
            chat_id,
            f"📄 {len(paths)} ta sahifa yuklandi.\n"
            "📕 PDF tayyorlanmoqda..."
        )

        pdf_path = os.path.join(
            temp_dir,
            "chapter.pdf"
        )

        images_to_pdf(
            paths,
            pdf_path
        )

        filename = (
            safe_filename(name)
            + ".pdf"
        )

        bot.send_message(
            chat_id,
            "✅ PDF tayyor!\n"
            "📤 Telegramga yuborilmoqda..."
        )

        with open(
            pdf_path,
            "rb"
        ) as file:

            bot.send_document(
                chat_id,
                file,
                visible_file_name=filename
            )

        if errors:

            bot.send_message(
                chat_id,
                "⚠️ Ayrim sahifalar yuklanmadi: "
                f"{len(errors)} ta"
            )

    except Exception as error:

        print(
            "PROCESS ERROR:",
            repr(error)
        )

        try:

            bot.send_message(
                chat_id,
                "❌ Xatolik:\n\n"
                f"{str(error)[:1500]}"
            )

        except Exception as telegram_error:

            print(
                "Telegram error:",
                repr(telegram_error)
            )

    finally:

        if temp_dir:

            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )


# =========================================================
# /START
# =========================================================

@bot.message_handler(
    commands=["start"]
)
def start(message):

    bot.reply_to(
        message,
        "👋 Salom!\n\n"
        "Menga ZazaZa chapter URL yuboring.\n\n"
        "Masalan:\n"
        "https://a.zazaza.me/"
        "legenda_o_regressii/vol1/192"
    )


# =========================================================
# URL
# =========================================================

@bot.message_handler(
    func=lambda message: True,
    content_types=["text"]
)
def handle_url(message):

    url = message.text.strip()

    if not ZAZAZA_RE.match(url):

        bot.reply_to(
            message,
            "❌ URL formati noto‘g‘ri.\n\n"
            "ZazaZa chapter URL yuboring."
        )

        return

    bot.reply_to(
        message,
        "✅ URL qabul qilindi.\n"
        "⏳ Ishlash boshlandi..."
    )

    threading.Thread(
        target=process_url,
        args=(
            message.chat.id,
            url
        ),
        daemon=True
    ).start()


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/")
def home():

    return (
        "Telegram Manhwa Bot ishlayapti.",
        200
    )


@app.get("/health")
def health():

    return {
        "status": "ok"
    }, 200


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.post("/webhook")
def webhook():

    content_type = request.headers.get(
        "content-type",
        ""
    )

    if "application/json" not in content_type:

        return (
            "Bad request",
            400
        )

    try:

        json_string = (
            request
            .get_data()
            .decode("utf-8")
        )

        update = (
            telebot.types.Update
            .de_json(json_string)
        )

        bot.process_new_updates(
            [update]
        )

        return "", 200

    except Exception as error:

        print(
            "WEBHOOK ERROR:",
            repr(error)
        )

        return "", 500


# =========================================================
# WEBHOOKNI GUNICORNDA HAM O'RNATISH
# =========================================================

def setup_webhook():

    public_url = os.getenv(
        "RENDER_EXTERNAL_URL"
    )

    if not public_url:

        print(
            "RENDER_EXTERNAL_URL topilmadi."
        )

        return

    webhook_url = (
        public_url.rstrip("/")
        + "/webhook"
    )

    try:

        bot.remove_webhook()

        bot.set_webhook(
            url=webhook_url
        )

        print(
            "Webhook set:",
            webhook_url
        )

    except Exception as error:

        print(
            "Webhook ERROR:",
            repr(error)
        )


# Gunicorn bot:app qilganda ham ishlaydi
setup_webhook()


# =========================================================
# LOCAL
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )

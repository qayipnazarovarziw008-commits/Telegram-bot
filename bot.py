import os
import re
import uuid
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import telebot
from bs4 import BeautifulSoup
import fitz  # PyMuPDF


# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi!")

bot = telebot.TeleBot(BOT_TOKEN)

MAX_WORKERS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; K) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0 Mobile Safari/537.36"
    )
}

BASE_DOMAIN = "a.zazaza.me"


# =========================================================
# URL TEKSHIRISH
# =========================================================

def is_valid_url(url):
    pattern = r"^https?://a\.zazaza\.me/[^/]+/vol\d+/\d+$"
    return re.match(pattern, url) is not None


# =========================================================
# SAHIFANI OLISH
# =========================================================

def get_page(url):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    return response.text


# =========================================================
# MANHWA NOMINI ANIQLASH
# =========================================================

def get_manhwa_name(soup, url):
    """
    Avval <h1> va title orqali nomni aniqlaydi.
    """

    # H1
    h1 = soup.find("h1")

    if h1:
        name = h1.get_text(" ", strip=True)

        if name:
            return name

    # TITLE
    title = soup.find("title")

    if title:
        text = title.get_text(" ", strip=True)

        # Masalan:
        # Чтение Манхва Легенда о регрессии...
        text = re.sub(
            r"^Чтение\s+.*?\s+",
            "",
            text,
            flags=re.IGNORECASE
        )

        if text:
            return text

    # Oxirgi variant: URL slug
    path = url.split("://", 1)[-1]
    slug = path.split("/")[1]

    return slug.replace("_", " ").title()


# =========================================================
# RASM URLLARINI TOPISH
# =========================================================

def find_image_urls(soup, page_url):
    """
    Sahifadagi rasmlarni topadi.

    Turli sayt konfiguratsiyalarida:
    src
    data-src
    data-original
    data-lazy-src
    kabi atributlar ishlatilishi mumkin.
    """

    found = []

    attributes = [
        "src",
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-url"
    ]

    for img in soup.find_all("img"):

        for attr in attributes:

            value = img.get(attr)

            if not value:
                continue

            value = value.strip()

            if not value:
                continue

            # Base64 rasmlarni o'tkazib yuboramiz
            if value.startswith("data:image"):
                continue

            # Faqat image fayllarni olish
            if not re.search(
                r"\.(jpg|jpeg|png|webp)(\?.*)?$",
                value,
                re.IGNORECASE
            ):
                continue

            # Relative URL → absolute URL
            if value.startswith("//"):
                value = "https:" + value

            elif value.startswith("/"):
                value = "https://a.zazaza.me" + value

            elif not value.startswith("http"):
                from urllib.parse import urljoin
                value = urljoin(page_url, value)

            if value not in found:
                found.append(value)

    return found


# =========================================================
# RASM RAQAMINI ANIQLASH
# =========================================================

def image_number(url, fallback):
    """
    Masalan:
    000001.jpg
    000002.jpg
    000100.jpg

    nomidan raqamni ajratadi.
    """

    filename = url.split("/")[-1]

    match = re.search(r"(\d+)(?:\.[a-zA-Z]+)(?:\?.*)?$", filename)

    if match:
        return int(match.group(1))

    return fallback


# =========================================================
# BIRTA RASMNI YUKLASH
# =========================================================

def download_image(item):
    number, url, folder = item

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=60
        )

        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type",
            ""
        ).lower()

        if not content_type.startswith("image/"):
            return None, f"{number}: image emas"

        extension = ".jpg"

        if "png" in content_type:
            extension = ".png"

        elif "webp" in content_type:
            extension = ".webp"

        filename = f"{number:06d}{extension}"

        path = os.path.join(
            folder,
            filename
        )

        with open(path, "wb") as f:
            f.write(response.content)

        return path, None

    except Exception as e:
        return None, f"{number}: {e}"


# =========================================================
# BARCHA RASMLARNI PARALLEL YUKLASH
# =========================================================

def download_images(image_urls, folder):

    os.makedirs(folder, exist_ok=True)

    jobs = []

    for index, url in enumerate(image_urls, start=1):

        number = image_number(
            url,
            index
        )

        jobs.append(
            (
                number,
                url,
                folder
            )
        )

    downloaded = []
    errors = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = [
            executor.submit(
                download_image,
                job
            )
            for job in jobs
        ]

        for future in as_completed(futures):

            path, error = future.result()

            if path:
                downloaded.append(path)

            if error:
                errors.append(error)

    downloaded.sort(
        key=lambda p: int(
            re.search(
                r"(\d+)",
                os.path.basename(p)
            ).group(1)
        )
    )

    return downloaded, errors


# =========================================================
# ESKI BOTINGDAGI PDF KOD
# =========================================================

def images_to_pdf(image_paths, output_path):

    doc = fitz.open()

    try:

        for p in image_paths:

            img_doc = fitz.open(p)

            try:

                img_page = img_doc[0]

                rect = img_page.rect

                page = doc.new_page(
                    width=rect.width,
                    height=rect.height
                )

                page.insert_image(
                    page.rect,
                    filename=p
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
# TOZALASH
# =========================================================

def cleanup(folder):

    try:

        if os.path.exists(folder):
            shutil.rmtree(folder)

    except Exception as e:

        print(
            "Cleanup error:",
            e
        )


# =========================================================
# ASOSIY ISH
# =========================================================

def process_url(chat_id, url):

    session_id = uuid.uuid4().hex[:10]

    folder = os.path.join(
        "temp",
        session_id
    )

    try:

        bot.send_message(
            chat_id,
            "🔎 Sahifa tekshirilmoqda..."
        )

        # -----------------------------------------
        # HTML
        # -----------------------------------------

        html = get_page(url)

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # -----------------------------------------
        # MANHWA NOMI
        # -----------------------------------------

        manhwa_name = get_manhwa_name(
            soup,
            url
        )

        # -----------------------------------------
        # IMAGE URL
        # -----------------------------------------

        bot.send_message(
            chat_id,
            f"📚 {manhwa_name}\n\n"
            "🖼 Rasmlar qidirilmoqda..."
        )

        image_urls = find_image_urls(
            soup,
            url
        )

        if not image_urls:

            bot.send_message(
                chat_id,
                "❌ Rasmlar topilmadi.\n\n"
                "Bu sayt reader rasmlarini JavaScript orqali "
                "yuklayotgan bo‘lishi mumkin."
            )

            return

        total = len(image_urls)

        bot.send_message(
            chat_id,
            f"✅ {total} ta rasm topildi.\n"
            f"⚡ {MAX_WORKERS} ta parallel yuklash boshlanmoqda..."
        )

        # -----------------------------------------
        # DOWNLOAD
        # -----------------------------------------

        images, errors = download_images(
            image_urls,
            folder
        )

        if not images:

            bot.send_message(
                chat_id,
                "❌ Birorta ham rasm yuklanmadi."
            )

            return

        bot.send_message(
            chat_id,
            f"📥 {len(images)}/{total} ta rasm yuklandi.\n"
            "📕 PDF yaratilmoqda..."
        )

        # -----------------------------------------
        # PDF
        # -----------------------------------------

        os.makedirs(
            folder,
            exist_ok=True
        )

        safe_name = re.sub(
            r'[\\/:*?"<>|]',
            "_",
            manhwa_name
        )

        output_pdf = os.path.join(
            folder,
            f"{safe_name}.pdf"
        )

        images_to_pdf(
            images,
            output_pdf
        )

        # -----------------------------------------
        # TELEGRAM
        # -----------------------------------------

        with open(
            output_pdf,
            "rb"
        ) as f:

            bot.send_document(
                chat_id,
                f,
                caption=(
                    f"✅ {manhwa_name}\n"
                    f"📄 {len(images)} sahifa"
                )
            )

    except Exception as e:

        print(
            "ERROR:",
            repr(e)
        )

        bot.send_message(
            chat_id,
            "❌ Xatolik yuz berdi.\n\n"
            f"{str(e)[:500]}"
        )

    finally:

        cleanup(folder)


# =========================================================
# TELEGRAM HANDLER
# =========================================================

@bot.message_handler(commands=["start"])
def start(message):

    bot.send_message(
        message.chat.id,
        "👋 Salom!\n\n"
        "Manhwa chapter URL'ini yuboring.\n\n"
        "Masalan:\n"
        "https://a.zazaza.me/legenda_o_regressii/vol1/192"
    )


@bot.message_handler(
    func=lambda message:
    message.content_type == "text"
)
def handle_url(message):

    url = message.text.strip()

    if not is_valid_url(url):

        bot.send_message(
            message.chat.id,
            "❌ URL noto‘g‘ri.\n\n"
            "ZazaZa chapter URL yuboring."
        )

        return

    # Har bir foydalanuvchi vazifasini alohida thread
    threading.Thread(
        target=process_url,
        args=(
            message.chat.id,
            url
        ),
        daemon=True
    ).start()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    print("🤖 Bot ishga tushdi...")

    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60
            )

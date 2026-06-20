import telebot
from telebot import types
import requests
import os
import json
import base64
import traceback
import threading
import queue
import uuid
from PIL import Image
import fitz  # PyMuPDF

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable o'rnatilmagan! Avval uni sozlang.")

bot = telebot.TeleBot(BOT_TOKEN)

DATA_FILE = "users.json"
UPSCALE_FACTOR = 1.5      # 10000 -> 15000 kabi kattalashtirish
MAX_DIMENSION = 8000      # Xotira portlashining oldini olish uchun maksimal o'lcham

# ---------- Foydalanuvchi ma'lumotlarini saqlash ----------

users_lock = threading.Lock()

def load_users():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_users(data):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        print("users.json saqlashda xato:", traceback.format_exc())

user_data = load_users()          # { "123456": {"api_key": "..."} }
waiting_for_key = set()           # API key kutilayotgan user_id lar

def get_api_key(user_id):
    with users_lock:
        return user_data.get(str(user_id), {}).get("api_key")

def set_api_key(user_id, key):
    with users_lock:
        user_data[str(user_id)] = {"api_key": key}
        save_users(user_data)

# ---------- Klaviatura ----------

def main_keyboard(user_id):
    markup = types.InlineKeyboardMarkup()
    if get_api_key(user_id):
        markup.add(types.InlineKeyboardButton("🔁 API key almashtirish", callback_data="change_key"))
    else:
        markup.add(types.InlineKeyboardButton("🔑 API key kiritish", callback_data="set_key"))
    return markup

# ---------- Yordamchi funksiyalar ----------

def pdf_to_images(pdf_path, prefix, dpi=150):
    """PyMuPDF (fitz) yordamida PDF sahifalarini PNG rasmlarga aylantiradi.
    poppler kabi tashqi tizim dasturiga muhtoj emas."""
    paths = []
    zoom = dpi / 72  # PDF nuqtalari standart 72 dpi, kerakli dpi'ga moslashtiramiz
    matrix = fitz.Matrix(zoom, zoom)
    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix)
            path = f"{prefix}_page_{i}.png"
            pix.save(path)
            paths.append(path)
    finally:
        doc.close()
    return paths

def upscale_image(image_path):
    """Rasm o'lchamini kattalashtiradi (masalan 10000 -> 15000), lekin max chegaradan oshmaydi.

    Eslatma: faylni o'qish va yana shu nomga yozish orasida rasm to'liq
    xotiraga yuklanishi va asl fayl yopilishi shart, aks holda Pillow'ning
    lazy-load mexanizmi (PNG'ni stream sifatida o'qishi) bilan fayl ustiga
    yozish to'qnashib, qisman buzilgan (siljigan/parchalangan) PNG hosil bo'ladi.
    """
    with Image.open(image_path) as img:
        img.load()  # rasmni to'liq xotiraga majburiy yuklaymiz
        new_w = min(int(img.width * UPSCALE_FACTOR), MAX_DIMENSION)
        new_h = min(int(img.height * UPSCALE_FACTOR), MAX_DIMENSION)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
    # `with` blokidan chiqib, asl fayl yopilgandan keyin xavfsiz saqlaymiz
    resized.save(image_path, "PNG")
    return image_path

def translate_image(image_path, api_key):
    try:
        with open(image_path, "rb") as f:
            response = requests.post(
                "https://api.toriitranslate.com/api/v2/upload",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": f},
                data={
                    "target_lang": "uz",
                    "translator": "gemini-flash-lite",
                    "font": "WildWords",
                    "text_align": "auto"
                },
                timeout=120
            )
    except requests.exceptions.RequestException as e:
        return None, f"Tarmoq xatosi: {e}"

    if response.status_code != 200:
        return None, f"Server xatosi (status: {response.status_code})"

    if response.headers.get("success") == "true":
        try:
            data = response.json()
            img_data = data["image"].split(",")[1]
            img_bytes = base64.b64decode(img_data)
            out_path = f"translated_{os.path.basename(image_path)}"
            with open(out_path, "wb") as f:
                f.write(img_bytes)
            return out_path, None
        except Exception:
            return None, "Javobni o'qishda xato (noto'g'ri format)"
    else:
        try:
            error_data = response.json()
            error_msg = error_data.get("error", "Noma'lum xato")
        except Exception:
            error_msg = f"API bilan bog'lanishda xato (status: {response.status_code})"
        return None, error_msg

def images_to_pdf(image_paths, output_path):
    """Rasmlarni asl tartibda (sahifa raqami bo'yicha) PDF qilib yig'adi."""
    imgs = []
    for p in image_paths:
        img = Image.open(p).convert("RGB")
        imgs.append(img)
    if imgs:
        imgs[0].save(output_path, save_all=True, append_images=imgs[1:])

def page_index(path):
    """page_0, page_1 ... dan raqamni ajratib oladi, tartib bo'yicha saralash uchun."""
    try:
        name = os.path.basename(path)
        num = name.split("_page_")[-1].split(".")[0]
        return int(num)
    except Exception:
        return 0

def cleanup(paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

# ---------- Navbat (queue) tizimi ----------
# Bitta worker thread PDF'larni ketma-ket ishlaydi.
# Shu bilan bir vaqtning o'zida faqat bitta PDF qayta ishlanadi,
# qolganlari navbatda kutib turadi.

task_queue = queue.Queue()
queue_lock = threading.Lock()
queue_size = 0  # navbatdagi (ishlanayotganlar bilan birga) vazifalar soni


def enqueue_task(chat_id, user_id, document):
    global queue_size
    with queue_lock:
        queue_size += 1
        position = queue_size  # bu vazifaning navbatdagi o'rni
    task_queue.put((chat_id, user_id, document))
    return position


def task_done():
    global queue_size
    with queue_lock:
        queue_size -= 1


def worker_loop():
    while True:
        chat_id, user_id, document = task_queue.get()
        try:
            process_pdf(chat_id, user_id, document)
        except Exception:
            print(traceback.format_exc())
            try:
                bot.send_message(chat_id, "❌ Kutilmagan xato yuz berdi. Iltimos, qayta urinib ko'ring.")
            except Exception:
                pass
        finally:
            task_done()
            task_queue.task_done()


# ---------- Handlerlar ----------

@bot.message_handler(commands=["start"])
def start(message):
    try:
        user_id = message.from_user.id
        bot.send_message(
            message.chat.id,
            "Salom! 👋\n\n"
            "Men manga/rasm PDF fayllarini o'zbekchaga tarjima qilaman.\n\n"
            "Davom etish uchun pastdagi tugmani bosing 👇",
            reply_markup=main_keyboard(user_id)
        )
    except Exception:
        print(traceback.format_exc())

@bot.callback_query_handler(func=lambda call: call.data in ["set_key", "change_key"])
def handle_key_button(call):
    try:
        user_id = call.from_user.id
        waiting_for_key.add(user_id)
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "🔑 Iltimos, Torii API keyingizni yuboring:")
    except Exception:
        print(traceback.format_exc())

@bot.message_handler(func=lambda m: m.content_type == "text" and not m.text.startswith("/"))
def handle_text(message):
    try:
        user_id = message.from_user.id
        text = message.text.strip()

        if user_id in waiting_for_key:
            set_api_key(user_id, text)
            waiting_for_key.discard(user_id)
            bot.send_message(
                message.chat.id,
                "✅ API key saqlandi!\n\n📄 Endi PDF fayl yuboring — men uni tarjima qilib qaytaraman!"
            )
            return

        if get_api_key(user_id):
            bot.send_message(message.chat.id, "📄 PDF fayl yuboring.")
        else:
            bot.send_message(
                message.chat.id,
                "Avval API key kiriting 👇",
                reply_markup=main_keyboard(user_id)
            )
    except Exception:
        print(traceback.format_exc())

@bot.message_handler(content_types=["document"])
def handle_document(message):
    user_id = message.from_user.id
    chat_id = message.chat.id

    try:
        api_key = get_api_key(user_id)
        if not api_key:
            bot.send_message(chat_id, "❌ Avval API keyingizni kiriting!", reply_markup=main_keyboard(user_id))
            return

        if not message.document.file_name.lower().endswith(".pdf"):
            bot.send_message(chat_id, "❌ Faqat PDF fayl yuboring!")
            return

        # Telegram bot orqali fayl yuklab olish chegarasi ~20MB
        if message.document.file_size and message.document.file_size > 20 * 1024 * 1024:
            bot.send_message(
                chat_id,
                "❌ Fayl hajmi 20MB dan katta. Telegram bot orqali bunday faylni yuklab bo'lmaydi.\n"
                "Iltimos, faylni siqib (kichraytirib), 20MB dan kam holatda qayta yuboring."
            )
            return

        position = enqueue_task(chat_id, user_id, message.document)

        if position <= 1:
            bot.send_message(chat_id, "⏳ Qabul qilindi, ishlanmoqda...")
        else:
            bot.send_message(
                chat_id,
                f"📥 Qabul qilindi! Navbatda turibsiz: {position - 1} ta fayl sizdan oldin ishlanadi."
            )

    except Exception:
        print(traceback.format_exc())
        try:
            bot.send_message(chat_id, "❌ Kutilmagan xato yuz berdi. Iltimos, qayta urinib ko'ring.")
        except Exception:
            pass


def process_pdf(chat_id, user_id, document):
    """Navbatdan olingan bitta PDF'ni to'liq qayta ishlaydi. Worker thread shu funksiyani chaqiradi."""
    all_temp_files = []
    status = None

    try:
        api_key = get_api_key(user_id)
        if not api_key:
            bot.send_message(chat_id, "❌ API key topilmadi, qayta kiriting.", reply_markup=main_keyboard(user_id))
            return

        status = bot.send_message(chat_id, "🚀 Navbatingiz keldi, ishlanmoqda...")

        # Har bir vazifa uchun unique session id — fayl nomlari to'qnashmasligi uchun
        session_id = f"{user_id}_{uuid.uuid4().hex[:8]}"

        file_info = bot.get_file(document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        pdf_path = f"input_{session_id}.pdf"
        with open(pdf_path, "wb") as f:
            f.write(downloaded)
        all_temp_files.append(pdf_path)

        bot.edit_message_text("📄 Sahifalar rasmga aylantirilmoqda...", chat_id, status.message_id)
        pages = pdf_to_images(pdf_path, prefix=f"u{session_id}")
        pages.sort(key=page_index)
        all_temp_files.extend(pages)
        total = len(pages)

        translated = []
        for i, page in enumerate(pages):
            bot.edit_message_text(f"🔎 Sifat oshirilmoqda: {i+1}/{total} sahifa...", chat_id, status.message_id)
            upscale_image(page)

            bot.edit_message_text(f"🔄 Tarjima: {i+1}/{total} sahifa...", chat_id, status.message_id)
            result, error = translate_image(page, api_key)
            if error:
                bot.edit_message_text(f"❌ {i+1}-sahifada xato: {error}", chat_id, status.message_id)
                cleanup(all_temp_files + translated)
                return
            translated.append(result)

        all_temp_files.extend(translated)

        bot.edit_message_text("📎 Yakuniy PDF yaratilmoqda...", chat_id, status.message_id)
        output_pdf = f"translated_{session_id}.pdf"
        # Tartib pages bilan bir xil bo'lishi uchun translated ro'yxati ham shu tartibda yig'ilgan
        images_to_pdf(translated, output_pdf)
        all_temp_files.append(output_pdf)

        with open(output_pdf, "rb") as f:
            bot.send_document(chat_id, f, caption="✅ Tarjima tayyor!")

        bot.delete_message(chat_id, status.message_id)

    except Exception:
        print(traceback.format_exc())
        try:
            if status:
                bot.edit_message_text("❌ Kutilmagan xato yuz berdi. Iltimos, qayta urinib ko'ring.", chat_id, status.message_id)
            else:
                bot.send_message(chat_id, "❌ Kutilmagan xato yuz berdi. Iltimos, qayta urinib ko'ring.")
        except Exception:
            pass
    finally:
        cleanup(all_temp_files)

# ---------- Botni doimiy ishlatish (crash bo'lsa qayta ishga tushadi) ----------

if __name__ == "__main__":
    # Navbatni ishlovchi worker thread'ni ishga tushiramiz
    worker_thread = threading.Thread(target=worker_loop, daemon=True)
    worker_thread.start()

    while True:
        try:
            print("Bot ishga tushdi...")
            bot.polling(none_stop=True, interval=1, timeout=60)
        except Exception:
            print("Bot yiqildi, qayta ishga tushiriladi:")
            print(traceback.format_exc())

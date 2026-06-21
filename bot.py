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
import fitz  # PyMuPDF
from PIL import Image

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable o'rnatilmagan! Avval uni sozlang.")

bot = telebot.TeleBot(BOT_TOKEN)

DATA_FILE = "users.json"

# Manhwa-style sahifalarni qayta bo'laklash uchun maksimal balandlik (piksel).
# Ketma-ket original sahifalar shu chegaragacha bir-biriga qo'shiladi;
# bitta sahifaning o'zi shundan katta bo'lsa, shu chegaradan bo'lib qirqiladi.
MAX_PAGE_HEIGHT = 15000

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
        try:
            error_data = response.json()
            detail = error_data.get("error") or error_data.get("message") or str(error_data)
        except Exception:
            detail = response.text[:300] if response.text else "tafsilot yo'q"
        return None, f"Server xatosi (status: {response.status_code}): {detail}"

    try:
        data = response.json()
    except Exception:
        return None, "Javobni o'qishda xato (noto'g'ri format)"

    # Server xatoni JSON body ichida success=false yoki "error" maydoni bilan
    # qaytarishi mumkin — buni header'dan emas, body'dan tekshiramiz.
    if data.get("success") is False or data.get("error"):
        error_msg = data.get("error", "Noma'lum xato")
        return None, error_msg

    try:
        img_data = data["image"].split(",")[1]
        img_bytes = base64.b64decode(img_data)
        out_path = f"translated_{os.path.basename(image_path)}"
        with open(out_path, "wb") as f:
            f.write(img_bytes)
        return out_path, None
    except Exception:
        return None, "Javobni o'qishda xato (noto'g'ri format)"

def images_to_pdf(image_paths, output_path):
    """Rasmlarni asl tartibda (sahifa raqami bo'yicha) PDF qilib yig'adi.

    To'g'ridan-to'g'ri PyMuPDF (fitz) orqali, faylni nomi bo'yicha o'qiydi —
    Pillow orqali ochish/RGB convert/PNG encode bosqichlarini butunlay
    chetlab o'tadi. Bu nafaqat tezroq, balki har bir rasm bittadan ochilib,
    sahifaga joylashtirilib, darhol yopilgani uchun BARCHA rasmlarni bir
    vaqtning o'zida RAM'da saqlamaydi — bu katta (masalan manga-style,
    20-30+ sahifali) PDF'larda xotira tugab qolishining (va shu sababli
    "Yakuniy PDF yaratilmoqda" bosqichida qotib qolishning) oldini oladi.
    """
    doc = fitz.open()
    try:
        for p in image_paths:
            img_doc = fitz.open(p)
            try:
                img_page = img_doc[0]
                rect = img_page.rect
                page = doc.new_page(width=rect.width, height=rect.height)
                page.insert_image(page.rect, filename=p)
            finally:
                img_doc.close()
        doc.save(output_path, deflate=True, garbage=3)
    finally:
        doc.close()

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

# ---------- Manhwa-style "smart regroup" ----------
# Manhwada ko'p sahifa pastga qarab uzun bo'ladi, ba'zilari esa qisqa.
# Bu funksiya original sahifalarni MAX_PAGE_HEIGHT chegarasiga moslab,
# qisqalarini birlashtirib, judaa uzunlarini esa bo'lib qayta yig'adi.
# Natijada hammasi bir xil (taxminan) balandlikdagi "page_N.png" fayllar bo'ladi
# va Torii API'ga shu fayllar yuboriladi (eski mantiqdagi pages o'rniga).

def regroup_pages(image_paths, prefix, max_height=MAX_PAGE_HEIGHT):
    """Ketma-ket rasmlarni max_height chegarasiga moslab qayta bo'laklaydi.

    - Ketma-ket rasmlar birlashtiriladi, lekin yig'indi balandlik
      max_height'dan oshmaydi.
    - Agar bitta rasmning o'zi max_height'dan katta bo'lsa, u
      max_height bo'yicha bo'laklarga bo'linadi (oxirgi qismi
      keyingi rasm bilan davom etadi).
    - Hamma rasmlar bir xil kenglikka (eng kichik kenglik bo'yicha)
      moslashtiriladi, chunki vertikal birlashtirish uchun kengliklar
      bir xil bo'lishi shart.

    Tezlik uchun: canvas har safar to'liq qayta yaratilmaydi (bu
    ko'p sahifali manga'larda juda sekin bo'lardi — O(n^2)). O'rniga
    canvas bir marta max_height o'lchamda yaratiladi va unga faqat
    "joylanadi" (paste), oxirida haqiqiy balandlikkacha kesib olinadi.

    Qaytaradi: yangi fayllar ro'yxati (tartib bo'yicha saralangan).
    """
    if not image_paths:
        return []

    print(f"[regroup] boshlandi: {len(image_paths)} ta original sahifa")

    # Eng kichik kenglikni topamiz — barcha rasmlarni shunga moslashtiramiz,
    # aks holda vertikal qo'shishda kengliklar mos kelmay xato beradi.
    widths = []
    for p in image_paths:
        with Image.open(p) as im:
            widths.append(im.width)
    target_width = min(widths)
    print(f"[regroup] target_width={target_width}")

    out_paths = []
    out_index = 0

    # Canvasni bir marta max_height balandlikda yaratamiz, faqat "paste"
    # qilamiz (hech qachon to'liq qayta yaratmaymiz/ko'chirmaymiz).
    current_canvas = Image.new("RGB", (target_width, max_height), "white")
    current_height = 0
    canvas_used = False  # canvasga hech narsa joylanmagan bo'lsa, saqlashda chiqarib tashlaymiz

    def flush_canvas():
        nonlocal current_canvas, current_height, out_index, canvas_used
        if not canvas_used:
            return
        out_path = f"{prefix}_page_{out_index}.png"
        # Faqat haqiqiy to'ldirilgan qismini saqlaymiz (bo'sh joy qolmasin)
        current_canvas.crop((0, 0, target_width, current_height)).save(out_path)
        out_paths.append(out_path)
        out_index += 1
        current_canvas = Image.new("RGB", (target_width, max_height), "white")
        current_height = 0
        canvas_used = False

    def append_to_canvas(img):
        """img'ni joriy canvasga pastdan joylaydi (faqat paste, hech narsa ko'chirilmaydi)."""
        nonlocal current_height, canvas_used
        current_canvas.paste(img, (0, current_height))
        current_height += img.height
        canvas_used = True

    for idx, path in enumerate(image_paths):
        print(f"[regroup] {idx+1}/{len(image_paths)}: {path} ochilmoqda...")
        with Image.open(path) as im:
            img = im.convert("RGB")
            # Kenglikni target_width'ga moslashtiramiz (proporsional balandlik bilan)
            if img.width != target_width:
                new_h = int(img.height * (target_width / img.width))
                img = img.resize((target_width, new_h), Image.LANCZOS)
            print(f"[regroup] {idx+1}/{len(image_paths)}: o'lcham {img.size}, current_height={current_height}")

            remaining = img
            safety_counter = 0
            while remaining is not None:
                safety_counter += 1
                if safety_counter > 1000:
                    # Xavfsizlik to'sig'i: agar biror sababdan tsikl normal yakunlanmasa,
                    # cheksiz aylanib qolmasligi uchun majburan to'xtatamiz.
                    print(f"[regroup] OGOHLANTIRISH: {path} uchun xavfsizlik chegarasiga yetdi, majburan to'xtatildi!")
                    break

                room_left = max_height - current_height

                if room_left <= 0:
                    # Joriy canvas allaqachon to'lgan — bo'shatamiz va yangidan boshlaymiz
                    flush_canvas()
                    room_left = max_height

                if remaining.height <= room_left:
                    # To'liq sig'adi — qo'shib, keyingi rasmga o'tamiz
                    append_to_canvas(remaining)
                    remaining = None
                else:
                    # Sig'maydi — kerakli joydan qirqib, qolganini keyingi bo'lakka qoldiramiz
                    top_part = remaining.crop((0, 0, target_width, room_left))
                    append_to_canvas(top_part)
                    bottom_part = remaining.crop((0, room_left, target_width, remaining.height))
                    remaining = bottom_part
                    # Canvas to'lgani aniq — bo'shatib, qolgan qismni keyingi davrada davom ettiramiz
                    flush_canvas()
        print(f"[regroup] {idx+1}/{len(image_paths)}: tugadi")

    flush_canvas()
    print(f"[regroup] yakunlandi: {len(out_paths)} ta yangi sahifa")
    return out_paths

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
            "Men PDF fayllarini o'zbekchaga tarjima qilaman.\n\n"
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

        file_name = message.document.file_name or ""
        if not file_name.lower().endswith(".pdf"):
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
        raw_pages = pdf_to_images(pdf_path, prefix=f"u{session_id}")
        raw_pages.sort(key=page_index)
        all_temp_files.extend(raw_pages)

        bot.edit_message_text("🧩 Sahifalar moslashtirilmoqda...", chat_id, status.message_id)
        pages = regroup_pages(raw_pages, prefix=f"r{session_id}")
        all_temp_files.extend(pages)
        total = len(pages)

        translated = []
        for i, page in enumerate(pages):
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

import telebot
import requests
import os
import base64
from PIL import Image
from pdf2image import convert_from_path

BOT_TOKEN = os.environ.get("BOT_TOKEN", "BOT_TOKENINGIZ")

bot = telebot.TeleBot(BOT_TOKEN)

# Har bir foydalanuvchining API keyini saqlash uchun
user_api_keys = {}

def pdf_to_images(pdf_path):
    images = convert_from_path(pdf_path, dpi=150)
    paths = []
    for i, img in enumerate(images):
        path = f"page_{i}.png"
        img.save(path, "PNG")
        paths.append(path)
    return paths

def translate_image(image_path, api_key):
    with open(image_path, "rb") as f:
        response = requests.post(
            "https://api.toriitranslate.com/api/v2/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": f},
            data={
                "target_lang": "uz",
                "translator": "gemini-flash-lite",
                "font": "NotoSans",
                "text_align": "auto"
            }
        )
    if response.headers.get("success") == "true":
        data = response.json()
        img_data = data["image"].split(",")[1]
        img_bytes = base64.b64decode(img_data)
        out_path = f"translated_{os.path.basename(image_path)}"
        with open(out_path, "wb") as f:
            f.write(img_bytes)
        return out_path, None
    else:
        try:
            error_data = response.json()
            error_msg = error_data.get("error", "Noma'lum xato")
        except:
            error_msg = "API bilan bog'lanishda xato"
        return None, error_msg

def images_to_pdf(image_paths, output_path):
    imgs = []
    for p in image_paths:
        img = Image.open(p).convert("RGB")
        imgs.append(img)
    if imgs:
        imgs[0].save(output_path, save_all=True, append_images=imgs[1:])

@bot.message_handler(commands=["start"])
def start(message):
    user_id = message.from_user.id
    bot.send_message(
        message.chat.id,
        "Salom! 👋\n\n"
        "Men manga/rasm PDF fayllarini o'zbekchaga tarjima qilaman.\n\n"
        "🔑 Ishni boshlash uchun avval Torii API keyingizni yuboring.\n\n"
        "API key olish uchun: https://toriitranslate.com/api"
    )
    user_api_keys[user_id] = None

@bot.message_handler(commands=["setkey"])
def set_key(message):
    bot.send_message(message.chat.id, "🔑 Torii API keyingizni yuboring:")

@bot.message_handler(func=lambda m: m.content_type == "text" and not m.text.startswith("/"))
def handle_text(message):
    user_id = message.from_user.id
    text = message.text.strip()

    # Agar foydalanuvchida hali API key bo'lmasa, bu xabarni key deb qabul qilamiz
    if user_api_keys.get(user_id) is None:
        user_api_keys[user_id] = text
        bot.send_message(
            message.chat.id,
            "✅ API key saqlandi!\n\n📄 Endi PDF fayl yuboring — men uni tarjima qilib qaytaraman!"
        )
    else:
        bot.send_message(
            message.chat.id,
            "📄 PDF fayl yuboring, yoki API keyni o'zgartirish uchun /setkey buyrug'ini yuboring."
        )

@bot.message_handler(content_types=["document"])
def handle_document(message):
    user_id = message.from_user.id
    api_key = user_api_keys.get(user_id)

    if not api_key:
        bot.send_message(message.chat.id, "❌ Avval /start bosib, API keyingizni kiriting!")
        return

    if not message.document.file_name.lower().endswith(".pdf"):
        bot.send_message(message.chat.id, "❌ Faqat PDF fayl yuboring!")
        return

    status = bot.send_message(message.chat.id, "⏳ Qabul qilindi, ishlanmoqda...")

    file_info = bot.get_file(message.document.file_id)
    downloaded = bot.download_file(file_info.file_path)
    pdf_path = f"input_{user_id}.pdf"
    with open(pdf_path, "wb") as f:
        f.write(downloaded)

    bot.edit_message_text("📄 Rasmlarga aylantirilmoqda...", message.chat.id, status.message_id)
    pages = pdf_to_images(pdf_path)
    total = len(pages)

    translated = []
    for i, page in enumerate(pages):
        bot.edit_message_text(f"🔄 Tarjima: {i+1}/{total} sahifa...", message.chat.id, status.message_id)
        result, error = translate_image(page, api_key)
        if error:
            bot.send_message(message.chat.id, f"❌ Xato: {error}")
            return
        translated.append(result)

    bot.edit_message_text("📎 PDF yaratilmoqda...", message.chat.id, status.message_id)
    output_pdf = f"translated_{user_id}.pdf"
    images_to_pdf(translated, output_pdf)

    with open(output_pdf, "rb") as f:
        bot.send_document(message.chat.id, f, caption="✅ Tarjima tayyor!")

    bot.delete_message(message.chat.id, status.message_id)

    for p in pages + translated:
        if os.path.exists(p):
            os.remove(p)
    for p in [pdf_path, output_pdf]:
        if os.path.exists(p):
            os.remove(p)

bot.polling()

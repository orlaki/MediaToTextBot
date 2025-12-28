import os
import time
import logging
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from faster_whisper import WhisperModel

BOT_TOKEN = os.environ.get("BOT_TOKEN", "7188814271:AAE6mUVUXnrMH9bQEdywNJLSrxfUfjZAh90")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

model = WhisperModel(
    model_size_or_path="tiny",
    device="cpu",
    compute_type="int8"
)

LANGS = [
("🇬🇧 English","en"), ("🇸🇦 العربية","ar"), ("🇪🇸 Español","es"), ("🇫🇷 Français","fr"),
("🇷🇺 Русский","ru"), ("🇩🇪 Deutsch","de"), ("🇮🇳 हिन्दी","hi"), ("🇮🇷 فارسی","fa"),
("🇮🇩 Indonesia","id"), ("🇺🇦 Українська","uk"), ("🇦🇿 Azərbaycan","az"), ("🇮🇹 Italiano","it"),
("🇹🇷 Türkçe","tr"), ("🇧🇬 Български","bg"), ("🇷🇸 Srpski","sr"), ("🇵🇰 اردو","ur"),
("🇹🇭 ไทย","th"), ("🇻🇳 Tiếng Việt","vi"), ("🇯🇵 日本語","ja"), ("🇰🇷 한국어","ko"),
("🇨🇳 中文","zh"), ("🇳🇱 Nederlands","nl"), ("🇸🇪 Svenska","sv"), ("🇳🇴 Norsk","no"),
("🇮🇱 עברית","he"), ("🇩🇰 Dansk","da"), ("🇪🇹 አማርኛ","am"), ("🇫🇮 Suomi","fi"),
("🇧🇩 বাংলা","bn"), ("🇰🇪 Kiswahili","sw"), ("🇪🇹 Oromo","om"), ("🇳🇵 नेपाली","ne"),
("🇵🇱 Polski","pl"), ("🇬🇷 Ελληνικά","el"), ("🇨🇿 Čeština","cs"), ("🇮🇸 Íslenska","is"),
("🇱🇹 Lietuvių","lt"), ("🇱🇻 Latviešu","lv"), ("🇭🇷 Hrvatski","hr"), ("🇧🇦 Bosanski","bs"),
("🇭🇺 Magyar","hu"), ("🇷🇴 Română","ro"), ("🇸🇴 Somali","so"), ("🇲🇾 Melayu","ms"),
("🇺🇿 O'zbekcha","uz"), ("🇵🇭 Tagalog","tl"), ("🇵🇹 Português","pt")
]

user_mode = {}
user_selected_lang = {}
pending_files = {}
user_transcriptions = {}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

def get_user_mode(uid):
    return user_mode.get(uid, "📄 Text File")

def build_lang_keyboard(origin):
    rows = []
    row = []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}"))
        if i % 3 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def ensure_joined(message):
    if not REQUIRED_CHANNEL:
        return True
    try:
        m = bot.get_chat_member(REQUIRED_CHANNEL, message.from_user.id)
        if m.status in ["member", "administrator", "creator"]:
            return True
    except:
        pass
    clean = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean}")]])
    bot.reply_to(message, "First join the channel", reply_markup=kb)
    return False

def whisper_transcribe(path, language):
    segments, _ = model.transcribe(path, language=language)
    text = []
    for s in segments:
        text.append(s.text)
    return "".join(text).strip()

def send_long_text(chat_id, text, reply_id, uid):
    mode = get_user_mode(uid)
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            last = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                last = bot.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return last
        else:
            fname = os.path.join(DOWNLOADS_DIR, "Transcript.txt")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            sent = bot.send_document(chat_id, open(fname, "rb"), reply_to_message_id=reply_id)
            os.remove(fname)
            return sent
    return bot.send_message(chat_id, text, reply_to_message_id=reply_id)

@bot.message_handler(commands=["start","help"])
def start_cmd(message):
    if not ensure_joined(message):
        return
    kb = build_lang_keyboard("file")
    bot.reply_to(message, "Send voice, audio or video\nSelect language:", reply_markup=kb)

@bot.message_handler(commands=["mode"])
def mode_cmd(message):
    if not ensure_joined(message):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
        [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
    ])
    bot.reply_to(message, "Choose output mode:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("mode|"))
def mode_cb(call):
    user_mode[call.from_user.id] = call.data.split("|")[1]
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    bot.answer_callback_query(call.id, "Mode updated")

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang|"))
def lang_cb(call):
    _, code, lbl, origin = call.data.split("|")
    chat_id = call.message.chat.id
    user_selected_lang[chat_id] = code
    try:
        bot.delete_message(chat_id, call.message.message_id)
    except:
        pass
    bot.answer_callback_query(call.id, f"Language set: {lbl}")
    pending = pending_files.pop(chat_id, None)
    if not pending:
        return
    path = pending["path"]
    msg = pending["message"]
    bot.send_chat_action(chat_id, "typing")
    try:
        text = whisper_transcribe(path, code)
        sent = send_long_text(chat_id, text, msg.id, msg.from_user.id)
        if sent:
            user_transcriptions.setdefault(chat_id, {})[sent.message_id] = text
    finally:
        if os.path.exists(path):
            os.remove(path)

@bot.message_handler(content_types=["voice","audio","video","document"])
def media_handler(message):
    if not ensure_joined(message):
        return
    media = message.voice or message.audio or message.video or message.document
    if getattr(media, "file_size", 0) > MAX_UPLOAD_SIZE:
        bot.reply_to(message, "File too large")
        return
    file_path = os.path.join(DOWNLOADS_DIR, f"{message.id}_{media.file_unique_id}")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        info = bot.get_file(media.file_id)
        data = bot.download_file(info.file_path)
        with open(file_path, "wb") as f:
            f.write(data)
        lang = user_selected_lang.get(message.chat.id)
        if not lang:
            pending_files[message.chat.id] = {"path": file_path, "message": message}
            kb = build_lang_keyboard("file")
            bot.reply_to(message, "Select language:", reply_markup=kb)
            return
        text = whisper_transcribe(file_path, lang)
        sent = send_long_text(message.chat.id, text, message.id, message.from_user.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.message_id] = text
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

if __name__ == "__main__":
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

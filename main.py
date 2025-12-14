import os
import threading
import json
import requests
import logging
import time
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update
import speech_recognition as sr
from pydub import AudioSegment
from pydub.silence import split_on_silence

BOT_TOKEN = os.environ.get("BOT2_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.path.join(os.getcwd(), "downloads")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
GEMINI_KEYS = os.environ.get("GEMINI_KEYS", GEMINI_KEY)
GEMINI_MODEL = "gemini-2.0-flash"

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KeyRotator:
    def __init__(self, keys):
        self.keys = [k.strip() for k in keys.split(",") if k.strip()] if isinstance(keys, str) else list(keys or [])
        self.pos = 0
        self.lock = threading.Lock()
    def get_key(self):
        with self.lock:
            if not self.keys:
                return None
            key = self.keys[self.pos]
            self.pos = (self.pos + 1) % len(self.keys)
            return key
    def mark_success(self, key):
        with self.lock:
            try:
                i = self.keys.index(key)
                self.pos = (i + 1) % len(self.keys)
            except ValueError:
                pass
    def mark_failure(self, key):
        self.mark_success(key)

gemini_rotator = KeyRotator(GEMINI_KEYS)

LANGS = [
("🇬🇧 English","en-US"), ("🇸🇦 العربية","ar-SA"), ("🇪🇸 Español","es-ES"), ("🇫🇷 Français","fr-FR"),
("🇷🇺 Русский","ru-RU"), ("🇩🇪 Deutsch","de-DE"), ("🇮🇳 हिन्दी","hi-IN"), ("🇮🇷 فارسی","fa-IR"),
("🇮🇩 Indonesia","id-ID"), ("🇺🇦 Українська","uk-UA"), ("🇦🇿 Azərbaycan","az-AZ"), ("🇮🇹 Italiano","it-IT"),
("🇹🇷 Türkçe","tr-TR"), ("🇧🇬 Български","bg-BG"), ("🇷🇸 Srpski","sr-RS"), ("🇵🇰 اردو","ur-PK"),
("🇹🇭 ไทย","th-TH"), ("🇻🇳 Tiếng Việt","vi-VN"), ("🇯🇵 日本語","ja-JP"), ("🇰🇷 한국어","ko-KR"),
("🇨🇳 中文","zh-CN"), ("🇳🇱 Nederlands","nl-NL"), ("🇸🇪 Svenska","sv-SE"), ("🇳🇴 Norsk","no-NO"),
("🇮🇱 עברית","he-IL"), ("🇩🇰 Dansk","da-DK"), ("🇪🇹 አማርኛ","am-ET"), ("🇫🇮 Suomi","fi-FI"),
("🇧🇩 বাংলা","bn-BD"), ("🇰🇪 Kiswahili","sw-KE"), ("🇪🇹 Oromo","om-ET"), ("🇳🇵 नेपाली","ne-NP"),
("🇵🇱 Polski","pl-PL"), ("🇬🇷 Ελληνικά","el-GR"), ("🇨🇿 Čeština","cs-CZ"), ("🇮🇸 Íslenska","is-IS"),
("🇱🇹 Lietuvių","lt-LT"), ("🇱🇻 Latviešu","lv-LV"), ("🇭🇷 Hrvatski","hr-HR"), ("🇷🇸 Bosanski","bs-BA"),
("🇭🇺 Magyar","hu-HU"), ("🇷🇴 Română","ro-RO"), ("🇸🇴 Somali","so-SO"), ("🇲🇾 Melayu","ms-MY"),
("🇺🇿 O'zbekcha","uz-UZ"), ("🇵🇭 Tagalog","tl-PH"), ("🇵🇹 Português","pt-PT")
]

user_mode = {}
user_transcriptions = {}
user_selected_lang = {}
pending_files = {}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
flask_app = Flask(__name__)

def get_user_mode(uid):
    return user_mode.get(uid, "📄 Text File")

def transcribe_audio_local(file_path, lang_code):
    recognizer = sr.Recognizer()
    
    if not file_path.endswith(".wav"):
        audio = AudioSegment.from_file(file_path)
        wav_path = file_path + ".wav"
        audio.export(wav_path, format="wav")
    else:
        wav_path = file_path

    audio_segment = AudioSegment.from_wav(wav_path)
    
    chunks = split_on_silence(
        audio_segment,
        min_silence_len=700,
        silence_thresh=audio_segment.dBFS - 14,
        keep_silence=500
    )

    full_text = []
    
    for i, chunk in enumerate(chunks):
        chunk_filename = os.path.join(DOWNLOADS_DIR, f"chunk{i}.wav")
        chunk.export(chunk_filename, format="wav")
        
        with sr.AudioFile(chunk_filename) as source:
            audio_data = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio_data, language=lang_code)
                full_text.append(text)
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                logging.error(f"Google Speech Request Error: {e}")
                continue
            finally:
                if os.path.exists(chunk_filename):
                    os.remove(chunk_filename)
    
    if not file_path.endswith(".wav") and os.path.exists(wav_path):
        os.remove(wav_path)
        
    return " ".join(full_text)

def gemini_api_call(endpoint, payload, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def execute_gemini_action(action_callback):
    last_exc = None
    total = len(gemini_rotator.keys) or 1
    for _ in range(total + 1):
        key = gemini_rotator.get_key()
        if not key:
            raise RuntimeError("No Gemini keys available")
        try:
            result = action_callback(key)
            gemini_rotator.mark_success(key)
            return result
        except Exception as e:
            last_exc = e
            logging.warning(f"Gemini error: {e}")
            gemini_rotator.mark_failure(key)
    raise RuntimeError(f"Gemini failed: {last_exc}")

def ask_gemini(text, instruction):
    if not gemini_rotator.keys:
        raise RuntimeError("GEMINI_KEY not configured")
    def perform(key):
        payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
        data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except:
            raise RuntimeError("Unexpected Gemini response")
    return execute_gemini_action(perform)

def build_action_keyboard(text_len):
    btns = []
    if text_len > 500:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
    return InlineKeyboardMarkup(btns)

def build_lang_keyboard(origin):
    btns, row = [], []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}"))
        if i % 3 == 0:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    return InlineKeyboardMarkup(btns)

def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)

def ensure_joined(message):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, message.from_user.id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
    except:
        pass
    clean = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean}")]])
    bot.reply_to(message, "Fadlan marka hore ku soo biir kanaalka, ka dibna dib u soo laabo 👍", reply_markup=kb)
    return False

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if ensure_joined(message):
        welcome_text = (
            "👋 Salaam!\n"
            "• Ii soo dir\n"
            "• Voice message\n"
            "• Audio file\n"
            "• Video\n"
            "Si aan ugu beddelo qoraal (Transcribe).\n\n"
            "Fadlan dooro luqadda looga hadlayo codka:"
        )
        kb = build_lang_keyboard("file")
        bot.reply_to(message, welcome_text, reply_markup=kb, parse_mode="Markdown")

@bot.message_handler(commands=['mode'])
def choose_mode(message):
    if ensure_joined(message):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
            [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
        ])
        bot.reply_to(message, "Sidee u rabtaa inaan kuu soo diro qoraalka dheer?:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('mode|'))
def mode_cb(call):
    if not ensure_joined(call.message):
        return
    mode = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode
    try:
        bot.edit_message_text(f"Waxaad dooratay: {mode}", call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    bot.answer_callback_query(call.id, f"Mode-ka waa la beddelay: {mode}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('lang|'))
def lang_cb(call):
    _, code, lbl, origin = call.data.split("|")
    chat_id = call.message.chat.id
    
    if origin != "file":
        process_text_action(call, origin, f"Translate to {lbl}", f"Translate this text in to language {lbl}. No extra text ONLY return the translated text.")
        return

    try:
        bot.delete_message(chat_id, call.message.message_id)
    except:
        pass
        
    user_selected_lang[chat_id] = code
    bot.answer_callback_query(call.id, f"Luqadda: {lbl}")
    
    pending = pending_files.pop(chat_id, None)
    if not pending:
        return
        
    file_path = pending.get("path")
    orig_msg = pending.get("message")
    
    msg_status = bot.send_message(chat_id, "⏳ Waa la shaqaynayaa, fadlan sug...")
    
    try:
        text = transcribe_audio_local(file_path, code)
        bot.delete_message(chat_id, msg_status.message_id)
        
        if not text:
            bot.send_message(chat_id, "❌ Waan ka xumahay, wax hadal ah waa laga waayey codka.")
            return
            
        sent = send_long_text(chat_id, text, orig_msg.id, orig_msg.from_user.id)
        if sent:
            user_transcriptions.setdefault(chat_id, {})[sent.message_id] = {"text": text, "origin": orig_msg.id}
            bot.edit_message_reply_markup(chat_id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
    except Exception as e:
        bot.send_message(chat_id, f"❌ Khalad baa dhacay: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

@bot.callback_query_handler(func=lambda c: c.data.startswith('summarize_menu|'))
def action_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_summarize_keyboard(call.message.id))
    except:
        pass

@bot.callback_query_handler(func=lambda c: c.data.startswith('summopt|'))
def summopt_cb(call):
    _, style, origin = call.data.split("|")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    
    prompts = {
        "Short": "Summarize this text in the original language in 1-2 concise sentences. No extra text.",
        "Detailed": "Summarize this text in the original language in a detailed paragraph. No extra text.",
        "Bulleted": "Summarize this text in the original language as a bulleted list. No extra text."
    }
    process_text_action(call, origin, f"Summarize ({style})", prompts.get(style))

def process_text_action(call, origin_msg_id, log_action, prompt_instr):
    chat_id = call.message.chat.id
    origin_id = int(origin_msg_id)
    data = user_transcriptions.get(chat_id, {}).get(origin_id)
    
    if not data:
        bot.answer_callback_query(call.id, "Xogta lama helin, fadlan mar kale soo dir file-ka.", show_alert=True)
        return
        
    bot.answer_callback_query(call.id, "Waa la diyaarinayaa...")
    try:
        res = ask_gemini(data["text"], prompt_instr)
        send_long_text(chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        bot.send_message(chat_id, f"Error: {e}")

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    if not ensure_joined(message):
        return
    media = message.voice or message.audio or message.video or (message.document if message.document.mime_type.startswith(('audio', 'video')) else None)
    if not media:
        return
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        bot.reply_to(message, f"Fadlan soo dir file ka yar {MAX_UPLOAD_MB}MB")
        return
        
    file_info = bot.get_file(media.file_id)
    downloaded = bot.download_file(file_info.file_path)
    ext = file_info.file_path.split('.')[-1]
    file_path = os.path.join(DOWNLOADS_DIR, f"{message.id}.{ext}")
    
    with open(file_path, 'wb') as f:
        f.write(downloaded)
        
    lang = user_selected_lang.get(message.chat.id)
    if not lang:
        pending_files[message.chat.id] = {"path": file_path, "message": message}
        bot.reply_to(message, "Dooro luqadda codka:", reply_markup=build_lang_keyboard("file"))
        return
        
    msg_status = bot.send_message(message.chat.id, "⏳ Waa la shaqaynayaa, fadlan sug...")
    try:
        text = transcribe_audio_local(file_path, lang)
        bot.delete_message(message.chat.id, msg_status.message_id)
        if text:
            sent = send_long_text(message.chat.id, text, message.id, message.from_user.id)
            user_transcriptions.setdefault(message.chat.id, {})[sent.message_id] = {"text": text, "origin": message.id}
            bot.edit_message_reply_markup(message.chat.id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
        else:
            bot.send_message(message.chat.id, "❌ Wax hadal ah waa laga waayey.")
    except Exception as e:
        bot.reply_to(message, f"❌ Khalad: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

def send_long_text(chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid)
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                sent = bot.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            with open(fname, 'rb') as f:
                sent = bot.send_document(chat_id, f, caption="Waa kan qoraalkaagii oo dhammaystiran 📄", reply_to_message_id=reply_id)
            os.remove(fname)
            return sent
    return bot.send_message(chat_id, text, reply_to_message_id=reply_id)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot is active", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([Update.de_json(request.get_data().decode('utf-8'))])
        return '', 200
    abort(403)

if __name__ == "__main__":
    if WEBHOOK_URL:
        bot.remove_webhook()
        time.sleep(0.1)
        bot.set_webhook(url=WEBHOOK_URL)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        bot.infinity_polling()

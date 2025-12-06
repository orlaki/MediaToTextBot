import os
import asyncio
import threading
import json
import requests
import logging
import time
import subprocess
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatAction, ChatMemberStatus

FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "/usr/bin/ffmpeg")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
MAX_AUDIO_DURATION_SEC = 9 * 60 * 60

DEFAULT_GEMINI_KEYS = os.environ.get("DEFAULT_GEMINI_KEYS", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEYS = os.environ.get("GEMINI_API_KEYS", DEFAULT_GEMINI_KEYS)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KeyRotator:
    def __init__(self, keys):
        self.keys = [k.strip() for k in keys.split(",") if k.strip()]
        self.pos = 0
        self.lock = threading.Lock()

    def get_order(self):
        with self.lock:
            n = len(self.keys)
            if n == 0:
                return []
            return [self.keys[(self.pos + i) % n] for i in range(n)]

    def mark_success(self, key):
        with self.lock:
            try:
                i = self.keys.index(key)
                self.pos = i
            except Exception:
                pass

    def mark_failure(self, key):
        with self.lock:
            n = len(self.keys)
            if n == 0:
                return
            try:
                i = self.keys.index(key)
                self.pos = (i + 1) % n
            except Exception:
                self.pos = (self.pos + 1) % n

gemini_rotator = KeyRotator(GEMINI_API_KEYS)

LANGS = [
("🇬🇧 English","en"), ("🇸🇦 العربية","ar"), ("🇪🇸 Español","es"), ("🇫🇷 Français","fr"),
("🇷🇺 Русский","ru"), ("🇩🇪 Deutsch","de"), ("🇮🇳 हिन्दी","hi"), ("🇮🇷 فارسی","fa"),
("🇮🇩 Indonesia","id"), ("🇺🇦 Українська","uk"), ("🇦🇿 Azərbaycan","az"), ("🇮🇹 Italiano","it"),
("🇹🇷 Türkçe","tr"), ("🇧🇬 Български","bg"), ("🇷🇸 Srpski","sr"), ("🇵🇰 اردو","ur"),
("🇹🇭 ไทย","th"), ("🇻🇳 Tiếng Việt","vi"), ("🇯🇵 日本語","ja"), ("🇰🇷 한국어","ko"),
("🇨🇳 中文","zh"), ("🇳🇱 Nederlands:nl", "nl"), ("🇸🇪 Svenska","sv"), ("🇳🇴 Norsk","no"),
("🇮🇱 עברית","he"), ("🇩🇰 Dansk","da"), ("🇪🇹 አማርኛ","am"), ("🇫🇮 Suomi","fi"),
("🇧🇩 বাংলা","bn"), ("🇰🇪 Kiswahili","sw"), ("🇪🇹 Oromo","om"), ("🇳🇵 नेपाली","ne"),
("🇵🇱 Polski","pl"), ("🇬🇷 Ελληνικά","el"), ("🇨🇿 Čeština","cs"), ("🇮🇸 Íslenska","is"),
("🇱🇹 Lietuvių","lt"), ("🇱🇻 Latviešu","lv"), ("🇭🇷 Hrvatski","hr"), ("🇷🇸 Bosanski","bs"),
("🇭🇺 Magyar","hu"), ("🇷🇴 Română","ro"), ("🇸🇴 Somali","so"), ("🇲🇾 Melayu","ms"),
("🇺🇿 O'zbekcha","uz"), ("🇵🇭 Tagalog","tl"), ("🇵🇹 Português","pt")
]

user_mode = {}
user_transcriptions = {}
action_usage = {}

app = Client("media_transcriber", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def get_user_mode(uid):
    return user_mode.get(uid, "📄 Text File")

def convert_to_wav_sync(input_path: str) -> str:
    if not FFMPEG_BINARY: raise RuntimeError("FFmpeg binary not found.")
    output_path = os.path.join(DOWNLOADS_DIR, f"{os.path.basename(input_path).split('.')[0]}_converted.wav")
    command = [FFMPEG_BINARY, "-i", input_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", output_path, "-y"]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path

def execute_gemini_action_sync(action_callback):
    last_exc = None
    for key in gemini_rotator.get_order():
        try:
            result = action_callback(key)
            gemini_rotator.mark_success(key)
            return result
        except Exception as e:
            last_exc = e
            logging.warning(f"Gemini error with key {str(key)[:4]}: {e}")
            gemini_rotator.mark_failure(key)
    raise RuntimeError(f"Gemini failed after rotations. Last error: {last_exc}")

def gemini_api_call_sync(endpoint, payload, key, headers=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_GEMINI)
    resp.raise_for_status()
    return resp.json()

def upload_and_transcribe_gemini_sync(file_path: str) -> str:
    original_path, converted_path = file_path, None
    file_ext = os.path.splitext(file_path)[1].lower()
    
    if file_ext not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
        converted_path = convert_to_wav_sync(file_path)
        file_path = converted_path
    
    file_size = os.path.getsize(file_path)
    mime_type = "audio/wav"

    def perform_upload_and_transcribe(key):
        uploaded_name = None
        try:
            upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
            headers = {
                "X-Goog-Upload-Protocol": "raw", "X-Goog-Upload-Command": "start, upload, finalize",
                "X-Goog-Upload-Header-Content-Length": str(file_size), "Content-Type": mime_type
            }
            with open(file_path, 'rb') as f:
                up_resp = requests.post(upload_url, headers=headers, data=f.read(), timeout=REQUEST_TIMEOUT_GEMINI).json()
            uploaded_name = up_resp.get("name", up_resp.get("file", {}).get("name"))
            uploaded_uri = up_resp.get("uri", up_resp.get("file", {}).get("uri"))
            if not uploaded_name: raise RuntimeError(f"Upload failed: {up_resp}")
            
            prompt = "Transcribe the audio in this file. Automatically detect the language and provide a clean transcription. Do not add intro phrases."
            payload = {"contents": [{"parts": [{"fileData": {"mimeType": mime_type, "fileUri": uploaded_uri}}, {"text": prompt}]}]}
            data = gemini_api_call_sync(f"models/{GEMINI_MODEL}:generateContent", payload, key, headers={"Content-Type": "application/json"})
            return data["candidates"][0]["content"]["parts"][0]["text"]
        finally:
            if uploaded_name:
                try: requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{uploaded_name}?key={key}", timeout=5)
                except: pass

    try:
        return execute_gemini_action_sync(perform_upload_and_transcribe)
    finally:
        if converted_path and os.path.exists(converted_path):
            os.remove(converted_path)

def ask_gemini_sync(text, instruction):
    def perform_text_query(key):
        payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
        data = gemini_api_call_sync(f"models/{GEMINI_MODEL}:generateContent", payload, key, headers={"Content-Type": "application/json"})
        return data["candidates"][0]["content"]["parts"][0]["text"]
    return execute_gemini_action_sync(perform_text_query)

def build_action_keyboard(text_len):
    btns = [[InlineKeyboardButton("⭐️ Get translating", callback_data="translate_menu|")]]
    if text_len > 1000:
        btns.append([InlineKeyboardButton("Summarize", callback_data="summarize|")])
    return InlineKeyboardMarkup(btns)

def build_lang_keyboard(origin):
    btns, row = [], []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}"))
        if i % 3 == 0:
            btns.append(row); row = []
    if row: btns.append(row)
    return InlineKeyboardMarkup(btns)

async def is_user_in_channel(client, user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await client.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, 
            ChatMemberStatus.OWNER, ChatMemberStatus.RESTRICTED
        )
    except Exception:
        return False

async def ensure_joined(client, obj):
    if not REQUIRED_CHANNEL:
        return True
    
    if isinstance(obj, CallbackQuery):
        uid = obj.from_user.id
        reply_target = obj.message
    else:
        uid = obj.from_user.id
        reply_target = obj

    try:
        if await is_user_in_channel(client, uid):
            return True
    except Exception:
        pass
    
    clean_channel_username = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean_channel_username}")]
    ])
    text = f"First, join my channel 😜"

    try:
        if isinstance(obj, CallbackQuery):
            await obj.answer("🚫 First join my channel", show_alert=True)
            await reply_target.reply_text(text, reply_markup=kb)
        else:
            await reply_target.reply_text(text, reply_markup=kb)
    except Exception:
        pass
    return False

async def send_long_text(client, chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid)
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                await client.send_chat_action(chat_id, ChatAction.TYPING)
                sent = await client.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(fname, "w", encoding="utf-8") as f: f.write(text)
            await client.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            sent = await client.send_document(chat_id, fname, caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            os.remove(fname)
            return sent
    return await client.send_message(chat_id, text, reply_to_message_id=reply_id)

@app.on_message(filters.command(["start", "help"]) & filters.private)
async def send_welcome(client, message: Message):
    if not await ensure_joined(client, message):
        return
    
    welcome_text = (
        "👋 **Salaam!**\n"
        "• Send me\n"
        "• **voice message**\n"
        "• **audio file**\n"
        "• **video**\n"
        "• to transcribe for free"
    )
    if message.text.split()[0] == "/help":
        welcome_text = (
            f"/start - Show welcome message\n"
            f"/help - This help message\n"
            f"/mode - Choose output format for long transcripts\n"
            f"Send a voice/audio/video (up to {MAX_UPLOAD_MB}MB) to transcribe"
        )
    
    await message.reply_text(welcome_text, parse_mode="Markdown")

@app.on_message(filters.command(['mode']) & filters.private)
async def choose_mode(client, message: Message):
    if not await ensure_joined(client, message):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
        [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
    ])
    await message.reply_text("How do I send you long transcripts?:", reply_markup=kb)

@app.on_callback_query(filters.regex(r'^mode\|'))
async def mode_cb(client, call: CallbackQuery):
    if not await ensure_joined(client, call): 
        return
    mode = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode
    await call.answer(f"Mode set to: {mode} ☑️")
    try:
        await call.message.edit_text(f"You chose: **{mode}**", reply_markup=None, parse_mode="Markdown")
    except Exception:
        pass

@app.on_callback_query(filters.regex(r'^lang\|'))
async def lang_cb(client, call: CallbackQuery):
    if not await ensure_joined(client, call): 
        return
    
    try:
        await call.message.edit_reply_markup(None)
    except Exception:
        pass
        
    _, code, lbl, origin = call.data.split("|")
    await call.answer(f"Translating to {lbl}...")
    
    await process_text_action(client, call, call.message.id, f"Translation to {lbl}", f"Translate this text in to language {lbl}. No extra text ONLY return the translated text.")

@app.on_callback_query(filters.regex(r'^(translate_menu|summarize)\|'))
async def action_cb(client, call: CallbackQuery):
    if not await ensure_joined(client, call): 
        return
        
    action, _ = call.data.split("|")
    
    if action == "translate_menu":
        await call.message.edit_reply_markup(build_lang_keyboard("trans"))
    else:
        try:
            await call.message.edit_reply_markup(None)
        except Exception:
            pass
            
        await process_text_action(client, call, call.message.id, "Summarize", "Summarize this in original language.")

async def process_text_action(client, call: CallbackQuery, msg_id, log_action, prompt_instr):
    chat_id = call.message.chat.id
    data = user_transcriptions.get(chat_id, {}).get(msg_id)
    
    if not data:
        await call.answer("Data not found (expired). Resend file.", show_alert=True)
        return
    
    text = data["text"]
    key = f"{chat_id}|{msg_id}|{log_action}"
    
    if "Summarize" in log_action and action_usage.get(key, 0) >= 1:
        await call.answer("Already summarized!", show_alert=True)
        return
        
    await call.answer("Processing...")
    await client.send_chat_action(chat_id, ChatAction.TYPING)
    
    try:
        res = await asyncio.to_thread(ask_gemini_sync, text, prompt_instr)
        
        if "Summarize" in log_action:
            action_usage[key] = action_usage.get(key, 0) + 1
            await call.message.edit_reply_markup(build_action_keyboard(len(text))) 

        await send_long_text(client, chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        await client.send_message(chat_id, f"❌ Error: {e}", reply_to_message_id=data["origin"])

@app.on_message(filters.private & (filters.audio | filters.voice | filters.video | filters.document))
async def handle_media(client, message: Message):
    if not await ensure_joined(client, message): 
        return
        
    media = message.voice or message.audio or message.video or message.document
    if not media: 
        return
    
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        await message.reply_text(f"Just Send me a file less than {MAX_UPLOAD_MB}MB 😎")
        return
    
    if getattr(media, 'duration', 0) > MAX_AUDIO_DURATION_SEC:
        hours = MAX_AUDIO_DURATION_SEC // 3600
        await message.reply_text(f"The bot does not accept a higher voice. {hours} hour. Please send a shorter one.")
        return
        
    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    file_path = None
    
    try:
        file_path = await message.download(file_name=os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_"))
        text = await asyncio.to_thread(upload_and_transcribe_gemini_sync, file_path)
        
        if not text: raise ValueError("Empty response or transcription failed.")
        
        sent = await send_long_text(client, message.chat.id, text, message.id, message.from_user.id)
        
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.id] = {"text": text, "origin": message.id}
            keyboard = build_action_keyboard(len(text))
            
            if len(text) > 1000:
                action_usage[f"{sent.chat.id}|{sent.id}|summarize"] = 0
            
            await sent.edit_reply_markup(keyboard)
            
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")
    finally:
        if file_path and os.path.exists(file_path): 
            os.remove(file_path)

if __name__ == "__main__":
    app.run()

import os
import asyncio
import requests
import logging
import time
import subprocess
import shutil
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputFile
from pyrogram.enums import ChatAction
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

DB_USER = "lakicalinuur"
DB_PASSWORD = "DjReFoWZGbwjry8K"
DB_APPNAME = "SpeechBot"
MONGO_URI = f"mongodb+srv://{DB_USER}:{DB_PASSWORD}@cluster0.n4hdlxk.mongodb.net/?retryWrites=true&w=majority&appName={DB_APPNAME}"

FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "ffmpeg")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "19"))
TUTORIAL_CHANNEL = "@NotifyBchat"
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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

user_transcriptions = {}
action_usage = {}
user_keys = {}
user_awaiting_key = {}
lock = asyncio.Lock()

app = Client("media_transcriber", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

mongo_client = None
db = None
users_col = None
actions_col = None

def now_ts():
    return int(time.time())

def init_mongo():
    global mongo_client, db, users_col, actions_col, user_keys, action_usage
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        db = mongo_client.get_database(DB_APPNAME or "SpeechBotDB")
        users_col = db.get_collection("users")
        actions_col = db.get_collection("action_usage")
        for doc in users_col.find({}):
            try:
                uid = int(doc["uid"])
                user_keys[uid] = {
                    "key": doc.get("key"),
                    "count": int(doc.get("count", 0)),
                    "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None
                }
            except:
                continue
        for doc in actions_col.find({}):
            k = doc.get("key")
            try:
                c = int(doc.get("count", 0))
            except:
                c = 0
            if k:
                action_usage[k] = c
    except ServerSelectionTimeoutError:
        mongo_client = None
        db = None
        users_col = None
        actions_col = None

init_mongo()

def persist_user_to_db(uid):
    if users_col is None:
        return
    info = user_keys.get(uid)
    if not info:
        users_col.delete_many({"uid": uid})
        return
    users_col.update_one(
        {"uid": uid},
        {"$set": {"uid": uid, "key": info.get("key"), "count": int(info.get("count", 0)), "window_start": info.get("window_start")}},
        upsert=True
    )

def persist_action_usage_to_db(key):
    if actions_col is None:
        return
    cnt = action_usage.get(key, 0)
    actions_col.update_one({"key": key}, {"$set": {"key": key, "count": int(cnt)}}, upsert=True)

def is_gemini_key(key):
    if not key:
        return False
    k = key.strip()
    return k.startswith("AIza") or k.startswith("AIzaSy")

async def store_user_key(uid, key):
    async with lock:
        user_keys[uid] = {"key": key.strip(), "count": 0, "window_start": now_ts()}
        user_awaiting_key.pop(uid, None)
    persist_user_to_db(uid)

async def reset_count_if_needed(uid):
    async with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if not doc:
                return
            info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
            user_keys[uid] = info
        if not info:
            return
        ws = info.get("window_start")
        if ws is None:
            info["count"] = 0
            info["window_start"] = now_ts()
            persist_user_to_db(uid)
            return
        elapsed = now_ts() - ws
        if elapsed >= 24 * 3600:
            info["count"] = 0
            info["window_start"] = now_ts()
            persist_user_to_db(uid)

async def increment_count(uid):
    async with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if not doc:
                return
            info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
            user_keys[uid] = info
        if not info:
            return
        info["count"] = info.get("count", 0) + 1
        if info.get("window_start") is None:
            info["window_start"] = now_ts()
        persist_user_to_db(uid)

async def seconds_left_for_user(uid):
    async with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if doc:
                info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
                user_keys[uid] = info
        if not info:
            return 0
        ws = info.get("window_start")
        if ws is None:
            return 0
        rem = 24 * 3600 - (now_ts() - ws)
        return rem if rem > 0 else 0

def format_hms(secs):
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s"

async def get_user_key_or_raise(uid):
    async with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if doc:
                info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
                user_keys[uid] = info
        if not info or not info.get("key"):
            raise RuntimeError("API_KEY_MISSING")
        ws = info.get("window_start")
        if ws is None:
            info["window_start"] = now_ts()
            info["count"] = 0
            persist_user_to_db(uid)
            return info["key"]
        elapsed = now_ts() - ws
        if elapsed >= 24 * 3600:
            info["window_start"] = now_ts()
            info["count"] = 0
            persist_user_to_db(uid)
            return info["key"]
        if info.get("count", 0) >= DAILY_LIMIT:
            rem = 24 * 3600 - elapsed
            raise RuntimeError(f"API_DAILY_LIMIT_REACHED|{int(rem)}")
        return info["key"]

def convert_to_wav_sync(input_path: str) -> str:
    if not FFMPEG_BINARY:
        raise RuntimeError("FFmpeg binary not found.")
    output_path = os.path.join(DOWNLOADS_DIR, f"{os.path.basename(input_path).split('.')[0]}_converted.wav")
    command = [FFMPEG_BINARY, "-i", input_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", output_path, "-y"]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path

async def convert_to_wav(input_path: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, convert_to_wav_sync, input_path)

def gemini_api_call_sync(endpoint, payload, key, headers=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    resp = requests.post(url, headers=headers or {"Content-Type": "application/json"}, json=payload, timeout=REQUEST_TIMEOUT_GEMINI)
    resp.raise_for_status()
    return resp.json()

async def upload_and_transcribe_gemini(file_path: str, uid: int) -> str:
    key = await get_user_key_or_raise(uid)
    original_path, converted_path = file_path, None
    if os.path.splitext(file_path)[1].lower() not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
        converted_path = await convert_to_wav(file_path)
        file_path = converted_path
    file_size = os.path.getsize(file_path)
    mime_type = "audio/wav"
    uploaded_name = None
    try:
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
        headers = {
            "X-Goog-Upload-Protocol": "raw",
            "X-Goog-Upload-Command": "start, upload, finalize",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "Content-Type": mime_type
        }
        loop = asyncio.get_event_loop()
        with open(file_path, 'rb') as f:
            data = f.read()
        up_resp = await loop.run_in_executor(None, lambda: requests.post(upload_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT_GEMINI))
        up_resp.raise_for_status()
        up_resp = up_resp.json()
        uploaded_name = up_resp.get("name", up_resp.get("file", {}).get("name"))
        uploaded_uri = up_resp.get("uri", up_resp.get("file", {}).get("uri"))
        if not uploaded_name and not uploaded_uri:
            raise RuntimeError("Upload failed.")
        prompt = "Transcribe the audio in this file. Automatically detect the language and provide a clean transcription. Do not add intro phrases."
        payload = {"contents": [{"parts": [{"fileData": {"mimeType": mime_type, "fileUri": uploaded_uri}}, {"text": prompt}]}]}
        data = await loop.run_in_executor(None, gemini_api_call_sync, f"models/{GEMINI_MODEL}:generateContent", payload, key)
        res_text = data["candidates"][0]["content"]["parts"][0]["text"]
        await increment_count(uid)
        return res_text
    finally:
        if uploaded_name:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{uploaded_name}?key={key}", timeout=5))
            except:
                pass
        if converted_path and os.path.exists(converted_path):
            os.remove(converted_path)

async def ask_gemini(text, instruction, uid):
    key = await get_user_key_or_raise(uid)
    payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, gemini_api_call_sync, f"models/{GEMINI_MODEL}:generateContent", payload, key)
    res_text = data["candidates"][0]["content"]["parts"][0]["text"]
    await increment_count(uid)
    return res_text

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
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    return InlineKeyboardMarkup(btns)

async def send_key_missing_alert(client, chat_id):
    try:
        chat_info = await client.get_chat(TUTORIAL_CHANNEL)
        if chat_info.pinned_message:
            await client.forward_messages(chat_id, TUTORIAL_CHANNEL, chat_info.pinned_message.id)
    except Exception:
        pass

@app.on_message(filters.command(["start", "help"]) & filters.private)
async def send_welcome(client, message: Message):
    welcome_text = (
        "👋 Salaam!\n"
        "• Send me\n"
        "• **voice message**\n"
        "• **audio file**\n"
        "• **video**\n"
        "• to transcribe for free"
    )
    await message.reply_text(welcome_text)
    user_awaiting_key[message.from_user.id] = True

@app.on_message(filters.command("setkey") & filters.private)
async def setkey_cmd(client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text("Usage: /setkey YOUR_GEMINI_KEY")
        return
    key = args[1].strip()
    if not is_gemini_key(key):
        user_awaiting_key[message.from_user.id] = True
        await message.reply_text("❌ not  Gemini key try again")
        return
    await store_user_key(message.from_user.id, key)
    await message.reply_text("☑️ Okay, your daily limit is 19 requests.\nNow send me the audio or video so I can transcribe")

@app.on_message(filters.private & filters.text & filters.regex(r"^(?!/)"))
async def text_handler(client, message: Message):
    uid = message.from_user.id
    if user_awaiting_key.get(uid):
        key = message.text.strip()
        if not is_gemini_key(key):
            user_awaiting_key[uid] = True
            await message.reply_text("❌ not  Gemini key try again")
            return
        await store_user_key(uid, key)
        await message.reply_text("☑️ Okay, your daily limit is 19 requests.\nNow send me the audio or video so I can transcribe")
        return

@app.on_message(filters.command("getcount") & filters.private)
async def getcount_cmd(client, message: Message):
    uid = message.from_user.id
    info = user_keys.get(uid)
    if not info:
        await send_key_missing_alert(client, message.chat.id)
        return
    await reset_count_if_needed(uid)
    cnt = info.get('count', 0)
    rem = await seconds_left_for_user(uid)
    if cnt >= DAILY_LIMIT:
        await message.reply_text(f"You have reached the daily limit of {DAILY_LIMIT}. Time remaining: {format_hms(rem)}.")
    else:
        await message.reply_text(f"Used: {cnt}. Remaining time in window: {format_hms(rem)}. Limit: {DAILY_LIMIT}.")

@app.on_message(filters.command("removekey") & filters.private)
async def removekey_cmd(client, message: Message):
    uid = message.from_user.id
    if uid in user_keys:
        user_keys.pop(uid, None)
        if users_col is not None:
            users_col.delete_many({"uid": uid})
        await message.reply_text("Key removed from memory.")
    else:
        await message.reply_text("No key found.")

@app.on_callback_query(filters.regex(r"^lang\|"))
async def lang_cb(client, call: CallbackQuery):
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        _, code, lbl, origin = call.data.split("|")
    except ValueError:
        await call.answer("Invalid callback data.", show_alert=True)
        return
    await process_text_action(client, call, call.message.id, "Translate", f"Translate this text in to language {lbl}. No extra text ONLY return the translated text.")

@app.on_callback_query(filters.regex(r"^(translate_menu|summarize)\|"))
async def action_cb(client, call: CallbackQuery):
    action, _ = call.data.split("|")
    if action == "translate_menu":
        try:
            await call.message.edit_reply_markup(reply_markup=build_lang_keyboard("trans"))
        except Exception:
            pass
    elif action == "summarize":
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await process_text_action(client, call, call.message.id, "Summarize", "Summarize this in original language.")

async def process_text_action(client, call: CallbackQuery, origin_msg_id, log_action, prompt_instr):
    chat_id, msg_id = call.message.chat.id, call.message.id
    data = user_transcriptions.get(chat_id, {}).get(msg_id)
    if not data:
        await call.answer("Data not found (expired). Resend file.", show_alert=True)
        return
    text = data["text"]
    key = f"{chat_id}|{msg_id}|{log_action}"
    used = action_usage.get(key, 0)
    if "Summarize" in log_action and used >= 1:
        await call.answer("Already summarized!", show_alert=True)
        return
    await call.answer("Processing...")
    await client.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        res = await ask_gemini(text, prompt_instr, call.from_user.id)
        async with lock:
            action_usage[key] = action_usage.get(key, 0) + 1
        persist_action_usage_to_db(key)
        await send_long_text(client, chat_id, res, data["origin"], log_action)
    except Exception as e:
        msg = str(e)
        if msg == "API_KEY_MISSING":
            await send_key_missing_alert(client, chat_id)
        elif msg.startswith("API_DAILY_LIMIT_REACHED"):
            parts = msg.split("|")
            secs = int(parts[1]) if len(parts) > 1 else await seconds_left_for_user(call.from_user.id)
            await client.send_message(chat_id, f"Daily limit reached. Time left: {format_hms(secs)}.", reply_to_message_id=data["origin"])
        else:
            await client.send_message(chat_id, f"❌ Error: {e}", reply_to_message_id=data["origin"])

@app.on_message(filters.private & (filters.voice | filters.audio | filters.video | filters.document))
async def handle_media(client, message: Message):
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if media.file_size > MAX_UPLOAD_SIZE:
        await message.reply_text(f"Just Send me a file less than {MAX_UPLOAD_MB}MB 😎")
        return
    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    file_path = None
    try:
        file_path = await message.download(file_name=os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{media.file_unique_id}"))
        try:
            text = await upload_and_transcribe_gemini(file_path, message.from_user.id)
        except Exception as e:
            em = str(e)
            if em == "API_KEY_MISSING":
                await send_key_missing_alert(client, message.chat.id)
                return
            if em.startswith("API_DAILY_LIMIT_REACHED"):
                parts = em.split("|")
                secs = int(parts[1]) if len(parts) > 1 else await seconds_left_for_user(message.from_user.id)
                await message.reply_text(f"Daily limit reached. Time left: {format_hms(secs)}.")
                return
            raise
        if not text:
            raise ValueError("Empty response")
        sent = await send_long_text(client, message.chat.id, text, message.id, "Transcript")
        sent_id = sent.id
        user_transcriptions.setdefault(message.chat.id, {})[sent_id] = {"text": text, "origin": message.id}
        try:
            await sent.edit_reply_markup(reply_markup=build_action_keyboard(len(text)))
        except Exception:
            pass
    except Exception as e:
        logging.error(f"Error handling media: {e}")
        await message.reply_text(f"❌ Error: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

async def send_long_text(client, chat_id, text, reply_id, action="Transcript"):
    if len(text) > MAX_MESSAGE_CHUNK:
        fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            sent = await client.send_document(
                chat_id,
                document=InputFile(fname),
                caption="Open this file and copy the text inside 👍",
                reply_to_message_id=reply_id
            )
            return sent
        finally:
            if os.path.exists(fname):
                os.remove(fname)
    return await client.send_message(chat_id, text, reply_to_message_id=reply_id)

fastapi_app = FastAPI()
fastapi_app.mount("/static", StaticFiles(directory=DOWNLOADS_DIR), name="static")

INDEX_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SpeechBot — Web UI</title>
<style>
:root{--bg1:#0f172a;--bg2:#0b1220;--card:#0f172a;--accent:#60a5fa}
*{box-sizing:border-box;font-family:Inter,Segoe UI,Arial;background-repeat:no-repeat}
html,body{height:100%;margin:0;background:linear-gradient(135deg,var(--bg1),#04132b)}
.scene{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:40px}
.card{width:100%;max-width:980px;background:linear-gradient(180deg,rgba(255,255,255,0.03),rgba(255,255,255,0.01));border-radius:16px;padding:28px;backdrop-filter: blur(6px);box-shadow:0 10px 40px rgba(2,6,23,0.6);position:relative;overflow:hidden}
.circle{position:absolute;border-radius:50%;filter:blur(40px);opacity:0.45;animation:float 8s ease-in-out infinite}
.c1{width:260px;height:260px;left:-60px;top:-80px;background:linear-gradient(90deg,#60a5fa,#7c3aed)}
.c2{width:220px;height:220px;right:-40px;bottom:-80px;background:linear-gradient(90deg,#fb7185,#f59e0b);animation-duration:10s;opacity:0.35}
.header{display:flex;align-items:center;gap:16px;z-index:2}
.logo{width:56px;height:56px;border-radius:12px;background:linear-gradient(135deg,#7c3aed,#60a5fa);display:flex;align-items:center;justify-content:center;color:white;font-weight:700}
.title{color:#e6eef8;font-size:20px}
.instructions{color:#9fb2d9;margin-top:8px}
.form{display:grid;grid-template-columns:1fr 360px;gap:18px;margin-top:20px;z-index:2}
.panel{background:rgba(255,255,255,0.02);padding:16px;border-radius:12px;min-height:220px}
.preview{height:220px;display:flex;align-items:center;justify-content:center;color:#bcd3f5;flex-direction:column}
.controls{display:flex;flex-direction:column;gap:10px}
.input,textarea,button{width:100%;padding:12px;border-radius:10px;border:1px solid rgba(255,255,255,0.06);background:transparent;color:#dbeafe}
.small{font-size:13px;color:#9fb2d9}
.btn{background:linear-gradient(90deg,#7c3aed,#60a5fa);border:none;padding:12px;border-radius:10px;color:white;font-weight:600;cursor:pointer}
.result{white-space:pre-wrap;max-height:420px;overflow:auto;padding:12px;background:rgba(0,0,0,0.2);border-radius:8px;color:#e6eef8}
.footer{text-align:right;color:#9fb2d9;margin-top:12px;font-size:13px}
.thanks{display:inline-block;padding:8px 12px;background:rgba(96,165,250,0.12);border-radius:999px;color:#60a5fa;font-weight:600}
@keyframes float{0%{transform:translateY(0)}50%{transform:translateY(-24px)}100%{transform:translateY(0)}}
@media(max-width:920px){.form{grid-template-columns:1fr;}.header{gap:12px}}
</style>
</head>
<body>
<div class="scene">
<div class="card">
<div class="circle c1"></div>
<div class="circle c2"></div>
<div class="header">
<div class="logo">SB</div>
<div>
<div class="title">SpeechBot — Web Interface</div>
<div class="instructions">Upload audio, paste your Gemini key, then click Transcribe. Waadku mahad san tahay inaad i kicisay</div>
</div>
</div>
<div class="form">
<div class="panel">
<div class="controls">
<label class="small">Audio file (max {{max_mb}}MB)</label>
<input id="afile" type="file" accept="audio/*,video/*" />
<label class="small">Gemini API Key</label>
<input id="akey" class="input" placeholder="AIza..." />
<button id="trans" class="btn">Transcribe</button>
<div id="status" class="small" style="margin-top:8px"></div>
</div>
</div>
<div class="panel">
<div class="preview" id="preview">
<div style="font-size:14px;color:#9fb2d9">Output</div>
<div id="out" class="result" style="margin-top:12px">No transcription yet</div>
<div class="footer"><span id="thanks" class="thanks" style="display:none">Waad ku mahadsan tahay</span></div>
</div>
</div>
</div>
</div>
<script>
const btn = document.getElementById("trans")
const infile = document.getElementById("afile")
const keyin = document.getElementById("akey")
const out = document.getElementById("out")
const status = document.getElementById("status")
const thanks = document.getElementById("thanks")
btn.onclick = async () => {
  if(!infile.files.length){status.textContent="Choose a file";return}
  const f = infile.files[0]
  if(f.size > {{max_bytes}}){status.textContent="File too large";return}
  const k = keyin.value.trim()
  if(!k){status.textContent="Provide Gemini key";return}
  status.textContent="Uploading..."
  out.textContent=""
  thanks.style.display="none"
  const fd = new FormData()
  fd.append("file", f)
  fd.append("key", k)
  try{
    const r = await fetch("/transcribe", {method:"POST", body:fd})
    const j = await r.json()
    if(r.ok){
      out.textContent = j.text || "No text returned"
      thanks.style.display = "inline-block"
      status.textContent = "Done"
    } else {
      out.textContent = j.error || "Error"
      status.textContent = "Failed"
    }
  } catch(e){
    out.textContent = String(e)
    status.textContent = "Error"
  }
}
</script>
</body>
</html>
"""

@fastapi_app.get("/", response_class=HTMLResponse)
async def index():
    html = INDEX_HTML.replace("{{max_mb}}", str(MAX_UPLOAD_MB)).replace("{{max_bytes}}", str(MAX_UPLOAD_SIZE))
    return HTMLResponse(html)

async def transcribe_file_with_key(file_path: str, key: str):
    original_path, converted_path = file_path, None
    if os.path.splitext(file_path)[1].lower() not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
        converted_path = await convert_to_wav(file_path)
        file_path = converted_path
    file_size = os.path.getsize(file_path)
    mime_type = "audio/wav"
    uploaded_name = None
    try:
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
        headers = {
            "X-Goog-Upload-Protocol": "raw",
            "X-Goog-Upload-Command": "start, upload, finalize",
            "X-Goog-Upload-Header-Content-Length": str(file_size),
            "Content-Type": mime_type
        }
        loop = asyncio.get_event_loop()
        with open(file_path, 'rb') as f:
            data = f.read()
        up_resp = await loop.run_in_executor(None, lambda: requests.post(upload_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT_GEMINI))
        up_resp.raise_for_status()
        up_resp = up_resp.json()
        uploaded_name = up_resp.get("name", up_resp.get("file", {}).get("name"))
        uploaded_uri = up_resp.get("uri", up_resp.get("file", {}).get("uri"))
        if not uploaded_name and not uploaded_uri:
            raise RuntimeError("Upload failed")
        prompt = "Transcribe the audio in this file. Automatically detect the language and provide a clean transcription. Do not add intro phrases."
        payload = {"contents": [{"parts": [{"fileData": {"mimeType": mime_type, "fileUri": uploaded_uri}}, {"text": prompt}]}]}
        data = await loop.run_in_executor(None, gemini_api_call_sync, f"models/{GEMINI_MODEL}:generateContent", payload, key)
        res_text = data["candidates"][0]["content"]["parts"][0]["text"]
        return res_text
    finally:
        if uploaded_name:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{uploaded_name}?key={key}", timeout=5))
            except:
                pass
        if converted_path and os.path.exists(converted_path):
            os.remove(converted_path)

@fastapi_app.post("/transcribe")
async def web_transcribe(file: UploadFile = File(...), key: str = Form(...)):
    if file.filename is None or file.filename == "":
        return JSONResponse({"error": "No file"}, status_code=400)
    if file.spool_max_size and file.spool_max_size > MAX_UPLOAD_SIZE:
        return JSONResponse({"error": "File too large"}, status_code=400)
    temp_name = os.path.join(DOWNLOADS_DIR, f"web_{int(time.time())}_{os.path.basename(file.filename)}")
    try:
        with open(temp_name, "wb") as out_f:
            shutil.copyfileobj(file.file, out_f)
        if os.path.getsize(temp_name) > MAX_UPLOAD_SIZE:
            return JSONResponse({"error": f"File exceeds {MAX_UPLOAD_MB}MB"}, status_code=400)
        if not is_gemini_key(key):
            return JSONResponse({"error": "Invalid Gemini key format"}, status_code=400)
        try:
            text = await transcribe_file_with_key(temp_name, key.strip())
            return JSONResponse({"text": text})
        except Exception as e:
            logging.exception("web transcribe error")
            return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        try:
            if os.path.exists(temp_name):
                os.remove(temp_name)
        except:
            pass

async def _run_uvicorn():
    config = uvicorn.Config(fastapi_app, host=WEB_HOST, port=WEB_PORT, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)
    await server.serve()

async def _main():
    uvicorn_task = asyncio.create_task(_run_uvicorn())
    await app.start()
    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        uvicorn_task.cancel()
        try:
            await uvicorn_task
        except:
            pass

if __name__ == "__main__":
    if not BOT_TOKEN or API_ID == 0 or not API_HASH:
        logging.error("BOT_TOKEN, API_ID, or API_HASH is missing. Please set environment variables.")
    else:
        asyncio.run(_main())

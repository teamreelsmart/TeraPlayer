import asyncio
import json
import os
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import uvicorn
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template
from pymongo import MongoClient
from pymongo.collection import Collection

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "https://service-name.onrender.com")
APP_PORT = int(os.getenv("PORT") or os.getenv("APP_PORT", "8000"))
XAPIVERSE_URL = os.getenv("XAPIVERSE_URL", "https://xapiverse.com/api/terabox-pro")
DEFAULT_VERIFY_WAIT_SECONDS = int(os.getenv("VERIFY_WAIT_SECONDS", "180"))
ACCESS_HOURS = int(os.getenv("ACCESS_HOURS", "4"))
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "teraplayer")
DEFAULT_ADMIN_IDS = os.getenv("ADMIN_IDS", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
web = FastAPI(title="TeraPlayer mini-app")

mongo = MongoClient(MONGODB_URI)
db = mongo[MONGODB_DB]
users_col: Collection = db["users"]
verify_col: Collection = db["verification_sessions"]
settings_col: Collection = db["settings"]
media_col: Collection = db["media_tokens"]



def utcnow() -> datetime:
    return datetime.now(timezone.utc)



def random_code(length: int = 5) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))



def init_db() -> None:
    users_col.create_index("user_id", unique=True)
    verify_col.create_index("token", unique=True)
    media_col.create_index("token", unique=True)
    settings_col.create_index("key", unique=True)
    if DEFAULT_ADMIN_IDS and not get_setting("admin_ids", ""):
        set_setting("admin_ids", DEFAULT_ADMIN_IDS)



def get_setting(key: str, default: str = "") -> str:
    row = settings_col.find_one({"key": key})
    return row.get("value", default) if row else default



def set_setting(key: str, value: str) -> None:
    settings_col.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)



def get_api_keys() -> list[str]:
    raw = get_setting("terabox_api_keys", "[]")
    try:
        keys = json.loads(raw)
        return [k for k in keys if k]
    except json.JSONDecodeError:
        return []



def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None





def get_admin_ids() -> set[int]:
    raw = get_setting("admin_ids", DEFAULT_ADMIN_IDS)
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}

def get_supported_domains() -> list[str]:
    raw = get_setting("supported_domains", "terabox.com,1024terabox.com")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]



def is_supported_link(url: str) -> bool:
    lower = url.lower()
    return any(domain in lower for domain in get_supported_domains())


async def log_event(text: str) -> None:
    log_id = get_setting("log_channel_id", "")
    if not log_id:
        return
    try:
        await bot.send_message(chat_id=int(log_id), text=text)
    except Exception:
        return


async def ensure_user(message: Message) -> dict[str, Any]:
    user_id = message.from_user.id
    user = users_col.find_one({"user_id": user_id})
    if user:
        return user
    now = utcnow().isoformat()
    users_col.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "username": message.from_user.username,
                "first_name": message.from_user.first_name,
                "is_premium": 0,
                "access_until": None,
                "warnings": 0,
                "is_banned": 0,
                "created_at": now,
            }
        },
        upsert=True,
    )
    await log_event(
        f"🆕 New user started bot\nID: {user_id}\nUsername: @{message.from_user.username or 'none'}"
    )
    return users_col.find_one({"user_id": user_id})


async def is_force_subscribed(user_id: int) -> bool:
    force_channel = get_setting("force_sub_channel_id", "")
    if not force_channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=int(force_channel), user_id=user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        return False



def is_user_active(user: dict[str, Any]) -> bool:
    if user.get("is_banned"):
        return False
    if user.get("is_premium"):
        return True
    access_until = parse_dt(user.get("access_until"))
    return bool(access_until and access_until > utcnow())


async def create_short_link(original_url: str) -> str:
    shortener_url = get_setting("shortener_api_url", "")
    shortener_key = get_setting("shortener_api_key", "")
    if not shortener_url:
        return original_url
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(shortener_url, json={"url": original_url, "api": shortener_key})
            data = r.json()
            return data.get("shortenedUrl") or data.get("short_url") or original_url
    except Exception:
        return original_url


async def start_verification(user_id: int) -> tuple[str, str]:
    token = f"v_{random_code(10)}"
    created = utcnow().isoformat()
    verify_page = f"{WEB_BASE_URL}/verify/{token}"
    short_link = await create_short_link(verify_page)
    verify_col.insert_one(
        {
            "token": token,
            "user_id": user_id,
            "created_at": created,
            "verified_at": None,
            "short_url": short_link,
            "status": "pending",
        }
    )
    return token, short_link


async def fetch_terabox_data(url: str) -> dict[str, Any]:
    api_keys = get_api_keys()
    if not api_keys:
        raise ValueError("No TeraBox API keys configured by admin.")
    headers = {"Content-Type": "application/json"}
    payload = {"url": url}
    async with httpx.AsyncClient(timeout=40) as client:
        for key in api_keys:
            response = await client.post(XAPIVERSE_URL, headers=headers | {"xAPIverse-Key": key}, json=payload)
            if response.is_success:
                data = response.json()
                if data.get("status") == "success" and data.get("list"):
                    return data
        raise ValueError("TeraBox API request failed for all configured keys.")



def make_media_token(user_id: int, item: dict[str, Any], stream_url: str, download_url: str) -> str:
    token = random_code(12)
    media_col.insert_one(
        {
            "token": token,
            "user_id": user_id,
            "stream_url": stream_url,
            "download_url": download_url,
            "name": item.get("name"),
            "quality": item.get("quality"),
            "size_formatted": item.get("size_formatted") or str(item.get("size")),
            "duration": item.get("duration"),
            "media_type": item.get("type"),
            "thumbnail": item.get("thumbnail"),
            "created_at": utcnow().isoformat(),
        }
    )
    return token


@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    user = await ensure_user(message)
    if not await is_force_subscribed(message.from_user.id):
        channel_username = get_setting("force_sub_channel_username", "")
        rows = []
        if channel_username:
            rows.append([InlineKeyboardButton(text="Join Channel", url=f"https://t.me/{channel_username.lstrip('@')}")])
        rows.append([InlineKeyboardButton(text="I Joined ✅", callback_data="check_sub")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.answer("⚠️ Please join the required channel before using this bot.", reply_markup=kb)
        return

    if user.get("is_banned"):
        await message.answer("🚫 You are banned from using this bot.")
        return

    start_image = get_setting("start_image", "")
    text = "👋 Welcome! Send a TeraBox link and I will generate play + download options."
    if start_image:
        await message.answer_photo(photo=start_image, caption=text)
    else:
        await message.answer(text)


@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(call: CallbackQuery) -> None:
    if await is_force_subscribed(call.from_user.id):
        await call.message.answer("✅ Subscription verified. Now send your TeraBox link.")
    else:
        await call.message.answer("❌ Channel join not detected yet.")
    await call.answer()


@dp.message(Command("admin"))
async def admin_panel(message: Message) -> None:
    admins = get_admin_ids()
    if message.from_user.id not in admins:
        await message.answer("Only admins can use this command.")
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Add premium user", callback_data="adm:add_premium")],
            [InlineKeyboardButton(text="Remove premium user", callback_data="adm:remove_premium")],
            [InlineKeyboardButton(text="Set shortener URL", callback_data="adm:set_short_url")],
            [InlineKeyboardButton(text="Set shortener API key", callback_data="adm:set_short_key")],
            [InlineKeyboardButton(text="Set verify tutorial link", callback_data="adm:set_tutorial")],
            [InlineKeyboardButton(text="Set premium QR image", callback_data="adm:set_qr")],
            [InlineKeyboardButton(text="Set force-sub channel ID", callback_data="adm:set_force")],
            [InlineKeyboardButton(text="Set force-sub channel username", callback_data="adm:set_force_username")],
            [InlineKeyboardButton(text="Set log channel ID", callback_data="adm:set_log")],
            [InlineKeyboardButton(text="Set TeraBox API keys", callback_data="adm:set_api_keys")],
            [InlineKeyboardButton(text="Set supported domains", callback_data="adm:set_domains")],
        ]
    )
    await message.answer("Admin panel", reply_markup=kb)


@dp.callback_query(F.data.startswith("adm:"))
async def admin_actions(call: CallbackQuery) -> None:
    action = call.data.split(":", 1)[1]
    set_setting(f"pending_admin_action:{call.from_user.id}", action)
    await call.message.answer(f"Send value for: {action}")
    await call.answer()


@dp.callback_query(F.data.startswith("verify_check:"))
async def verify_check(call: CallbackQuery) -> None:
    token = call.data.split(":", 1)[1]
    session = verify_col.find_one({"token": token})
    if not session or session.get("status") != "verified":
        await call.answer("Please complete verification first.", show_alert=True)
        return

    created = parse_dt(session.get("created_at"))
    if not created:
        await call.answer("Invalid verification session.", show_alert=True)
        return

    elapsed = (utcnow() - created).total_seconds()
    if elapsed < DEFAULT_VERIFY_WAIT_SECONDS:
        user = users_col.find_one({"user_id": call.from_user.id}) or {}
        warnings = int(user.get("warnings", 0)) + 1
        users_col.update_one({"user_id": call.from_user.id}, {"$set": {"warnings": warnings}})
        await log_event(
            f"⚠️ Verification bypass suspected\nUser: {call.from_user.id}\nWarnings: {warnings}"
        )
        _, new_link = await start_verification(call.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="New verify link", url=new_link)]])
        await call.message.answer(
            "⚠️ You verified too quickly and got a warning. Please verify again with a new link.",
            reply_markup=kb,
        )
        await call.answer()
        return

    access_until = (utcnow() + timedelta(hours=ACCESS_HOURS)).isoformat()
    users_col.update_one({"user_id": call.from_user.id}, {"$set": {"access_until": access_until}})
    await call.message.answer(f"✅ Verification complete. Access granted for {ACCESS_HOURS} hours.")
    await call.answer()


@dp.callback_query(F.data == "buy_premium")
async def buy_premium(call: CallbackQuery) -> None:
    qr = get_setting("premium_qr_image", "")
    text = "To buy premium, contact the admin."
    if qr:
        await call.message.answer_photo(photo=qr, caption=text)
    else:
        await call.message.answer(text)
    await call.answer()


async def handle_admin_input(message: Message, action: str) -> None:
    value = message.text.strip()
    if action == "add_premium":
        users_col.update_one({"user_id": int(value)}, {"$set": {"is_premium": 1}}, upsert=True)
    elif action == "remove_premium":
        users_col.update_one({"user_id": int(value)}, {"$set": {"is_premium": 0}}, upsert=True)
    elif action == "set_short_url":
        set_setting("shortener_api_url", value)
    elif action == "set_short_key":
        set_setting("shortener_api_key", value)
    elif action == "set_tutorial":
        set_setting("verify_tutorial_link", value)
    elif action == "set_qr":
        set_setting("premium_qr_image", value)
    elif action == "set_force":
        set_setting("force_sub_channel_id", value)
    elif action == "set_force_username":
        set_setting("force_sub_channel_username", value)
    elif action == "set_log":
        set_setting("log_channel_id", value)
    elif action == "set_api_keys":
        set_setting("terabox_api_keys", json.dumps([x.strip() for x in value.split(",") if x.strip()]))
    elif action == "set_domains":
        set_setting("supported_domains", value)
    await message.answer("✅ Saved")


@dp.message(F.text)
async def text_router(message: Message) -> None:
    user = await ensure_user(message)
    pending = get_setting(f"pending_admin_action:{message.from_user.id}", "")
    if pending:
        await handle_admin_input(message, pending)
        set_setting(f"pending_admin_action:{message.from_user.id}", "")
        return

    text = message.text.strip()
    if not is_supported_link(text):
        return

    if user.get("is_banned"):
        await message.answer("🚫 You are banned from using this bot.")
        return

    if not is_user_active(user):
        token, short_link = await start_verification(user["user_id"])
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Verify now", url=short_link)],
                [InlineKeyboardButton(text="Check verification", callback_data=f"verify_check:{token}")],
                [InlineKeyboardButton(text="Watch tutorial", url=get_setting("verify_tutorial_link", "https://t.me"))],
                [InlineKeyboardButton(text="Buy premium", callback_data="buy_premium")],
            ]
        )
        await message.answer(
            f"You are a free user. Complete verification to unlock {ACCESS_HOURS} hours access.",
            reply_markup=kb,
        )
        return

    await message.answer("⏳ Processing your link...")
    try:
        data = await fetch_terabox_data(text)
        item = data["list"][0]

        if user.get("is_premium"):
            stream_url = next(iter((item.get("fast_stream_url") or {}).values()), item.get("stream_url"))
            download_url = item.get("fast_download_link") or item.get("download_link")
            quality_text = ", ".join((item.get("fast_stream_url") or {}).keys()) or item.get("quality") or "N/A"
        else:
            stream_url = item.get("stream_url")
            download_url = item.get("download_link")
            quality_text = item.get("quality") or "N/A"

        media_token = make_media_token(user["user_id"], item, stream_url, download_url)
        mini_url = f"{WEB_BASE_URL}/mini/{media_token}"
        download_gate = f"{WEB_BASE_URL}/d/{media_token}"

        caption = (
            f"🎬 Name: {item.get('name')}\n"
            f"Type: {item.get('type')}\n"
            f"Size: {item.get('size_formatted') or item.get('size')}\n"
            f"Duration: {item.get('duration')}\n"
            f"Quality: {quality_text}\n"
            f"Status: {data.get('status')}\n"
            f"Total files: {data.get('total_files')}\n"
            f"FS ID: {item.get('fs_id')}"
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Play in Mini App", web_app=WebAppInfo(url=mini_url))],
                [InlineKeyboardButton(text="⬇️ Download", url=download_gate)],
            ]
        )

        if item.get("thumbnail"):
            await message.answer_photo(photo=item.get("thumbnail"), caption=caption, reply_markup=kb)
        else:
            await message.answer(caption, reply_markup=kb)
    except Exception as error:
        await message.answer(f"❌ Failed to process link: {error}")


@web.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@web.get("/verify/{token}")
async def verify_landing(token: str) -> RedirectResponse:
    row = verify_col.find_one({"token": token})
    if not row:
        raise HTTPException(status_code=404, detail="Invalid token")
    verify_col.update_one(
        {"token": token},
        {"$set": {"status": "verified", "verified_at": utcnow().isoformat()}},
    )
    return RedirectResponse("https://t.me")


@web.get("/mini/{token}", response_class=HTMLResponse)
async def mini_player(token: str) -> HTMLResponse:
    row = media_col.find_one({"token": token})
    if not row:
        raise HTTPException(status_code=404, detail="Media not found")
    html = Template(
        """
        <html><body style="font-family:Arial;background:#111;color:#fff;padding:20px;">
        <h3>{{name}}</h3>
        <video controls autoplay style="width:100%;max-width:900px" src="{{stream}}"></video>
        <p>Quality: {{quality}} | Duration: {{duration}} | Size: {{size}}</p>
        </body></html>
        """
    ).render(
        name=row.get("name", "Video"),
        stream=row.get("stream_url", ""),
        quality=row.get("quality", "N/A"),
        duration=row.get("duration", "N/A"),
        size=row.get("size_formatted", "N/A"),
    )
    return HTMLResponse(html)


@web.get("/d/{token}", response_class=HTMLResponse)
async def download_page(token: str) -> HTMLResponse:
    row = media_col.find_one({"token": token})
    if not row:
        raise HTTPException(status_code=404, detail="Media not found")
    html = Template(
        """
        <html><body style="font-family:Arial;padding:30px">
        <h2>{{name}}</h2>
        <p>Click the button below to start download.</p>
        <a href="{{download}}" style="background:#0a7;color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none">Download</a>
        </body></html>
        """
    ).render(name=row.get("name", "File"), download=row.get("download_url", "#"))
    return HTMLResponse(html)


async def run_bot() -> None:
    await dp.start_polling(bot)


async def run_web() -> None:
    config = uvicorn.Config(web, host="0.0.0.0", port=APP_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    init_db()
    await asyncio.gather(run_bot(), run_web())


if __name__ == "__main__":
    asyncio.run(main())

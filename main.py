# -*- coding: utf-8 -*-
# DishFlow — MVP (Render/Cloud ready)
# Функции: коды доступа, старт/конец смены с фото в Drive, запись в Google Sheets.

import logging
from datetime import datetime
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

# -------------------- НАСТРОЙКИ (замени при необходимости) --------------------
BOT_TOKEN = "7956865373:AAGwKAsQ8VMBSdWlosTPya5cAllXlntjjCw"

# Google Sheets (таблица с листами "Смены" и "Коды_Доступа")
SPREADSHEET_ID = "1li6HIqiwVkEPn91aQn9Fprwv9Zg8cFvpUkLJGIE5O7g"

# JSON-файл сервисного аккаунта (должен лежать рядом с main.py в репозитории)
SERVICE_ACCOUNT_FILE = "dishflow-375ee9dfb3a6.json"

# Корневая папка в Google Drive — сюда будут складываться фото входа/выхода.
# Если этой папки не окажется доступной, скрипт создаст новую и будет писать в неё.
DRIVE_ROOT_FOLDER_ID = "1xAWBrTehWgRY-4yizcKGYXV4yWpIpO5f"
SUBFOLDER_IN_ENTRY = "smeny_vhod"
SUBFOLDER_OUT_EXIT = "smeny_vyhod"

# Названия листов
SHEET_SHIFTS = "Смены"
SHEET_CODES  = "Коды_Доступа"

# Разрешённые роли
VALID_ROLES = {"кассир", "шаурмен", "бармен", "владелец"}
PLACE_DEFAULT = "Казан Шаверма"
# ------------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dishflow")
print("DishFlow bot boot: Render/MVP")

# -------------------- Google авторизация --------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)
drive = build("drive", "v3", credentials=creds)

def get_or_create_worksheet(name: str, header: Optional[list] = None):
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=1000, cols=20)
        if header:
            ws.append_row(header, value_input_option="USER_ENTERED")
    return ws

ws_shifts = get_or_create_worksheet(
    SHEET_SHIFTS,
    header=["Дата","Имя","Telegram ID","Роль","Время входа","Время выхода",
            "Длительность (ч)","Фото вход","Фото выход","Заведение"]
)
ws_codes = get_or_create_worksheet(
    SHEET_CODES,
    header=["Код","Роль","ФИО","Telegram ID","Статус","Заведение"]
)

def safe_get_file(file_id: str) -> bool:
    """Проверить, существует ли файл/папка по id (не валимся на 403/404)."""
    try:
        drive.files().get(fileId=file_id, fields="id").execute()
        return True
    except HttpError as e:
        if e.resp and e.resp.status in (403, 404):
            return False
        raise

def create_root_fallback() -> str:
    """Если заданная корневая папка недоступна — создать свою DishFlow-Auto."""
    meta = {"name": "DishFlow-Auto", "mimeType": "application/vnd.google-apps.folder"}
    created = drive.files().create(body=meta, fields="id").execute()
    fid = created["id"]
    # даём просмотр по ссылке, чтобы было удобно открывать
    drive.permissions().create(fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
    log.warning(f"[Drive] Корень недоступен. Создан новый: {fid}")
    return fid

def ensure_subfolder(parent_id: str, name: str) -> str:
    q = (f"'{parent_id}' in parents and name = '{name}' "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    res = drive.files().list(q=q, fields="files(id,name)").execute()
    if res.get("files"):
        return res["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    created = drive.files().create(body=meta, fields="id").execute()
    return created["id"]

def upload_photo_bytes(parent_id: str, filename: str, content: bytes) -> str:
    media = MediaInMemoryUpload(content, mimetype="image/jpeg")
    created = drive.files().create(
        body={"name": filename, "parents": [parent_id]},
        media_body=media, fields="id"
    ).execute()
    file_id = created["id"]
    # публичная ссылка для просмотра
    drive.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()
    return f"https://drive.google.com/file/d/{file_id}/view"

# Готовим корневую папку и подпапки
if not safe_get_file(DRIVE_ROOT_FOLDER_ID):
    DRIVE_ROOT_FOLDER_ID = create_root_fallback()
FOLDER_ENTRY_ID = ensure_subfolder(DRIVE_ROOT_FOLDER_ID, SUBFOLDER_IN_ENTRY)
FOLDER_EXIT_ID  = ensure_subfolder(DRIVE_ROOT_FOLDER_ID, SUBFOLDER_OUT_EXIT)

# -------------------- Память во время работы --------------------
user_state: Dict[int, Dict] = {}       # {uid: {started_at, entry_photo_link, exit_photo_link, awaiting}}
user_role_cache: Dict[int, Dict] = {}  # {uid: {role, place, fio}}

# -------------------- Вспомогательные функции (Sheets) --------------------
def sheet_find_code_row(code: str) -> Optional[int]:
    """Найти строку кода в листе Коды_Доступа (по колонке A)."""
    code = code.strip().upper()
    for idx, val in enumerate(ws_codes.col_values(1), start=1):
        if val.strip().upper() == code:
            return idx
    return None

def sheet_find_user_by_id(user_id: int) -> Optional[int]:
    """Найти строку пользователя по Telegram ID (колонка D)."""
    for idx, val in enumerate(ws_codes.col_values(4), start=1):
        if str(user_id) == val.strip():
            return idx
    return None

def load_role_from_sheet_row(idx: int) -> Optional[Dict]:
    row = ws_codes.row_values(idx)
    try:
        return {
            "code": row[0].strip(),
            "role": row[1].strip().lower(),
            "fio":  row[2].strip(),
            "telegram_id": row[3].strip(),
            "status": row[4].strip().lower(),
            "place": row[5].strip() if len(row) > 5 else PLACE_DEFAULT,
        }
    except IndexError:
        return None

def activate_code_for_user(idx: int, user_id: int, fio: str):
    ws_codes.update_cell(idx, 3, fio)
    ws_codes.update_cell(idx, 4, str(user_id))
    ws_codes.update_cell(idx, 5, "активирован")

# -------------------- Telegram bot --------------------
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(bot)

kb_staff = ReplyKeyboardMarkup(resize_keyboard=True)
kb_staff.add(KeyboardButton("🔓 Начать смену"), KeyboardButton("🔒 Завершить смену"))
kb_staff.add(KeyboardButton("🗒 Мои последние смены"))

kb_owner = ReplyKeyboardMarkup(resize_keyboard=True)
kb_owner.add(KeyboardButton("📊 Отчёт по сменам"), KeyboardButton("🔑 Выдать код (manual)"))

async def get_file_bytes(file_id: str) -> bytes:
    """Скачать файл из Telegram"""
    f = await bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}"
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            return await r.read()

@dp.message_handler(commands=["start"])
async def start_(m: types.Message):
    uid = m.from_user.id
    row_idx = sheet_find_user_by_id(uid)
    if row_idx:
        rec = load_role_from_sheet_row(row_idx)
        if rec and rec["role"] in VALID_ROLES:
            user_role_cache[uid] = {"role": rec["role"], "place": rec["place"], "fio": rec["fio"]}
            kb = kb_staff if rec["role"] in {"кассир","шаурмен","бармен"} else kb_owner
            await m.answer(
                f"Добро пожаловать, {rec['fio']}.\nРоль: {rec['role'].capitalize()}\nТочка: {rec['place']}",
                reply_markup=kb
            )
            return
    await m.answer("🔐 Введите персональный код (например, STAFF-KASSIR-XXXX):")

@dp.message_handler(lambda x: x.text and x.text.strip().upper().startswith(("STAFF-","OWNER-","BAR-")))
async def code_(m: types.Message):
    uid  = m.from_user.id
    code = m.text.strip().upper()
    idx  = sheet_find_code_row(code)
    if not idx:
        await m.reply("❌ Код не найден.")
        return
    rec = load_role_from_sheet_row(idx)
    if not rec:
        await m.reply("❌ Ошибка кода.")
        return
    if rec["telegram_id"] and rec["telegram_id"] != str(uid):
        await m.reply("⚠️ Код уже привязан к другому пользователю.")
        return
    role = rec["role"]
    if role not in VALID_ROLES:
        await m.reply("❌ Неверная роль в коде.")
        return
    fio = m.from_user.full_name
    activate_code_for_user(idx, uid, fio)
    user_role_cache[uid] = {"role": role, "place": rec["place"], "fio": fio}
    kb = kb_staff if role in {"кассир","шаурмен","бармен"} else kb_owner
    await m.answer(f"✅ Код принят. Роль: {role.capitalize()}.\nТочка: {rec['place']}", reply_markup=kb)

@dp.message_handler(lambda x: x.text == "🔓 Начать смену")
async def start_shift(m: types.Message):
    uid = m.from_user.id
    role = user_role_cache.get(uid, {}).get("role")
    if role not in {"кассир","шаурмен","бармен"}:
        await m.reply("Доступно только для персонала.")
        return
    if user_state.get(uid, {}).get("started_at"):
        await m.reply("Смена уже начата.")
        return
    user_state[uid] = {
        "started_at": datetime.now(),
        "entry_photo_link": None,
        "exit_photo_link": None,
        "awaiting": "entry_photo",
    }
    await m.reply("🟢 Смена начата. Пришлите селфи для фиксации входа.")

@dp.message_handler(lambda x: x.text == "🔒 Завершить смену")
async def end_shift(m: types.Message):
    uid = m.from_user.id
    role = user_role_cache.get(uid, {}).get("role")
    if role not in {"кассир","шаурмен","бармен"}:
        await m.reply("Доступно только для персонала.")
        return
    st = user_state.get(uid)
    if not st or not st.get("started_at"):
        await m.reply("Смена ещё не начата.")
        return
    if not st.get("entry_photo_link"):
        user_state[uid]["awaiting"] = "entry_photo"
        await m.reply("Сначала пришлите фото входа.")
        return
    user_state[uid]["awaiting"] = "exit_photo"
    await m.reply("🔴 Пришлите фото выхода (селфи).")

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def photo_(m: types.Message):
    uid = m.from_user.id
    role = user_role_cache.get(uid, {}).get("role")
    if role not in {"кассир","шаурмен","бармен"}:
        await m.reply("Фото принимаются только от персонала.")
        return
    st = user_state.get(uid)
    if not st or not st.get("awaiting"):
        await m.reply("Сначала нажмите «🔓 Начать смену».")
        return

    photo_bytes = await get_file_bytes(m.photo[-1].file_id)
    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    place = user_role_cache.get(uid, {}).get("place", PLACE_DEFAULT)
    fio   = user_role_cache.get(uid, {}).get("fio", m.from_user.full_name)

    if st["awaiting"] == "entry_photo":
        link = upload_photo_bytes(FOLDER_ENTRY_ID, f"entry_{role}_{uid}_{now_str}.jpg", photo_bytes)
        user_state[uid]["entry_photo_link"] = link
        user_state[uid]["awaiting"] = None
        await m.reply("✅ Фото входа сохранено. В конце смены нажмите «🔒 Завершить смену».")
        return

    if st["awaiting"] == "exit_photo":
        link = upload_photo_bytes(FOLDER_EXIT_ID, f"exit_{role}_{uid}_{now_str}.jpg", photo_bytes)
        user_state[uid]["exit_photo_link"] = link

        started_at: datetime = user_state[uid]["started_at"]
        ended_at = datetime.now()
        duration = round((ended_at - started_at).total_seconds() / 3600, 2)

        ws_shifts.append_row(
            [
                ended_at.strftime("%Y-%m-%d"),
                fio, str(uid), role,
                started_at.strftime("%H:%M:%S"),
                ended_at.strftime("%H:%M:%S"),
                duration,
                user_state[uid]["entry_photo_link"],
                user_state[uid]["exit_photo_link"],
                place,
            ],
            value_input_option="USER_ENTERED",
        )
        user_state[uid] = {}
        await m.reply(f"✅ Смена завершена. Отработано: {duration} ч.")

@dp.errors_handler()
async def err_handler(_, e):
    log.error(f"Ошибка: {e}")
    return True

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)

# app/main_bot.py
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity
from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from app.db import AsyncSessionLocal, init_db
from app.models import User, Channel, ChannelAdmin, Post
from sqlalchemy.future import select
from datetime import datetime, time as dtime, timedelta
from app.utils import compute_next_weekday_time_tz
from zoneinfo import ZoneInfo

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

MAIN_TEXT = "Привет! Я бот автопостинга."

# ---------- общие хелперы ----------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel")],
        [InlineKeyboardButton(text="📝 Новый пост", callback_data="new_post")],
        [InlineKeyboardButton(text="📚 Мои каналы", callback_data="my_channels")],
    ])

def channel_display_name(ch: Channel) -> str:
    return ch.title or (f"@{ch.username}" if ch.username else str(ch.chat_id))

async def safe_edit_message_text(msg: types.Message, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=kb)
        try:
            await msg.delete()
        except Exception:
            pass

class NewPost(StatesGroup):
    choose_channel = State()
    choose_weekday = State()
    choose_time = State()
    input_content = State()
    ask_button = State()
    input_button = State()
    preview = State()

class ManageAdmins(StatesGroup):
    wait_input = State()

async def ensure_user(telegram_id: int, name: str = None):
    async with AsyncSessionLocal() as session:
        q = await session.execute(select(User).where(User.telegram_id == telegram_id))
        u = q.scalar_one_or_none()
        if not u:
            u = User(telegram_id=telegram_id, name=name)
            session.add(u)
            await session.commit()
        return u

# ---------- /start, главное меню ----------

@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await ensure_user(message.from_user.id, message.from_user.full_name)
    await message.answer(MAIN_TEXT, reply_markup=main_menu_kb())

@dp.callback_query(lambda c: c.data == "back_start")
async def cb_back_start(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_message_text(cq.message, MAIN_TEXT, main_menu_kb())
    await cq.answer()

# ---------- добавление канала ----------

@dp.callback_query(lambda c: c.data == "add_channel")
async def cb_add_channel(cq: types.CallbackQuery):
    await cq.message.answer("Перешли сообщение из канала (бот должен быть админом) или введи @username канала.")
    await cq.answer()

# ---------- мои каналы ----------

@dp.callback_query(lambda c: c.data == "my_channels")
async def cb_my_channels(cq: types.CallbackQuery):
    await ensure_user(cq.from_user.id, cq.from_user.full_name)
    async with AsyncSessionLocal() as session:
        owner_res = await session.execute(select(Channel).where(Channel.owner_id == cq.from_user.id))
        owner_channels = owner_res.scalars().all()
        admin_res = await session.execute(
            select(Channel).join(ChannelAdmin, ChannelAdmin.channel_id == Channel.id)
            .where(ChannelAdmin.telegram_id == cq.from_user.id)
        )
        admin_channels = admin_res.scalars().all()
        seen_ids, channels = set(), []
        for ch in owner_channels + admin_channels:
            if ch.id not in seen_ids:
                seen_ids.add(ch.id)
                channels.append(ch)
        if not channels:
            await safe_edit_message_text(cq.message, "У тебя пока нет каналов. Нажми ‘Добавить канал’.", main_menu_kb())
        else:
            rows = [[InlineKeyboardButton(text=channel_display_name(ch), callback_data=f"open_channel:{ch.id}")] for ch in channels]
            rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_start")])
            await safe_edit_message_text(cq.message, "Выбери канал:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("open_channel:"))
async def cb_open_channel(cq: types.CallbackQuery):
    ch_id = int(cq.data.split(":", 1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        title = channel_display_name(ch)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Запланированные посты", callback_data=f"posts_list:{ch_id}")],
        [InlineKeyboardButton(text="👤 Админы", callback_data=f"manage_admins:{ch_id}")],
        [InlineKeyboardButton(text="🗑 Удалить канал", callback_data=f"confirm_del_channel:{ch_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="my_channels")],
    ])
    await safe_edit_message_text(cq.message, f"Канал: {title}", kb)
    await cq.answer()

# ---------- список запланированных постов ----------

@dp.callback_query(lambda c: c.data and c.data.startswith("posts_list:"))
async def cb_posts_list(cq: types.CallbackQuery):
    ch_id = int(cq.data.split(":", 1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Post).where(Post.channel_id == ch_id, Post.next_run != None).order_by(Post.next_run.asc())
        )
        posts = res.scalars().all()
    if not posts:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"open_channel:{ch_id}")]])
        await safe_edit_message_text(cq.message, "Запланированных постов пока нет.", kb)
        await cq.answer()
        return
    rows = []
    for p in posts:
        wd = WEEKDAYS[p.weekday] if p.weekday is not None else "?"
        t = p.time_text or "?"
        prev = (p.text or "").replace("\n", " ")[:25]
        label = f"{wd} {t}" + (f" — {prev}" if prev else "")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"post_view:{p.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"open_channel:{ch_id}")])
    await safe_edit_message_text(cq.message, "Запланированные посты:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("post_view:"))
async def cb_post_view(cq: types.CallbackQuery):
    post_id = int(cq.data.split(":", 1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Post).where(Post.id == post_id))
        p = res.scalar_one_or_none()
    if not p:
        await cq.answer("Пост не найден", show_alert=True)
        return
    when = "не запланирован"
    if p.next_run:
        local = (p.next_run if p.next_run.tzinfo else p.next_run.replace(tzinfo=ZoneInfo("UTC"))).astimezone(ZoneInfo("Europe/Moscow"))
        when = local.strftime("%d.%m.%Y %H:%M (МСК)")
    wd = WEEKDAYS_FULL[p.weekday] if p.weekday is not None else "?"
    try:
        await cq.message.delete()
    except Exception:
        pass
    # Превью
    rows = []
    if p.buttons:
        for b in p.buttons:
            t = (b.get("text") or "").strip()
            u = (b.get("url") or "").strip()
            if t and u:
                rows.append([InlineKeyboardButton(text=t, url=u)])
    post_kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    chat_id = cq.message.chat.id
    if p.src_chat_id and p.src_message_ids:
        try:
            await bot.copy_messages(chat_id=chat_id, from_chat_id=p.src_chat_id, message_ids=list(p.src_message_ids))
        except Exception:
            await bot.send_message(chat_id=chat_id, text=p.text or "📸 Альбом")
    elif p.src_chat_id and p.src_message_id:
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=p.src_chat_id, message_id=p.src_message_id, reply_markup=post_kb)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=p.text or "(превью недоступно)", reply_markup=post_kb)
    elif p.media_group:
        media = []
        for it in p.media_group:
            t = it.get("type")
            fid = it.get("file_id")
            if t == "photo":
                media.append(InputMediaPhoto(media=fid))
            elif t == "video":
                media.append(InputMediaVideo(media=fid))
            elif t == "document":
                media.append(InputMediaDocument(media=fid))
        if media:
            await bot.send_media_group(chat_id=chat_id, media=media)
        if p.text or post_kb:
            await bot.send_message(chat_id=chat_id, text=p.text or "⬇️", reply_markup=post_kb)
    elif p.media_type and p.media_file_id:
        sender = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "document": bot.send_document,
            "voice": bot.send_voice,
        }.get(p.media_type)
        if sender:
            kwargs = {"chat_id": chat_id, "caption": p.text, "reply_markup": post_kb}
            kwargs[p.media_type] = p.media_file_id
            await sender(**kwargs)
        else:
            await bot.send_message(chat_id=chat_id, text=p.text or "(медиа)", reply_markup=post_kb)
    else:
        await bot.send_message(chat_id=chat_id, text=p.text or "(пусто)", reply_markup=post_kb)

    manage = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"post_del:{p.id}")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data=f"posts_list:{p.channel_id}")],
    ])
    await bot.send_message(chat_id=chat_id, text=f"📅 {wd} в {p.time_text}\n⏰ Ближайшая отправка: {when}", reply_markup=manage)
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("post_del:"))
async def cb_post_del(cq: types.CallbackQuery):
    post_id = int(cq.data.split(":", 1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Post).where(Post.id == post_id))
        p = res.scalar_one_or_none()
        if not p:
            await cq.answer("Пост не найден", show_alert=True)
            return
        ch_id = p.channel_id
        await session.delete(p)
        await session.commit()
    await cq.answer("Удалён")
    cq.data = f"posts_list:{ch_id}"
    await cb_posts_list(cq)

@dp.callback_query(lambda c: c.data and c.data.startswith("confirm_del_channel:"))
async def cb_confirm_delete(cq: types.CallbackQuery):
    ch_id = int(cq.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delete_channel:{ch_id}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"open_channel:{ch_id}")],
    ])
    await safe_edit_message_text(cq.message, "Точно удалить канал? Это действие необратимо.", kb)
    await cq.answer()

@dp.callback_query(lambda c: c.data and (c.data.startswith("delete_channel:") or c.data.startswith("del_channel:")))
async def cb_delete_channel(cq: types.CallbackQuery):
    ch_id = int(cq.data.split(":", 1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        if ch.owner_id != cq.from_user.id:
            await cq.answer("Удалять канал может только владелец", show_alert=True)
            return
        await session.delete(ch)
        await session.commit()
    await safe_edit_message_text(cq.message, "Канал удалён.")
    await cb_my_channels(cq)

# ---------- админы ----------

@dp.callback_query(lambda c: c.data and c.data.startswith("manage_admins:"))
async def cb_manage_admins(cq: types.CallbackQuery, state: FSMContext):
    ch_id = int(cq.data.split(":", 1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        if ch.owner_id != cq.from_user.id:
            await cq.answer("Управлять администраторами может только владелец", show_alert=True)
            return
        admins_res = await session.execute(select(ChannelAdmin).where(ChannelAdmin.channel_id == ch_id))
        admins = admins_res.scalars().all()
        title = channel_display_name(ch)
        lines = [f"Админы канала: {title}"]
        rows = []
        if admins:
            for a in admins:
                lines.append(f"• {a.telegram_id}")
                rows.append([InlineKeyboardButton(text=f"❌ Удалить {a.telegram_id}", callback_data=f"remove_admin:{ch_id}:{a.telegram_id}")])
        else:
            lines.append("Пока никого нет.")
        rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"add_admin:{ch_id}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"open_channel:{ch_id}")])
        await safe_edit_message_text(cq.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("add_admin:"))
async def cb_add_admin(cq: types.CallbackQuery, state: FSMContext):
    ch_id = int(cq.data.split(":", 1)[1])
    await state.set_state(ManageAdmins.wait_input)
    await state.update_data(admin_channel_id=ch_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Отмена", callback_data=f"manage_admins:{ch_id}")]])
    await safe_edit_message_text(cq.message, "Пришли Telegram ID, @username или перешли сообщение от нужного пользователя.", kb)
    await cq.answer()

@dp.message(StateFilter(ManageAdmins.wait_input))
async def on_admin_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ch_id = data.get("admin_channel_id")
    if not ch_id:
        await state.clear()
        await message.answer("Сессия сброшена. Попробуй ещё раз.")
        return
    new_admin_id = None
    if message.forward_from:
        new_admin_id = message.forward_from.id
    elif message.text:
        txt = message.text.strip()
        if txt.startswith("@"):
            try:
                chat = await bot.get_chat(txt)
                if chat.type == "private":
                    new_admin_id = chat.id
            except Exception:
                pass
        elif txt.lstrip("-").isdigit():
            try:
                new_admin_id = int(txt)
            except Exception:
                new_admin_id = None
    if not new_admin_id:
        await message.answer("Не удалось определить пользователя. Пришли @username, ID или перешли сообщение.")
        return
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await state.clear()
            await message.answer("Канал не найден.")
            return
        if ch.owner_id != message.from_user.id:
            await state.clear()
            await message.answer("Добавлять админов может только владелец.")
            return
        if new_admin_id == ch.owner_id:
            await message.answer("Владелец уже имеет полный доступ.")
            return
        await ensure_user(new_admin_id)
        exists_res = await session.execute(
            select(ChannelAdmin).where(ChannelAdmin.channel_id == ch_id, ChannelAdmin.telegram_id == new_admin_id)
        )
        if exists_res.scalar_one_or_none():
            await message.answer("Этот пользователь уже админ.")
        else:
            session.add(ChannelAdmin(channel_id=ch_id, telegram_id=new_admin_id))
            await session.commit()
            await message.answer("Администратор добавлен.")
    await state.clear()

@dp.callback_query(lambda c: c.data and c.data.startswith("remove_admin:"))
async def cb_remove_admin(cq: types.CallbackQuery, state: FSMContext):
    _, rest = cq.data.split(":", 1)
    ch_id_str, tg_id_str = rest.split(":", 1)
    ch_id = int(ch_id_str)
    tg_id = int(tg_id_str)
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        if ch.owner_id != cq.from_user.id:
            await cq.answer("Удалять админов может только владелец", show_alert=True)
            return
        adm_res = await session.execute(select(ChannelAdmin).where(ChannelAdmin.channel_id == ch_id, ChannelAdmin.telegram_id == tg_id))
        adm = adm_res.scalar_one_or_none()
        if not adm:
            await cq.answer("Такого админа нет", show_alert=True)
            return
        await session.delete(adm)
        await session.commit()
    await cq.answer("Удалён")
    cq.data = f"manage_admins:{ch_id}"
    await cb_manage_admins(cq, state)

# ---------- захват пересланного канала / @username вне FSM ----------

async def _handle_channel_input(message: types.Message) -> bool:
    if message.forward_from_chat and message.forward_from_chat.type == "channel":
        ch = message.forward_from_chat
        async with AsyncSessionLocal() as session:
            await ensure_user(message.from_user.id, message.from_user.full_name)
            res = await session.execute(select(Channel).where(Channel.chat_id == ch.id))
            exists = res.scalar_one_or_none()
            if not exists:
                new = Channel(chat_id=ch.id, username=ch.username, title=ch.title or ch.username, owner_id=message.from_user.id)
                session.add(new)
                await session.commit()
                await message.reply(f"Канал {ch.title} добавлен и ты назначен владельцем.", reply_markup=main_menu_kb())
            else:
                await message.reply("Канал уже добавлен.")
        return True
    if message.text and message.text.startswith("@"):
        uname = message.text.strip()
        try:
            info = await bot.get_chat(uname)
            async with AsyncSessionLocal() as session:
                await ensure_user(message.from_user.id, message.from_user.full_name)
                res = await session.execute(select(Channel).where(Channel.chat_id == info.id))
                exists = res.scalar_one_or_none()
                if not exists:
                    new = Channel(chat_id=info.id, username=info.username, title=info.title or info.username, owner_id=message.from_user.id)
                    session.add(new)
                    await session.commit()
                    await message.reply(f"Канал {info.title} добавлен.", reply_markup=main_menu_kb())
                else:
                    await message.reply("Канал уже добавлен.")
        except Exception:
            await message.reply("Не удалось получить информацию о канале. Убедись, что бот добавлен и имеет доступ.")
        return True
    return False

# ---------- создание поста: канал ----------

@dp.callback_query(lambda c: c.data == "new_post")
async def cb_new_post(cq: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await ensure_user(cq.from_user.id, cq.from_user.full_name)
    async with AsyncSessionLocal() as session:
        owner_res = await session.execute(select(Channel).where(Channel.owner_id == cq.from_user.id))
        owner_channels = owner_res.scalars().all()
        admin_res = await session.execute(
            select(Channel).join(ChannelAdmin, ChannelAdmin.channel_id == Channel.id)
            .where(ChannelAdmin.telegram_id == cq.from_user.id)
        )
        admin_channels = admin_res.scalars().all()
        seen, channels = set(), []
        for ch in owner_channels + admin_channels:
            if ch.id not in seen:
                seen.add(ch.id)
                channels.append(ch)
    if not channels:
        await cq.answer("Нет доступных каналов", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=channel_display_name(ch), callback_data=f"np_ch:{ch.id}")] for ch in channels]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_start")])
    await state.set_state(NewPost.choose_channel)
    await safe_edit_message_text(cq.message, "1️⃣ Выбери канал:", InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()

# ---------- создание поста: день недели ----------

@dp.callback_query(lambda c: c.data and c.data.startswith("np_ch:"), StateFilter(NewPost.choose_channel))
async def np_choose_channel(cq: types.CallbackQuery, state: FSMContext):
    ch_id = int(cq.data.split(":", 1)[1])
    await state.update_data(ch_id=ch_id)
    await _show_weekday_menu(cq.message, state)
    await cq.answer()

async def _show_weekday_menu(message: types.Message, state: FSMContext):
    rows = [[InlineKeyboardButton(text=WEEKDAYS_FULL[i], callback_data=f"np_wd:{i}")] for i in range(7)]
    rows.append([InlineKeyboardButton(text="⬅️ Каналы", callback_data="new_post")])
    await state.set_state(NewPost.choose_weekday)
    await safe_edit_message_text(message, "2️⃣ Выбери день недели:", InlineKeyboardMarkup(inline_keyboard=rows))

# ---------- создание поста: время ----------

@dp.callback_query(lambda c: c.data and c.data.startswith("np_wd:"), StateFilter(NewPost.choose_weekday))
async def np_choose_weekday(cq: types.CallbackQuery, state: FSMContext):
    wd = int(cq.data.split(":", 1)[1])
    await state.update_data(weekday=wd)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="np_back_to_wd")]])
    await state.set_state(NewPost.choose_time)
    await safe_edit_message_text(cq.message, f"3️⃣ {WEEKDAYS_FULL[wd]}\nВведи время в формате HH:MM (МСК):", kb)
    await cq.answer()

@dp.callback_query(lambda c: c.data == "np_back_to_wd", StateFilter(NewPost.choose_time, NewPost.input_content, NewPost.ask_button, NewPost.input_button, NewPost.preview))
async def np_back_to_wd(cq: types.CallbackQuery, state: FSMContext):
    await _show_weekday_menu(cq.message, state)
    await cq.answer()

@dp.message(StateFilter(NewPost.choose_time))
async def np_input_time(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    try:
        hh, mm = map(int, txt.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await message.answer("Неверный формат. Введи HH:MM")
        return
    await state.update_data(time_text=f"{hh:02d}:{mm:02d}")
    await state.set_state(NewPost.input_content)
    await message.answer(
        "4️⃣ Пришли пост одним сообщением: текст и/или фото/видео/документ.\n"
        "Поддерживаются альбомы и premium-эмодзи (всё форматирование сохранится).",
    )

# ---------- создание поста: контент (одно сообщение или альбом) ----------

# Буфер для сборки альбомов: media_group_id -> {"chat_id":int, "ids":[message_id], "user_id":int, "task":Task}
_album_buffer: dict[str, dict] = {}
_album_lock = asyncio.Lock()

@dp.message(StateFilter(NewPost.input_content))
async def np_input_content(message: types.Message, state: FSMContext):
    # Альбом — собираем все message_id с одинаковым media_group_id
    if message.media_group_id:
        mgid = message.media_group_id
        async with _album_lock:
            entry = _album_buffer.get(mgid)
            if not entry:
                entry = {
                    "chat_id": message.chat.id,
                    "ids": [],
                    "user_id": message.from_user.id,
                    "state": state,
                    "task": None,
                }
                _album_buffer[mgid] = entry
            entry["ids"].append(message.message_id)
            # перезапускаем таймер ожидания (1.5с после последнего сообщения)
            if entry["task"]:
                entry["task"].cancel()
            entry["task"] = asyncio.create_task(_finalize_album(mgid))
        return

    # Одиночное сообщение: текст или медиа+caption
    src_chat_id = message.chat.id
    src_message_id = message.message_id
    text = message.caption if message.caption is not None else message.text
    await state.update_data(
        src_chat_id=src_chat_id,
        src_message_id=src_message_id,
        src_message_ids=None,
        text=text,
        text_entities=None,
        media_type=None,
        media_file_id=None,
        media_group=None,
    )
    await _show_buttons_menu(message, state)

async def _finalize_album(mgid: str):
    try:
        await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        return
    async with _album_lock:
        entry = _album_buffer.pop(mgid, None)
    if not entry:
        return
    state: FSMContext = entry["state"]
    ids = sorted(entry["ids"])
    await state.update_data(
        src_chat_id=entry["chat_id"],
        src_message_id=None,
        src_message_ids=ids,
        text=None,
        text_entities=None,
        media_type=None,
        media_file_id=None,
        media_group=None,
    )
    fake = await bot.send_message(chat_id=entry["chat_id"], text=f"📸 Альбом из {len(ids)} элемент(ов) принят.")
    await _show_buttons_menu(fake, state)

# ---------- создание поста: кнопки ----------

async def _show_buttons_menu(message: types.Message, state: FSMContext):
    await state.set_state(NewPost.ask_button)
    data = await state.get_data()
    buttons = data.get("buttons") or []
    rows = [[InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="np_btn_add")]]
    if buttons:
        for i, b in enumerate(buttons):
            rows.append([InlineKeyboardButton(text=f"❌ {b.get('text','')[:30]}", callback_data=f"np_btn_del:{i}")])
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="np_btn_done")])
    else:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="np_btn_done")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="np_back_to_wd")])
    text = "5️⃣ Кнопки поста:\n" + ("\n".join([f"• {b.get('text','')} → {b.get('url','')}" for b in buttons]) if buttons else "пока нет")
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(lambda c: c.data == "np_btn_add", StateFilter(NewPost.ask_button))
async def np_btn_add(cq: types.CallbackQuery, state: FSMContext):
    await state.set_state(NewPost.input_button)
    await safe_edit_message_text(cq.message, "Пришли текст кнопки и ссылку через перенос строки:\nТекст\nhttps://example.com")
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("np_btn_del:"), StateFilter(NewPost.ask_button))
async def np_btn_del(cq: types.CallbackQuery, state: FSMContext):
    idx = int(cq.data.split(":", 1)[1])
    data = await state.get_data()
    buttons = data.get("buttons") or []
    if 0 <= idx < len(buttons):
        buttons.pop(idx)
        await state.update_data(buttons=buttons)
    try:
        await cq.message.delete()
    except Exception:
        pass
    await _show_buttons_menu(cq.message, state)
    await cq.answer("Кнопка удалена")

@dp.callback_query(lambda c: c.data == "np_btn_done", StateFilter(NewPost.ask_button))
async def np_btn_done(cq: types.CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(NewPost.preview)
    await send_post_preview(cq.message, state)

@dp.message(StateFilter(NewPost.input_button))
async def np_input_button(message: types.Message, state: FSMContext):
    lines = (message.text or "").splitlines()
    if len(lines) < 2 or not lines[1].strip().startswith("http"):
        await message.answer("Неверный формат. Пример:\nЗаголовок\nhttps://example.com")
        return
    text = lines[0].strip()
    url = lines[1].strip()
    data = await state.get_data()
    buttons = data.get("buttons") or []
    buttons.append({"text": text, "url": url})
    await state.update_data(buttons=buttons)
    await _show_buttons_menu(message, state)

# ---------- предпросмотр и сохранение ----------

async def send_post_preview(message: types.Message, state: FSMContext):
    data = await state.get_data()
    buttons = data.get("buttons") or []
    rows = []
    for b in buttons:
        t = (b.get("text") or "").strip()
        u = (b.get("url") or "").strip()
        if t and u:
            rows.append([InlineKeyboardButton(text=t, url=u)])
    post_kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    src_chat_id = data.get("src_chat_id")
    src_message_id = data.get("src_message_id")
    src_message_ids = data.get("src_message_ids")
    text = data.get("text")

    # Превью через copy_message — это сохранит premium-эмодзи и форматирование 1:1
    if src_chat_id and src_message_ids:
        await bot.copy_messages(chat_id=message.chat.id, from_chat_id=src_chat_id, message_ids=src_message_ids)
        if post_kb:
            await bot.send_message(chat_id=message.chat.id, text=(text or "⬇️"), reply_markup=post_kb)
            await bot.send_message(chat_id=message.chat.id, text="ℹ️ У альбома кнопки прикрепляются отдельным сообщением.")
    elif src_chat_id and src_message_id:
        await bot.copy_message(chat_id=message.chat.id, from_chat_id=src_chat_id, message_id=src_message_id, reply_markup=post_kb)
    else:
        await bot.send_message(chat_id=message.chat.id, text=(text or "Пост без содержимого"), reply_markup=post_kb)

    # сводка времени
    weekday = data.get("weekday")
    time_text = data.get("time_text")
    summary = f"📅 {WEEKDAYS_FULL[weekday]} в {time_text} (МСК)"
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="np_preview_save")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="np_preview_back")],
    ])
    await bot.send_message(chat_id=message.chat.id, text=f"6️⃣ Предпросмотр\n{summary}\n\nСохранить пост?", reply_markup=confirm_kb)

@dp.callback_query(lambda c: c.data == "np_preview_save", StateFilter(NewPost.preview))
async def np_preview_save(cq: types.CallbackQuery, state: FSMContext):
    await finalize_post(cq.message, state)
    await cq.answer()

@dp.callback_query(lambda c: c.data == "np_preview_back", StateFilter(NewPost.preview))
async def np_preview_back(cq: types.CallbackQuery, state: FSMContext):
    await _show_buttons_menu(cq.message, state)
    await cq.answer()

async def finalize_post(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ch_id = data["ch_id"]
    weekday = data["weekday"]
    time_text = data["time_text"]
    text = data.get("text")
    src_chat_id = data.get("src_chat_id")
    src_message_id = data.get("src_message_id")
    src_message_ids = data.get("src_message_ids")
    buttons = data.get("buttons") or None

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await state.clear()
            await message.answer("Канал не найден")
            return
        hh, mm = map(int, time_text.split(":"))
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        next_run = compute_next_weekday_time_tz(now_utc, weekday, dtime(hh, mm), "Europe/Moscow")

        editing_post_id = data.get("editing_post_id")
        if editing_post_id:
            eres = await session.execute(select(Post).where(Post.id == editing_post_id))
            existing = eres.scalar_one_or_none()
        else:
            existing = None

        if existing:
            existing.text = text
            existing.src_chat_id = src_chat_id
            existing.src_message_id = src_message_id
            existing.src_message_ids = src_message_ids
            existing.buttons = buttons
            existing.time_text = time_text
            existing.weekday = weekday
            existing.week_in_cycle = None
            existing.next_run = next_run
            # сбрасываем legacy-поля
            existing.media_type = None
            existing.media_file_id = None
            existing.media_group = None
            existing.text_entities = None
        else:
            post = Post(
                channel_id=ch_id,
                text=text,
                src_chat_id=src_chat_id,
                src_message_id=src_message_id,
                src_message_ids=src_message_ids,
                buttons=buttons,
                next_run=next_run,
                weekday=weekday,
                time_text=time_text,
                created_by=message.from_user.id,
            )
            session.add(post)
        await session.commit()
    await state.clear()
    end_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый пост", callback_data="new_post")],
        [InlineKeyboardButton(text="📚 Мои каналы", callback_data="my_channels")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_start")],
    ])
    await message.answer(f"✅ Пост сохранён.\n📅 {WEEKDAYS_FULL[weekday]} в {time_text} (МСК)", reply_markup=end_kb)

# ---------- ловушка вне FSM: пересланный канал / @username ----------

@dp.message(StateFilter(None))
async def catch_outside_fsm(message: types.Message, state: FSMContext):
    handled = await _handle_channel_input(message)
    if not handled:
        # игнорируем
        pass

# ---------- entry point ----------

async def main():
    print("Starting bot...")
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

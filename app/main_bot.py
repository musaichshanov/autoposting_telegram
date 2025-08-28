# app/main_bot.py
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
from app.db import AsyncSessionLocal, init_db
from app.models import User, Channel, ChannelAdmin, Post
from sqlalchemy.future import select
from sqlalchemy import insert
from datetime import datetime, time, timedelta
from app.utils import compute_next_run_from_weekday_and_time
from zoneinfo import ZoneInfo

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=MemoryStorage())

# Helpers
MAIN_TEXT = "Привет! Я бот автопостинга. Авторизация через Telegram — ты уже в системе."

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
        sent = await bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=kb)
        try:
            await msg.delete()
        except Exception:
            pass

class NewPost(StatesGroup):
    choose_channel = State()
    choose_week = State()
    choose_weekday = State()
    choose_time = State()
    input_text = State()
    input_media = State()
    ask_button = State()
    input_button = State()
    choose_parse = State()
    preview = State()

class ManageAdmins(StatesGroup):
    wait_input = State()

async def ensure_user(telegram_id:int, name:str=None):
    async with AsyncSessionLocal() as session:
        q = await session.execute(select(User).where(User.telegram_id==telegram_id))
        u = q.scalar_one_or_none()
        if not u:
            u = User(telegram_id=telegram_id, name=name)
            session.add(u)
            await session.commit()
        return u

@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message, state: FSMContext):
    await ensure_user(message.from_user.id, message.from_user.full_name)
    await message.answer(MAIN_TEXT, reply_markup=main_menu_kb())

@dp.callback_query(lambda c: c.data=="back_start")
async def cb_back_start(cq: types.CallbackQuery):
    await cq.message.edit_text(MAIN_TEXT, reply_markup=main_menu_kb())
    await cq.answer()

# Добавление канала (пересланное сообщение или @username)
@dp.callback_query(lambda c: c.data=="add_channel")
async def cb_add_channel(cq: types.CallbackQuery):
    await cq.message.answer("Перешли сообщение из канала (бот должен быть админом) или введи @username канала.")
    await cq.answer()

# Мои каналы — показать список каналов кнопками
@dp.callback_query(lambda c: c.data=="my_channels")
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
            await cq.message.edit_text("У тебя пока нет каналов. Нажми ‘Добавить канал’.", reply_markup=main_menu_kb())
        else:
            rows = []
            for ch in channels:
                title = channel_display_name(ch)
                rows.append([InlineKeyboardButton(text=title, callback_data=f"open_channel:{ch.id}")])
            rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_start")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await cq.message.edit_text("Выбери канал:", reply_markup=kb)
    await cq.answer()

# Подменю конкретного канала (+ настройки цикла)
@dp.callback_query(lambda c: c.data and c.data.startswith("open_channel:"))
async def cb_open_channel(cq: types.CallbackQuery):
    _, ch_id_str = cq.data.split(":", 1)
    ch_id = int(ch_id_str)
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        title = channel_display_name(ch)
        text = (f"Канал: {title}\n"
                f"Цикл (недели): {ch.cycle_weeks}\n")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить канал", callback_data=f"confirm_del_channel:{ch.id}")],
            [InlineKeyboardButton(text="👤 Админы", callback_data=f"manage_admins:{ch.id}")],
            [InlineKeyboardButton(text="⚙️ Настройки цикла", callback_data=f"cycle_settings:{ch.id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="my_channels")],
        ])
        await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("cycle_settings:"))
async def cb_cycle_settings(cq: types.CallbackQuery):
    ch_id = int(cq.data.split(":",1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id==ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        # только владелец может менять цикл
        if ch.owner_id != cq.from_user.id:
            await cq.answer("Изменять цикл может только владелец", show_alert=True)
            return
        rows = []
        for i in range(1, 9):
            mark = " ✅" if ch.cycle_weeks == i else ""
            rows.append([InlineKeyboardButton(text=f"{i} недель{mark}", callback_data=f"set_weeks:{ch_id}:{i}")])
        rows.append([InlineKeyboardButton(text="Сбросить старт цикла на сегодня", callback_data=f"reset_cycle_start:{ch_id}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"open_channel:{ch_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await cq.message.edit_text(f"Настройки цикла (сейчас {ch.cycle_weeks} недель):", reply_markup=kb)
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("set_weeks:"))
async def cb_set_weeks(cq: types.CallbackQuery):
    _, ch_id_str, weeks_str = cq.data.split(":", 2)
    ch_id, weeks = int(ch_id_str), int(weeks_str)
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id==ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        if ch.owner_id != cq.from_user.id:
            await cq.answer("Изменять цикл может только владелец", show_alert=True)
            return
        ch.cycle_weeks = max(1, min(weeks, 52))
        await session.commit()
    await cq.answer("Сохранено")
    await cb_cycle_settings(cq)

@dp.callback_query(lambda c: c.data and c.data.startswith("reset_cycle_start:"))
async def cb_reset_cycle_start(cq: types.CallbackQuery):
    ch_id = int(cq.data.split(":",1)[1])
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id==ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        if ch.owner_id != cq.from_user.id:
            await cq.answer("Изменять цикл может только владелец", show_alert=True)
            return
        from datetime import datetime
        ch.cycle_start = datetime.utcnow()
        await session.commit()
    await cq.answer("Старт цикла сброшен на сегодня")
    await cb_cycle_settings(cq)

# Подтверждение удаления
@dp.callback_query(lambda c: c.data and c.data.startswith("confirm_del_channel:"))
async def cb_confirm_delete(cq: types.CallbackQuery):
    _, ch_id_str = cq.data.split(":", 1)
    ch_id = int(ch_id_str)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delete_channel:{ch_id}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"open_channel:{ch_id}")],
    ])
    await cq.message.edit_text("Точно удалить канал? Это действие необратимо.", reply_markup=kb)
    await cq.answer()

# Удаление канала владельцем
@dp.callback_query(lambda c: c.data and (c.data.startswith("delete_channel:") or c.data.startswith("del_channel:")))
async def cb_delete_channel(cq: types.CallbackQuery):
    _, ch_id_str = cq.data.split(":", 1)
    ch_id = int(ch_id_str)

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
    await cq.message.edit_text("Канал удалён. Возврат к списку…")
    await cb_my_channels(cq)

# Управление администраторами: список + кнопки
@dp.callback_query(lambda c: c.data and c.data.startswith("manage_admins:"))
async def cb_manage_admins(cq: types.CallbackQuery, state: FSMContext):
    _, ch_id_str = cq.data.split(":", 1)
    ch_id = int(ch_id_str)
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await cq.answer("Канал не найден", show_alert=True)
            return
        if ch.owner_id != cq.from_user.id:
            await cq.answer("Управлять администраторами может только владелец", show_alert=True)
            return
        # собираем список админов
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
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await cq.message.edit_text("\n".join(lines), reply_markup=kb)
    await cq.answer()

# Кнопка Добавить администратора — просим ввод
@dp.callback_query(lambda c: c.data and c.data.startswith("add_admin:"))
async def cb_add_admin(cq: types.CallbackQuery, state: FSMContext):
    _, ch_id_str = cq.data.split(":", 1)
    ch_id = int(ch_id_str)
    await state.set_state(ManageAdmins.wait_input)
    await state.update_data(admin_channel_id=ch_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Отмена", callback_data=f"manage_admins:{ch_id}")]])
    await cq.message.edit_text("Пришли Telegram ID, @username или перешли сообщение от нужного пользователя.", reply_markup=kb)
    await cq.answer()

# Приём ввода администратора в состоянии
@dp.message(StateFilter(ManageAdmins.wait_input))
async def on_admin_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ch_id = data.get("admin_channel_id")
    if not ch_id:
        await state.clear()
        await message.answer("Сессия сброшена. Попробуй ещё раз.")
        return

    # определяем telegram_id
    new_admin_id = None
    # 1) пересланное сообщение
    if message.forward_from:
        new_admin_id = message.forward_from.id
    # 2) текст
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
        # проверяем владельца
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
        # создаём запись пользователя, если нужно
        await ensure_user(new_admin_id)
        # проверяем, есть ли уже
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
    # Подскажем вернуться в меню админов
    await message.answer("Администратор добавлен. Открой ‘Админы’ ещё раз из меню канала.")

# Удаление администратора
@dp.callback_query(lambda c: c.data and c.data.startswith("remove_admin:"))
async def cb_remove_admin(cq: types.CallbackQuery):
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
    await cb_manage_admins(cq, FSMContext(storage=None, key=None))

@dp.message(StateFilter(None))
async def catch_channel(message: types.Message, state: FSMContext):
    # if forwarded from channel:
    if message.forward_from_chat and message.forward_from_chat.type == "channel":
        ch = message.forward_from_chat
        async with AsyncSessionLocal() as session:
            # find or create user
            await ensure_user(message.from_user.id, message.from_user.full_name)
            res = await session.execute(select(Channel).where(Channel.chat_id==ch.id))
            exists = res.scalar_one_or_none()
            if not exists:
                new = Channel(chat_id=ch.id, username=ch.username, title=ch.title or ch.username, owner_id=message.from_user.id)
                session.add(new)
                await session.commit()
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настроить цикл", callback_data=f"cycle_settings:{new.id}")]])
                await message.reply(f"Канал {ch.title} добавлен и ты назначен владельцем.", reply_markup=kb)
            else:
                await message.reply("Канал уже добавлен.")
        return
    # if text @username:
    if message.text and message.text.startswith("@"):
        uname = message.text.strip()
        try:
            info = await bot.get_chat(uname)
            async with AsyncSessionLocal() as session:
                await ensure_user(message.from_user.id, message.from_user.full_name)
                res = await session.execute(select(Channel).where(Channel.chat_id==info.id))
                exists = res.scalar_one_or_none()
                if not exists:
                    new = Channel(chat_id=info.id, username=info.username, title=info.title or info.username, owner_id=message.from_user.id)
                    session.add(new)
                    await session.commit()
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настроить цикл", callback_data=f"cycle_settings:{new.id}")]])
                    await message.reply(f"Канал {info.title} добавлен.", reply_markup=kb)
                else:
                    await message.reply("Канал уже добавлен.")
        except Exception as e:
            await message.reply("Не удалось получить информацию о канале. Убедись, что бот добавлен и имеет доступ.")
        return
    # else ignore other messages (or handle FSM flows)

# Старт создания поста
@dp.callback_query(lambda c: c.data=="new_post")
async def cb_new_post(cq: types.CallbackQuery, state: FSMContext):
    await ensure_user(cq.from_user.id, cq.from_user.full_name)
    # показать выбор канала, где юзер владелец или админ
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
    rows = [[InlineKeyboardButton(text=channel_display_name(ch), callback_data=f"np_ch:{ch.id}")]
            for ch in channels]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_start")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await state.set_state(NewPost.choose_channel)
    await cq.message.edit_text("Выбери канал:", reply_markup=kb)
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("np_ch:"), StateFilter(NewPost.choose_channel))
async def np_choose_channel(cq: types.CallbackQuery, state: FSMContext):
    ch_id = int(cq.data.split(":",1)[1])
    await state.update_data(ch_id=ch_id)
    # спросить неделю цикла
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id==ch_id))
        ch = res.scalar_one_or_none()
    weeks = ch.cycle_weeks or 1
    rows = [[InlineKeyboardButton(text=f"Неделя {i+1}", callback_data=f"np_week:{i}")] for i in range(weeks)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="↩️ Назад", callback_data="new_post")]])
    await state.set_state(NewPost.choose_week)
    await cq.message.edit_text("Выбери неделю в цикле:", reply_markup=kb)
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("np_week:"), StateFilter(NewPost.choose_week))
async def np_choose_week(cq: types.CallbackQuery, state: FSMContext):
    week = int(cq.data.split(":",1)[1])
    await state.update_data(week=week)
    # выбрать день недели с отметками существующих постов
    weekdays = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    data = await state.get_data()
    ch_id = data.get("ch_id")
    existing_days = set()
    async with AsyncSessionLocal() as session:
        from .models import Post
        res = await session.execute(select(Post.weekday).where(Post.channel_id==ch_id, Post.week_in_cycle==week))
        for (wd,) in res.all():
            if wd is not None:
                existing_days.add(int(wd))
    rows = []
    for i in range(7):
        mark = " •" if i in existing_days else ""
        rows.append([InlineKeyboardButton(text=f"{weekdays[i]}{mark}", callback_data=f"np_wd:{i}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Недели", callback_data="new_post")]])
    await state.set_state(NewPost.choose_weekday)
    await cq.message.edit_text("Выбери день недели:", reply_markup=kb)
    await cq.answer()

# Назад к неделям из экрана дня
@dp.callback_query(lambda c: c.data and c.data.startswith("np_week:"), StateFilter(NewPost.choose_weekday))
async def np_back_to_weeks(cq: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ch_id = data.get("ch_id")
    if not ch_id:
        await cq.answer("Нет данных канала", show_alert=True)
        return
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Channel).where(Channel.id==ch_id))
        ch = res.scalar_one_or_none()
    weeks = (ch.cycle_weeks or 1) if ch else 1
    rows = [[InlineKeyboardButton(text=f"Неделя {i+1}", callback_data=f"np_week:{i}")] for i in range(weeks)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Каналы", callback_data="new_post")]])
    await state.set_state(NewPost.choose_week)
    await cq.message.edit_text("Выбери неделю в цикле:", reply_markup=kb)
    await cq.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("np_wd:"), StateFilter(NewPost.choose_weekday))
async def np_choose_weekday(cq: types.CallbackQuery, state: FSMContext):
    wd = int(cq.data.split(":",1)[1])
    await state.update_data(weekday=wd)
    data = await state.get_data()
    ch_id = data.get("ch_id")
    week = data.get("week")
    # получаем все посты для дня
    async with AsyncSessionLocal() as session:
        from .models import Post
        res = await session.execute(
            select(Post).where(Post.channel_id==ch_id, Post.week_in_cycle==week, Post.weekday==wd).order_by(Post.created_at.asc())
        )
        posts = res.scalars().all()
    weekdays = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    if posts:
        await render_day_posts_menu(cq.message, ch_id, week, wd)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"np_week:{week}")]])
        await state.set_state(NewPost.choose_time)
        await cq.message.edit_text("Введи время в формате HH:MM (МСК)", reply_markup=kb)
    await cq.answer()

# Назад из ввода времени: поддержим возврат к списку дней или недель
@dp.callback_query(lambda c: c.data and (c.data.startswith("np_week:") or c.data.startswith("np_wd:")), StateFilter(NewPost.choose_time))
async def np_back_from_time(cq: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    ch_id = data.get("ch_id")
    week = data.get("week")
    if cq.data.startswith("np_wd:"):
        wd = int(cq.data.split(":",1)[1])
        await state.set_state(NewPost.choose_weekday)
        await state.update_data(weekday=wd)
        await render_day_posts_menu(cq.message, ch_id, week, wd)
    else:
        # назад к неделям
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Channel).where(Channel.id==ch_id))
            ch = res.scalar_one_or_none()
        weeks = (ch.cycle_weeks or 1) if ch else 1
        rows = [[InlineKeyboardButton(text=f"Неделя {i+1}", callback_data=f"np_week:{i}")] for i in range(weeks)]
        kb = InlineKeyboardMarkup(inline_keyboard=rows + [[InlineKeyboardButton(text="⬅️ Каналы", callback_data="new_post")]])
        await state.set_state(NewPost.choose_week)
        await cq.message.edit_text("Выбери неделю в цикле:", reply_markup=kb)
    await cq.answer()

# Кнопка: добавить новый пост в выбранный день
@dp.callback_query(lambda c: c.data and c.data.startswith("np_add:"), StateFilter(NewPost.choose_weekday))
async def np_add_new_post(cq: types.CallbackQuery, state: FSMContext):
    wd = int(cq.data.split(":",1)[1])
    data = await state.get_data()
    week = data.get("week")
    await state.update_data(weekday=wd)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"np_wd:{wd}")]])
    await state.set_state(NewPost.choose_time)
    await cq.message.edit_text("Введи время в формате HH:MM (МСК)", reply_markup=kb)
    await cq.answer()

# Просмотр конкретного поста
@dp.callback_query(lambda c: c.data and c.data.startswith("np_view:"), StateFilter(NewPost.choose_weekday))
async def np_view_post(cq: types.CallbackQuery, state: FSMContext):
    post_id = int(cq.data.split(":",1)[1])
    async with AsyncSessionLocal() as session:
        from .models import Post
        res = await session.execute(select(Post).where(Post.id==post_id))
        p = res.scalar_one_or_none()
    if not p:
        await cq.answer("Пост не найден", show_alert=True)
        return
    await state.update_data(weekday=p.weekday, week=p.week_in_cycle, ch_id=p.channel_id)
    # Время (МСК)
    try:
        from zoneinfo import ZoneInfo
        if p.next_run:
            local_dt = (p.next_run if p.next_run.tzinfo else p.next_run.replace(tzinfo=ZoneInfo("UTC"))).astimezone(ZoneInfo("Europe/Moscow"))
            when_str = local_dt.strftime("%d.%m.%Y %H:%M (МСК)")
        else:
            when_str = "не запланирован"
    except Exception:
        when_str = "не запланирован"

    kb_manage = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"np_start_edit:{p.id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"np_del_confirm:{p.id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"np_wd:{p.weekday}")],
    ])
    # подготавливаем entities для форматирования
    entities = None
    if p.text_entities:
        try:
            from aiogram.types import MessageEntity
            entities = [MessageEntity(**e) for e in p.text_entities]
        except Exception:
            entities = None
    try:
        await cq.message.delete()
    except Exception:
        pass
    if p.media_group:
        # отправим альбом, потом отдельным сообщением текст с форматированием и клавиатурой управления
        from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
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
            await bot.send_media_group(chat_id=cq.message.chat.id, media=media)
        text_preview = p.text or "Предпросмотр медиагруппы"
        await bot.send_message(chat_id=cq.message.chat.id, text=text_preview, entities=entities, reply_markup=kb_manage)
    elif p.media_type == "photo":
        await bot.send_photo(chat_id=cq.message.chat.id, photo=p.media_file_id, caption=p.text, caption_entities=entities, reply_markup=kb_manage)
    elif p.media_type == "video":
        await bot.send_video(chat_id=cq.message.chat.id, video=p.media_file_id, caption=p.text, caption_entities=entities, reply_markup=kb_manage)
    elif p.media_type == "document":
        await bot.send_document(chat_id=cq.message.chat.id, document=p.media_file_id, caption=p.text, caption_entities=entities, reply_markup=kb_manage)
    elif p.media_type == "video_note":
        await bot.send_video_note(chat_id=cq.message.chat.id, video_note=p.media_file_id)
        await bot.send_message(chat_id=cq.message.chat.id, text=(p.text or "Кружок."), entities=entities, reply_markup=kb_manage)
    else:
        await bot.send_message(chat_id=cq.message.chat.id, text=(p.text or "Пост без текста"), entities=entities, reply_markup=kb_manage)
    await cq.answer()

# Подтверждение удаления поста
@dp.callback_query(lambda c: c.data and c.data.startswith("np_del_confirm:"), StateFilter(NewPost.choose_weekday))
async def np_del_confirm(cq: types.CallbackQuery, state: FSMContext):
    post_id = int(cq.data.split(":",1)[1])
    async with AsyncSessionLocal() as session:
        from .models import Post
        res = await session.execute(select(Post).where(Post.id==post_id))
        p = res.scalar_one_or_none()
    if not p:
        await cq.answer("Пост не найден", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"np_del:{post_id}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"np_view:{post_id}")],
    ]
    await safe_edit_message_text(cq.message, "Удалить этот пост?", InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()

# Начать редактирование поста (работает из любого состояния)
@dp.callback_query(lambda c: c.data and c.data.startswith("np_start_edit:"))
async def np_start_edit(cq: types.CallbackQuery, state: FSMContext):
    post_id = int(cq.data.split(":",1)[1])
    await state.update_data(editing_post_id=post_id)
    # загрузим пост, чтобы показать текущее время и кнопку назад
    async with AsyncSessionLocal() as session:
        from .models import Post
        res = await session.execute(select(Post).where(Post.id==post_id))
        p = res.scalar_one_or_none()
    if not p:
        await cq.answer("Пост не найден", show_alert=True)
        return
    cur_time = p.time_text or "HH:MM"
    await state.set_state(NewPost.choose_time)
    await safe_edit_message_text(
        cq.message,
        f"Введи время (МСК), текущее {cur_time}:",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"np_wd:{p.weekday}")]])
    )
    await cq.answer()

# Удаление поста
@dp.callback_query(lambda c: c.data and c.data.startswith("np_del:"), StateFilter(NewPost.choose_weekday))
async def np_delete_post(cq: types.CallbackQuery, state: FSMContext):
    post_id = int(cq.data.split(":",1)[1])
    async with AsyncSessionLocal() as session:
        from .models import Post
        res = await session.execute(select(Post).where(Post.id==post_id))
        p = res.scalar_one_or_none()
        if not p:
            await cq.answer("Пост не найден", show_alert=True)
            return
        week, wd = p.week_in_cycle, p.weekday
        await session.delete(p)
        await session.commit()
    await cq.answer("Удалено")
    # вернуться к списку постов дня
    data = await state.get_data()
    ch_id = data.get("ch_id")
    week = data.get("week")
    await render_day_posts_menu(cq.message, ch_id, week, wd)

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
    await state.set_state(NewPost.input_text)
    await message.answer("Пришли текст поста (или отправь ‘-’ чтобы пропустить)")

@dp.message(StateFilter(NewPost.input_text))
async def np_input_text(message: types.Message, state: FSMContext):
    # В режиме редактирования '-' оставит текст/entities без изменений
    data = await state.get_data()
    is_edit = bool(data.get("editing_post_id"))
    if message.text == "-" and is_edit:
        text_value = None
        entities_value = None
    else:
        text_value = message.text
        # Сохраняем entities от пользователя (Telegram парсит Markdown/HTML автоматически)
        entities_value = [e.model_dump(mode="json") for e in (message.entities or [])] if hasattr(message, "entities") else None
    await state.update_data(text=text_value, text_entities=entities_value)
    await state.set_state(NewPost.input_media)
    await message.answer("Пришли медиа (фото/видео/документ) или ‘-’ чтобы пропустить")

@dp.message(StateFilter(NewPost.input_media))
async def np_input_media(message: types.Message, state: FSMContext):
    media_type, media_id = None, None
    media_group = None
    if message.text and message.text.strip() == "-":
        # в режиме редактирования '-' оставит медиа без изменений
        pass
    elif message.photo:
        media_type = "photo"
        media_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        media_id = message.video.file_id
    elif message.document:
        media_type = "document"
        media_id = message.document.file_id
    elif message.animation:
        media_type = "document"
        media_id = message.animation.file_id
    elif message.video_note:
        # кружок — отдельный тип, у него нет caption и кнопок
        media_type = "video_note"
        media_id = message.video_note.file_id
        await message.answer("Добавлен кружок. Учти: к кружкам нельзя добавлять подпись и кнопки.")
    elif message.media_group_id:
        # медиагруппа: соберём файлы из альбома
        # в aiogram 3 альбом приходит серией сообщений; здесь мы фиксируем один элемент
        item = None
        if message.photo:
            item = {"type": "photo", "file_id": message.photo[-1].file_id}
        elif message.video:
            item = {"type": "video", "file_id": message.video.file_id}
        elif message.document:
            item = {"type": "document", "file_id": message.document.file_id}
        if item:
            cur = (await state.get_data()).get("media_group") or []
            cur.append(item)
            await state.update_data(media_group=cur)
            await message.answer("Медиагруппа: элемент добавлен. Отправь ещё элементы альбома или '-' для завершения.")
            return
    await state.update_data(media_type=media_type, media_file_id=media_id)
    await state.set_state(NewPost.ask_button)
    buttons = (await state.get_data()).get("buttons") or []
    rows = [
        [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="np_btn_add")],
    ]
    if buttons:
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="np_btn_done")])
        rows.append([InlineKeyboardButton(text="🗑 Очистить", callback_data="np_btn_clear")])
    else:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="np_btn_done")])
    await message.answer("Кнопки поста:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(lambda c: c.data in ("np_btn_add","np_btn_done","np_btn_clear"), StateFilter(NewPost.ask_button))
async def np_buttons_menu(cq: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buttons = data.get("buttons") or []
    if cq.data == "np_btn_add":
        await state.set_state(NewPost.input_button)
        await cq.message.edit_text("Пришли текст кнопки и ссылку через перенос строки:\nТекст\nhttps://example.com")
    elif cq.data == "np_btn_clear":
        await state.update_data(buttons=[])
        await cq.message.edit_text("Кнопки очищены. Можно добавить новые или продолжить.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="np_btn_add")],[InlineKeyboardButton(text="Пропустить", callback_data="np_btn_done")]]))
    else:
        # Готово — показываем предпросмотр перед сохранением
        await state.set_state(NewPost.preview)
        await send_post_preview(cq.message, state)
    await cq.answer()

# Если в состоянии выбора кнопок пользователь прислал текст, а не нажал inline — повторно показываем меню
@dp.message(StateFilter(NewPost.ask_button))
async def np_buttons_menu_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    buttons = data.get("buttons") or []
    rows = [[InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="np_btn_add")]]
    if buttons:
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="np_btn_done")])
        rows.append([InlineKeyboardButton(text="🗑 Очистить", callback_data="np_btn_clear")])
    else:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="np_btn_done")])
    await message.answer("Пожалуйста, воспользуйся кнопками ниже.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

# Ввод одной кнопки: две строки (текст и ссылка). После добавления возвращаемся в меню кнопок
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
    await state.set_state(NewPost.ask_button)
    rows = [
        [InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="np_btn_add")],
        [InlineKeyboardButton(text="✅ Готово", callback_data="np_btn_done")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data="np_btn_clear")],
    ]
    await message.answer("Кнопка добавлена. Добавить ещё?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

async def finalize_post(message_or_message, state: FSMContext):
    data = await state.get_data()
    ch_id = data["ch_id"]
    week = data["week"]
    weekday = data["weekday"]
    time_text = data["time_text"]
    text = data.get("text")
    media_type = data.get("media_type")
    media_file_id = data.get("media_file_id")
    button_text = data.get("button_text")
    button_url = data.get("button_url")
    text_entities = data.get("text_entities")
    buttons = data.get("buttons") or None

    async with AsyncSessionLocal() as session:
        # вычисляем next_run
        from datetime import datetime, time as dtime
        from .models import Channel, Post
        from .utils import compute_next_run_cycle_tz
        res = await session.execute(select(Channel).where(Channel.id == ch_id))
        ch = res.scalar_one_or_none()
        if not ch:
            await state.clear()
            await message_or_message.answer("Канал не найден")
            return
        # если редактирование — загрузим текущий пост
        editing_post_id = data.get("editing_post_id")
        existing = None
        if editing_post_id:
            eres = await session.execute(select(Post).where(Post.id==editing_post_id))
            existing = eres.scalar_one_or_none()
        hh, mm = map(int, time_text.split(":"))
        # рассчитываем по МСК, в БД сохраняем UTC
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        next_run = compute_next_run_cycle_tz(
            now_utc=now_utc,
            cycle_weeks=ch.cycle_weeks or 1,
            cycle_start_utc=ch.cycle_start if ch.cycle_start.tzinfo else ch.cycle_start.replace(tzinfo=ZoneInfo("UTC")),
            week_in_cycle=week,
            weekday=weekday,
            t_local=dtime(hh, mm),
            tz_name="Europe/Moscow",
        )
        # Если пользователь выбрал уже прошедшее время сегодня (МСК) с небольшим опозданием, пошлём ближайшим временем
        try:
            now_local = now_utc.astimezone(ZoneInfo("Europe/Moscow"))
            candidate_local = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if now_local.weekday() == weekday and now_local >= candidate_local and (now_local - candidate_local) <= timedelta(minutes=15):
                next_run = now_utc + timedelta(seconds=30)
        except Exception:
            pass
        if existing:
            # слияние значений: None означает оставить как было
            existing.text = existing.text if text is None else text
            if text_entities is not None:
                existing.text_entities = text_entities
            if media_type is not None:
                existing.media_type = media_type
                existing.media_file_id = media_file_id
            # кнопка: если не пришла — оставляем как было
            if button_text is not None and button_url is not None:
                existing.button_text = button_text
                existing.button_url = button_url
            if buttons is not None:
                existing.buttons = buttons
            existing.time_text = time_text
            existing.next_run = next_run
            existing.week_in_cycle = week
            existing.weekday = weekday
        else:
            post = Post(
                channel_id=ch_id,
                text=text,
                text_entities=text_entities,
                media_type=media_type,
                media_file_id=media_file_id,
                button_text=button_text,
                button_url=button_url,
                buttons=buttons,
                next_run=next_run,
                week_in_cycle=week,
                weekday=weekday,
                time_text=time_text,
                created_by=message_or_message.from_user.id,
            )
            session.add(post)
        await session.commit()
    await state.clear()
    end_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый пост", callback_data="new_post")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_start")],
    ])
    await message_or_message.answer("Слот сохранён.", reply_markup=end_kb)

# Предпросмотр слота перед сохранением
async def send_post_preview(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("text")
    text_entities = data.get("text_entities")
    media_type = data.get("media_type")
    media_file_id = data.get("media_file_id")
    media_group = data.get("media_group")
    buttons = data.get("buttons") or []

    # Собираем клавиатуру поста (внешние кнопки)
    rows = []
    for b in buttons:
        t = (b.get("text") or "").strip()
        u = (b.get("url") or "").strip()
        if t and u:
            rows.append([InlineKeyboardButton(text=t, url=u)])
    post_kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    # Entities
    entities = None
    if text_entities:
        try:
            from aiogram.types import MessageEntity
            entities = [MessageEntity(**e) for e in text_entities]
        except Exception:
            entities = None

    # Предупреждения по ограничениям
    warn_lines = []
    if media_type == "video_note" and (text or buttons):
        warn_lines.append("Внимание: к кружкам нельзя добавлять подпись и кнопки. Текст/кнопки будут отправлены отдельно.")
    if media_group and buttons:
        warn_lines.append("Внимание: у медиагруппы нет кнопок — они будут отправлены отдельным сообщением.")

    # Отрисовка предпросмотра
    if media_group:
        from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
        media = []
        for it in media_group:
            t = it.get("type")
            fid = it.get("file_id")
            if t == "photo":
                media.append(InputMediaPhoto(media=fid))
            elif t == "video":
                media.append(InputMediaVideo(media=fid))
            elif t == "document":
                media.append(InputMediaDocument(media=fid))
        if media:
            await bot.send_media_group(chat_id=message.chat.id, media=media)
        if text or post_kb or warn_lines:
            extra = ("\n"+"\n".join(warn_lines)) if warn_lines else ""
            await bot.send_message(chat_id=message.chat.id, text=(text or "Предпросмотр медиагруппы")+extra, entities=entities, reply_markup=post_kb)
    elif media_type == "photo":
        await bot.send_photo(chat_id=message.chat.id, photo=media_file_id, caption=text, caption_entities=entities, reply_markup=post_kb)
    elif media_type == "video":
        await bot.send_video(chat_id=message.chat.id, video=media_file_id, caption=text, caption_entities=entities, reply_markup=post_kb)
    elif media_type == "document":
        await bot.send_document(chat_id=message.chat.id, document=media_file_id, caption=text, caption_entities=entities, reply_markup=post_kb)
    elif media_type == "video_note":
        await bot.send_video_note(chat_id=message.chat.id, video_note=media_file_id)
        if text or buttons or warn_lines:
            extra = ("\n"+"\n".join(warn_lines)) if warn_lines else ""
            await bot.send_message(chat_id=message.chat.id, text=(text or "Кружок")+extra, entities=entities)
    else:
        await bot.send_message(chat_id=message.chat.id, text=(text or "Пост без текста"), entities=entities, reply_markup=post_kb)

    # Кнопки подтверждения предпросмотра
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="np_preview_save")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="np_preview_back")],
    ])
    await bot.send_message(chat_id=message.chat.id, text="Сохранить этот слот?", reply_markup=confirm_kb)

@dp.callback_query(lambda c: c.data=="np_preview_save", StateFilter(NewPost.preview))
async def np_preview_save(cq: types.CallbackQuery, state: FSMContext):
    await finalize_post(cq.message, state)
    await cq.answer()

@dp.callback_query(lambda c: c.data=="np_preview_back", StateFilter(NewPost.preview))
async def np_preview_back(cq: types.CallbackQuery, state: FSMContext):
    # вернёмся в меню кнопок
    await state.set_state(NewPost.ask_button)
    data = await state.get_data()
    buttons = data.get("buttons") or []
    rows = [[InlineKeyboardButton(text="➕ Добавить кнопку", callback_data="np_btn_add")]]
    if buttons:
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="np_btn_done")])
        rows.append([InlineKeyboardButton(text="🗑 Очистить", callback_data="np_btn_clear")])
    else:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="np_btn_done")])
    await cq.message.edit_text("Кнопки поста:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()

async def render_day_posts_menu(message: types.Message, ch_id: int, week: int, wd: int):
    async def safe_edit_message_text(msg: types.Message, text: str, kb: InlineKeyboardMarkup | None = None):
        try:
            await msg.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            # Текущее сообщение может быть медиа с подписью — отправим новое и попробуем удалить старое
            sent = await bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=kb)
            try:
                await msg.delete()
            except Exception:
                pass
    async with AsyncSessionLocal() as session:
        from .models import Post
        res = await session.execute(
            select(Post)
            .where(Post.channel_id == ch_id, Post.week_in_cycle == week, Post.weekday == wd)
            .order_by(Post.created_at.asc())
        )
        posts = res.scalars().all()

    weekdays = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    if posts:
        rows = []
        for idx, p in enumerate(posts, start=1):
            rows.append([InlineKeyboardButton(text=f"Пост {idx}", callback_data=f"np_view:{p.id}")])
        rows.append([InlineKeyboardButton(text="➕ Добавить пост", callback_data=f"np_add:{wd}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"np_week:{week}")])
        await safe_edit_message_text(message, f"{weekdays[wd]}: выбери пост или добавь новый", InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"np_week:{week}")]])
        await safe_edit_message_text(message, "В этом дне ещё нет постов. Нажми ‘➕ Добавить пост’ ниже или вернись назад.", kb)

# Специальный хэндлер: пересланное сообщение из канала — добавление канала
@dp.message()
async def add_channel_from_forward(message: types.Message, state: FSMContext):
    # обрабатываем только пересланные из каналов
    if not (message.forward_from_chat and getattr(message.forward_from_chat, "type", None) == "channel"):
        return
    # если сейчас вводим админа — не перехватываем
    cur = await state.get_state()
    if cur == ManageAdmins.wait_input:
        return
    ch = message.forward_from_chat
    async with AsyncSessionLocal() as session:
        await ensure_user(message.from_user.id, message.from_user.full_name)
        res = await session.execute(select(Channel).where(Channel.chat_id==ch.id))
        exists = res.scalar_one_or_none()
        if not exists:
            new = Channel(chat_id=ch.id, username=ch.username, title=ch.title or ch.username, owner_id=message.from_user.id)
            session.add(new)
            await session.commit()
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настроить цикл", callback_data=f"cycle_settings:{new.id}")]])
            await message.reply(f"Канал {ch.title} добавлен и ты назначен владельцем.", reply_markup=kb)
        else:
            await message.reply("Канал уже добавлен.")
    return

        
async def main():
    print("Starting bot...")
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

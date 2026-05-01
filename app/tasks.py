# app/tasks.py
from .celery_app import celery
from .models import Post, Channel
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import MessageEntity
from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument
import os
import asyncio
from sqlalchemy.future import select
from datetime import datetime
from sqlalchemy import update, and_
from zoneinfo import ZoneInfo
from celery.utils.log import get_task_logger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

logger = get_task_logger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

async def open_session():
    engine = create_async_engine(
        DATABASE_URL,
        future=True,
        echo=False,
        poolclass=NullPool,
    )
    SessionLocal = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = SessionLocal()
    return engine, session

@celery.task(bind=True, name="send_post")
def send_post(self, post_id: int):
    return asyncio.run(_send_post_async(post_id))

async def _send_post_async(post_id: int):
    engine, session = await open_session()
    bot = Bot(token=BOT_TOKEN)
    try:
        # Атомарно "захватим" пост, чтобы исключить повторную отправку
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        result = await session.execute(
            update(Post)
            .where(and_(Post.id == post_id, Post.next_run != None, Post.next_run <= now_utc))
            .values(last_status="sending")
            .returning(Post.id)
        )
        claimed_id = result.scalar_one_or_none()
        if not claimed_id:
            logger.info(f"send_post: skip {post_id}, not due or already claimed")
            await session.commit()
            return {"ok": False, "reason": "not_due_or_claimed"}
        # Загрузим актуальные данные поста/канала
        q = await session.execute(select(Post).where(Post.id == post_id))
        p = q.scalar_one_or_none()
        if not p:
            await session.commit()
            logger.warning(f"send_post: post {post_id} not found after claim")
            return {"ok": False, "reason": "post not found"}
        channel_q = await session.execute(select(Channel).where(Channel.id == p.channel_id))
        ch = channel_q.scalar_one_or_none()
        if not ch:
            await session.commit()
            logger.warning(f"send_post: channel for post {post_id} not found")
            return {"ok": False, "reason": "channel not found"}

        try:
            kb = None
            rows = []
            if p.buttons:
                try:
                    for b in p.buttons:
                        t = (b.get("text") or "").strip()
                        u = (b.get("url") or "").strip()
                        if t and u:
                            rows.append([InlineKeyboardButton(text=t, url=u)])
                except Exception:
                    rows = []
            # legacy: одиночная кнопка
            if p.button_text and p.button_url:
                rows.append([InlineKeyboardButton(text=p.button_text, url=p.button_url)])
            if rows:
                kb = InlineKeyboardMarkup(inline_keyboard=rows)
            entities = None
            if p.text_entities:
                try:
                    entities = [MessageEntity(**e) for e in p.text_entities]
                except Exception:
                    entities = None
            def detect_parse_mode(text: str | None) -> str | None:
                if not text:
                    return None
                if any(tag in text for tag in ("<b>", "<i>", "<u>", "<a ", "</")):
                    return "HTML"
                if any(ch in text for ch in ("*", "_", "~", "`", "[", "]", "(", ")", ">", "#")):
                    return "MarkdownV2"
                return None
            pm = None if entities else detect_parse_mode(p.text)

            # 1) copy_messages — альбом, скопированный из исходного чата (сохраняет premium emoji)
            if p.src_chat_id and p.src_message_ids:
                ids = list(p.src_message_ids)
                await bot.copy_messages(chat_id=ch.chat_id, from_chat_id=p.src_chat_id, message_ids=ids)
                if kb:
                    # отдельным сообщением кнопки + текст (если есть)
                    btn_text = p.text or "⬇️"
                    await bot.send_message(chat_id=ch.chat_id, text=btn_text, entities=entities, parse_mode=pm, reply_markup=kb)
            # 2) copy_message — одиночное сообщение из исходного чата
            elif p.src_chat_id and p.src_message_id:
                await bot.copy_message(
                    chat_id=ch.chat_id,
                    from_chat_id=p.src_chat_id,
                    message_id=p.src_message_id,
                    reply_markup=kb,
                )
            # 3) legacy fallback — отправка по сохранённому file_id
            elif p.media_group:
                media = []
                add_caption_to_first = not kb and (p.text or entities)
                for idx, it in enumerate(p.media_group):
                    t = it.get("type")
                    fid = it.get("file_id")
                    if idx == 0 and add_caption_to_first:
                        if entities:
                            if t == "photo":
                                media.append(InputMediaPhoto(media=fid, caption=p.text, caption_entities=entities))
                            elif t == "video":
                                media.append(InputMediaVideo(media=fid, caption=p.text, caption_entities=entities))
                            elif t == "document":
                                media.append(InputMediaDocument(media=fid, caption=p.text, caption_entities=entities))
                        else:
                            if t == "photo":
                                media.append(InputMediaPhoto(media=fid, caption=p.text, parse_mode=pm))
                            elif t == "video":
                                media.append(InputMediaVideo(media=fid, caption=p.text, parse_mode=pm))
                            elif t == "document":
                                media.append(InputMediaDocument(media=fid, caption=p.text, parse_mode=pm))
                    else:
                        if t == "photo":
                            media.append(InputMediaPhoto(media=fid))
                        elif t == "video":
                            media.append(InputMediaVideo(media=fid))
                        elif t == "document":
                            media.append(InputMediaDocument(media=fid))
                if media:
                    await bot.send_media_group(chat_id=ch.chat_id, media=media)
                    if kb and p.text:
                        await bot.send_message(chat_id=ch.chat_id, text=p.text, entities=entities, parse_mode=pm, reply_markup=kb)
            elif p.media_type == "photo":
                await bot.send_photo(chat_id=ch.chat_id, photo=p.media_file_id, caption=p.text, caption_entities=entities, parse_mode=pm, reply_markup=kb)
            elif p.media_type == "video":
                await bot.send_video(chat_id=ch.chat_id, video=p.media_file_id, caption=p.text, caption_entities=entities, parse_mode=pm, reply_markup=kb)
            elif p.media_type == "document":
                await bot.send_document(chat_id=ch.chat_id, document=p.media_file_id, caption=p.text, caption_entities=entities, parse_mode=pm, reply_markup=kb)
            elif p.media_type == "voice":
                await bot.send_voice(chat_id=ch.chat_id, voice=p.media_file_id, caption=p.text, caption_entities=entities, parse_mode=pm, reply_markup=kb)
            elif p.media_type == "video_note":
                await bot.send_video_note(chat_id=ch.chat_id, video_note=p.media_file_id)
            else:
                await bot.send_message(chat_id=ch.chat_id, text=p.text, entities=entities, parse_mode=pm, reply_markup=kb)

            # success: пост одноразовый — next_run сбрасываем
            await session.execute(
                update(Post).where(Post.id == p.id).values(last_status="ok", next_run=None)
            )
            await session.commit()
            logger.info(f"send_post: sent post {p.id} to chat {ch.chat_id} (one-shot)")
            return {"ok": True, "post_id": p.id}
        except Exception as e:
            # next_run обнуляем, чтобы не ретраить бесконечно (пост одноразовый)
            await session.execute(
                update(Post).where(Post.id == p.id).values(last_status=f"error:{str(e)}", next_run=None)
            )
            await session.commit()
            logger.exception(f"send_post: error sending post {p.id}: {e}")
            return {"ok": False, "reason": str(e)}
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
        try:
            await session.close()
        except Exception:
            pass
        try:
            await engine.dispose()
        except Exception:
            pass

@celery.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    # check due posts every 10 seconds
    sender.add_periodic_task(10.0, enqueue_due_posts.s(), name="enqueue due posts every 10s")

@celery.task(name="enqueue_due_posts")
def enqueue_due_posts():
    return asyncio.run(_enqueue_due_async())

async def _enqueue_due_async():
    engine, session = await open_session()
    try:
        now = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        q = await session.execute(select(Post).where(Post.next_run != None).where(Post.next_run <= now))
        rows = q.scalars().all()
        ids = [p.id for p in rows]
        if ids:
            logger.info(f"enqueue_due_posts: found due posts (<= {now}): {ids}")
        for pid in ids:
            send_post.delay(pid)
        return {"enqueued": ids}
    finally:
        try:
            await session.close()
        except Exception:
            pass
        try:
            await engine.dispose()
        except Exception:
            pass

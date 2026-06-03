# app/sender.py
# Единая логика доставки поста в чат — используется и предпросмотром в боте,
# и планировщиком в воркере, чтобы поведение не расходилось.
#
# Ключевое: premium-эмодзи (custom_emoji) сохраняются только при НАТИВНОЙ отправке
# (send_message/send_photo/... с entities) при условии, что у владельца бота есть
# Telegram Premium. copy_message их срезает. Поэтому новые посты шлём нативно с
# entities, а copy/forward оставлены как fallback для стикеров и старых постов.
from aiogram import Bot
from aiogram.types import (
    InlineKeyboardMarkup,
    MessageEntity,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
)

_BTN_PLACEHOLDER = "👇 Подробнее"

# media_type -> (метод бота, имя аргумента file_id)
_SEND_MAP = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "animation": ("send_animation", "animation"),
    "document": ("send_document", "document"),
    "voice": ("send_voice", "voice"),
    "audio": ("send_audio", "audio"),
}

_ALBUM_MEDIA_CLS = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}


def build_entities(text_entities):
    """Список dict -> список MessageEntity. Битые сущности пропускаем поштучно."""
    if not text_entities:
        return None
    out = []
    for e in text_entities:
        try:
            out.append(MessageEntity(**e))
        except Exception:
            pass
    return out or None


async def deliver_post(
    bot: Bot,
    chat_id: int,
    *,
    text: str | None = None,
    text_entities=None,
    media_type: str | None = None,
    media_file_id: str | None = None,
    media_group=None,
    src_chat_id: int | None = None,
    src_message_id: int | None = None,
    src_message_ids=None,
    kb: InlineKeyboardMarkup | None = None,
):
    entities = build_entities(text_entities)

    # 1) стикер — только forward сохраняет premium-эффект (copy его теряет)
    if media_type == "sticker" and src_chat_id and src_message_id:
        await bot.forward_message(chat_id=chat_id, from_chat_id=src_chat_id, message_id=src_message_id)
        if kb:
            await bot.send_message(chat_id=chat_id, text=text or _BTN_PLACEHOLDER, reply_markup=kb)
        return

    # 2) альбом — нативно, caption + entities на первом элементе.
    #    У альбома нельзя прикрепить inline-клавиатуру, поэтому кнопки шлём отдельно.
    if media_group:
        media = []
        for idx, it in enumerate(media_group):
            cls = _ALBUM_MEDIA_CLS.get(it.get("type"))
            if not cls:
                continue
            if idx == 0:
                media.append(cls(media=it.get("file_id"), caption=text, caption_entities=entities))
            else:
                media.append(cls(media=it.get("file_id")))
        if media:
            await bot.send_media_group(chat_id=chat_id, media=media)
        if kb:
            await bot.send_message(chat_id=chat_id, text=_BTN_PLACEHOLDER, reply_markup=kb)
        return

    # 3) одиночное медиа — нативно с caption_entities (premium-эмодзи в подписи сохраняются)
    if media_type in _SEND_MAP and media_file_id:
        method_name, arg = _SEND_MAP[media_type]
        method = getattr(bot, method_name)
        await method(
            chat_id=chat_id,
            caption=text,
            caption_entities=entities,
            reply_markup=kb,
            **{arg: media_file_id},
        )
        return

    # video_note не поддерживает подпись/кнопку — шлём отдельно
    if media_type == "video_note" and media_file_id:
        await bot.send_video_note(chat_id=chat_id, video_note=media_file_id)
        if kb:
            await bot.send_message(chat_id=chat_id, text=text or _BTN_PLACEHOLDER, reply_markup=kb)
        return

    # 4) legacy fallback: альбом, сохранённый как copy (старые посты)
    if src_chat_id and src_message_ids:
        await bot.copy_messages(chat_id=chat_id, from_chat_id=src_chat_id, message_ids=list(src_message_ids))
        if kb:
            await bot.send_message(chat_id=chat_id, text=text or _BTN_PLACEHOLDER, reply_markup=kb)
        return

    # 5) legacy fallback: одиночное сообщение, сохранённое как copy (старые посты)
    if src_chat_id and src_message_id:
        await bot.copy_message(chat_id=chat_id, from_chat_id=src_chat_id, message_id=src_message_id, reply_markup=kb)
        return

    # 6) просто текст
    await bot.send_message(chat_id=chat_id, text=text or " ", entities=entities, reply_markup=kb)

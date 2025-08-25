# app/models.py
from sqlalchemy import Column, Integer, String, Text, Boolean, ForeignKey, DateTime, BigInteger
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.postgresql import JSONB

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)  # internal id
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    name = Column(String(200))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Channel(Base):
    __tablename__ = "channels"
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255))
    title = Column(String(255))
    owner_id = Column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    cycle_weeks = Column(Integer, nullable=False, server_default="1")
    cycle_start = Column(DateTime(timezone=True), server_default=func.now())

class ChannelAdmin(Base):
    __tablename__ = "channel_admins"
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    telegram_id = Column(BigInteger, nullable=False, index=True)

class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text)
    media_type = Column(String(50))
    media_file_id = Column(String(400))
    button_text = Column(String(255))
    button_url = Column(String(1000))
    buttons = Column(JSONB, nullable=True)  # [{"text":"...","url":"..."}, ...]
    next_run = Column(DateTime(timezone=True), index=True)
    repeat_type = Column(String(50))  # legacy, не используем
    repeat_val = Column(Integer, nullable=True)  # legacy
    weekday = Column(Integer, nullable=True) # 0=Mon
    time_text = Column(String(5), nullable=True) # HH:MM
    week_in_cycle = Column(Integer, nullable=True) # 0..cycle_weeks-1
    parse_mode = Column(String(20), nullable=True)  # 'HTML', 'MarkdownV2', or None
    text_entities = Column(JSONB, nullable=True)  # Telegram entities for text/caption
    created_by = Column(BigInteger, nullable=False) # telegram_id of creator
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_status = Column(String(100), nullable=True)

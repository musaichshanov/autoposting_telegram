# app/db.py
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_async_engine(
    DATABASE_URL,
    future=True,
    echo=False,
    pool_size=20,
    max_overflow=40,
)
AsyncSessionLocal = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Создание схемы БД при старте
from app.models import Base  # импорт после создания engine, чтобы избежать циклов

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# dependency for FastAPI
async def get_session():
    async with AsyncSessionLocal() as session:
        yield session

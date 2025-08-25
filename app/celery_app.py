# app/celery_app.py
import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
celery = Celery("app", broker=REDIS_URL, backend=REDIS_URL)
celery.conf.task_serializer = "json"
celery.conf.result_serializer = "json"
celery.conf.accept_content = ["json"]
celery.conf.timezone = "UTC"
# ВАЖНО: Явно импортируем задачи, чтобы beat загрузил periodic tasks
celery.conf.imports = ("app.tasks",)

celery.conf.beat_schedule = {
    "enqueue-due-posts": {
        "task": "enqueue_due_posts",
        "schedule": 10.0,  # каждые 10 сек
    }
}
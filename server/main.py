"""
server/main.py

Сервер аналитики и выдачи последней версии (Этап 6, FastAPI).

Эндпоинты:
    GET  /             — health-check (Render любит пинговать корень);
    GET  /api/version  — последняя версия и ссылка на скачивание;
    POST /api/ping     — приём анонимного ping, в ответе сразу latest_version
                         (один сетевой вызов клиента вместо двух);
    GET  /api/stats    — агрегированная анонимная статистика.

Запуск локально:
    cd server
    pip install -r requirements.txt
    uvicorn main:app --reload

Запуск на Render:
    uvicorn main:app --host 0.0.0.0 --port $PORT

Последняя версия и ссылка на скачивание берутся из переменных окружения
LATEST_VERSION / DOWNLOAD_URL — это позволяет «выпустить» новую версию,
не передеплоивая код, а просто поменяв переменную окружения в Render.

Приватность: храним только анонимный install_id, версию и строку ОС.
Никаких персональных данных. Пользователь может отключить аналитику в клиенте.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

import storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("analytics")

LATEST_VERSION = os.environ.get("LATEST_VERSION", "0.1.0")
DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Стартовая инициализация хранилища (современная замена @app.on_event).
    storage.init()
    logger.info("Сервер аналитики запущен; latest_version=%s", LATEST_VERSION)
    yield
    # Здесь при необходимости можно закрыть ресурсы при остановке.


app = FastAPI(
    title="Excel Query Scheduler — Analytics",
    version=LATEST_VERSION,
    lifespan=lifespan,
)


class Ping(BaseModel):
    """Тело запроса POST /api/ping (всё анонимно)."""

    install_id: str
    version: str | None = None
    os: str | None = None


@app.get("/")
def health() -> dict:
    return {"ok": True, "service": "excel-query-scheduler-analytics"}


@app.get("/api/version")
def version() -> dict:
    return {"latest_version": LATEST_VERSION, "download_url": DOWNLOAD_URL}


@app.post("/api/ping")
def ping(p: Ping) -> dict:
    # Учёт ping не должен ронять ответ: ошибки хранилища только логируем.
    try:
        storage.record_ping(p.install_id, p.version, p.os)
    except Exception:
        logger.exception("Не удалось записать ping")
    # Возвращаем версию сразу — клиенту хватит одного запроса.
    return {"ok": True, "latest_version": LATEST_VERSION, "download_url": DOWNLOAD_URL}


@app.get("/api/stats")
def get_stats() -> dict:
    return storage.stats()
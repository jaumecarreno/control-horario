"""Application configuration."""

from __future__ import annotations

import os


class Config:
    ENV = os.getenv("ENV", "development")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://control_horario:control_horario@localhost:5432/control_horario",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "control_horario_session")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = ENV == "production"

    WTF_CSRF_TIME_LIMIT = None
    APP_URL = os.getenv("APP_URL", "http://localhost:5000")
    APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Madrid")

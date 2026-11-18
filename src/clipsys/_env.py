"""Global environment settings loaded from .env / environment variables."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings  # pydantic v2 companion lib

# Load .env relative to cwd so the CLI works from the project root.
load_dotenv(Path.cwd() / ".env", override=False)


class Settings(BaseSettings):
    db_path: Path = Field(default=Path("data/app.db"))
    log_level: str = "INFO"

    # ffmpeg — set these if ffmpeg/ffprobe are not on PATH
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:latest"

    # YouTube
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_data_api_key: str = ""

    # TikTok
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""

    # Facebook / Meta
    facebook_app_id: str = ""
    facebook_app_secret: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Resend
    resend_api_key: str = ""

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_verify_service_sid: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

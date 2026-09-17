"""Settings โหลดจาก .env"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# ให้ OPENAI_API_KEY / OPENAI_BASE_URL / DOCKER_HOST ใน .env เข้า os.environ ด้วย
# (langchain และ docker SDK อ่านจาก environment)
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    model: str = "openai:gpt-5-mini"

    # Sandbox
    sandbox_image: str = "poc-deepagent-sandbox:latest"
    sandbox_network: str = "none"
    sandbox_mem: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 256
    sandbox_exec_timeout: int = 60
    sandbox_max_output_bytes: int = 50_000
    sandbox_idle_ttl: int = 900
    sandbox_workdir: str = "/workspace"
    sandbox_user: str = "1000:1000"
    sandbox_init: bool = True  # ใช้ tini เป็น PID 1 (Docker Desktop มีให้)
    sandbox_label: str = "poc-deepagent-sandbox"


settings = Settings()


def ensure_docker_host() -> None:
    """Docker Desktop บาง config ไม่มี /var/run/docker.sock → ชี้ไป socket ใน home แทน."""
    if os.environ.get("DOCKER_HOST"):
        return
    if Path("/var/run/docker.sock").exists():
        return
    user_sock = Path.home() / ".docker" / "run" / "docker.sock"
    if user_sock.exists():
        os.environ["DOCKER_HOST"] = f"unix://{user_sock}"

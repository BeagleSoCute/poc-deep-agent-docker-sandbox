# Image ของ FastAPI + Deep Agent (ตัว "สมอง" — มี API key, ไม่ได้รันโค้ดของ agent)
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# ลง dependency ก่อน (cache layer ไว้ แก้โค้ดแล้วไม่ต้องลงใหม่)
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY app ./app

EXPOSE 8000
# exec form → uvicorn เป็นคนรับ SIGTERM ตอน compose down แล้วลบ sandbox ก่อนปิด
CMD ["uv", "run", "--no-dev", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

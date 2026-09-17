# Image ที่ agent จะใช้ "รันโค้ด" — ไม่มี API key, ไม่มีโค้ดของ agent อยู่ข้างใน
FROM python:3.12-slim

# ติดตั้งของที่ agent น่าจะต้องใช้ไว้ล่วงหน้า (เพราะ runtime ปิด network → pip install ไม่ได้)
RUN pip install --no-cache-dir numpy pandas \
 && useradd --create-home --uid 1000 --shell /bin/bash sandbox \
 && mkdir -p /workspace && chown sandbox:sandbox /workspace

# BaseSandbox ของ deepagents ใช้ python3 + coreutils (มีอยู่แล้วใน python:slim)
USER sandbox
WORKDIR /workspace

# container แค่ "มีชีวิตรอ" ให้ docker exec เข้ามา
CMD ["sleep", "infinity"]

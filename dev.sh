#!/usr/bin/env bash
# คำสั่งช่วยรัน POC
#   แบบ uv:      ./dev.sh install | build | run | test
#   แบบ compose: ./dev.sh up | down | logs
#   เดโม:        ./dev.sh demo | demo-chat | clean
set -euo pipefail
cd "$(dirname "$0")"
IMAGE=${SANDBOX_IMAGE:-poc-deepagent-sandbox:latest}

case "${1:-help}" in
  install)   uv sync ;;                                              # ติดตั้ง dependency
  build)     docker build -t "$IMAGE" sandbox ;;                     # build image ของ sandbox
  run)       uv run uvicorn app.main:app --reload --reload-dir app --port 8000 ;;  # http://localhost:8000/docs
  up)        docker compose up --build -d && echo "→ http://localhost:8000/docs   (ดู log: ./dev.sh logs)" ;;
  down)      docker compose down ;;                                  # api ลบ sandbox ทุกตัวก่อนปิด
  logs)      docker compose logs -f api ;;
  test)      uv run pytest -v ;;                                     # ทดสอบ sandbox กับ Docker จริง (ไม่ใช้ LLM)
  demo)      bash scripts/demo_isolation.sh ;;                       # เดโม isolation (ไม่ใช้ LLM)
  demo-chat) bash scripts/demo_chat.sh ;;                            # เดโมคุยกับ agent (ใช้ LLM)
  clean)     ids=$(docker ps -aq --filter label=app=poc-deepagent-sandbox); [ -n "$ids" ] && docker rm -f $ids || echo "nothing to clean" ;;
  *)         echo "usage: ./dev.sh <install|build|run|test|up|down|logs|demo|demo-chat|clean>" ;;
esac

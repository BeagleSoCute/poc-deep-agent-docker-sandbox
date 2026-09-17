#!/usr/bin/env bash
# เดโม isolation ของ sandbox โดยไม่ใช้ LLM (ยิง /exec ตรง ๆ)
# ใช้: ./scripts/demo_isolation.sh   (ต้องรัน `make run` ไว้อีก terminal)
set -uo pipefail
API=${API:-http://localhost:8000}

pretty() { python3 -c '
import sys, json
d = json.load(sys.stdin)
print("  exit=" + str(d.get("exit_code")))
print("  " + d.get("output", str(d)).strip().replace("\n", "\n  "))'; }

run() { # $1=thread $2=title $3=command [$4=timeout]
  echo; echo "▶ [$1] $2"; echo "  \$ $3"
  local body
  body=$(python3 -c 'import json,sys; d={"command":sys.argv[1]}; t=sys.argv[2]; d.update({"timeout":int(t)} if t else {}); print(json.dumps(d))' "$3" "${4:-}")
  curl -s -X POST "$API/sessions/$1/exec" -H 'content-type: application/json' -d "$body" | pretty
}

run demo-a "1. เป็นใคร อยู่ที่ไหน (ต้องไม่ใช่ root, อยู่ใน /workspace)" "id && pwd && hostname"
run demo-a "2. ออก internet ได้ไหม (ต้องไม่ได้)" "python3 -c \"import urllib.request as u; u.urlopen('https://example.com', timeout=3)\" 2>&1 | tail -1"
run demo-a "3. เห็นไฟล์บน Mac ไหม (ต้องไม่เห็น)" "ls /Users 2>&1; ls /"
run demo-a "4. มี API key ใน env ไหม (ต้องไม่มี)" "env | grep -i -E 'key|token|secret' || echo 'no secrets found'"
run demo-a "5. เขียนนอก /workspace ได้ไหม (ต้องไม่ได้)" "touch /etc/pwned"
run demo-a "6. คำสั่งค้างนาน → ถูกตัดด้วย timeout (3s)" "sleep 30" 3
run demo-a "7. ใช้ RAM เกิน limit → ถูก kill (exit 137)" "python3 -c 'b = bytearray(2 * 1024**3)'"
run demo-a "8. เขียนไฟล์ใน session A" "echo 'hello from A' > /workspace/a.txt && ls /workspace"
run demo-b "9. session B มองไม่เห็นไฟล์ของ A" "cat /workspace/a.txt; ls -la /workspace"

echo; echo "▶ 10. container ที่ถูกสร้าง (docker ps)"
docker ps --filter label=app=poc-deepagent-sandbox --format '  {{.Names}}\t{{.Status}}\t{{.Image}}'

echo; echo "▶ 11. limit ที่ตั้งไว้ของ session A"
curl -s "$API/sessions/demo-a" | python3 -m json.tool | sed 's/^/  /'

if [[ "${KEEP:-0}" != "1" ]]; then
  echo; echo "▶ 12. ลบ session → container หายไป"
  curl -s -X DELETE "$API/sessions/demo-a" >/dev/null
  curl -s -X DELETE "$API/sessions/demo-b" >/dev/null
  docker ps --filter label=app=poc-deepagent-sandbox --format '  {{.Names}}' ; echo "  (ว่าง = ลบแล้ว)"
fi

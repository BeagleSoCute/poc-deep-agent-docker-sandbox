#!/usr/bin/env bash
# เดโมคุยกับ agent (ใช้ LLM) — แสดงคำตอบ + tool ที่ agent เรียกใน sandbox
set -euo pipefail
API=${API:-http://localhost:8000}
THREAD=${THREAD:-chat-demo}

ask() {
  echo; echo "🧑 $1"
  body=$(python3 -c 'import json,sys; print(json.dumps({"thread_id":sys.argv[1],"message":sys.argv[2]}))' "$THREAD" "$1")
  curl -s -X POST "$API/chat" -H 'content-type: application/json' -d "$body" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if "steps" not in d: print(d); sys.exit(1)
for s in d["steps"]:
    arg = s["args"].get("command") or s["args"].get("file_path") or s["args"]
    print("  🔧 " + s["tool"] + ": " + str(arg)[:120])
print("🤖", d["answer"])'
}

# สร้างไฟล์ CSV ตัวอย่างแล้วอัปโหลดเข้า sandbox
printf 'month,sales\nJan,120\nFeb,98\nMar,143\nApr,170\n' > /tmp/sales.csv
curl -s -F file=@/tmp/sales.csv "$API/sessions/$THREAD/files"; echo

ask "เขียน python script ที่ /workspace/fib.py หา fibonacci 15 ตัวแรก แล้วรันให้ดู"
ask "อ่าน /workspace/sales.csv ด้วย pandas หาเดือนที่ยอดขายสูงสุดและค่าเฉลี่ย แล้วบันทึกสรุปลง /workspace/summary.txt"
ask "ลอง pip install requests ให้หน่อย"   # ควรล้มเหลว เพราะไม่มี network

echo; echo "📥 ดาวน์โหลด summary.txt:"
curl -s "$API/sessions/$THREAD/files?path=/workspace/summary.txt"; echo

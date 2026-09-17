# Deep Agent × Docker Sandbox (FastAPI POC)

POC แบบง่ายสำหรับอธิบายว่า **sandbox ของ Deep Agents ทำงานอย่างไร** โดยใช้ Docker บนเครื่องตัวเอง
และเปิดให้ใช้งานผ่าน FastAPI

---

## 1. Sandbox คืออะไร ทำไมต้องมี

Deep Agent มี tool `execute` ให้ LLM **รัน shell command ได้จริง** (เขียนโค้ด → รัน → ดูผล → แก้)
ถ้ารันบนเครื่องเราตรง ๆ LLM ที่หลงทาง (หรือโดน prompt injection) อาจลบไฟล์ อ่าน API key หรือยิง network ออกไปได้

**Sandbox = ห้องปิดที่ agent รันคำสั่งได้อย่างอิสระ แต่ออกมาทำอะไรข้างนอกไม่ได้**

## 2. สถาปัตยกรรม (pattern: *sandbox as tool*)

```
          HTTP                         docker exec / put_archive / get_archive
Client ────────► FastAPI + Deep Agent ────────────────────────────────► Container "sandbox-<thread_id>"
                 (บน Mac, มี API key)                                     (ไม่มี key, ไม่มี network,
                        │                                                  non-root, จำกัด RAM/CPU/PID)
                        └── LLM API
```

- **Agent อยู่นอก sandbox** → API key ไม่เคยเข้าไปใน container (LangChain แนะนำ pattern นี้: *"Never put secrets inside a sandbox"*)
- **Sandbox แค่รันคำสั่ง** → ลบทิ้งได้ทุกเมื่อ ไม่กระทบ agent
- **1 thread_id = 1 container** → ผู้ใช้แต่ละคนแยกไฟล์กันเด็ดขาด

## 3. Deep Agents ต้องการอะไรจาก sandbox — แค่ 4 อย่าง

`app/docker_sandbox.py` สืบทอด `BaseSandbox` แล้วเขียนแค่:

| เมธอด | ทำอะไร | ใช้ Docker API อะไร |
|---|---|---|
| `id` | ชื่อ sandbox | `container.name` |
| `execute(cmd, timeout)` | รันคำสั่ง คืน output + exit code | `exec_run(["timeout", N, "sh", "-c", cmd], user=1000)` |
| `upload_files([(path, bytes)])` | เอาไฟล์เข้า | `put_archive` (ส่งเป็น tar) |
| `download_files([path])` | เอาไฟล์ออก | `get_archive` |

tool อื่นที่ agent เห็น — `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep` —
**`BaseSandbox` สร้างให้ฟรี** โดยแปลงเป็นคำสั่ง shell/python3 แล้วส่งผ่าน `execute()`
(หรือ `upload_files` ตอนเขียนไฟล์) → image จึงต้องมี `python3` + coreutils

```
agent เรียก read_file("/workspace/a.py")
   └─ BaseSandbox.read()  → สร้างคำสั่ง python3 -c "..."
        └─ DockerSandbox.execute()  → docker exec
             └─ container รันแล้วส่ง output กลับ
```

## 4. ชั้นป้องกันที่ตั้งไว้ (`app/sessions.py::_run_container`)

| ค่า | ป้องกันอะไร |
|---|---|
| `network_mode="none"` | ออก internet ไม่ได้ (ขโมยข้อมูลส่งออกไม่ได้, pip install ไม่ได้) |
| `user="1000:1000"` | ไม่ใช่ root แก้ไฟล์ระบบไม่ได้ |
| `cap_drop=["ALL"]` + `no-new-privileges` | ตัดสิทธิ์พิเศษของ kernel, ยกระดับสิทธิ์ผ่าน setuid ไม่ได้ |
| `mem_limit` / `memswap_limit` | ใช้ RAM เกิน → ถูก kill (exit 137) |
| `nano_cpus` | จำกัด CPU |
| `pids_limit` | กัน fork bomb |
| `timeout` ทุกคำสั่ง | คำสั่งค้าง → ถูกตัด (exit 124) |
| ตัด output เกิน N bytes | กัน context ของ LLM ระเบิด |
| `environment={}` | ไม่มี secret ใน container |
| ไม่ mount โฟลเดอร์ของเครื่อง | มองไม่เห็นไฟล์บน Mac |
| เช็กสิทธิ์ก่อน `put_archive` | Docker เขียนไฟล์ในนาม root เสมอ จึงเช็กในนาม user 1000 ก่อน ไม่งั้น `write_file("/etc/x")` จะผ่าน |
| Idle TTL + ลบตอนปิดแอป + ลบ orphan ตาม label | container ไม่ค้างเต็มเครื่อง |

> ⚠️ Container ธรรมดาแชร์ kernel กับเครื่อง — พอสำหรับ POC/ภายใน
> ถ้าจะรันโค้ดจากผู้ใช้ภายนอกจริง ให้ใช้ gVisor (`runtime="runsc"`) หรือ sandbox แบบ managed (E2B, Daytona, Modal)
> ซึ่งเปลี่ยนแค่ class backend ส่วน FastAPI เหมือนเดิม

## 5. โครงสร้างโปรเจกต์

```
app/
  config.py          # ค่าจาก .env
  docker_sandbox.py  # ★ DockerSandbox(BaseSandbox)
  sessions.py        # ★ สร้าง/ลบ container ต่อ thread_id + TTL
  agent.py           # create_deep_agent(backend=DockerSandbox)
  main.py            # FastAPI endpoints
sandbox/Dockerfile   # image ที่ใช้รันโค้ด (python + numpy + pandas, user 1000)
scripts/
  demo_isolation.sh  # เดโมความปลอดภัย (ไม่ใช้ LLM)
  demo_chat.sh       # เดโมคุยกับ agent (ใช้ LLM)
tests/               # ทดสอบกับ Docker จริง
dev.sh               # คำสั่งช่วย: install / build / run / test / up / down / logs / demo / clean
Dockerfile           # image ของ api (ใช้กับ compose)
docker-compose.yml   # socket-proxy + api + sandbox-image
```

## 6. วิธีรัน (macOS)

ต้องมี: Docker Desktop (เปิดไว้), [uv](https://docs.astral.sh/uv/)

```bash
cp .env.example .env           # ใส่ MODEL / OPENAI_API_KEY (/ OPENAI_BASE_URL ถ้ามี)
./dev.sh install               # uv sync
./dev.sh build                 # docker build -t poc-deepagent-sandbox sandbox
./dev.sh test                  # ทดสอบ sandbox (ไม่ใช้ LLM)
./dev.sh run                   # http://localhost:8000/docs
```

อีก terminal:

```bash
./dev.sh demo        # เดโม isolation 12 ข้อ (ไม่ใช้ LLM, ไม่เสียเงิน)
./dev.sh demo-chat   # ให้ agent เขียนโค้ด/วิเคราะห์ CSV ใน sandbox
docker ps --filter label=app=poc-deepagent-sandbox   # ดู container ที่ถูกสร้าง
```

ดู log ของ terminal ที่รัน `./dev.sh run` จะเห็นทุกคำสั่งที่ agent ส่งเข้า sandbox (`sandbox: [sandbox-xxx] $ ...`)

## 6.1 รันทั้งหมดด้วย Docker Compose (ไม่ต้องลง Python บนเครื่อง)

```bash
cp .env.example .env
./dev.sh up        # = docker compose up --build -d
./dev.sh logs      # ดู log ของ api (เห็นคำสั่งที่ agent ส่งเข้า sandbox)
./dev.sh demo      # เดโมเหมือนเดิม
./dev.sh down      # api ลบ sandbox ทุกตัวก่อนปิด
```

```
docker compose up
├─ socket-proxy   ← mount /var/run/docker.sock ที่ตัวนี้ตัวเดียว, เปิดเฉพาะ API ที่ใช้
├─ api            ← FastAPI + agent, DOCKER_HOST=tcp://socket-proxy:2375
└─ sandbox-image  ← build image ของ sandbox แล้วจบ (api รอให้เสร็จก่อน start)

sandbox-<thread_id>  ← api สั่งสร้างผ่าน proxy → Docker ของเครื่องสร้างให้
                       (อยู่ระดับเดียวกับ api ไม่ได้ซ้อนอยู่ข้างใน)
```

| Service | หน้าที่ | Network |
|---|---|---|
| `socket-proxy` | ด่านกลางระหว่าง api กับ Docker อนุญาตแค่ `CONTAINERS`, `EXEC`, `IMAGES`, `POST` | `internal` (ออก internet ไม่ได้, ไม่เปิดพอร์ต) |
| `api` | FastAPI + agent + API key | `internal` (คุยกับ proxy) + `default` (ออกไปหา LLM, เปิดพอร์ต 8000) |
| `sandbox-image` | build image เท่านั้น | none |

**ทำไมไม่ mount `docker.sock` เข้า api ตรง ๆ** — ใครยึด api ได้ = คุม Docker ทั้งเครื่อง (เท่ากับ root)
proxy ช่วยปิด API ที่ไม่ใช้ (network, volume, build, swarm, secrets ฯลฯ)
แต่ **กรองพารามิเตอร์ตอนสร้าง container ไม่ได้** → เพียงพอสำหรับ POC; production ให้ใช้ managed sandbox หรือ rootless Docker

**ข้อควรรู้**
- ถ้าวันหลังจะ mount โฟลเดอร์เข้า sandbox ต้องใช้ **path บนเครื่อง host** ไม่ใช่ path ใน api container
- แก้โค้ดแล้วต้อง `./dev.sh up` ใหม่ (build ใหม่) — ตอนพัฒนาใช้ `./dev.sh run` จะเร็วกว่า
- อย่าตั้ง `DOCKER_HOST` ใน `.env` ตอนใช้ compose (compose จะอ่านไปใช้เองด้วย)

## 7. API

| Method | Path | ใช้ทำอะไร |
|---|---|---|
| POST | `/chat` `{thread_id, message}` | คุยกับ agent — คืน `answer` + `steps` (tool ที่เรียก) |
| POST | `/sessions/{id}/exec` `{command, timeout?}` | รันคำสั่งตรง ๆ ใน sandbox (ไม่ผ่าน LLM) |
| GET | `/sessions` | รายการ session |
| GET | `/sessions/{id}` | ข้อมูล container, limits, ไฟล์ใน /workspace |
| POST | `/sessions/{id}/files` (multipart `file`, `path?`) | อัปโหลดไฟล์ |
| GET | `/sessions/{id}/files?path=/workspace/x` | ดาวน์โหลดไฟล์ |
| DELETE | `/sessions/{id}` | ลบ session + container |
| GET | `/health` | เช็ก Docker |

ตัวอย่าง:

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"thread_id":"demo","message":"เขียน python หา prime 20 ตัวแรกแล้วรันให้ดู"}' | python3 -m json.tool
```

## 8. Config (`.env`)

| ตัวแปร | ค่าเริ่มต้น | หมายเหตุ |
|---|---|---|
| `MODEL` | `openai:gpt-5-mini` | รูปแบบ `provider:model` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | – | base url สำหรับ endpoint ที่ compatible กับ OpenAI |
| `SANDBOX_IMAGE` | `poc-deepagent-sandbox:latest` | |
| `SANDBOX_NETWORK` | `none` | `bridge` = เปิด internet |
| `SANDBOX_MEM` / `SANDBOX_CPUS` / `SANDBOX_PIDS_LIMIT` | `512m` / `1.0` / `256` | |
| `SANDBOX_EXEC_TIMEOUT` | `60` | วินาที/คำสั่ง |
| `SANDBOX_MAX_OUTPUT_BYTES` | `50000` | |
| `SANDBOX_IDLE_TTL` | `900` | วินาที |
| `DOCKER_HOST` | auto | ถ้าไม่มี `/var/run/docker.sock` จะใช้ `~/.docker/run/docker.sock` ให้เอง |

## 9. ข้อจำกัดของ POC / ขั้นต่อไป

- Checkpointer เป็น `InMemorySaver` → restart แล้วประวัติแชตหาย (เปลี่ยนเป็น Postgres ได้)
- ไฟล์ใน sandbox หายเมื่อ container ถูกลบ (ถ้าต้องเก็บ ใช้ Docker volume ต่อ session)
- `/chat` ยังไม่ stream (เพิ่ม SSE ด้วย `agent.astream(...)` ได้)
- ถ้าต้องการให้คนอนุมัติก่อนรันคำสั่ง: `create_deep_agent(..., interrupt_on={"execute": True})`

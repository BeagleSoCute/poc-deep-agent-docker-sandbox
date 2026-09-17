# คู่มือ: ใช้ Docker เป็น Sandbox ให้ Deep Agents (LangChain) + FastAPI

> สรุปจาก POC `poc_docker_deepagent` · deepagents 0.7.x · กันยายน 2026
> ใช้เป็นคู่มือทีม และเป็นโครงของ presentation (แต่ละหัวข้อ ≈ 1 สไลด์ — ดู "โครงสไลด์" ท้ายไฟล์)

---

## 1. TL;DR

- Deep Agents ให้ LLM **รัน shell command ได้จริง** ผ่าน tool `execute` → ต้องมี sandbox กันไม่ให้ agent ทำอะไรกับเครื่องจริง
- ยังไม่มี Docker provider สำเร็จรูป แต่ **เขียนเองได้ ~170 บรรทัด** — สืบทอด `BaseSandbox` แล้วเขียนแค่ **4 อย่าง**
- Pattern ที่แนะนำ: **Sandbox as tool** — agent + API key อยู่ข้างนอก, container มีไว้รันคำสั่งเท่านั้น
- 1 session (`thread_id`) = 1 container, ปิด network, non-root, จำกัด RAM/CPU/PID, timeout ทุกคำสั่ง, ลบอัตโนมัติ
- รันได้ 2 แบบ: `uv run` (dev) หรือ `docker compose up` (มี `socket-proxy` คั่น)
- ความยาก: **ง่าย–ปานกลาง** · POC ใช้เวลา ~0.5–1 วัน · compose เพิ่มอีก ~1–2 ชม.

---

## 2. ทำไมต้องมี Sandbox

| ถ้ารัน `execute` บนเครื่องตรง ๆ | ผลที่อาจเกิด |
|---|---|
| LLM เขียนคำสั่งผิด | `rm -rf` ไฟล์จริง |
| โดน prompt injection จากเอกสาร/เว็บ | อ่าน `.env` / API key แล้วส่งออก internet |
| โค้ดวนไม่รู้จบ / กิน RAM | เครื่องหรือ server ค้าง |
| หลายผู้ใช้พร้อมกัน | เห็นไฟล์ของกันและกัน |

**Sandbox = ห้องปิดที่ agent ทำอะไรก็ได้ข้างใน แต่ออกมาข้างนอกไม่ได้ และทิ้งได้ทุกเมื่อ**

---

## 3. เลือก Pattern

| | **Sandbox as tool** ✅ | Agent in sandbox |
|---|---|---|
| Agent รันที่ไหน | นอก container (FastAPI) | ใน container |
| API key | อยู่นอก sandbox | ต้องอยู่ใน sandbox ⚠️ |
| แก้โค้ด agent | ไม่ต้อง rebuild image | ต้อง rebuild |
| ข้อเสีย | latency เพิ่มเล็กน้อยต่อคำสั่ง | ความเสี่ยง key รั่ว |

LangChain docs แนะนำ *sandbox as tool* และย้ำกฎข้อเดียวที่ห้ามพลาด: **"Never put secrets inside a sandbox."**

---

## 4. สถาปัตยกรรม

```
            HTTP                           docker exec / put_archive / get_archive
Client ──────────► FastAPI + Deep Agent ─────────────────────────────► sandbox-<thread_id>
                   (มี API key)                                          • ไม่มี key
                         │                                               • network: none
                         └──► LLM API                                    • user 1000, cap_drop ALL
                                                                         • RAM/CPU/PID limit
```

ลำดับการทำงานเมื่อ agent เรียก tool:

```
agent: read_file("/workspace/a.py")
  └─ BaseSandbox.read()              ← deepagents สร้างคำสั่ง python3 ให้
       └─ DockerSandbox.execute()    ← โค้ดของเรา
            └─ docker exec → container รัน → ส่ง output กลับ
```

---

## 5. สัญญาที่ Deep Agents ต้องการ (`BaseSandbox`)

เขียนเองแค่ 4 อย่าง:

| สมาชิก | หน้าที่ | Docker API ที่ใช้ |
|---|---|---|
| `id` (property) | ชื่อ sandbox | `container.name` |
| `execute(command, *, timeout=None) -> ExecuteResponse` | รันคำสั่ง คืน `output`, `exit_code`, `truncated` | `exec_run` |
| `upload_files([(path, bytes)]) -> list[FileUploadResponse]` | เอาไฟล์เข้า | `put_archive` (tar) |
| `download_files([path]) -> list[FileDownloadResponse]` | เอาไฟล์ออก | `get_archive` (tar) |

**ได้ฟรีจาก `BaseSandbox`:** `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep` (+ async ทุกตัวผ่าน `asyncio.to_thread`)

ข้อกำหนดที่ต้องทำตาม:
- path ต้องเป็น **absolute** ไม่งั้นคืน `error="invalid_path"`
- upload/download ต้อง **สำเร็จบางส่วนได้** — จับ exception รายไฟล์ แล้วคืน error code (`file_not_found`, `permission_denied`, `is_directory`, `invalid_path`) แทนการ raise
- image ต้องมี **`python3` + coreutils** เพราะ helper ของ `BaseSandbox` ใช้

```python
from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse, FileDownloadResponse

class DockerSandbox(BaseSandbox):
    @property
    def id(self) -> str: ...
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse: ...
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]: ...
    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]: ...
```

---

## 6. หัวใจของ implementation (`app/docker_sandbox.py`)

### 6.1 `execute`

```python
exit_code, raw = container.exec_run(
    ["timeout", "-k", "2", str(secs), "sh", "-c", command],  # timeout ฝั่ง container
    demux=False,                 # stdout+stderr รวมกันตามลำดับ
    workdir="/workspace",
    user="1000:1000",            # รันในนาม user ธรรมดา
)
# ตัด output เกิน N bytes → truncated=True   (กัน context LLM ระเบิด)
# exit 124 → ต่อท้ายข้อความ "[command timed out]"
```

### 6.2 `upload_files` — จุดที่ต้องระวังที่สุด ⚠️

`put_archive` ของ Docker **เขียนไฟล์ในนาม root เสมอ** ถ้าไม่เช็ก agent จะ `write_file("/etc/xxx")` ผ่าน
→ ก่อนเขียน ให้ exec เช็กสิทธิ์ในนาม user sandbox:

```sh
mkdir -p <parent> && test -w <parent> && { [ ! -e <path> ] || { [ -f <path> ] && [ -w <path> ]; }; }
```

แล้วค่อยส่ง tar ที่ตั้ง `uid/gid = 1000` (agent จะได้แก้ไฟล์ที่อัปโหลดได้)

### 6.3 `download_files`

- เช็ก `test -r` ในนาม user sandbox ก่อน (เหตุผลเดียวกัน — `get_archive` อ่านในนาม root)
- ดู bit directory จาก `stat["mode"] & (1 << 31)` → คืน `is_directory`
- แตก tar เอาไฟล์แรก

---

## 7. Config ความปลอดภัยของ container (`app/sessions.py`)

```python
client.containers.run(
    image, name=f"sandbox-{thread_id}", detach=True,
    init=True,                                # tini เป็น PID 1
    labels={"app": "poc-deepagent-sandbox", "thread_id": thread_id},
    user="1000:1000", working_dir="/workspace",
    network_mode="none",
    mem_limit="512m", memswap_limit="512m",
    nano_cpus=int(1.0 * 1e9),
    pids_limit=256,
    cap_drop=["ALL"],
    security_opt=["no-new-privileges:true"],
    environment={},
)
```

| Config | ป้องกันอะไร | ผลในเดโม |
|---|---|---|
| `network_mode="none"` | ส่งข้อมูลออก / ดาวน์โหลดของแปลกปลอม | `urlopen` → name resolution error |
| `user="1000:1000"` | แก้ไฟล์ระบบ | `touch /etc/x` → Permission denied |
| `cap_drop=["ALL"]` + `no-new-privileges` | ยกระดับสิทธิ์ (setuid, raw socket ฯลฯ) | – |
| `mem_limit` + `memswap_limit` | กิน RAM จนเครื่องค้าง | จอง 2 GB → **exit 137** (killed) |
| `nano_cpus` | กิน CPU | – |
| `pids_limit` | fork bomb | – |
| `timeout` ทุกคำสั่ง | คำสั่งค้าง | `sleep 30` → **exit 124** |
| ตัด output | context LLM ระเบิด | `truncated=True` |
| `environment={}` + ไม่ส่ง `.env` | key รั่ว | `env` → ไม่มี secret |
| ไม่ bind-mount โฟลเดอร์ host | อ่านไฟล์บนเครื่อง | `ls /Users` → ไม่มี |
| 1 container ต่อ `thread_id` | ผู้ใช้เห็นไฟล์กัน | session B อ่านไฟล์ของ A ไม่ได้ |
| `init=True` | zombie process, stop ช้า | – |

Sandbox image (`sandbox/Dockerfile`):

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy pandas \          # ลงไว้ก่อน เพราะ runtime ไม่มี network
 && useradd --create-home --uid 1000 sandbox \
 && mkdir -p /workspace && chown sandbox:sandbox /workspace
USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]                              # อยู่รอให้ exec เข้ามา
```

---

## 8. วงจรชีวิตของ Sandbox

| เหตุการณ์ | สิ่งที่เกิด |
|---|---|
| request แรกของ `thread_id` | สร้าง container (lazy) |
| request ต่อ ๆ ไป | ใช้ container เดิม, อัปเดต `last_used` |
| ไม่ได้ใช้เกิน `SANDBOX_IDLE_TTL` | background task ลบทิ้ง (เช็กทุก 30 วินาที) |
| `DELETE /sessions/{id}` | ลบ container + ประวัติแชตของ thread |
| แอปปิด (lifespan shutdown) | ลบ sandbox ทุกตัว |
| แอป start | ลบ container ที่ค้างจากรอบก่อน (หาโดย **label**) |

> ถ้าไม่ทำส่วนนี้ container จะค้างเต็มเครื่อง — เป็นปัญหาที่เจอบ่อยที่สุด

---

## 9. ต่อเข้ากับ Deep Agent + FastAPI

```python
from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model

agent = create_deep_agent(
    model=init_chat_model("openai:gpt-5-mini", use_responses_api=False),
    system_prompt="... Work only inside /workspace. No internet access ...",
    backend=DockerSandbox(container),       # ← จุดเดียวที่ผูก sandbox
    checkpointer=shared_checkpointer,       # แยก thread ด้วย thread_id
)
```

- `backend=` รับ **instance เดียว** → compile agent แยกต่อ session (เบา) และใช้ checkpointer ร่วมกัน
- บอก agent ใน system prompt ว่าทำงานใต้ `/workspace` และไม่มี internet
- เรียก Docker SDK (sync) ด้วย `asyncio.to_thread` ใน endpoint async

Endpoints ของ POC:

| Method | Path | ใช้ทำอะไร |
|---|---|---|
| POST | `/chat` | คุยกับ agent → `answer` + `steps` (tool ที่เรียก) |
| POST | `/sessions/{id}/exec` | รันคำสั่งตรง ๆ ไม่ผ่าน LLM (ใช้เดโม/ทดสอบ) |
| GET | `/sessions`, `/sessions/{id}` | ดู session, limit, ไฟล์ใน `/workspace` |
| POST / GET | `/sessions/{id}/files` | อัปโหลด / ดาวน์โหลด (จำกัดใต้ `/workspace`) |
| DELETE | `/sessions/{id}` | ลบ session |
| GET | `/health` | เช็ก Docker |

---

## 10. ค่า Config (`.env`)

| ตัวแปร | ค่าเริ่มต้น | หมายเหตุ |
|---|---|---|
| `MODEL` | `openai:gpt-5-mini` | รูปแบบ `provider:model` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | – | base url สำหรับ LiteLLM / endpoint ที่ compatible |
| `SANDBOX_IMAGE` | `poc-deepagent-sandbox:latest` | |
| `SANDBOX_NETWORK` | `none` | `bridge` = เปิด internet (ไม่แนะนำ) |
| `SANDBOX_MEM` | `512m` | |
| `SANDBOX_CPUS` | `1.0` | |
| `SANDBOX_PIDS_LIMIT` | `256` | |
| `SANDBOX_EXEC_TIMEOUT` | `60` | วินาที/คำสั่ง |
| `SANDBOX_MAX_OUTPUT_BYTES` | `50000` | |
| `SANDBOX_IDLE_TTL` | `900` | วินาที |
| `SANDBOX_INIT` | `true` | ใช้ tini |
| `DOCKER_HOST` | auto | เฉพาะแบบ uv run; **ห้ามตั้ง** ตอนใช้ compose |

---

## 11. วิธีรัน 2 แบบ

| | แบบ A: `uv run` | แบบ B: Docker Compose |
|---|---|---|
| ต้องมีบนเครื่อง | Docker + Python + uv | Docker อย่างเดียว |
| คำสั่ง | `./dev.sh install && ./dev.sh build && ./dev.sh run` | `docker compose up --build` |
| FastAPI อยู่ที่ | process บนเครื่อง | container `api` |
| คุยกับ Docker ผ่าน | socket ของเครื่องโดยตรง | `socket-proxy` |
| เหมาะกับ | พัฒนา / debug | ส่งให้ทีม / server |

ทั้งสองแบบใช้โค้ด Python ชุดเดียวกัน ต่างกันแค่ `DOCKER_HOST`

---

## 12. Docker Compose + socket-proxy

```
docker compose up
├─ socket-proxy   ← ตัวเดียวที่ mount /var/run/docker.sock (read-only)
├─ api            ← DOCKER_HOST=tcp://socket-proxy:2375
└─ sandbox-image  ← build แล้วจบ (api รอให้เสร็จก่อน)

sandbox-<id>      ← Docker ของเครื่องสร้างให้ (อยู่ระดับเดียวกับ api ไม่ได้ซ้อนข้างใน)
```

```yaml
socket-proxy:
  image: tecnativa/docker-socket-proxy:latest
  environment:
    CONTAINERS: 1   # create/start/inspect/list/delete/archive
    EXEC: 1         # docker exec
    IMAGES: 1       # เช็กว่ามี image
    POST: 1         # อนุญาต method ที่ไม่ใช่ GET
  volumes: ["/var/run/docker.sock:/var/run/docker.sock:ro"]
  networks: [internal]            # ไม่เปิดพอร์ต

api:
  environment: { DOCKER_HOST: tcp://socket-proxy:2375 }
  networks: [internal, default]   # internal = คุย proxy, default = ออกไปหา LLM
  stop_grace_period: 30s          # ให้เวลาลบ sandbox ก่อนปิด
  depends_on:
    sandbox-image: { condition: service_completed_successfully }

networks:
  internal: { internal: true }
```

**ทำไมไม่ mount `docker.sock` เข้า api ตรง ๆ?** ใครยึด api ได้ = คุม Docker ทั้งเครื่อง (≈ root)
proxy ปิด API ที่ไม่ใช้ (network, volume, build, swarm, secrets …)

**ข้อจำกัด:** proxy กรองตาม *ประเภท API* ไม่ได้กรอง *พารามิเตอร์* ตอนสร้าง container → พอสำหรับ POC/ภายใน

Docker API ที่แอปเรียกจริง (ตรวจแล้วผ่านกฎ proxy ทั้งหมด):
`_ping`, `version`, `images/{name}/json`, `containers/create|json|{id}/json|{id}/start|{id}/exec|{id}/archive (GET/PUT)`, `DELETE containers/{id}`, `exec/{id}/start|json`

---

## 13. ขั้นตอนนำไปใช้ในโปรเจกต์ของทีม (Checklist)

1. ☐ เพิ่ม dependency: `deepagents`, `docker`, `langchain-openai` (หรือ provider ที่ใช้)
2. ☐ ทำ sandbox image ของทีม — ลง library ที่ agent ต้องใช้ให้ครบ, มี `python3`, user non-root, `WORKDIR /workspace`
3. ☐ คัดลอก `app/docker_sandbox.py` (ไม่ต้องแก้)
4. ☐ คัดลอก/ปรับ `app/sessions.py` — ตั้ง limit ตาม workload, ตั้ง label ของทีม
5. ☐ `create_deep_agent(backend=DockerSandbox(...), checkpointer=...)` + system prompt เรื่อง `/workspace` และ no-internet
6. ☐ ใส่ lifecycle: TTL reaper, ลบตอน shutdown, ลบ orphan ตอน start
7. ☐ endpoint อัปโหลด/ดาวน์โหลด — จำกัด path ใต้ `/workspace`
8. ☐ ไม่ส่ง env/secret เข้า container, ไม่ bind-mount โฟลเดอร์ host
9. ☐ รัน `tests/test_docker_sandbox.py` กับ image ของทีม (12 เคส ไม่ใช้ LLM)
10. ☐ ถ้า deploy ด้วย compose: ใช้ `socket-proxy`, network `internal`, `stop_grace_period`

---

## 14. การทดสอบและเดโม

| คำสั่ง | ทำอะไร | ใช้ LLM |
|---|---|---|
| `./dev.sh test` | pytest 12 เคส: execute, exit code, non-root, timeout, truncate, no network, no secrets, เขียนนอก workspace ไม่ได้, memory limit, แยก session, upload/download, error codes, helper ของ BaseSandbox | ไม่ |
| `./dev.sh demo` | เดโม isolation 12 ขั้น + `docker ps` + ดู limit + ลบ session | ไม่ |
| `./dev.sh demo-chat` | agent เขียน/รัน Python, วิเคราะห์ CSV ที่อัปโหลด, ลอง `pip install` (ต้องล้มเหลว), ดาวน์โหลดผล | ใช่ |

ผลเดโม isolation (ตัวอย่าง):

```
id            → uid=1000(sandbox)
urlopen(...)  → URLError: Temporary failure in name resolution
ls /Users     → No such file or directory
env | grep key→ no secrets found
touch /etc/x  → Permission denied           exit=1
sleep 30 (3s) → [command timed out]         exit=124
2GB bytearray → Killed                      exit=137
session B     → cat /workspace/a.txt: No such file
```

---

## 15. บทเรียน / Gotchas

1. **`put_archive`/`get_archive` ทำงานในนาม root** → ต้องเช็กสิทธิ์เองในนาม user sandbox (ไม่งั้น agent เขียน `/etc` ได้)
2. **ปิด network แล้ว `pip install` ไม่ได้** → ลง package ไว้ใน image
3. **`backend=` รับ instance เดียว** → agent ต่อ session, checkpointer ร่วม
4. **Docker SDK เป็น sync** → ครอบ `asyncio.to_thread` ใน FastAPI
5. **Container ค้าง** → ใช้ label + TTL + ลบตอน start/stop
6. **`docker cp` เข้า tmpfs ไม่ทำงาน** → อย่าใช้ `read_only` + tmpfs กับ `/workspace` ถ้าใช้ `put_archive`
7. **Docker Desktop บาง config ไม่มี `/var/run/docker.sock`** → fallback ไป `~/.docker/run/docker.sock`
8. **Endpoint ที่ compatible กับ OpenAI (LiteLLM ฯลฯ)** → ใช้ `use_responses_api=False`
9. **Compose:** path ที่จะ mount เข้า sandbox ต้องเป็น path ของ host ไม่ใช่ของ api container
10. **ใส่ `.env` ก่อน `docker compose up`** และแก้โค้ดแล้วต้อง `--build`

---

## 16. ข้อจำกัดของ POC → ทางไป Production

| ตอนนี้ | ถ้าจะใช้จริง |
|---|---|
| Container ธรรมดา (แชร์ kernel) | gVisor (`runtime="runsc"`), Kata/Firecracker, หรือ managed sandbox (E2B, Daytona, Modal, Runloop, LangSmith) — เปลี่ยนแค่ class backend |
| socket-proxy กรองแค่ประเภท API | service สร้าง sandbox แยก, rootless Docker, หรือ Kubernetes Job/Pod |
| `InMemorySaver` | Postgres checkpointer |
| ไฟล์หายเมื่อลบ container | Docker volume / object storage ต่อ session |
| `/chat` ไม่ stream | SSE ด้วย `agent.astream(...)` |
| agent รันคำสั่งได้เลย | human-in-the-loop: `interrupt_on={"execute": True}` |
| ไม่มี auth / quota | auth, rate limit, จำกัดจำนวน sandbox ต่อผู้ใช้ |
| log ลง stdout | ส่ง audit log ของทุกคำสั่งเข้าระบบกลาง |

---

## 17. ประเมินความยาก

| งาน | ความยาก | เวลา |
|---|---|---|
| `DockerSandbox` (4 เมธอด) | ง่าย–กลาง (ระวังเรื่องสิทธิ์ไฟล์) | 2–3 ชม. |
| Session / lifecycle | ง่าย | 1–2 ชม. |
| FastAPI + agent | ง่าย | 1–2 ชม. |
| Tests + demo scripts | ง่าย | 1–2 ชม. |
| Docker Compose + socket-proxy | ง่าย | 1–2 ชม. |
| **รวม POC** | **ง่าย–ปานกลาง** | **~0.5–1.5 วัน** |

---

## 18. โครงสร้างโปรเจกต์ POC

```
poc_docker_deepagent/
├─ app/
│  ├─ config.py          # ค่าจาก .env
│  ├─ docker_sandbox.py  # ★ DockerSandbox(BaseSandbox)
│  ├─ sessions.py        # ★ container ต่อ thread_id + security + TTL
│  ├─ agent.py           # create_deep_agent(backend=...)
│  └─ main.py            # FastAPI endpoints
├─ sandbox/Dockerfile    # image สำหรับรันโค้ด
├─ Dockerfile            # image ของ api (compose)
├─ docker-compose.yml    # socket-proxy + api + sandbox-image
├─ scripts/              # demo_isolation.sh, demo_chat.sh
├─ tests/                # test_docker_sandbox.py
├─ dev.sh                # install/build/run/test/up/down/logs/demo/clean
└─ .env.example
```

---

## โครงสไลด์ที่แนะนำ (~14 สไลด์)

| # | หัวข้อสไลด์ | ข้อความหลัก | อ้างอิงหัวข้อ |
|---|---|---|---|
| 1 | Docker Sandbox for Deep Agents | POC: ให้ agent รันโค้ดได้อย่างปลอดภัย | – |
| 2 | ปัญหา | `execute` บนเครื่องจริง = เสี่ยง (ลบไฟล์, key รั่ว, ค้าง) | 2 |
| 3 | เลือก Pattern | Sandbox as tool — key ไม่เข้า sandbox | 3 |
| 4 | Architecture | FastAPI + agent → docker exec → container ต่อ session | 4 |
| 5 | เขียนแค่ 4 อย่าง | `id`, `execute`, `upload_files`, `download_files` — ที่เหลือได้ฟรี | 5 |
| 6 | Implementation สำคัญ | timeout, truncate, เช็กสิทธิ์ก่อน put_archive | 6 |
| 7 | ชั้นป้องกัน | ตาราง config → ป้องกันอะไร | 7 |
| 8 | Lifecycle | lazy create, TTL, cleanup, orphan | 8 |
| 9 | Config | `.env` ที่ต้องตั้ง | 10 |
| 10 | รัน 2 แบบ | uv run vs compose | 11 |
| 11 | Compose + socket-proxy | ทำไมไม่ mount docker.sock ตรง ๆ | 12 |
| 12 | เดโม | ผล isolation (exit 124/137, no network, no secret) | 14 |
| 13 | Checklist สำหรับทีม | 10 ขั้นนำไปใช้ | 13 |
| 14 | Gotchas + Next steps | บทเรียน + ทางไป production + ความยาก | 15–17 |

---

**อ้างอิง**
- LangChain Deep Agents — Sandboxes: https://docs.langchain.com/oss/python/deepagents/sandboxes
- `BaseSandbox` reference: https://reference.langchain.com/python/deepagents/backends/sandbox/BaseSandbox
- deepagents source: https://github.com/langchain-ai/deepagents
- docker-socket-proxy: https://github.com/Tecnativa/docker-socket-proxy

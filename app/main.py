"""FastAPI app — ทุก endpoint บางมาก งานจริงอยู่ใน sessions.py / docker_sandbox.py."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import posixpath
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config import ensure_docker_host, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
log = logging.getLogger("api")

ensure_docker_host()
from app.sessions import Session, SessionManager  # noqa: E402  (ต้องตั้ง DOCKER_HOST ก่อน)

manager: SessionManager | None = None


async def _reaper() -> None:
    while True:
        await asyncio.sleep(30)
        await asyncio.to_thread(manager.reap_idle)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = SessionManager()
    manager.client.ping()
    manager.client.images.get(settings.sandbox_image)  # ยังไม่ build → error ชัด ๆ ตั้งแต่ start
    removed = manager.remove_orphans()
    log.info("docker ok, removed %d orphan sandbox(es)", removed)
    task = asyncio.create_task(_reaper())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.to_thread(manager.shutdown)


app = FastAPI(title="Deep Agent × Docker Sandbox POC", lifespan=lifespan)


# ---------------------------------------------------------------- helpers
async def _session(thread_id: str) -> Session:
    try:
        return await asyncio.to_thread(manager.get_or_create, thread_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _existing(thread_id: str) -> Session:
    s = manager.get(thread_id)
    if s is None:
        raise HTTPException(404, f"no session '{thread_id}'")
    s.touch()
    return s


def _workspace_path(path: str) -> str:
    p = posixpath.normpath(path)
    root = settings.sandbox_workdir
    if not (p == root or p.startswith(root + "/")):
        raise HTTPException(400, f"path must be under {root}")
    return p


def _text(msg) -> str:
    t = getattr(msg, "text", "")
    return t() if callable(t) else (t or "")


def _clip(s: str, n: int = 2000) -> str:
    return s if len(s) <= n else s[:n] + " …"


# ---------------------------------------------------------------- models
class ChatIn(BaseModel):
    thread_id: str = Field(examples=["demo"])
    message: str = Field(examples=["เขียน python หา fibonacci 20 ตัวแรก บันทึกเป็นไฟล์แล้วรันให้ดู"])


class ExecIn(BaseModel):
    command: str = Field(examples=["whoami && pwd && ls -la"])
    timeout: int | None = Field(default=None, ge=1, le=600)


# ---------------------------------------------------------------- routes
@app.get("/health")
async def health():
    await asyncio.to_thread(manager.client.ping)
    return {"docker": "ok", "image": settings.sandbox_image, "sessions": len(manager.list())}


@app.post("/chat")
async def chat(body: ChatIn):
    """คุยกับ agent — ตอบกลับพร้อม `steps` ว่า agent เรียก tool อะไรบ้างใน sandbox."""
    s = await _session(body.thread_id)
    agent = manager.get_agent(s)
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": body.message}]},
            config={"configurable": {"thread_id": body.thread_id}, "recursion_limit": 100},
        )
    except Exception as e:  # noqa: BLE001
        log.exception("agent failed")
        raise HTTPException(502, f"agent error: {e}") from e
    s.touch()

    messages = result["messages"]
    last_human = max(i for i, m in enumerate(messages) if m.type == "human")
    turn = messages[last_human + 1 :]

    outputs = {m.tool_call_id: _text(m) for m in turn if m.type == "tool"}
    steps = [
        {"tool": tc["name"], "args": tc["args"], "result": _clip(outputs.get(tc["id"], ""))}
        for m in turn if m.type == "ai"
        for tc in (m.tool_calls or [])
    ]
    return {
        "thread_id": body.thread_id,
        "sandbox": s.sandbox.id,
        "answer": _text(turn[-1]) if turn else "",
        "steps": steps,
    }


@app.post("/sessions/{thread_id}/exec")
async def exec_command(thread_id: str, body: ExecIn):
    """รันคำสั่งตรง ๆ ผ่าน DockerSandbox.execute() (ไม่ผ่าน LLM) — ใช้เดโม isolation."""
    s = await _session(thread_id)
    r = await asyncio.to_thread(s.sandbox.execute, body.command, timeout=body.timeout)
    return {"sandbox": s.sandbox.id, "exit_code": r.exit_code, "truncated": r.truncated, "output": r.output}


@app.get("/sessions")
async def list_sessions():
    return [
        {"thread_id": s.thread_id, "sandbox": s.sandbox.id, "created_at": s.created_at, "last_used": s.last_used}
        for s in manager.list()
    ]


@app.get("/sessions/{thread_id}")
async def session_info(thread_id: str):
    """ข้อมูล container + limit ที่ตั้งไว้ + ไฟล์ใน /workspace."""
    s = _existing(thread_id)
    c = s.sandbox.container
    await asyncio.to_thread(c.reload)
    hc = c.attrs["HostConfig"]
    ls = await asyncio.to_thread(
        s.sandbox.execute, f"find {settings.sandbox_workdir} -maxdepth 3 -not -name '.*' | sort | head -200"
    )
    return {
        "thread_id": thread_id,
        "container": {"name": c.name, "id": c.short_id, "image": c.attrs["Config"]["Image"], "status": c.status},
        "limits": {
            "user": c.attrs["Config"].get("User"),
            "network_mode": hc.get("NetworkMode"),
            "memory_bytes": hc.get("Memory"),
            "nano_cpus": hc.get("NanoCpus"),
            "pids_limit": hc.get("PidsLimit"),
            "cap_drop": hc.get("CapDrop"),
            "security_opt": hc.get("SecurityOpt"),
        },
        "idle_ttl_seconds": settings.sandbox_idle_ttl,
        "workspace": ls.output.splitlines(),
    }


@app.post("/sessions/{thread_id}/files")
async def upload_file(thread_id: str, file: UploadFile = File(...), path: str | None = Form(None)):
    """อัปโหลดไฟล์เข้า sandbox (ค่าเริ่มต้น /workspace/<ชื่อไฟล์>)."""
    s = await _session(thread_id)
    dest = _workspace_path(path or f"{settings.sandbox_workdir}/{file.filename}")
    data = await file.read()
    [r] = await asyncio.to_thread(s.sandbox.upload_files, [(dest, data)])
    if r.error:
        raise HTTPException(400, f"upload failed: {r.error}")
    return {"path": dest, "bytes": len(data)}


@app.get("/sessions/{thread_id}/files")
async def download_file(thread_id: str, path: str = Query(..., examples=["/workspace/result.txt"])):
    s = _existing(thread_id)
    p = _workspace_path(path)
    [r] = await asyncio.to_thread(s.sandbox.download_files, [p])
    if r.error:
        raise HTTPException(404 if r.error == "file_not_found" else 400, r.error)
    filename = posixpath.basename(p)
    return Response(
        r.content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={json.dumps(filename)}"},
    )


@app.delete("/sessions/{thread_id}")
async def delete_session(thread_id: str):
    if not await asyncio.to_thread(manager.destroy, thread_id):
        raise HTTPException(404, f"no session '{thread_id}'")
    return {"deleted": thread_id}

"""จัดการวงจรชีวิตของ sandbox: หนึ่ง thread_id = หนึ่ง container."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import docker
from docker.errors import NotFound
from langgraph.checkpoint.memory import InMemorySaver

from app.agent import build_agent
from app.config import settings
from app.docker_sandbox import DockerSandbox

log = logging.getLogger("sessions")

THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class Session:
    thread_id: str
    sandbox: DockerSandbox
    agent: Any = None
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()


class SessionManager:
    def __init__(self) -> None:
        self.client = docker.from_env()
        self.checkpointer = InMemorySaver()
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ create
    def _run_container(self, thread_id: str):
        """สร้าง container พร้อมค่าความปลอดภัยทั้งหมด — ส่วนที่ควรอธิบายในเดโม."""
        return self.client.containers.run(
            settings.sandbox_image,
            name=f"sandbox-{thread_id}",
            detach=True,
            init=settings.sandbox_init,              # tini เป็น PID 1 เก็บ zombie + stop เร็ว
            labels={"app": settings.sandbox_label, "thread_id": thread_id},
            user=settings.sandbox_user,              # ไม่ใช่ root
            working_dir=settings.sandbox_workdir,
            network_mode=settings.sandbox_network,   # none = ไม่มี internet
            mem_limit=settings.sandbox_mem,          # จำกัด RAM
            memswap_limit=settings.sandbox_mem,      # ห้ามใช้ swap เพิ่ม
            nano_cpus=int(settings.sandbox_cpus * 1e9),
            pids_limit=settings.sandbox_pids_limit,  # กัน fork bomb
            cap_drop=["ALL"],                        # ตัด Linux capabilities ทั้งหมด
            security_opt=["no-new-privileges:true"], # ห้ามยกระดับสิทธิ์ (setuid)
            environment={},                          # ไม่ส่ง secret ใด ๆ เข้าไป
        )

    def get_or_create(self, thread_id: str) -> Session:
        if not THREAD_ID_RE.match(thread_id):
            raise ValueError("thread_id must match [A-Za-z0-9_-]{1,64}")
        with self._lock:
            s = self._sessions.get(thread_id)
            if s is None:
                try:  # เผื่อมี container ชื่อซ้ำค้างอยู่
                    self.client.containers.get(f"sandbox-{thread_id}").remove(force=True)
                except NotFound:
                    pass
                container = self._run_container(thread_id)
                log.info("created %s (%s)", container.name, container.short_id)
                sandbox = DockerSandbox(
                    container,
                    workdir=settings.sandbox_workdir,
                    user=settings.sandbox_user,
                    default_timeout=settings.sandbox_exec_timeout,
                    max_output_bytes=settings.sandbox_max_output_bytes,
                )
                s = Session(thread_id=thread_id, sandbox=sandbox)
                self._sessions[thread_id] = s
            s.touch()
            return s

    def get_agent(self, session: Session):
        # สร้าง agent ตอนเรียก /chat ครั้งแรก เพื่อให้ /exec ใช้ได้โดยไม่ต้องมี API key
        if session.agent is None:
            session.agent = build_agent(session.sandbox, self.checkpointer)
        return session.agent

    def get(self, thread_id: str) -> Session | None:
        return self._sessions.get(thread_id)

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    # ----------------------------------------------------------- destroy
    def destroy(self, thread_id: str) -> bool:
        with self._lock:
            s = self._sessions.pop(thread_id, None)
        if s is None:
            return False
        try:
            s.sandbox.container.remove(force=True)
            log.info("removed %s", s.sandbox.id)
        except NotFound:
            pass
        self.checkpointer.delete_thread(thread_id)
        return True

    def reap_idle(self) -> list[str]:
        now = time.time()
        idle = [t for t, s in list(self._sessions.items()) if now - s.last_used > settings.sandbox_idle_ttl]
        for t in idle:
            log.info("idle TTL reached → removing sandbox-%s", t)
            self.destroy(t)
        return idle

    def remove_orphans(self) -> int:
        """ลบ container ที่ค้างจากการรันครั้งก่อน (หาโดย label)."""
        n = 0
        for c in self.client.containers.list(all=True, filters={"label": f"app={settings.sandbox_label}"}):
            if c.labels.get("thread_id") not in self._sessions:
                c.remove(force=True)
                n += 1
        return n

    def shutdown(self) -> None:
        for t in list(self._sessions):
            self.destroy(t)

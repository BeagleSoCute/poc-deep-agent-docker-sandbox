"""DockerSandbox — หัวใจของ POC.

deepagents กำหนดให้ sandbox backend ต้องมีแค่ 4 อย่าง:

    id               -> ชื่อ/รหัสของ sandbox
    execute()        -> รัน shell command แล้วคืน output + exit code
    upload_files()   -> เอาไฟล์เข้า sandbox
    download_files() -> เอาไฟล์ออกจาก sandbox

ที่เหลือ (ls, read_file, write_file, edit_file, glob, grep) `BaseSandbox`
สร้างให้เองโดยประกอบเป็น shell/python command แล้วส่งผ่าน execute()
หรือ upload/download_files อีกที  → เราจึงเขียนแค่ "ท่อ" ไปหา Docker
"""

from __future__ import annotations

import io
import logging
import posixpath
import shlex
import tarfile

import docker
from docker.models.containers import Container
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

log = logging.getLogger("sandbox")

# Go's os.ModeDir bit ที่ Docker ส่งมาใน stat ของ get_archive
_GO_MODE_DIR = 1 << 31
_TIMEOUT_EXIT = 124


class DockerSandbox(BaseSandbox):
    """Sandbox backend ที่รันคำสั่งผ่าน `docker exec` ใน container ที่สร้างไว้แล้ว."""

    def __init__(
        self,
        container: Container,
        *,
        workdir: str = "/workspace",
        user: str = "1000:1000",
        default_timeout: int = 60,
        max_output_bytes: int = 50_000,
    ) -> None:
        self._container = container
        self._workdir = workdir
        self._user = user
        uid, _, gid = user.partition(":")
        self._uid, self._gid = int(uid), int(gid or uid)
        self._default_timeout = default_timeout
        self._max_output_bytes = max_output_bytes

    # ------------------------------------------------------------------ id
    @property
    def id(self) -> str:
        return self._container.name

    @property
    def container(self) -> Container:
        return self._container

    # ------------------------------------------------------------- execute
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """รัน `sh -c <command>` ใน container.

        - ครอบด้วย coreutils `timeout` → คำสั่งค้างจะถูก kill ฝั่ง container เอง
        - stdout+stderr รวมกันตามลำดับ (docker-py demux=False)
        - output ยาวเกินจะถูกตัด เพื่อไม่ให้ context ของ LLM ระเบิด
        """
        secs = timeout or self._default_timeout
        log.info("[%s] $ %s", self.id, command if len(command) < 300 else command[:300] + " …")

        exit_code, raw = self._container.exec_run(
            ["timeout", "-k", "2", str(secs), "sh", "-c", command],
            stdout=True,
            stderr=True,
            demux=False,
            workdir=self._workdir,
            user=self._user,
            environment={"HOME": "/home/sandbox"},
        )
        data = raw if isinstance(raw, bytes) else b"".join(raw)

        truncated = len(data) > self._max_output_bytes
        if truncated:
            data = data[: self._max_output_bytes]
        output = data.decode("utf-8", errors="replace")
        if truncated:
            output += f"\n... [output truncated at {self._max_output_bytes} bytes]"
        if exit_code == _TIMEOUT_EXIT:
            output += f"\n[command timed out after {secs}s]"

        log.info("[%s] exit=%s bytes=%s", self.id, exit_code, len(data))
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=truncated)

    # -------------------------------------------------------------- upload
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [self._upload_one(path, content) for path, content in files]

    def _upload_one(self, path: str, content: bytes) -> FileUploadResponse:
        if not path.startswith("/") or path.endswith("/"):
            return FileUploadResponse(path=path, error="invalid_path")
        path = posixpath.normpath(path)
        parent, name = posixpath.split(path)

        # put_archive ของ Docker เขียนในนามของ root เสมอ
        # จึงเช็กสิทธิ์ในนามของ user sandbox ก่อน ไม่งั้น agent จะเขียน /etc ได้
        q_parent, q_path = shlex.quote(parent), shlex.quote(path)
        check = self._container.exec_run(
            ["sh", "-c",
             f"mkdir -p {q_parent} && test -w {q_parent} && "
             f"{{ [ ! -e {q_path} ] || {{ [ -f {q_path} ] && [ -w {q_path} ]; }}; }}"],
            user=self._user,
        )
        if check.exit_code != 0:
            code = "is_directory" if self._is_dir(path) else "permission_denied"
            return FileUploadResponse(path=path, error=code)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o644
            info.uid, info.gid = self._uid, self._gid
            tar.addfile(info, io.BytesIO(content))
        try:
            self._container.put_archive(parent, buf.getvalue())
        except docker.errors.NotFound:
            return FileUploadResponse(path=path, error="file_not_found")
        except docker.errors.APIError as e:
            return FileUploadResponse(path=path, error=f"docker error: {e.explanation or e}")
        return FileUploadResponse(path=path, error=None)

    def _is_dir(self, path: str) -> bool:
        return self._container.exec_run(["test", "-d", path], user=self._user).exit_code == 0

    # ------------------------------------------------------------ download
    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [self._download_one(p) for p in paths]

    def _download_one(self, path: str) -> FileDownloadResponse:
        if not path.startswith("/"):
            return FileDownloadResponse(path=path, error="invalid_path")
        # อ่านได้เฉพาะไฟล์ที่ user sandbox อ่านได้ (get_archive เองอ่านในนาม root)
        if self._container.exec_run(["test", "-r", path], user=self._user).exit_code != 0:
            exists = self._container.exec_run(["test", "-e", path]).exit_code == 0
            return FileDownloadResponse(path=path, error="permission_denied" if exists else "file_not_found")
        try:
            stream, stat = self._container.get_archive(path)
        except docker.errors.NotFound:
            return FileDownloadResponse(path=path, error="file_not_found")
        except docker.errors.APIError as e:
            return FileDownloadResponse(path=path, error=f"docker error: {e.explanation or e}")

        if stat.get("mode", 0) & _GO_MODE_DIR:
            return FileDownloadResponse(path=path, error="is_directory")

        with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as tar:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None:
                return FileDownloadResponse(path=path, error="file_not_found")
            f = tar.extractfile(member)
            content = f.read() if f else b""
        return FileDownloadResponse(path=path, content=content, error=None)

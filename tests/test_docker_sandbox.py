"""ทดสอบ DockerSandbox กับ Docker จริง (ไม่ใช้ LLM).

รัน:  ./dev.sh build && ./dev.sh test
"""

from __future__ import annotations

import uuid

import pytest

docker = pytest.importorskip("docker")

from app.config import ensure_docker_host, settings  # noqa: E402

ensure_docker_host()


@pytest.fixture(scope="module")
def manager():
    try:
        from app.sessions import SessionManager

        m = SessionManager()
        m.client.ping()
        m.client.images.get(settings.sandbox_image)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"docker/sandbox image not available: {e}")
    yield m
    m.shutdown()


@pytest.fixture()
def sb(manager):
    tid = f"test-{uuid.uuid4().hex[:8]}"
    s = manager.get_or_create(tid)
    yield s.sandbox
    manager.destroy(tid)


# ---------------------------------------------------------------- execute
def test_execute_basic(sb):
    r = sb.execute("echo hello && echo oops >&2 && exit 3")
    assert "hello" in r.output and "oops" in r.output
    assert r.exit_code == 3


def test_runs_as_non_root_in_workspace(sb):
    r = sb.execute("id -u && pwd")
    assert r.output.split() == ["1000", "/workspace"]


def test_timeout(sb):
    r = sb.execute("sleep 30", timeout=2)
    assert r.exit_code == 124
    assert "timed out" in r.output


def test_output_truncated(sb):
    r = sb.execute(f"head -c {settings.sandbox_max_output_bytes * 2} /dev/zero | tr '\\0' 'a'")
    assert r.truncated


# -------------------------------------------------------------- isolation
def test_no_network(sb):
    r = sb.execute("python3 -c \"import urllib.request; urllib.request.urlopen('http://example.com', timeout=3)\"")
    assert r.exit_code != 0


def test_no_secrets_in_env(sb):
    r = sb.execute("env")
    assert "API_KEY" not in r.output


def test_cannot_write_outside_workspace(sb):
    assert sb.execute("touch /etc/pwned").exit_code != 0
    [u] = sb.upload_files([("/etc/pwned", b"x")])
    assert u.error == "permission_denied"


def test_memory_limit(sb):
    r = sb.execute("python3 -c \"b = bytearray(2 * 1024**3)\"")
    assert r.exit_code != 0  # 137 (OOM-killed) หรือ MemoryError


def test_sessions_are_isolated(manager):
    a = manager.get_or_create("test-iso-a")
    b = manager.get_or_create("test-iso-b")
    try:
        a.sandbox.execute("echo secret > /workspace/a.txt")
        assert b.sandbox.execute("cat /workspace/a.txt").exit_code != 0
    finally:
        manager.destroy("test-iso-a")
        manager.destroy("test-iso-b")


# ------------------------------------------------------------------ files
def test_upload_download_roundtrip(sb):
    data = bytes(range(256))
    [u] = sb.upload_files([("/workspace/sub/dir/blob.bin", data)])
    assert u.error is None
    [d] = sb.download_files(["/workspace/sub/dir/blob.bin"])
    assert d.error is None and d.content == data
    # agent (user 1000) ต้องแก้ไฟล์ที่อัปโหลดได้
    assert sb.execute("echo more >> /workspace/sub/dir/blob.bin").exit_code == 0


def test_download_errors(sb):
    [missing, directory, rel] = sb.download_files(["/workspace/nope.txt", "/workspace", "relative.txt"])
    assert missing.error == "file_not_found"
    assert directory.error == "is_directory"
    assert rel.error == "invalid_path"


# ------------------------------------------- BaseSandbox helpers (ได้มาฟรี)
def test_basesandbox_file_tools(sb):
    assert sb.write("/workspace/hello.py", "print('hi')\n").error is None
    assert sb.edit("/workspace/hello.py", "hi", "hello").error is None
    read = sb.read("/workspace/hello.py")
    assert read.error is None and "hello" in read.file_data["content"]
    ls = sb.ls("/workspace")
    assert any(e["path"].endswith("hello.py") for e in ls.entries)
    assert sb.execute("python3 /workspace/hello.py").output.strip() == "hello"

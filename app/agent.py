"""สร้าง Deep Agent ที่ใช้ DockerSandbox เป็น backend."""

from __future__ import annotations

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.config import settings
from app.docker_sandbox import DockerSandbox

SYSTEM_PROMPT = """You are a helpful coding assistant with access to an isolated Linux sandbox.

- Use the `execute` tool to run shell commands (python3, numpy and pandas are installed).
- Work only inside /workspace. Always use absolute paths (e.g. /workspace/main.py).
- The sandbox has NO internet access, so you cannot pip install new packages.
- Each command has a time limit; avoid long-running or interactive commands.
- When you create files for the user, tell them the path so they can download it.
"""


def build_model():
    kwargs = {}
    if settings.model.startswith("openai:"):
        # Chat Completions API ใช้ได้กับ endpoint ที่ compatible กับ OpenAI ทุกเจ้า (LiteLLM ฯลฯ)
        kwargs["use_responses_api"] = False
    return init_chat_model(settings.model, **kwargs)


def build_agent(sandbox: DockerSandbox, checkpointer: BaseCheckpointSaver):
    """หนึ่ง session = หนึ่ง agent graph ที่ผูกกับ sandbox ของตัวเอง.

    `backend=` รับ instance ตัวเดียว เราจึง compile agent แยกต่อ session
    (เบามาก) ส่วน checkpointer ใช้ร่วมกันและแยกกันด้วย thread_id
    """
    return create_deep_agent(
        model=build_model(),
        system_prompt=SYSTEM_PROMPT,
        backend=sandbox,
        checkpointer=checkpointer,
    )

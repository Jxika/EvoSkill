"""OpenCode harness — server management, query execution, response parsing.

Uses raw httpx to talk to the opencode server (the Python SDK sends
model/provider as flat fields which the server ignores; the correct
format is a nested ``model: {providerID, modelID}`` object).
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Type

import httpx
from pydantic import BaseModel, ValidationError

from ..provider_auth import apply_openrouter_env, ensure_openrouter_api_key

# ── module-level state ────────────────────────────────────────────────
_SERVER_PORTS: dict[str, int] = {}
_SERVER_PIDS: dict[str, int] = {}
_SPAWNED_THIS_RUN: set[str] = set()

_TIMEOUT = 1800  # per-request HTTP timeout (30 min — opencode agents can take 15+ min on complex queries)


# ── provider auth ─────────────────────────────────────────────────────

_PROVIDER_ENV_KEYS = {
    "openrouter": ["OPENROUTER_API_KEY", "LLM_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "together": ["TOGETHER_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "xai": ["XAI_API_KEY"],
}

"""
OpenCode 不直接读.env,而是从自己的 auth store 读key。启动server后会：
"""
def _push_provider_auth(base_url: str) -> None:
    """Push all available API keys from env into the opencode server's auth store.

    The opencode server reads credentials from its own auth store, not env vars.
    This syncs any provider keys found in the environment so the server can
    authenticate regardless of which provider the user configures.
    """
    for provider, env_vars in _PROVIDER_ENV_KEYS.items():
        for var in env_vars:
            key = os.environ.get(var)
            if key:
                try:
                    httpx.put(
                        f"{base_url}/auth/{provider}",
                        json={"type": "api", "key": key},
                        timeout=5,
                    )
                except Exception:
                    pass
                break


# ── server lifecycle ──────────────────────────────────────────────────

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_key(cwd: str | Path | None) -> str:
    return str(Path(cwd).resolve()) if cwd else ""


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(10):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _kill_all_opencode_servers() -> None:
    """Kill all opencode serve processes on this machine."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/IM", "opencode.exe"],
                capture_output=True,
                timeout=5,
            )
        else:
            subprocess.run(
                ["pkill", "-f", "opencode serve"],
                capture_output=True,
                timeout=5,
            )
    except Exception:
        pass


def _opencode_executable_candidates() -> list[str]:
    """Return candidate paths/names for the OpenCode CLI, in priority order."""
    override = os.environ.get("OPENCODE_BIN", "").strip()
    if override:
        return [override]

    candidates: list[str] = []
    if os.name == "nt":
        candidates.extend(["opencode.exe", "opencode.cmd", "opencode"])
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            npm_bin = Path(appdata) / "npm"
            candidates.extend(
                str(p)
                for p in (
                    npm_bin / "opencode.cmd",
                    npm_bin / "opencode.exe",
                    npm_bin / "opencode.ps1",
                )
            )
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(str(Path(local_app) / "Programs" / "opencode" / "opencode.exe"))

        # Local npm install (e.g. npm install opencode-ai in project root)
        for base in (Path.cwd(), *Path.cwd().parents):
            node_modules = base / "node_modules"
            for pattern in (
                "opencode-windows-x64/bin/opencode.exe",
                "opencode-ai/node_modules/opencode-windows-x64/bin/opencode.exe",
                ".bin/opencode.cmd",
            ):
                candidates.append(str(node_modules / pattern))
    else:
        candidates.append("opencode")

    return candidates

#第二部分:Server生命周期
'''
  1.OPENCODE_BIN 环境变量
  2.PATH 上的 opencode.exe/opencode.cmd
  3.%APPDATA%\npm\opencode.exe
  4.项目node_modules里的本地安装
找不到则抛出带安装提示的 FileNotFoundError(之前遇到的WinError2就是这里)。
'''
def _resolve_opencode_executable() -> str:
    """Locate the OpenCode CLI executable or raise a helpful error."""
    for candidate in _opencode_executable_candidates():
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
        found = shutil.which(candidate)
        if found:
            return found

    raise FileNotFoundError(
        "OpenCode CLI not found. Install it and ensure it is on PATH, for example:\n"
        "  npm install -g opencode-ai\n"
        "  scoop install opencode\n"
        "Or set OPENCODE_BIN to the full path of opencode.exe / opencode.cmd."
    )


def _ensure_provider_api_key(provider_id: str | None) -> None:
    """Fail fast when a configured provider has no API key in the environment."""
    if not provider_id:
        return
    env_vars = _PROVIDER_ENV_KEYS.get(provider_id.strip().lower(), [])
    if not env_vars:
        return
    if any(os.environ.get(name) for name in env_vars):
        return
    raise RuntimeError(
        f"Missing API key for provider {provider_id!r}. "
        f"Set one of: {', '.join(env_vars)} (e.g. in a .env file at the project root)."
    )


def shutdown_project_server(project_root: str | Path | None) -> None:
    key = _resolve_key(project_root)
    pid = _SERVER_PIDS.pop(key, None)
    if pid is not None:
        _kill_pid(pid)
    _SERVER_PORTS.pop(key, None)
    _SPAWNED_THIS_RUN.discard(key)


def shutdown_all_servers() -> None:
    for key in list(set(_SERVER_PORTS) | set(_SERVER_PIDS) | set(_SPAWNED_THIS_RUN)):
        shutdown_project_server(key)


def _wait_for_port(port: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.5)

# 核心启动逻辑
def _ensure_server(options: dict[str, Any]) -> str:
    """Return ``http://127.0.0.1:<port>`` for a running opencode server.

    First call per project per process kills ALL stale opencode servers,
    then spawns a fresh one. Subsequent calls reuse the same server.
    """
    key = _resolve_key(options.get("cwd"))

    if key in _SPAWNED_THIS_RUN:
        port = _SERVER_PORTS.get(key)
        if port is not None:
            return f"http://127.0.0.1:{port}"

    # kill ALL opencode servers from previous runs
    _kill_all_opencode_servers() # 清理所有旧server，避免端口冲突
    time.sleep(0.5)

    port = _find_free_port()
    env = dict(os.environ)
    apply_openrouter_env(options.get("provider_id"), env)

    opencode_bin = _resolve_opencode_executable()
    popen_kwargs: dict[str, Any] = {
        "cwd": options.get("cwd"),
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [opencode_bin, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
        **popen_kwargs,
    )

    _SERVER_PORTS[key] = port
    _SERVER_PIDS[key] = proc.pid
    _SPAWNED_THIS_RUN.add(key)
    _wait_for_port(port)

    base_url = f"http://127.0.0.1:{port}"
    _push_provider_auth(base_url)
    return base_url


# ── query execution ──────────────────────────────────────────────────
# 发一轮对话
async def execute_query(options: dict[str, Any], query: str) -> list[Any]:
    if not isinstance(options, dict):
        raise TypeError(f"OpenCode executor requires dict options, got {type(options)}")

    provider_id = options.get("provider_id")
    ensure_openrouter_api_key(provider_id)
    _ensure_provider_api_key(provider_id)
    base_url = _ensure_server(options)

    async with httpx.AsyncClient(base_url=base_url, timeout=_TIMEOUT) as client:
        # 1. create session
        r = await client.post("/session", json={})
        r.raise_for_status()
        session_id = r.json()["id"]

        # 2. send message (nested model object — required by the server)
        
        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": query}],
            "model": {
                "providerID": options.get("provider_id", "anthropic"),
                "modelID": options.get("model_id", "claude-sonnet-4-6"),
            },
        }
        print( f"body: {body}")
        if options.get("system"):
            body["system"] = options["system"]
        if options.get("tools"):
            body["tools"] = options["tools"]
        if options.get("mode"):
            body["mode"] = options["mode"]
        if options.get("format"):
            body["format"] = options["format"]
        #当前的400错误发生在 POST .../message:raise_for_status()时OpenCode拒绝做个format结构。
        r = await client.post(f"/session/{session_id}/message", json=body)
        r.raise_for_status()
        chat_info = r.json()

        # 3. fetch full messages (with parts + structured output)
        r = await client.get(f"/session/{session_id}/message")
        r.raise_for_status()
        messages = r.json()

    return [{"session_id": session_id, "chat_info": chat_info, "messages": messages}]


# ── response parsing ─────────────────────────────────────────────────
'''
parse_response ---转成AgentTrace 字段
1.找最后一条 assistant消息
2.拼文本结果

'''
def parse_response(messages: list[Any],response_model: Type[BaseModel],get_options: Callable[[], Any],) -> dict[str, Any]:
    payload = messages[0]
    all_msgs: list[dict] = payload.get("messages", [])

    # find last assistant message
    assistant_info: dict = {}
    assistant_parts: list[dict] = []
    for msg in reversed(all_msgs):
        info = msg.get("info", {})
        if info.get("role") == "assistant":
            assistant_info = info
            assistant_parts = msg.get("parts", [])
            break

    # extract text
    result_text = "".join(
        p.get("text", "") for p in assistant_parts if p.get("type") == "text"
    )

    # structured output
    output = None
    parse_error = None
    raw_structured = assistant_info.get("structured")

    if raw_structured is not None:
        try:
            output = response_model.model_validate(raw_structured)
        except (ValidationError, TypeError, ValueError) as e:
            parse_error = f"{type(e).__name__}: {e}"

    # fallback: parse JSON from text (even if structured failed)
    if output is None and result_text.strip():
        text = result_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            lines = lines[1:] if lines[0].startswith("```") else lines
            lines = lines[:-1] if lines and lines[-1].strip() == "```" else lines
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
            output = response_model.model_validate(parsed)
            raw_structured = parsed
            parse_error = None
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as e:
            if parse_error is None:
                parse_error = f"{type(e).__name__}: {e}"

    if output is None and parse_error is None:
        parse_error = "No structured output returned (context limit likely exceeded)"

    # cost / usage
    cost = assistant_info.get("cost", 0.0) or 0.0
    usage = assistant_info.get("tokens", {}) or {}

    # model / tools from options
    opts = get_options()
    model = opts.get("model", "unknown") if isinstance(opts, dict) else "unknown"
    tools = list(opts.get("tools", {}).keys()) if isinstance(opts, dict) and opts.get("tools") else []

    session_id = payload.get("session_id", "unknown")

    return dict(
        uuid=session_id,
        session_id=session_id,
        model=model,
        tools=tools,
        duration_ms=0,
        total_cost_usd=cost,
        num_turns=1,
        usage=usage,
        result=result_text,
        is_error=parse_error is not None,
        output=output,
        parse_error=parse_error,
        raw_structured_output=raw_structured,
        messages=messages,
    )


atexit.register(shutdown_all_servers)

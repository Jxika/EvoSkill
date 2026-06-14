"""OpenCode SDK option building and permission management.

All OpenCode-specific construction logic lives here:
    - Tool name mapping (Claude PascalCase → OpenCode lowercase)
    - Model string parsing ("anthropic/claude-sonnet-4-6" → provider + model)
    - Permission auto-config (opencode.json)
    - The build_opencode_options() function
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..model_aliases import DEFAULT_ANTHROPIC_MODEL, normalize_harness_model
from ..utils import resolve_project_root, resolve_data_dirs


DEFAULT_OPENCODE_MODEL = DEFAULT_ANTHROPIC_MODEL

# OpenCode 1.14.48+ rejects/persists format.json_schema incorrectly; see
# https://github.com/anomalyco/opencode/issues/26929
_USE_OPENCODE_API_JSON_SCHEMA = os.environ.get(
    "OPENCODE_JSON_SCHEMA_FORMAT", ""
).lower() in ("1", "true", "yes")

#JSON格式
def _system_with_json_schema_instruction(system: str, schema: dict[str, Any]) -> str:
    """Ask the model for JSON in-band when API json_schema format is disabled."""
    schema_text = json.dumps(schema, indent=2)
    return (
        f"{system.rstrip()}\n\n"
        "Return your answer as a single JSON object matching this schema "
        "(raw JSON only, no markdown fences or extra text):\n"
        f"{schema_text}"
    )


CLAUDE_TO_OPENCODE_TOOL = {
    "Read": "read",
    "Write": "write",
    "Bash": "bash",
    "Glob": "glob",
    "Grep": "grep",
    "Edit": "edit",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
    "TodoWrite": "todowrite",
    "Skill": "skill",
    # OpenCode does not expose a separate BashOutput tool.
    "BashOutput": None,
}


def split_opencode_model(model: str | None) -> tuple[str, str]:
    """Parse 'provider/model' string into (provider_id, model_id)."""
    full = normalize_harness_model("opencode", model)
    if "/" in full:
        return full.split("/", 1)
    return "anthropic", full


def to_opencode_tools(tools: Iterable[str]) -> dict[str, bool]:
    """Map Claude tool names (PascalCase) to OpenCode names (lowercase)."""
    converted: dict[str, bool] = {}
    for tool in tools:
        normalized = CLAUDE_TO_OPENCODE_TOOL.get(tool, tool.lower())
        if normalized is not None:
            converted[normalized] = True
    return converted


def _normalize_permission_block(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        return {"*": value}
    if isinstance(value, dict):
        return dict(value)
    return {}


def ensure_opencode_project_permissions(
    project_root: str | Path | None,
    data_dirs: Iterable[str] | None = None,
) -> None:
    """Auto-create/update opencode.json to grant file access to data directories."""
    root = resolve_project_root(project_root)
    resolved_add_dirs = resolve_data_dirs(root, data_dirs)
    if not resolved_add_dirs:
        return

    jsonc_path = root / "opencode.jsonc"
    config_path = root / "opencode.json"
    if jsonc_path.exists() and not config_path.exists():
        return

    config: dict[str, Any] = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            return

    config.setdefault("$schema", "https://opencode.ai/config.json")
    permission = _normalize_permission_block(config.get("permission"))
    external_directory = _normalize_permission_block(
        permission.get("external_directory")
    )

    changed = False
    for raw_path in resolved_add_dirs:
        path = str(Path(raw_path).resolve())
        for pattern in (path, f"{path}/**"):
            if external_directory.get(pattern) != "allow":
                external_directory[pattern] = "allow"
                changed = True

    if not changed and config_path.exists():
        return

    permission["external_directory"] = external_directory
    config["permission"] = permission
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def build_opencode_options(
    *,
    system: str,
    schema: dict[str, Any],
    tools: Iterable[str],
    project_root: str | Path | None = None,
    model: str | None = None,
    mode: str = "build",
    data_dirs: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build an options dict for the OpenCode SDK."""
    root = resolve_project_root(project_root)
    provider_id, model_id = split_opencode_model(model)
    resolved_add_dirs = resolve_data_dirs(root, data_dirs)
    ensure_opencode_project_permissions(root, resolved_add_dirs)

    system_with_dirs = system
    if resolved_add_dirs:
        dirs_note = "\n".join(f"- {path}" for path in resolved_add_dirs)
        system_with_dirs = (
            f"{system.rstrip()}\n\n"
            "Additional accessible data directories are available outside the project root.\n"
            "Use absolute paths when you need to inspect them:\n"
            f"{dirs_note}"
        )

    options: dict[str, Any] = {
        "system": system_with_dirs,
        "tools": to_opencode_tools(tools),
        "mode": mode,
        "provider_id": provider_id,
        "model_id": model_id,
        "cwd": str(root),
        "add_dirs": resolved_add_dirs,
        "response_schema": schema,
    }
    if _USE_OPENCODE_API_JSON_SCHEMA:
        options["format"] = {
            "type": "json_schema",
            "schema": schema,
        }
    else:
        options["system"] = _system_with_json_schema_instruction(
            system_with_dirs, schema
        )
    return options

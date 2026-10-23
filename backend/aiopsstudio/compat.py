"""Bounded migration identifiers; never move, rename or rewrite existing assets.

Only this module declares the previous product identifiers. Fresh installations
use the current namespace; existing durable assets keep their physical identity.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from datetime import datetime
import math
import os

ENV_PREFIX = "AIOPSSTUDIO_"
LEGACY_ENV_PREFIX = "EASYOPS_"
COOKIE_NAME = "aiopsstudio_session"
LEGACY_COOKIE_NAME = "easyops_session"
TABLE_PREFIX = "aiopsstudio"
LEGACY_TABLE_PREFIX = "easyops"
TABLE_SUFFIXES = ("documents", "events", "worker_slot")
DOCKER_STATE_LABEL = "aiopsstudio.state_id"
LEGACY_DOCKER_STATE_LABEL = "easyops.state_id"
DOCKER_OWNER_LABEL = "aiopsstudio.owner"
LEGACY_DATABASE = {"container": "easyops-db", "volume": "easyops-db-data", "user": "easyops", "name": "easyops", "image": "postgres:16-alpine"}
LEGACY_APPLICATION = "easyops.application:create_app"
LEGACY_STATE_NAMES = ("easyops", "easyops-workbench")


def environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Accept old names once, reject ambiguous values without exposing them."""
    source = dict(os.environ if environ is None else environ)
    result = {key: value for key, value in source.items() if not key.startswith(LEGACY_ENV_PREFIX)}
    for key, value in source.items():
        if key.startswith(LEGACY_ENV_PREFIX):
            current = ENV_PREFIX + key.removeprefix(LEGACY_ENV_PREFIX)
            if current in source and source[current] != value:
                raise ValueError(f"{current} 与旧版同名配置冲突；请保留一致的单一配置")
            result[current] = value
    return result


def state_directory(environ: Mapping[str, str] | None = None, *, home: Path | None = None) -> Path:
    """Read-only discovery: reuse one old state; refuse an ambiguous split."""
    config = environment(environ)
    explicit = config.get(ENV_PREFIX + "STATE_DIR")
    if explicit is not None:
        state = Path(explicit).expanduser()
        if not state.is_absolute():
            raise ValueError("AIOPSSTUDIO_STATE_DIR 必须是绝对路径")
        return state
    base = (home or Path.home()) / ".local/share"
    current = base / "aiopsstudio"
    candidates = [path for path in [current, *(base / name for name in LEGACY_STATE_NAMES)] if path.exists()]
    if len(candidates) > 1:
        raise ValueError("检测到多个状态目录；请显式设置 AIOPSSTUDIO_STATE_DIR，未合并或修改数据")
    return candidates[0] if candidates else current


def legacy_state(state: Path, environ: Mapping[str, str]) -> bool:
    """Explicit old state settings retain old Docker defaults unless overridden."""
    previous_defaults = {Path.home() / ".local/share" / name for name in LEGACY_STATE_NAMES}
    return state in previous_defaults or LEGACY_ENV_PREFIX + "STATE_DIR" in environ


def table_namespace(existing: set[str]) -> str:
    current = {f"{TABLE_PREFIX}_{suffix}" for suffix in TABLE_SUFFIXES}
    previous = {f"{LEGACY_TABLE_PREFIX}_{suffix}" for suffix in TABLE_SUFFIXES}
    present_new, present_old = existing & current, existing & previous
    if (present_new and present_old) or (present_new and present_new != current) or (present_old and present_old != previous):
        raise ValueError("数据库包含冲突或不完整的工作台表；未创建、合并或重命名数据，请先核对迁移状态")
    return LEGACY_TABLE_PREFIX if present_old else TABLE_PREFIX


def docker_state_matches(output: str, state_id: str) -> bool:
    """Accept either ownership label, but never accept conflicting labels."""
    labels = {part.strip() for part in output.split("|") if part.strip() not in {"", "<no value>"}}
    return labels == {state_id}


def legacy_cookie_expiry(session: dict | None) -> float:
    """Legacy cookies cannot outlive the original 12-hour session window."""
    try:
        expires = min(float(session["expires_at"]), datetime.fromisoformat(session["created_at"]).timestamp() + 12 * 3600)
        return expires if math.isfinite(expires) else 0
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0

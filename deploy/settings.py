"""本机部署的外部参数；不读取业务凭据或创建运行资产。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re


DEFAULT_PORTS = {"web": 3000, "api": 8000, "postgres": 5432}
DEFAULT_DATABASE = {"container": "easyops-db", "volume": "easyops-db-data", "user": "easyops", "name": "easyops", "image": "postgres:16-alpine"}


def strict_json(text):
    def reject_constant(_value):
        raise ValueError("JSON 不支持非有限数值")
    def unique_fields(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON 对象不能包含重复字段")
            result[key] = value
        return result
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_fields)


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError(f"{label}必须是只包含已支持字段的 JSON 对象")
    return value


def _port(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError(f"{label}必须是 1 到 65535 的整数")
    return value


def _name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError(f"{label}格式不正确")
    return value


def load_config(path=None, environ=None):
    """显式 JSON 路径或 EASYOPS_DEPLOY_CONFIG；未指定时使用通用默认值。"""
    environ = os.environ if environ is None else environ
    path = path or environ.get("EASYOPS_DEPLOY_CONFIG")
    data = {}
    if path:
        try:
            config_path = Path(path).expanduser()
            if not config_path.is_absolute() or not config_path.is_file() or config_path.stat().st_size > 65536:
                raise ValueError("部署配置必须是绝对路径的小型 JSON 文件")
            data = strict_json(config_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("无法读取部署 JSON 配置") from error
    _object(data, {"ports", "docker_context", "database", "node_binary", "ssh_tunnel"}, "部署配置")
    ports = {**DEFAULT_PORTS, **_object(data.get("ports", {}), DEFAULT_PORTS, "ports")}
    for name, value in ports.items():
        _port(value, f"ports.{name}")
    if len(set(ports.values())) != len(ports):
        raise ValueError("Web、API 和数据库端口不能重复")
    database = {**DEFAULT_DATABASE, **_object(data.get("database", {}), DEFAULT_DATABASE, "database")}
    for key in ("container", "volume", "user", "name"):
        _name(database[key], f"database.{key}")
    if not isinstance(database["image"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._:@-]{0,199}", database["image"]):
        raise ValueError("database.image 格式不正确")
    context = data.get("docker_context")
    if context is not None:
        _name(context, "docker_context")
    node = data.get("node_binary", "node")
    if not isinstance(node, str) or not node.strip() or "\x00" in node or "\n" in node:
        raise ValueError("node_binary 必须是 Node 可执行文件名或路径")
    tunnel = {"enabled": False, **_object(data.get("ssh_tunnel", {}), {"enabled", "host", "local_port", "remote_host", "remote_port"}, "ssh_tunnel")}
    if not isinstance(tunnel["enabled"], bool):
        raise ValueError("ssh_tunnel.enabled 必须是布尔值")
    if tunnel["enabled"]:
        if not isinstance(tunnel.get("host"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,252}", tunnel["host"]):
            raise ValueError("启用 SSH 转发时必须指定有效 host")
        _port(tunnel.get("local_port"), "ssh_tunnel.local_port")
        _port(tunnel.get("remote_port"), "ssh_tunnel.remote_port")
        if tunnel["local_port"] in ports.values():
            raise ValueError("SSH 转发本地端口不能与应用端口重复")
        tunnel.setdefault("remote_host", "127.0.0.1")
        if not isinstance(tunnel["remote_host"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", tunnel["remote_host"]):
            raise ValueError("SSH 转发 remote_host 格式不正确")
    state = Path(environ.get("EASYOPS_STATE_DIR", str(Path.home() / ".local/share/easyops"))).expanduser()
    if not state.is_absolute():
        raise ValueError("EASYOPS_STATE_DIR 必须是绝对路径")
    return {"ports": ports, "database": database, "docker_context": context, "node_binary": node, "ssh_tunnel": tunnel, "state_dir": state}

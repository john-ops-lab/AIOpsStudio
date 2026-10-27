#!/usr/bin/env python3
"""从用户显式提供的外部 JSON 引导新配置；不调用模型或业务工具。"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import stat
import sys
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.settings import load_config, strict_json


BOOTSTRAP_FIELDS = {"version", "mcp_servers", "model_configs", "default_model_name", "settings", "skills"}


def read_external_json(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("引导文件必须是仓库外、不超过 2MiB 的普通 JSON 文件")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("引导文件包含私密配置，权限必须为 0600 或更严格")
    try:
        body = strict_json(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("无法读取引导 JSON 文件") from error
    return validate_bootstrap(body, path.parent)


def validate_bootstrap(body, base_dir):
    if not isinstance(body, dict) or set(body) - BOOTSTRAP_FIELDS or body.get("version") != 1 or not isinstance(body.get("version"), int) or isinstance(body.get("version"), bool):
        raise ValueError("引导文件必须使用 version=1 和已支持的顶层字段")
    body = copy.deepcopy(body)
    for group in ("mcp_servers", "model_configs", "skills"):
        items = body.setdefault(group, [])
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError(f"{group}必须是最多 100 项的数组")
        names = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip() or item["name"].strip() in names:
                raise ValueError(f"{group}中的名称必须非空且不重复")
            item["name"] = item["name"].strip()
            names.add(item["name"])
    if not isinstance(body.setdefault("settings", {}), dict):
        raise ValueError("settings 必须是 JSON 对象")
    if "default_model_name" in body and not isinstance(body["default_model_name"], str):
        raise ValueError("default_model_name 必须是模型配置名称")
    for item in body["mcp_servers"]:
        allowed = item.get("allowed_tools")
        if not isinstance(item.get("url"), str) or not isinstance(allowed, list) or not all(isinstance(x, str) and x for x in allowed) or len(set(allowed)) != len(allowed):
            raise ValueError("MCP 配置必须显式提供 url 和无重复的 allowed_tools 数组")
        if any(k in item for k in ("tools", "current_version", "versions", "revision", "id")):
            raise ValueError("引导文件不能提供服务端工具发现结果或配置版本")
    for item in body["model_configs"]:
        if not all(isinstance(item.get(k), str) and item[k].strip() for k in ("provider", "model", "base_url")):
            raise ValueError("模型配置必须提供 provider、model 和 base_url")
    for item in body["skills"]:
        if "content_file" in item:
            if "content" in item or not isinstance(item["content_file"], str):
                raise ValueError("Skill 的 content 与 content_file 只能提供一个")
            base = Path(base_dir).resolve()
            relative = Path(item.pop("content_file"))
            path = (base / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(base) or not path.is_file() or path.stat().st_size > 256 * 1024:
                raise ValueError("Skill content_file 必须是引导目录内、不超过 256KiB 的文件")
            try:
                item["content"] = path.read_text()
            except (OSError, UnicodeError) as error:
                raise ValueError("Skill 内容文件必须是可读取的 UTF-8 文本") from error
        if not isinstance(item.get("content"), str) or not item["content"].strip():
            raise ValueError("Skill 必须提供正文或相对引导目录的内容文件")
        if not isinstance(item.get("dependencies", []), list):
            raise ValueError("Skill dependencies 必须是数组")
        for dep in item.get("dependencies", []):
            if not isinstance(dep, dict) or not isinstance(dep.get("tool_name"), str) or not dep["tool_name"]:
                raise ValueError("Skill 依赖必须指定 server_name/tool_name 或 provider=ongrid/tool_name")
            if "server_name" in dep and (not isinstance(dep["server_name"], str) or not dep["server_name"].strip() or "provider" in dep):
                raise ValueError("Skill server_name 必须是非空名称，不能同时指定 provider")
            if "server_name" not in dep and dep.get("provider") != "ongrid":
                raise ValueError("Skill 依赖必须指定 server_name/tool_name 或 provider=ongrid/tool_name")
    return body


def local_origin(url):
    try:
        value = urlsplit(url)
        valid = value.scheme == "http" and value.hostname in {"127.0.0.1", "localhost"} and value.username is None and value.password is None and value.path in {"", "/"} and not value.query and not value.fragment
        if value.port is not None and not 1 <= value.port <= 65535:
            valid = False
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("引导目标必须是本机回环 HTTP 工作台入口，不能包含认证信息、路径或查询参数")
    return "http://127.0.0.1" + (f":{value.port}" if value.port is not None else "")


def apply_bootstrap(client, body):
    """导入草稿仍走管理 API；正式发布须由工作台完成质量门槛。"""
    def api(method, path, payload=None):
        response = client.request(method, "/api" + path, json=payload)
        if not response.is_success:
            raise RuntimeError(f"管理请求失败 HTTP {response.status_code}；请在工作台检查对应配置和连接")
        return response.json()

    servers = {x["name"]: x for x in api("GET", "/mcp-servers")["items"]}
    models = {x["name"]: x for x in api("GET", "/model-configs")["items"]}
    skills = {x["name"]: x for x in api("GET", "/skills")["items"]}
    result = {"created": 0, "published": 0, "drafts": 0, "preserved_existing": 0, "default_model_pending": False}
    for definition in body["mcp_servers"]:
        name = definition["name"]
        if name in servers:
            if not servers[name].get("current_version"):
                raise ValueError("存在同名未发布 MCP 草稿，请先在工作台完成或移除该草稿")
            result["preserved_existing"] += 1
            continue
        # Discovery is authoritative; caller-provided tool definitions are forbidden.
        wanted = definition["allowed_tools"]
        server = api("POST", "/mcp-servers", {k: v for k, v in definition.items() if k != "allowed_tools"})
        result["created"] += 1
        server = api("POST", f"/mcp-servers/{server['id']}/discover", {})["config"]
        tools = server["tools"]
        if set(wanted) - {tool["name"] for tool in tools}:
            raise ValueError("明确开放的工具未在实际发现目录中，保留草稿并停止引导")
        for tool in tools:
            tool["readonly"] = tool["name"] in wanted
        server = api("PUT", f"/mcp-servers/{server['id']}", {"expected_revision": server["revision"], "tools": tools, "allowed_tools": wanted})
        servers[name] = server
        result["drafts"] += 1
    for definition in body["model_configs"]:
        name = definition["name"]
        if name in models:
            if not models[name].get("current_version"):
                raise ValueError("存在同名未发布模型草稿，请先在工作台处理")
            result["preserved_existing"] += 1
            continue
        model = api("POST", "/model-configs", definition)
        result["created"] += 1
        models[name] = model
        result["drafts"] += 1
    settings = body["settings"]
    if body.get("default_model_name"):
        model = models.get(body["default_model_name"])
        if not model:
            raise ValueError("default_model_name 未匹配已有或本次导入的模型配置")
        if model.get("current_version"):
            settings = {**settings, "default_model_id": model["id"]}
        else:
            result["default_model_pending"] = True
    if settings:
        current = api("GET", "/settings")
        api("PUT", "/settings", {**settings, "expected_revision": current["revision"]})
    for definition in body["skills"]:
        name = definition["name"]
        if name in skills:
            if not skills[name].get("current_version"):
                raise ValueError("存在同名未发布 Skill 草稿，请先在工作台处理")
            result["preserved_existing"] += 1
            continue
        definition = copy.deepcopy(definition)
        for dependency in definition.get("dependencies", []):
            server_name = dependency.pop("server_name", None)
            if server_name:
                if server_name not in servers:
                    raise ValueError("Skill 依赖的 server_name 未匹配 MCP 配置")
                dependency["server_id"] = servers[server_name]["id"]
        skill = api("POST", "/skills", definition)
        result["created"] += 1
        skills[name] = skill
        result["drafts"] += 1
    return result


def main(argv=None):
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", required=True, help="仓库外、权限 0600 的 version=1 JSON 文件")
    parser.add_argument("--url", default=f"http://127.0.0.1:{config['ports']['web']}")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", type=Path, default=config["state_dir"] / "private/admin-password")
    args = parser.parse_args(argv)
    origin = local_origin(args.url)
    body = read_external_json(args.bootstrap)
    password_path = args.password_file.expanduser().resolve()
    if not password_path.is_file() or password_path.stat().st_size > 4096:
        raise ValueError("管理员口令文件不存在或过大")
    password = password_path.read_text().strip()
    with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=90, follow_redirects=False) as client:
        response = client.post("/api/login", json={"username": args.username, "password": password})
        if not response.is_success:
            raise RuntimeError(f"工作台登录失败 HTTP {response.status_code}")
        print(json.dumps(apply_bootstrap(client, body), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as error:
        # Never echo request bodies, passwords or transport URLs from exceptions.
        print(str(error) if isinstance(error, (ValueError, RuntimeError)) else "引导未完成，请检查本机配置和连接", file=sys.stderr)
        sys.exit(1)

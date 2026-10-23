#!/usr/bin/env python3
"""Install only in this checkout; never overwrite unrelated hooks."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from security_scan import GateError, configured_profile_path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-profile', type=Path)
    args = parser.parse_args()
    if not shutil.which('gitleaks'):
        raise ValueError('请先安装 Gitleaks 8.30.1 或兼容的 8.x 版本')
    previous = git('config', '--get', 'core.hooksPath')
    if previous.returncode not in {0, 1}:
        raise ValueError('无法读取 Git 钩子配置')
    if previous.returncode == 0 and previous.stdout.strip() != '.githooks':
        raise ValueError('已有其他 hooksPath，未覆盖；请先明确协调现有钩子')
    if previous.returncode == 1:
        path = Path(git('rev-parse', '--git-path', 'hooks').stdout.strip())
        if not path.is_absolute():
            path = ROOT / path
        if any(p.is_file() and not p.name.endswith('.sample') for p in path.glob('*')):
            raise ValueError('已有自定义钩子，未覆盖')
    existing_profile = configured_profile_path()
    profile = args.local_profile
    if profile:
        profile = profile.expanduser().resolve()
        if existing_profile is not None and profile != existing_profile:
            raise ValueError('私有 profile 与已有配置冲突，未覆盖')
    else:
        profile = existing_profile
    if profile:
        if profile.is_relative_to(ROOT) or not profile.is_file() or profile.stat().st_mode & 0o077:
            raise ValueError('私有 profile 必须在仓库外且权限为 0600 或更严格')
        data = json.loads(profile.read_text())
        if not isinstance(data, dict):
            raise ValueError('私有 profile 格式无效')
        version = data.get('version')
        if type(version) is not int or version != 1:
            raise ValueError('私有 profile 版本无效')
    for name in ['pre-commit', 'pre-push']:
        hook = ROOT / '.githooks' / name
        if not hook.is_file() or not hook.stat().st_mode & 0o111:
            raise ValueError('钩子文件缺失或不可执行')
    if profile:
        result = git('config', '--local', 'aiopsstudio.securityProfile', str(profile))
        if result.returncode:
            raise ValueError('私有 profile 配置失败')
    if git('config', '--local', 'core.hooksPath', '.githooks').returncode:
        raise ValueError('钩子安装失败')
    print('已启用当前副本的 pre-commit / pre-push 敏感信息门禁。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (GateError, ValueError, OSError):
        print('安装失败：请检查 Gitleaks、现有钩子及仓库外 profile；未操作运行实例。', file=sys.stderr)
        sys.exit(1)

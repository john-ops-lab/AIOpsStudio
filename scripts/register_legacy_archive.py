#!/usr/bin/env python3
"""Offline maintenance only. Private plans must stay outside the Git checkout."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from aiopsstudio.legacy_archive import plan_archive, register_archive
from aiopsstudio.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--plan", type=Path)
    actions.add_argument("--register-plan", type=Path)
    args = parser.parse_args()
    path = (args.plan or args.register_plan).resolve()
    if path.is_relative_to(ROOT):
        parser.error("private archive plans must be outside the repository")
    database_url = os.environ.get("AIOPSSTUDIO_DATABASE_URL")
    if not database_url:
        parser.error("AIOPSSTUDIO_DATABASE_URL is required; never pass credentials as command arguments")
    store = Store(database_url)
    try:
        if args.plan:
            plan = plan_archive(store)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as target:
                json.dump(plan, target, ensure_ascii=False)
            counts = {kind: sum(e["kind"] == kind for e in plan["entries"]) for kind in ("run", "conversation", "attachment")}
            print(json.dumps({"planned": True, "counts": counts}))
        else:
            registered = register_archive(store, json.loads(path.read_text()))
            print(json.dumps({"registered": True, "entries": len(registered["entries"])}))
    finally:
        store.close()


if __name__ == "__main__":
    main()

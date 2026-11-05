#!/usr/bin/env python3
"""Merge the Ponytail Mobile provider into an OpenCode config.

OpenCode V1 and V2 intentionally have different plugin/provider schemas. This
script keeps the version switch in one testable place instead of embedding it
in the Termux installer.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

MODEL = "ponytail-free/free"
PLUGIN = "@dietrichgebert/ponytail"
TOKEN_REF = "{file:/root/.config/ponytail/gateway.token}"


def under(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"refusing to overwrite invalid OpenCode JSON {path}: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"refusing to overwrite non-object OpenCode config {path}")
    return data


def append_unique(items, value):
    if not isinstance(items, list):
        items = []
    if value not in items:
        items.append(value)
    return items


def configure(cfg: dict, major: int) -> dict:
    cfg = dict(cfg)
    cfg.setdefault("$schema", "https://opencode.ai/config.json")

    if major >= 2:
        plugins = cfg.get("plugins")
        legacy = cfg.pop("plugin", None)
        if isinstance(legacy, list):
            for item in legacy:
                if item not in (plugins if isinstance(plugins, list) else []):
                    plugins = append_unique(plugins, item)
        cfg["plugins"] = append_unique(plugins, PLUGIN)

        old = cfg.get("provider")
        if isinstance(old, dict):
            old = dict(old)
            old.pop("ponytail-free", None)
            if old:
                cfg["provider"] = old
            else:
                cfg.pop("provider", None)

        providers = cfg.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        providers = dict(providers)
        providers["ponytail-free"] = {
            "name": "Ponytail Mobile Free Gateway",
            "package": "@opencode/ai/providers/openai-compatible",
            "settings": {
                "baseURL": "http://127.0.0.1:8766/v1",
                "apiKey": TOKEN_REF,
                "encoding": "none",
            },
            "models": {
                "free": {
                    "modelID": "free",
                    "name": "Ponytail Free Auto",
                    "limit": {"context": 128000, "output": 16384},
                }
            },
        }
        cfg["providers"] = providers
    else:
        plugins = cfg.get("plugin")
        newer = cfg.pop("plugins", None)
        if isinstance(newer, list):
            for item in newer:
                if isinstance(item, str) and item not in (plugins if isinstance(plugins, list) else []):
                    plugins = append_unique(plugins, item)
        cfg["plugin"] = append_unique(plugins, PLUGIN)

        old = cfg.get("providers")
        if isinstance(old, dict):
            old = dict(old)
            old.pop("ponytail-free", None)
            if old:
                cfg["providers"] = old
            else:
                cfg.pop("providers", None)

        providers = cfg.get("provider")
        if not isinstance(providers, dict):
            providers = {}
        providers = dict(providers)
        providers["ponytail-free"] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Ponytail Mobile Free Gateway",
            "options": {
                "baseURL": "http://127.0.0.1:8766/v1",
                "apiKey": TOKEN_REF,
            },
            "models": {
                "free": {
                    "name": "Ponytail Free Auto",
                    "limit": {"context": 128000, "output": 16384},
                }
            },
        }
        cfg["provider"] = providers

    cfg["model"] = MODEL
    return cfg


def write_atomic(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="opencode.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--major", type=int, required=True)
    ap.add_argument("--root", default="/", help="alternate filesystem root for tests")
    args = ap.parse_args()
    root = Path(args.root)
    path = under(root, "/root/.config/opencode/opencode.json")
    if path.exists():
        backup = path.with_name("opencode.pre-ponytail-mobile.json")
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
    cfg = load(path)
    write_atomic(path, configure(cfg, args.major))
    print(f"Configured OpenCode {args.major} profile: {MODEL}")


if __name__ == "__main__":
    main()

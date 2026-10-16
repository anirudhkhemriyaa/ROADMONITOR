"""Configuration loader (Phase B).

Reads backend/config.yaml. Env overrides: ROADMONITOR_DB (db.url).
No other module reads YAML directly so the config stays single-source.
"""
import os
from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


def load_config() -> dict:
    with open(BACKEND_DIR / "config.yaml", "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if os.environ.get("ROADMONITOR_DB"):
        cfg.setdefault("db", {})["url"] = os.environ["ROADMONITOR_DB"]
    return cfg


CFG = load_config()


def cfg_get(path: str, default=None):
    """cfg_get("api.insert_batch_size", 500) -> nested lookup."""
    node = CFG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node

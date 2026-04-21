"""Persistent configuration (domain + download marks) at a fixed path."""
import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "brightspace_downloader" / "config.json"


def load():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def normalize_targets(raw):
    """Accept both the new {modules, topics} form and the legacy flat list."""
    if isinstance(raw, list):
        return {"modules": raw, "topics": []}
    return {"modules": raw.get("modules", []), "topics": raw.get("topics", [])}


def all_targets(cfg):
    return cfg.get("download_targets") or cfg.get("sync_targets") or {}


def summary(cfg):
    """Return (marked_courses, marked_modules, marked_topics)."""
    courses = modules = topics = 0
    for raw in all_targets(cfg).values():
        n = normalize_targets(raw)
        if n["modules"] or n["topics"]:
            courses += 1
        modules += len(n["modules"])
        topics += len(n["topics"])
    return courses, modules, topics

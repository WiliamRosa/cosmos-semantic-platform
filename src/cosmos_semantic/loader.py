"""Loading a semantic model from YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from cosmos_semantic.errors import ModelError
from cosmos_semantic.spec import Project

_LIST_KEYS = ("semantic_models", "metrics", "glossary", "business_rules")


def load_file(path: Path) -> Project:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ModelError(f"{path}: the file must contain a YAML mapping")
    try:
        return Project.model_validate(data)
    except ValidationError as exc:
        raise ModelError(f"{path}: {exc}") from exc


def merge(projects: list[Project]) -> Project:
    """Merges several files into one project; duplicate names are checked by the compiler."""
    merged: dict = {"version": 1, "user_context": {}, "custom_extensions": {}, "timezone": None}
    for key in _LIST_KEYS:
        merged[key] = []
    for project in projects:
        for key in _LIST_KEYS:
            merged[key].extend(getattr(project, key))
        for name, field in project.user_context.items():
            if name in merged["user_context"] and merged["user_context"][name] != field:
                raise ModelError(f"user_context.{name} is declared in conflicting ways")
            merged["user_context"][name] = field
        merged["custom_extensions"].update(project.custom_extensions)
        if project.timezone:
            if merged["timezone"] and merged["timezone"] != project.timezone:
                raise ModelError("model files declare different `timezone` values")
            merged["timezone"] = project.timezone
    return Project.model_construct(**merged)


def load_project(path: str | Path) -> Project:
    """Loads one file, or every ``*.yml``/``*.yaml`` file under a directory, recursively."""
    root = Path(path)
    if root.is_file():
        return load_file(root)
    if not root.is_dir():
        raise ModelError(f"{root}: path not found")
    files = sorted(p for p in root.rglob("*") if p.suffix in (".yml", ".yaml"))
    if not files:
        raise ModelError(f"{root}: no .yml files found")
    return merge([load_file(f) for f in files])

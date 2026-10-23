"""Leitura do modelo semântico a partir de arquivos YAML."""

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
        raise ModelError(f"{path}: o arquivo precisa ser um objeto YAML")
    try:
        return Project.model_validate(data)
    except ValidationError as exc:
        raise ModelError(f"{path}: {exc}") from exc


def merge(projects: list[Project]) -> Project:
    """Junta vários arquivos num único projeto; nomes duplicados são checados no compilador."""
    merged: dict = {"version": 1, "user_context": {}, "custom_extensions": {}, "timezone": None}
    for key in _LIST_KEYS:
        merged[key] = []
    for project in projects:
        for key in _LIST_KEYS:
            merged[key].extend(getattr(project, key))
        for name, field in project.user_context.items():
            if name in merged["user_context"] and merged["user_context"][name] != field:
                raise ModelError(f"user_context.{name} declarado de formas diferentes")
            merged["user_context"][name] = field
        merged["custom_extensions"].update(project.custom_extensions)
        if project.timezone:
            if merged["timezone"] and merged["timezone"] != project.timezone:
                raise ModelError("arquivos do modelo declaram fusos diferentes em `timezone`")
            merged["timezone"] = project.timezone
    return Project.model_construct(**merged)


def load_project(path: str | Path) -> Project:
    """Lê um arquivo ou todos os ``*.yml``/``*.yaml`` de um diretório, recursivamente."""
    root = Path(path)
    if root.is_file():
        return load_file(root)
    if not root.is_dir():
        raise ModelError(f"{root}: caminho não encontrado")
    files = sorted(p for p in root.rglob("*") if p.suffix in (".yml", ".yaml"))
    if not files:
        raise ModelError(f"{root}: nenhum arquivo .yml encontrado")
    return merge([load_file(f) for f in files])

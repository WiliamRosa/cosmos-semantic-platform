"""Importing models from other tools: dbt, Apache Ossie and Cube (phase 1), Power BI (phase 3).

No conversion loses information silently: whatever has no equivalent becomes a warning in the
report or is kept in ``custom_extensions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cosmos_semantic.spec import Project


@dataclass
class ImportResult:
    project: Project
    warnings: list[str] = field(default_factory=list)


def read_yaml_files(path: str | Path) -> list[tuple[Path, Any]]:
    root = Path(path)
    files = (
        [root]
        if root.is_file()
        else sorted(p for p in root.rglob("*") if p.suffix in (".yml", ".yaml", ".json"))
    )
    out = []
    for file in files:
        text = file.read_text(encoding="utf-8")
        out.append((file, text))
    return out


def dump_project(project: Project) -> str:
    data = project.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    data = {"version": project.version, **{k: v for k, v in data.items() if k != "version"}}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def import_model(kind: str, path: str | Path, **options: Any) -> ImportResult:
    if kind == "dbt":
        from cosmos_semantic.interchange.dbt import import_dbt

        return import_dbt(path, **options)
    if kind == "ossie":
        from cosmos_semantic.interchange.ossie import import_ossie

        return import_ossie(path)
    if kind == "cube":
        from cosmos_semantic.interchange.cube import import_cube

        return import_cube(path, **options)
    if kind == "powerbi":
        from cosmos_semantic.interchange.powerbi import import_powerbi

        return import_powerbi(path, **options)
    raise ValueError(f"unknown format: {kind} (use dbt, ossie, cube or powerbi)")

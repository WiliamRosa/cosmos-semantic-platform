"""Ponto de entrada do motor: modelo + conexões -> consultas executadas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from cosmos_semantic.compiler import SemanticGraph, compile_project
from cosmos_semantic.connectors import ConnectionsFile, Connector, DataSourceConfig, connect
from cosmos_semantic.errors import ModelError
from cosmos_semantic.loader import load_project
from cosmos_semantic.planner import CompiledQuery, Planner, Principal, QueryRequest


@dataclass
class QueryResult:
    compiled: CompiledQuery
    table: pa.Table

    def to_pylist(self) -> list[dict]:
        return self.table.to_pylist()


class Engine:
    def __init__(self, graph: SemanticGraph, connections: ConnectionsFile) -> None:
        self.graph = graph
        self.connections = connections
        self._connectors: dict[str, Connector] = {}

    @classmethod
    def from_paths(cls, model_path: str | Path, connections: ConnectionsFile) -> Engine:
        return cls(compile_project(load_project(model_path)), connections)

    def _source_name(self, name: str | None) -> str:
        name = name or self.connections.default
        if name is None:
            if len(self.connections.data_sources) == 1:
                return next(iter(self.connections.data_sources))
            raise ModelError("defina `default` em connections.yml ou `data_source` nos modelos")
        if name not in self.connections.data_sources:
            raise ModelError(f"origem `{name}` não está em connections.yml")
        return name

    def config(self, name: str | None = None) -> DataSourceConfig:
        return self.connections.data_sources[self._source_name(name)]

    def connector(self, name: str | None = None) -> Connector:
        key = self._source_name(name)
        if key not in self._connectors:
            self._connectors[key] = connect(self.connections.data_sources[key])
        return self._connectors[key]

    def compile(self, request: QueryRequest, principal: Principal | None = None) -> CompiledQuery:
        sources = {m.spec.data_source for m in self.graph.models.values()}
        config = self.config(next(iter(sources)) if len(sources) == 1 else None)
        compiled = Planner(self.graph, config.dialect, config.schema_map).plan(request, principal)
        actual = self.config(compiled.data_source)
        if actual is not config:
            compiled = Planner(self.graph, actual.dialect, actual.schema_map).plan(
                request, principal
            )
        return compiled

    def query(self, request: QueryRequest, principal: Principal | None = None) -> QueryResult:
        compiled = self.compile(request, principal)
        table = self.connector(compiled.data_source).execute(compiled.sql, max_rows=request.limit)
        return QueryResult(compiled, table)

    def close(self) -> None:
        for connector in self._connectors.values():
            connector.close()
        self._connectors.clear()

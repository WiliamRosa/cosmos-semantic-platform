"""Formato de autoria do Cosmos Semantic, versão 1.

Os modelos Pydantic deste módulo são a fonte da verdade do formato: o JSON Schema
publicado em ``schema/`` é gerado a partir deles (``cosmos schema``).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FORMAT_VERSION = 1

Name = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]*$", max_length=64)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(_Base):
    """Chave que liga modelos entre si; os joins são inferidos a partir delas."""

    name: Name
    type: Literal["primary", "unique", "foreign"]
    expr: str | None = Field(None, description="Expressão SQL; sem ela, usa a coluna `name`.")
    description: str | None = None


class Dimension(_Base):
    name: Name
    type: Literal["categorical", "time", "number", "boolean"]
    expr: str | None = Field(None, description="Expressão SQL; sem ela, usa a coluna `name`.")
    description: str | None = None
    sensitivity: Literal["personal", "sensitive"] | None = Field(
        None,
        description="Classificação de LGPD. Agentes nunca recebem o valor bruto dessas colunas.",
    )


class Measure(_Base):
    name: Name
    agg: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    expr: str | None = Field(
        None,
        description="Expressão SQL agregada; sem ela, `count` conta linhas e os demais usam a coluna `name`.",
    )
    description: str | None = None


class RowFilter(_Base):
    name: Name
    filter: str = Field(
        description="Expressão SQL com `{dimensao}` ou `{modelo.dimensao}` e `$user.<campo>`."
    )


class SemanticModel(_Base):
    name: Name
    table: str
    description: str | None = None
    data_source: str | None = None
    entities: list[Entity] = []
    dimensions: list[Dimension] = []
    measures: list[Measure] = []
    row_filters: list[RowFilter] = []


class _MetricBase(_Base):
    name: Name
    description: str | None = None
    label: str | None = None


class SimpleMetric(_MetricBase):
    type: Literal["simple"]
    measure: Name
    filter: str | None = None


class RatioMetric(_MetricBase):
    type: Literal["ratio"]
    numerator: Name
    denominator: Name
    filter: str | None = None


class DerivedMetric(_MetricBase):
    type: Literal["derived"]
    expr: str = Field(
        description="Expressão sobre medidas ou métricas, referenciadas como `{nome}`."
    )


class CumulativeMetric(_MetricBase):
    type: Literal["cumulative"]
    measure: Name
    grain_to_date: Literal["day", "week", "month", "quarter", "year"] | None = None
    window: str | None = Field(None, description="Janela móvel, como `7 days`. Fora do MVP.")

    @model_validator(mode="after")
    def _one_of(self) -> CumulativeMetric:
        if self.grain_to_date and self.window:
            raise ValueError("use `grain_to_date` ou `window`, não os dois")
        return self


Metric = Annotated[
    SimpleMetric | RatioMetric | DerivedMetric | CumulativeMetric, Field(discriminator="type")
]


class UserContextField(_Base):
    claim: str = Field(description="Nome da declaração no token do provedor de identidade.")
    required: bool = True
    description: str | None = None


LocalizedText = dict[str, str]


class Concept(_Base):
    """Conceito do glossário, no estilo SKOS."""

    concept: Name
    pref_label: LocalizedText
    alt_labels: dict[str, list[str]] = {}
    definition: str | None = None
    broader: Name | None = None
    related: list[Name] = []
    maps_to: str | None = Field(
        None,
        pattern=r"^(entity|dimension|metric|measure|rule):[a-z_][a-z0-9_.]*$",
        description="Membro ou regra que define o conceito: `metric:net_revenue`, `rule:cliente_ativo`.",
    )


class DecisionRow(_Base):
    when: str | None = None
    then: str | None = None
    otherwise: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> DecisionRow:
        if self.otherwise is not None:
            if self.when is not None or self.then is not None:
                raise ValueError("`otherwise` não combina com `when`/`then`")
        elif self.when is None or self.then is None:
            raise ValueError("cada linha precisa de `when` e `then`, ou só `otherwise`")
        return self


class RuleBinding(_Base):
    model: Name
    bind: dict[str, str] = Field(description="Entrada da regra -> coluna ou dimensão do modelo.")


class RuleTest(_Base):
    given: dict[str, Any]
    today: dt.date | None = None
    expect: Any


class BusinessRule(_Base):
    name: Name
    kind: Literal["predicate", "classification", "derived"]
    owner: str | None = Field(
        None, description="Área ou pessoa responsável. Obrigatório na validação."
    )
    effective_from: dt.date | None = Field(None, description="Desde quando a versão atual vale.")
    description: str | None = None
    inputs: dict[str, Literal["categorical", "time", "number", "boolean"]]
    logic: str | None = None
    decision_table: list[DecisionRow] | None = None
    applies_to: list[RuleBinding]
    tests: list[RuleTest] = []

    @model_validator(mode="after")
    def _logic(self) -> BusinessRule:
        if self.kind == "classification":
            if (self.logic is None) == (self.decision_table is None):
                raise ValueError("`classification` usa `logic` ou `decision_table`")
        elif self.logic is None or self.decision_table is not None:
            raise ValueError(f"`{self.kind}` usa `logic`")
        return self


class Project(_Base):
    """Um arquivo (ou o conjunto de arquivos) do modelo semântico."""

    version: Literal[1] = Field(description="Versão do formato de autoria.")
    semantic_models: list[SemanticModel] = []
    metrics: list[Metric] = []
    user_context: dict[Name, UserContextField] = {}
    glossary: list[Concept] = []
    business_rules: list[BusinessRule] = []
    custom_extensions: dict[str, Any] = Field(
        {}, description="Informação de outras ferramentas, preservada na importação."
    )


def json_schema() -> dict[str, Any]:
    schema = Project.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Cosmos Semantic, formato de autoria v1"
    return schema

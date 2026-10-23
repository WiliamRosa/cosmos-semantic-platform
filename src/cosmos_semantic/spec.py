"""Cosmos Semantic authoring format, version 1.

The Pydantic models in this module are the source of truth for the format: the JSON Schema
published in ``schema/`` is generated from them (``cosmos schema``).
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
    """A key that links models together; joins are inferred from entities."""

    name: Name
    type: Literal["primary", "unique", "foreign"]
    expr: str | None = Field(None, description="SQL expression; defaults to the `name` column.")
    description: str | None = None


class Dimension(_Base):
    name: Name
    type: Literal["categorical", "time", "number", "boolean"]
    expr: str | None = Field(None, description="SQL expression; defaults to the `name` column.")
    description: str | None = None
    sensitivity: Literal["personal", "sensitive"] | None = Field(
        None,
        description="LGPD (personal data) classification. AI agents never receive raw values.",
    )


class Measure(_Base):
    name: Name
    agg: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    expr: str | None = Field(
        None,
        description="SQL expression to aggregate; without it `count` counts rows and the other "
        "aggregations use the `name` column.",
    )
    description: str | None = None


class RowFilter(_Base):
    name: Name
    filter: str = Field(
        description="SQL expression with `{dimension}` or `{model.dimension}` and `$user.<field>`."
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
    expr: str = Field(description="Expression over measures or metrics, referenced as `{name}`.")


class CumulativeMetric(_MetricBase):
    type: Literal["cumulative"]
    measure: Name
    grain_to_date: Literal["day", "week", "month", "quarter", "year"] | None = None
    window: str | None = Field(
        None, description="Rolling window, such as `7 days`. Not in the MVP."
    )

    @model_validator(mode="after")
    def _one_of(self) -> CumulativeMetric:
        if self.grain_to_date and self.window:
            raise ValueError("use `grain_to_date` or `window`, not both")
        return self


Metric = Annotated[
    SimpleMetric | RatioMetric | DerivedMetric | CumulativeMetric, Field(discriminator="type")
]


class UserContextField(_Base):
    claim: str = Field(description="Claim name in the identity provider's token.")
    required: bool = True
    description: str | None = None


LocalizedText = dict[str, str]


class Concept(_Base):
    """A SKOS-style glossary concept."""

    concept: Name
    pref_label: LocalizedText
    alt_labels: dict[str, list[str]] = {}
    definition: str | None = None
    broader: Name | None = None
    related: list[Name] = []
    maps_to: str | None = Field(
        None,
        pattern=r"^(entity|dimension|metric|measure|rule):[a-z_][a-z0-9_.]*$",
        description="Member or rule that defines the concept: `metric:net_revenue`, `rule:active_customer`.",
    )


class DecisionRow(_Base):
    when: str | None = None
    then: str | None = None
    otherwise: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> DecisionRow:
        if self.otherwise is not None:
            if self.when is not None or self.then is not None:
                raise ValueError("`otherwise` cannot be combined with `when`/`then`")
        elif self.when is None or self.then is None:
            raise ValueError("each row needs `when` and `then`, or only `otherwise`")
        return self


class RuleBinding(_Base):
    model: Name
    bind: dict[str, str] = Field(description="Rule input -> model column or dimension.")


class RuleTest(_Base):
    given: dict[str, Any]
    today: dt.date | None = None
    expect: Any


class BusinessRule(_Base):
    name: Name
    kind: Literal["predicate", "classification", "derived"]
    owner: str | None = Field(
        None, description="Team or person responsible. Validation warns when it is missing."
    )
    effective_from: dt.date | None = Field(
        None, description="Since when the current version applies."
    )
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
                raise ValueError("`classification` uses either `logic` or `decision_table`")
        elif self.logic is None or self.decision_table is not None:
            raise ValueError(f"`{self.kind}` uses `logic`")
        return self


class Project(_Base):
    """One file (or the merged set of files) of a semantic model."""

    version: Literal[1] = Field(description="Authoring format version.")
    timezone: str | None = Field(
        None,
        description=(
            'Business time zone, such as `America/Sao_Paulo`. Defines what "today" means for '
            "rules and filters using CURRENT_DATE, identically on every data source. Default: UTC."
        ),
    )
    semantic_models: list[SemanticModel] = []
    metrics: list[Metric] = []
    user_context: dict[Name, UserContextField] = {}
    glossary: list[Concept] = []
    business_rules: list[BusinessRule] = []
    custom_extensions: dict[str, Any] = Field(
        {}, description="Information from other tools, preserved on import."
    )


def json_schema() -> dict[str, Any]:
    schema = Project.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Cosmos Semantic authoring format v1"
    return schema

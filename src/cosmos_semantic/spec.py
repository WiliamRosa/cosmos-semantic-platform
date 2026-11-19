"""Cosmos Semantic authoring format, version 1.

The Pydantic models in this module are the source of truth for the format: the JSON Schema
published in ``schema/`` is generated from them (``cosmos schema``).
"""

from __future__ import annotations

import datetime as dt
import re
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
    owner: str | None = Field(None, description="Team responsible for the data.")
    data_source: str | None = None
    entities: list[Entity] = []
    dimensions: list[Dimension] = []
    measures: list[Measure] = []
    row_filters: list[RowFilter] = []


class _MetricBase(_Base):
    name: Name
    description: str | None = None
    label: str | None = None
    owner: str | None = Field(None, description="Team responsible for the definition.")


class SimpleMetric(_MetricBase):
    type: Literal["simple"]
    measure: Name
    filter: str | None = None


class RatioMetric(_MetricBase):
    type: Literal["ratio"]
    numerator: Name
    denominator: Name
    filter: str | None = None


# Query grains each offset unit fits (DEC-18), with how many units make one period of the
# grain: an offset must be a whole number of periods, so a shifted period start is the start
# of another period. Only days and months, for now.
OFFSET_GRAINS = {"day": {"day": 1, "week": 7}, "month": {"month": 1, "quarter": 3, "year": 12}}


def parse_offset(text: str) -> tuple[int, str] | None:
    """``(N, unit)`` for an offset of ``N days`` or ``N months`` (singular too); None for
    anything else."""
    match = re.fullmatch(r"\s*(\d+)\s+(day|month)s?\s*", text or "", re.IGNORECASE)
    if not match or int(match.group(1)) <= 0:
        return None
    return int(match.group(1)), match.group(2).lower()


def offset_problem(text: str) -> str | None:
    """Why an offset cannot be read; None when it can. Validation and the planner agree."""
    if parse_offset(text) is None:
        return (
            f"offset `{text}` is not supported; use a whole number of days or months, "
            "such as `7 days` or `1 month`"
        )
    return None


def offset_grain_problem(text: str, grain: str | None) -> str | None:
    """Why an offset cannot be applied at the query's time ``grain``; None when it can."""
    parsed = parse_offset(text)
    if parsed is None:
        return offset_problem(text)
    amount, unit = parsed
    size = OFFSET_GRAINS[unit].get(grain or "")
    if size is not None and amount % size == 0:
        return None
    fits = ", ".join(
        f"by {g}" if n == 1 else f"by {g} (in multiples of {n} {unit}s)"
        for g, n in OFFSET_GRAINS[unit].items()
    )
    found = f"by {grain}" if grain else "to a time dimension without a grain"
    return f"an offset of `{text}` cannot be applied {found}; query it {fits}"


class OffsetInput(_Base):
    """A derived metric's input read at an earlier period (DEC-18)."""

    metric: Name = Field(
        description="Measure or metric referenced in `expr` as `{name}`, read shifted in time."
    )
    offset: str = Field(
        description=(
            "How far back, such as `1 month` or `7 days`: the value for a period is the "
            "input's value for the period that starts that much earlier, at the query's time "
            "grain. Only days and months, for now."
        )
    )


class DerivedMetric(_MetricBase):
    type: Literal["derived"]
    expr: str = Field(description="Expression over measures or metrics, referenced as `{name}`.")
    inputs: list[OffsetInput] = Field(
        [],
        description=(
            "References of `expr` read at an earlier period, each with its `offset`; the "
            "others are read at the same period. Queries need exactly one time dimension."
        ),
    )

    def offsets(self) -> dict[str, str]:
        """Referenced name -> offset, for the references read at an earlier period."""
        return {i.metric: i.offset for i in self.inputs}


# Aggregations whose per-period values combine over a rolling window or a running (to-date)
# cumulative metric (DEC-21, DEC-22): sums and counts add up, minima and maxima take the
# extreme. An average or a distinct count cannot be rebuilt from per-period values.
WINDOW_AGGS = ("sum", "count", "min", "max")


def window_agg_refusal(agg: str) -> str | None:
    """Why a rolling window cannot run over a measure with this aggregation; None if it can.
    Model validation and the planner give the same reason."""
    if agg in WINDOW_AGGS:
        return None
    return (
        f"a rolling window over a `{agg}` measure is not supported; "
        "it cannot be combined from daily values (use sum, count, min or max)"
    )


class CumulativeMetric(_MetricBase):
    type: Literal["cumulative"]
    measure: Name
    grain_to_date: Literal["day", "week", "month", "quarter", "year"] | None = None
    window: str | None = Field(
        None,
        description=(
            "Rolling window, such as `7 days`: the value on day D aggregates the measure over "
            "the days D - (N - 1) to D. Only windows in days, queried at day grain, for now."
        ),
    )

    @model_validator(mode="after")
    def _one_of(self) -> CumulativeMetric:
        if self.grain_to_date and self.window:
            raise ValueError("use `grain_to_date` or `window`, not both")
        return self

    def window_days(self) -> int | None:
        """N for a `window` of `N days` (or `1 day`); None for any other window."""
        match = re.fullmatch(r"\s*(\d+)\s+days?\s*", self.window or "", re.IGNORECASE)
        return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def cumulative_agg_problem(metric: CumulativeMetric, agg: str) -> str | None:
    """Why a running cumulative metric over a measure with ``agg`` cannot be computed, or
    None when it can. Rolling windows are checked by ``window_agg_refusal``. Model validation
    and the planner give the same reason."""
    if metric.window or agg in WINDOW_AGGS:
        return None
    period = f"a {metric.grain_to_date}-to-date" if metric.grain_to_date else "a running total"
    return (
        f"{period} over a `{agg}` measure is not supported; it cannot be accumulated from "
        "per-period values (use a sum, count, min or max measure)"
    )


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


class RuleVersion(_Base):
    """An earlier version of a business rule, kept so past numbers can be recomputed."""

    effective_from: dt.date = Field(description="Since when this version applied.")
    change_note: str | None = Field(None, description="Why the next version replaced it.")
    description: str | None = None
    logic: str | None = None
    decision_table: list[DecisionRow] | None = None
    tests: list[RuleTest] = []


def _check_logic(kind: str, logic: str | None, table: list[DecisionRow] | None, where: str) -> None:
    if kind == "classification":
        if (logic is None) == (table is None):
            raise ValueError(f"{where}`classification` uses either `logic` or `decision_table`")
    elif logic is None or table is not None:
        raise ValueError(f"{where}`{kind}` uses `logic`")


class BusinessRule(_Base):
    name: Name
    kind: Literal["predicate", "classification", "derived"]
    owner: str | None = Field(
        None, description="Team or person responsible. Validation warns when it is missing."
    )
    effective_from: dt.date | None = Field(
        None, description="Since when the current version applies."
    )
    change_note: str | None = Field(None, description="What changed in the current version.")
    description: str | None = None
    inputs: dict[str, Literal["categorical", "time", "number", "boolean"]]
    logic: str | None = None
    decision_table: list[DecisionRow] | None = None
    applies_to: list[RuleBinding]
    tests: list[RuleTest] = []
    history: list[RuleVersion] = Field(
        [],
        description=(
            "Earlier versions. A query uses the version in effect on its `rules_as_of` date "
            "(default: `as_of`); inputs and bindings are shared by all versions."
        ),
    )

    @model_validator(mode="after")
    def _logic(self) -> BusinessRule:
        _check_logic(self.kind, self.logic, self.decision_table, "")
        for old in self.history:
            where = f"history {old.effective_from}: "
            _check_logic(self.kind, old.logic, old.decision_table, where)
            if self.effective_from is None or old.effective_from >= self.effective_from:
                raise ValueError(
                    f"{where}earlier versions must start before the current `effective_from`"
                )
        dates = [old.effective_from for old in self.history]
        if len(set(dates)) != len(dates):
            raise ValueError("two versions in `history` share the same `effective_from`")
        return self

    def versions(self) -> list[RuleVersion]:
        """Every version, oldest first; the last one is the current version."""
        current = RuleVersion.model_construct(
            effective_from=self.effective_from,
            change_note=self.change_note,
            description=self.description,
            logic=self.logic,
            decision_table=self.decision_table,
            tests=self.tests,
        )
        return [*sorted(self.history, key=lambda v: v.effective_from), current]

    def version_at(self, day: dt.date) -> RuleVersion:
        """The version in effect on ``day``; before the first version, the first one."""
        versions = self.versions()
        chosen = versions[0]
        for version in versions:
            if version.effective_from is not None and version.effective_from <= day:
                chosen = version
        return chosen if self.history else versions[-1]


class Team(_Base):
    """Who owns definitions, data and processes."""

    name: str
    description: str | None = None
    contact: str | None = Field(None, description="E-mail, channel or page, for questions.")


class System(_Base):
    """Where data comes from or goes to: an application, a pipeline, a warehouse, a BI tool."""

    name: Name
    kind: Literal["application", "database", "warehouse", "pipeline", "bi", "api", "other"] = (
        "other"
    )
    owner: str | None = None
    description: str | None = None
    feeds: list[Name] = Field([], description="Semantic models whose data originates here.")


class Process(_Base):
    """A business process and the definitions it depends on."""

    name: Name
    owner: str | None = None
    description: str | None = None
    systems: list[Name] = Field([], description="Systems the process runs on.")
    uses: list[str] = Field(
        [],
        description=(
            "Members the process relies on, as `kind:name`: metric, measure, rule, concept, "
            "model or dimension (`dimension:model.name`)."
        ),
    )


_MAX_AGE_UNITS = {"hour": "hours", "day": "days", "week": "weeks"}


def parse_max_age(text: str) -> dt.timedelta | None:
    """The age in ``N hours``, ``N days`` or ``N weeks`` (singular too); None for anything
    else."""
    match = re.fullmatch(r"\s*(\d{1,4})\s+(hour|day|week)s?\s*", text or "", re.IGNORECASE)
    if not match or int(match.group(1)) <= 0:
        return None
    return dt.timedelta(**{_MAX_AGE_UNITS[match.group(2).lower()]: int(match.group(1))})


class HealthSettings(_Base):
    """How member health is judged (``explain`` and the console)."""

    max_age: str | None = Field(
        None,
        description=(
            "How old the last reconciliation of a metric may be, such as `7 days`, before a "
            "healthy metric needs attention (`reconciliation_stale`). Hours, days or weeks. "
            "Default: no limit."
        ),
    )

    @model_validator(mode="after")
    def _check_max_age(self) -> HealthSettings:
        if self.max_age is not None and parse_max_age(self.max_age) is None:
            raise ValueError(
                f"health.max_age `{self.max_age}` is not supported; use a whole number of "
                "hours, days or weeks, such as `7 days`"
            )
        return self


class Project(_Base):
    """One file (or the merged set of files) of a semantic model."""

    version: Literal[1] = Field(description="Authoring format version.")
    timezone: str | None = Field(
        None,
        description=(
            'Business time zone, such as `America/New_York`. Defines what "today" means for '
            "rules and filters using CURRENT_DATE, identically on every data source. Default: UTC."
        ),
    )
    health: HealthSettings | None = Field(
        None, description="How member health is judged, such as `max_age: 7 days`."
    )
    semantic_models: list[SemanticModel] = []
    metrics: list[Metric] = []
    user_context: dict[Name, UserContextField] = {}
    glossary: list[Concept] = []
    business_rules: list[BusinessRule] = []
    teams: list[Team] = []
    systems: list[System] = []
    processes: list[Process] = []
    custom_extensions: dict[str, Any] = Field(
        {}, description="Information from other tools, preserved on import."
    )


def json_schema() -> dict[str, Any]:
    schema = Project.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Cosmos Semantic authoring format v1"
    return schema

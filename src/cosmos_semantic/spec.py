"""Cosmos Semantic authoring format, version 1.

The Pydantic models in this module are the source of truth for the format: the JSON Schema
published in ``schema/`` is generated from them (``cosmos schema``).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

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
    column_type: Literal["timestamp"] | None = Field(
        None,
        description=(
            "Time dimensions only: `timestamp` marks a column that holds an instant in UTC, "
            "such as a BigQuery `TIMESTAMP`. On BigQuery the engine reads it as its UTC date "
            "and time (`DATETIME(column, 'UTC')`); other data sources ignore it. Default: a "
            "date or a date and time without a time zone."
        ),
    )

    @model_validator(mode="after")
    def _column_type_on_time(self) -> Dimension:
        if self.column_type is not None and self.type != "time":
            raise ValueError(
                f"dimension `{self.name}`: `column_type` applies to time dimensions only"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_unset_column_type(self, handler) -> dict[str, Any]:
        # Without a column type the dump (and so the model hash) stays as it was before
        # DEC-57, so evidence and replay of existing models do not change.
        data = handler(self)
        if self.column_type is None and isinstance(data, dict):
            data.pop("column_type", None)
        return data


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


# The tolerance reconciliation uses when a metric declares none (DEC-39): equal up to float
# noise. Kept here so the spec, the reconciler and the docs state the same default.
DEFAULT_ABS_TOLERANCE = 1e-6
DEFAULT_REL_TOLERANCE = 1e-9

Tolerance = Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]


class ReconciliationSettings(_Base):
    """How close a legacy number must be to the governed one to agree (DEC-39)."""

    abs_tolerance: Tolerance | None = Field(
        None,
        description=(
            "Largest absolute difference that still agrees, such as `0.01` for a report "
            f"rounded to cents. Default: {DEFAULT_ABS_TOLERANCE:g}."
        ),
    )
    rel_tolerance: Tolerance | None = Field(
        None,
        description=(
            "Largest difference relative to the larger of the two values that still agrees, "
            f"such as `0.001` for 0.1%. Default: {DEFAULT_REL_TOLERANCE:g}."
        ),
    )

    @model_validator(mode="after")
    def _not_empty(self) -> ReconciliationSettings:
        if self.abs_tolerance is None and self.rel_tolerance is None:
            raise ValueError("`reconciliation` needs `abs_tolerance`, `rel_tolerance` or both")
        return self

    def tolerances(self) -> tuple[float, float]:
        """``(abs, rel)``, with the default for the one left out."""
        return (
            DEFAULT_ABS_TOLERANCE if self.abs_tolerance is None else self.abs_tolerance,
            DEFAULT_REL_TOLERANCE if self.rel_tolerance is None else self.rel_tolerance,
        )


class _MetricBase(_Base):
    name: Name
    description: str | None = None
    label: str | None = None
    owner: str | None = Field(None, description="Team responsible for the definition.")
    reconciliation: ReconciliationSettings | None = Field(
        None,
        description=(
            "Tolerance for reconciliation and check runs, such as "
            "`{abs_tolerance: 0.01, rel_tolerance: 0.001}`: a legacy value agrees when it is "
            "within either one. Default: equal up to float noise."
        ),
    )

    @model_serializer(mode="wrap")
    def _omit_unset_reconciliation(self, handler) -> dict[str, Any]:
        # Without a declared tolerance the dump (and so the model hash) stays as it was
        # before DEC-39, so evidence and replay of existing models do not change.
        data = handler(self)
        if self.reconciliation is None and isinstance(data, dict):
            data.pop("reconciliation", None)
        return data


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
# of another period. Offsets are in days or months; weeks and years are read as 7 days and
# 12 months (DEC-46).
OFFSET_GRAINS = {"day": {"day": 1, "week": 7}, "month": {"month": 1, "quarter": 3, "year": 12}}

# Units an offset may be written in, as (base unit, how many base units one of them is).
OFFSET_UNITS = {"day": ("day", 1), "week": ("day", 7), "month": ("month", 1), "year": ("month", 12)}


def parse_offset(text: str) -> tuple[int, str] | None:
    """``(N, unit)`` for an offset of ``N days`` or ``N months`` (singular too), with weeks
    read as 7 days and years as 12 months (``2 weeks`` is ``(14, "day")``, ``1 year`` is
    ``(12, "month")``); None for anything else. The planner works on this normalized form
    only, so an offset in weeks or years compiles to the SQL of its days or months; evidence
    and messages keep the offset as written."""
    match = re.fullmatch(r"\s*(\d+)\s+(day|week|month|year)s?\s*", text or "", re.IGNORECASE)
    if not match or int(match.group(1)) <= 0:
        return None
    unit, size = OFFSET_UNITS[match.group(2).lower()]
    return int(match.group(1)) * size, unit


def offset_problem(text: str) -> str | None:
    """Why an offset cannot be read; None when it can. Validation and the planner agree."""
    if parse_offset(text) is None:
        return (
            f"offset `{text}` is not supported; use a whole number of days, weeks, months or "
            "years, such as `7 days`, `2 weeks`, `1 month` or `1 year`"
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
            "grain. In days, weeks (7 days), months or years (12 months)."
        )
    )
    alias: Name | None = Field(
        None,
        description=(
            "Name `expr` uses for this shifted read, as `{alias}`, so the expression can also "
            "read `metric` at the same period, such as `revenue_prev` for `revenue` read "
            "`1 month` earlier (DEC-44). Without it, every `{metric}` in `expr` is shifted."
        ),
    )

    @model_serializer(mode="wrap")
    def _omit_unset_alias(self, handler) -> dict[str, Any]:
        # Without an alias the dump (and so the model hash) stays as it was before DEC-44.
        data = handler(self)
        if self.alias is None and isinstance(data, dict):
            data.pop("alias", None)
        return data

    @property
    def ref(self) -> str:
        """The name ``expr`` reads this input by: its alias, or else the metric's name."""
        return self.alias or self.metric


class DerivedMetric(_MetricBase):
    type: Literal["derived"]
    expr: str = Field(description="Expression over measures or metrics, referenced as `{name}`.")
    inputs: list[OffsetInput] = Field(
        [],
        description=(
            "References of `expr` read at an earlier period, each with its `offset`; the "
            "others are read at the same period. Queries need exactly one time dimension. "
            "Give an input an `alias` to read the same metric both shifted and unshifted."
        ),
    )

    def offsets(self) -> dict[str, str]:
        """Name used in ``expr`` (an input's alias, or else its metric) -> offset, for the
        references read at an earlier period."""
        return {i.ref: i.offset for i in self.inputs}

    def aliases(self) -> dict[str, str]:
        """Alias -> the metric or measure it reads, for the inputs with an alias (DEC-44)."""
        return {i.alias: i.metric for i in self.inputs if i.alias}

    def target(self, ref: str) -> str:
        """The metric or measure a name used in ``expr`` reads: an alias's metric, or the
        name itself."""
        return self.aliases().get(ref, ref)


def input_problems(metric: DerivedMetric, refs: set[str], taken: set[str]) -> list[str]:
    """Why the ``inputs`` of a derived metric cannot be read, apart from their offsets: each
    must be named once, used in ``expr`` (whose references are ``refs``), and an alias must not
    be the name of a measure or metric in ``taken`` (DEC-18, DEC-44). Empty when they can."""
    problems: list[str] = []
    seen: dict[str, OffsetInput] = {}
    for item in metric.inputs:
        first = seen.setdefault(item.ref, item)
        if first is item:
            continue
        if item.alias is None and first.alias is None:
            problems.append(
                f"`{item.metric}` is listed more than once in `inputs`; to read it at "
                "several periods, give each input of it an `alias` and read it in `expr` "
                "by that name"
            )
        else:
            problems.append(f"`{item.ref}` names more than one input; use another `alias`")
    for item in metric.inputs:
        if item.alias is not None and item.alias in taken:
            problems.append(
                f"alias `{item.alias}` is already the name of a measure or metric; choose "
                f"another, so `{{{item.alias}}}` in `expr` reads one thing"
            )
        elif item.ref not in refs:
            if item.alias is None:
                problems.append(f"input `{item.metric}` is not referenced in `expr`")
            else:
                problems.append(
                    f"alias `{item.alias}` (input `{item.metric}`) is not referenced in `expr`"
                )
    return problems


# Units a rolling window may be written in, as days per unit: weeks are read as 7 days
# (DEC-27), as offsets read them (DEC-46).
WINDOW_UNITS = {"day": 1, "week": 7}


def window_parts(text: str | None) -> tuple[int, str] | None:
    """``(N, unit)`` for a rolling window of ``N days`` or ``N weeks`` (singular too), with the
    unit as written (``2 weeks`` is ``(2, "week")``); None for anything else, such as `1 month`
    or `0 days`. The exports write a window in its own unit; the planner reads its days."""
    match = re.fullmatch(r"\s*(\d+)\s+(day|week)s?\s*", text or "", re.IGNORECASE)
    if not match or int(match.group(1)) <= 0:
        return None
    return int(match.group(1)), match.group(2).lower()


def parse_window(text: str | None) -> int | None:
    """The days of a rolling window of ``N days`` or ``N weeks`` (``2 weeks`` is 14); None
    for anything else."""
    parts = window_parts(text)
    return parts[0] * WINDOW_UNITS[parts[1]] if parts else None


def parse_month_window(text: str | None) -> int | None:
    """N for a rolling window of ``N months`` (``1 month`` too, DEC-59); None for anything
    else, such as `7 days` or `0 months`. A month window is a range of calendar months, queried
    at month grain; the exports and imports do not carry it."""
    match = re.fullmatch(r"\s*(\d+)\s+months?\s*", text or "", re.IGNORECASE)
    if not match or int(match.group(1)) <= 0:
        return None
    return int(match.group(1))


# Aggregations whose per-period values combine over a rolling window or a running (to-date)
# cumulative metric (DEC-21, DEC-22): sums and counts add up, minima and maxima take the
# extreme. An average or a distinct count cannot be rebuilt from per-period values.
WINDOW_AGGS = ("sum", "count", "min", "max")
# Aggregations a rolling window recomputes over each window from the facts instead (DEC-49): a
# customer seen on several days of the window counts once. Running totals still refuse them.
RECOMPUTED_WINDOW_AGGS = ("count_distinct",)


def window_agg_refusal(agg: str, in_months: bool = False) -> str | None:
    """Why a rolling window cannot run over a measure with this aggregation; None if it can.
    A window in months (DEC-59, ``in_months``) only combines monthly values, so it refuses a
    distinct count too. Model validation and the planner give the same reason."""
    if in_months and agg not in WINDOW_AGGS:
        return (
            f"a rolling window in months over a `{agg}` measure is not supported; "
            "it is combined from monthly values (use sum, count, min or max, or a window "
            "in days or weeks for count_distinct)"
        )
    if agg in WINDOW_AGGS or agg in RECOMPUTED_WINDOW_AGGS:
        return None
    return (
        f"a rolling window over a `{agg}` measure is not supported; "
        "it cannot be combined from daily values (use sum, count, count_distinct, min or max)"
    )


def combined_window_refusal(agg: str) -> str | None:
    """Why a rolling window over a measure with this aggregation is not carried by the
    interchange formats (dbt, Cube, Power BI), which only translate windows combined from
    daily values; None if it is. A `count_distinct` window runs in the engine (DEC-49), but
    the imports and exports keep refusing it until their results are checked against it."""
    if agg in RECOMPUTED_WINDOW_AGGS:
        return (
            f"a rolling window over a `{agg}` measure is recomputed from the facts of each "
            "window in Cosmos (DEC-49), and the interchange formats do not carry it until "
            "their results are checked against the engine's"
        )
    return window_agg_refusal(agg)


# Why the dbt and Cube imports skip a rolling window in months (DEC-59), as they skip a
# distinct-count window (DEC-49): the engine computes it by calendar months, which the source
# tool may not (MetricFlow subtracts the interval from each day of a time spine), and no import
# is checked against the source tool's numbers yet.
MONTH_WINDOW_IMPORT_REFUSAL = (
    "a rolling window in months is computed by calendar months in Cosmos since DEC-59, and "
    "the import is not checked against the source tool's numbers"
)


class CumulativeMetric(_MetricBase):
    type: Literal["cumulative"]
    measure: Name
    grain_to_date: Literal["day", "week", "month", "quarter", "year"] | None = None
    window: str | None = Field(
        None,
        description=(
            "Rolling window, such as `7 days`: the value on day D aggregates the measure over "
            "the days D - (N - 1) to D. In days or weeks (7 days), queried at day grain; or in "
            "months, such as `3 months`, queried at month grain: the value of month M "
            "aggregates the months M - (N - 1) to M."
        ),
    )

    @model_validator(mode="after")
    def _one_of(self) -> CumulativeMetric:
        if self.grain_to_date and self.window:
            raise ValueError("use `grain_to_date` or `window`, not both")
        return self

    def window_days(self) -> int | None:
        """N for a `window` of `N days` (or `1 day`), 7N for one of `N weeks` (or `1 week`,
        DEC-27); None for any other window. The planner and the exports read only these days,
        so a window in weeks compiles to the SQL of its days; evidence and messages keep the
        window as written."""
        return parse_window(self.window)

    def window_months(self) -> int | None:
        """N for a `window` of `N months` (or `1 month`, DEC-59); None for any other window."""
        return parse_month_window(self.window)


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

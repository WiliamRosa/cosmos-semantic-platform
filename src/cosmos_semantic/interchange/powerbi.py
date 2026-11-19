"""Importing Power BI semantic models (ADR 0009): ``.bim`` (TMSL JSON) or TMDL folders.

Tables become semantic models, with the physical table read from the partition (Power Query
navigation, a native SQL query or a legacy query partition). Active many-to-one relationships
become entities. DAX measures are translated when they map onto the Cosmos metric types:

| DAX | Cosmos |
| --- | --- |
| ``SUM``, ``AVERAGE``, ``MIN``, ``MAX``, ``COUNT``, ``DISTINCTCOUNT``, ``DISTINCTCOUNTNOBLANK``, ``COUNTROWS`` | measure |
| ``SUMX``, ``AVERAGEX``, ``MINX``, ``MAXX`` over the measure's own table, with ``+ - *`` over its numeric columns | ``sum``, ``avg``, ``min``, ``max`` measure |
| ``DIVIDE(a, b)`` | ratio metric |
| ``CALCULATE(m, T[c] = "x", ...)`` with simple column filters | metric with ``filter`` |
| ``TOTALYTD``, ``TOTALQTD``, ``TOTALMTD`` over a sum, count, min or max measure | cumulative metric |
| ``CALCULATE(m, DATESINPERIOD(D[c], MAX(D[c]), -N, DAY))`` over such a measure | ``window: N days`` |
| ``CALCULATE(m, SAMEPERIODLASTYEAR(D[c]))``, ``DATEADD(D[c], -N, unit)``, ``PARALLELPERIOD(D[c], -N, MONTH)`` | derived metric with ``inputs: [{metric, offset}]`` |
| arithmetic over measures (``[A] - [B]``) | derived metric |

Everything else (other iterators such as ``COUNTX``; ``SUMX``, ``AVERAGEX``, ``MINX`` or
``MAXX`` over another table, with a division, a function or ``RELATED``; ``TOTALYTD``,
``TOTALQTD`` or ``TOTALMTD`` over an average or a distinct count; ``DATESINPERIOD`` in
other units or from another anchor, ``DATESBETWEEN``; ``DATEADD`` forward, ``PARALLELPERIOD``
by quarter or year, period shifts over the fact table's own date column; ``FILTER``, ``ALL``,
variables, calculated columns and tables, calculation groups, row-level security roles) is
reported as a warning; nothing is dropped silently. No DAX is ever executed.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cosmos_semantic.interchange import ImportResult
from cosmos_semantic.interchange.cube import _table_from_sql
from cosmos_semantic.spec import OFFSET_GRAINS, WINDOW_AGGS, Project, window_agg_refusal

_DATA_TYPES = {
    "string": "categorical",
    "int64": "number",
    "decimal": "number",
    "double": "number",
    "currency": "number",
    "datetime": "time",
    "date": "time",
    "boolean": "boolean",
}
_AGGS = {
    "SUM": "sum",
    "AVERAGE": "avg",
    "MIN": "min",
    "MAX": "max",
    "COUNT": "count",
    "COUNTA": "count",
    "DISTINCTCOUNT": "count_distinct",
    "DISTINCTCOUNTNOBLANK": "count_distinct",
}
_TO_DATE = {"TOTALYTD": "year", "TOTALQTD": "quarter", "TOTALMTD": "month"}
# Iterators over the measure's own table: like SQL, AVERAGEX, MINX and MAXX skip BLANK rows,
# and SUMX, AVERAGEX, MINX and MAXX over only BLANK rows are BLANK.
_ITERATORS = {"SUMX": "sum", "AVERAGEX": "avg", "MINX": "min", "MAXX": "max"}
# Period shifts read as an offset input (DEC-35): months per DAX unit; DAY stays in days.
_SHIFTS = ("SAMEPERIODLASTYEAR", "DATEADD", "PARALLELPERIOD")
_SHIFT_MONTHS = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}
_SQL_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ident(name: str) -> str:
    """A Power BI display name as a Cosmos identifier: ``'Order Items'`` -> ``order_items``."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unnamed"
    return f"_{text}" if text[0].isdigit() else text[:64]


# -- reading ----------------------------------------------------------------------------


def read_model(path: str | Path) -> dict[str, Any]:
    """The ``model`` object of a ``.bim`` file, or the same shape built from TMDL files."""
    root = Path(path)
    if root.is_dir():
        bims = sorted(root.rglob("*.bim"))
        if bims:
            root = bims[0]
        else:
            files = sorted(root.rglob("*.tmdl"))
            if not files:
                raise ValueError(f"{path}: no .bim or .tmdl files found")
            return _tmdl_model(files)
    if root.suffix == ".tmdl":
        return _tmdl_model([root])
    data = json.loads(root.read_text(encoding="utf-8-sig"))
    return data.get("model", data)


@dataclass
class _Node:
    kind: str
    name: str
    value: str | None = None
    props: dict[str, Any] = field(default_factory=dict)
    children: list[_Node] = field(default_factory=list)
    description: str | None = None


_OBJECTS = {
    "model",
    "table",
    "column",
    "measure",
    "partition",
    "relationship",
    "role",
    "tablePermission",
    "columnPermission",
    "hierarchy",
    "level",
    "calculationGroup",
    "calculationItem",
    "annotation",
    "extendedProperty",
    "perspective",
    "perspectiveTable",
    "perspectiveColumn",
    "perspectiveMeasure",
    "culture",
    "expression",
    "dataSource",
    "ref",
    "linguisticMetadata",
    "variation",
    "changedProperty",
    "queryGroup",
    "database",
}
_DECL = re.compile(r"^(\w+)\s+('(?:[^']|'')*'|[^\s=]+)\s*(?:=\s*(.*))?$")
_PROP = re.compile(r"^(\w+)\s*(:|=)\s*(.*)$")


def _indent(line: str) -> int:
    expanded = line.replace("    ", "\t")
    return len(expanded) - len(expanded.lstrip("\t"))


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == "'":
        return name[1:-1].replace("''", "'")
    return name


def _parse_tmdl(text: str) -> list[_Node]:
    lines = text.splitlines()
    roots: list[_Node] = []
    stack: list[tuple[int, _Node]] = []
    description: list[str] = []
    i = 0

    def block(start: int, base: int, opened: bool = False) -> tuple[str, int]:
        """Collects an expression continued on deeper lines, or a ``` fenced block (opened on
        the declaration line, or on the next one)."""
        out, j = [], start
        if opened or (j < len(lines) and lines[j].strip().startswith("```")):
            j += 0 if opened else 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                out.append(lines[j])
                j += 1
            return "\n".join(out).strip(), j + 1
        while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) >= base + 2):
            out.append(lines[j])
            j += 1
        return "\n".join(out).strip(), j

    while i < len(lines):
        raw = lines[i]
        text_line = raw.strip()
        if not text_line:
            i += 1
            continue
        depth = _indent(raw)
        if text_line.startswith("///"):
            description.append(text_line[3:].strip())
            i += 1
            continue
        while stack and stack[-1][0] >= depth:
            stack.pop()
        ref = re.match(r"^ref\s+table\s+(.+)$", text_line)
        if ref:  # `ref table X` in model.tmdl fixes the table order
            roots.append(_Node("reftable", _unquote(ref.group(1))))
            i += 1
            continue
        decl = _DECL.match(text_line)
        if decl and decl.group(1) in _OBJECTS:
            kind, name, value = decl.group(1), _unquote(decl.group(2)), decl.group(3)
            i += 1
            if value is not None and (not value.strip() or value.strip() == "```"):
                value, i = block(i, depth, opened=value.strip() == "```")
            node = _Node(kind, name, value, description=" ".join(description) or None)
            description = []
            (stack[-1][1].children if stack else roots).append(node)
            stack.append((depth, node))
            continue
        description = []
        prop = _PROP.match(text_line)
        i += 1
        if not stack:
            continue
        if prop:
            key, sep, value = prop.groups()
            if sep == "=" and (not value.strip() or value.strip() == "```"):
                value, i = block(i, depth, opened=value.strip() == "```")
            stack[-1][1].props[key] = value.strip()
        else:
            stack[-1][1].props[text_line] = True  # a flag such as `isHidden`
    return roots


def _split_ref(ref: str) -> tuple[str, str]:
    """``'Order Items'.'Order Id'`` or ``Sales.CustomerKey`` -> (table, column)."""
    match = re.match(r"^('(?:[^']|'')*'|[^.]+)\.(.+)$", ref.strip())
    if not match:
        return ref, ""
    return _unquote(match.group(1)), _unquote(match.group(2))


def _tmdl_model(files: list[Path]) -> dict[str, Any]:
    nodes: list[_Node] = []
    for file in files:
        nodes += _parse_tmdl(file.read_text(encoding="utf-8-sig"))
    flat: list[_Node] = []
    for node in nodes:
        flat += node.children if node.kind in ("model", "database") else [node]
        if node.kind in ("model", "database"):
            flat.append(_Node("modelprops", node.name, props=node.props))
    model: dict[str, Any] = {"tables": [], "relationships": [], "roles": []}
    for node in flat:
        if node.kind == "table":
            model["tables"].append(_tmdl_table(node))
        elif node.kind == "relationship":
            p = node.props
            from_table, from_column = _split_ref(p.get("fromColumn", ""))
            to_table, to_column = _split_ref(p.get("toColumn", ""))
            item = {
                "name": node.name,
                "fromTable": from_table,
                "fromColumn": from_column,
                "toTable": to_table,
                "toColumn": to_column,
            }
            for key in ("fromCardinality", "toCardinality", "crossFilteringBehavior"):
                if key in p:
                    item[key] = p[key]
            if "isActive" in p:
                item["isActive"] = str(p["isActive"]).lower() != "false"
            model["relationships"].append(item)
        elif node.kind == "role":
            model["roles"].append(
                {
                    "name": node.name,
                    "tablePermissions": [
                        {"name": c.name, "filterExpression": c.value}
                        for c in node.children
                        if c.kind == "tablePermission"
                    ],
                }
            )
    order = [n.name for n in flat if n.kind == "reftable"]
    if order:
        rank = {name: i for i, name in enumerate(order)}
        model["tables"].sort(key=lambda t: rank.get(t["name"], len(rank)))
    return model


def _tmdl_table(node: _Node) -> dict[str, Any]:
    table: dict[str, Any] = {
        "name": node.name,
        "columns": [],
        "measures": [],
        "partitions": [],
        "hierarchies": [],
    }
    if node.description:
        table["description"] = node.description
    if node.props.get("isHidden"):
        table["isHidden"] = True
    for child in node.children:
        p = child.props
        if child.kind == "column":
            column = {"name": child.name, "dataType": p.get("dataType", "string")}
            if child.value is not None:
                column.update(type="calculated", expression=child.value)
            if "sourceColumn" in p:
                column["sourceColumn"] = _unquote(p["sourceColumn"])
            if p.get("isHidden"):
                column["isHidden"] = True
            if child.description:
                column["description"] = child.description
            table["columns"].append(column)
        elif child.kind == "measure":
            measure = {"name": child.name, "expression": child.value or ""}
            if child.description:
                measure["description"] = child.description
            table["measures"].append(measure)
        elif child.kind == "partition":
            kind = (child.value or "m").strip()
            source: dict[str, Any] = {"type": kind}
            if "source" in p:
                key = "query" if kind == "query" else "expression"
                source[key] = p["source"]
            if "query" in p:
                source["query"] = p["query"]
            table["partitions"].append({"name": child.name, "source": source})
        elif child.kind == "hierarchy":
            table["hierarchies"].append({"name": child.name})
        elif child.kind == "calculationGroup":
            table["calculationGroup"] = {}
    return table


# -- physical tables --------------------------------------------------------------------

_SCHEMA_ITEM = re.compile(
    r'\[\s*(?:Schema\s*=\s*"(?P<s1>[^"]+)"\s*,\s*Item\s*=\s*"(?P<i1>[^"]+)"'
    r'|Item\s*=\s*"(?P<i2>[^"]+)"\s*,\s*Schema\s*=\s*"(?P<s2>[^"]+)")'
)
_NAME_KIND = re.compile(r'\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"([^"]+)"\s*\]')
_M_STRING = re.compile(r'"((?:[^"]|"")*)"')


def physical_table(source: dict[str, Any]) -> str | None:
    """The table a partition reads, when it can be determined without running Power Query."""
    if source.get("type") == "query" or "query" in source:
        query = source.get("query")
        if isinstance(query, list):
            query = "\n".join(query)
        return _table_from_sql(query) if query else None
    expression = source.get("expression")
    if isinstance(expression, list):
        expression = "\n".join(expression)
    if not expression:
        return None
    match = _SCHEMA_ITEM.search(expression)
    if match:
        return f"{match['s1'] or match['s2']}.{match['i1'] or match['i2']}"
    names = [name for name, kind in _NAME_KIND.findall(expression)]
    kinds = [kind for _, kind in _NAME_KIND.findall(expression)]
    if names and kinds[-1] in ("Table", "View"):
        return ".".join(names)
    if "NativeQuery" in expression or re.search(r"\bQuery\s*=", expression):
        for literal in _M_STRING.findall(expression):
            sql = literal.replace('""', '"').strip()
            if re.match(r"(?i)(select|with)\b", sql):
                return _table_from_sql(sql)
    return None


# -- DAX ------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|//[^\n]*|--[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<col>(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)\s*\[[^\]]+\])
  | (?P<mref>\[[^\]]+\])
  | (?P<table>'(?:[^']|'')+')
  | (?P<name>[A-Za-z_][\w.]*)
  | (?P<op>&&|\|\||<>|<=|>=|==|[=<>+\-*/&(),{}])
    """,
    re.X | re.S,
)


class DaxError(ValueError):
    pass


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise DaxError(f"unexpected `{text[pos : pos + 10]}`")
        pos = match.end()
        if match.lastgroup != "ws":
            out.append((match.lastgroup, match.group()))
    return out


class _Parser:
    """A small recursive-descent parser for the DAX subset that can be translated."""

    def __init__(self, text: str) -> None:
        self.tokens = _tokens(text)
        self.pos = 0

    def peek(self, value: str | None = None) -> bool:
        if self.pos >= len(self.tokens):
            return False
        kind, text = self.tokens[self.pos]
        return value is None or text.upper() == value

    def take(self) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise DaxError("unexpected end of expression")
        self.pos += 1
        return self.tokens[self.pos - 1]

    def expect(self, value: str) -> None:
        kind, text = self.take()
        if text.upper() != value:
            raise DaxError(f"expected `{value}`, got `{text}`")

    def parse(self) -> tuple:
        if self.peek("VAR"):
            raise DaxError("variables (VAR/RETURN) are not translated")
        node = self.binary(0)
        if self.pos != len(self.tokens):
            raise DaxError(f"unexpected `{self.tokens[self.pos][1]}`")
        return node

    _LEVELS = [
        ("||",),
        ("&&",),
        ("=", "==", "<>", "<", ">", "<=", ">=", "IN"),
        ("+", "-", "&"),
        ("*", "/"),
    ]

    def binary(self, level: int) -> tuple:
        if level == len(self._LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while (
            self.pos < len(self.tokens) and self.tokens[self.pos][1].upper() in self._LEVELS[level]
        ):
            op = self.take()[1].upper()
            right = self.binary(level + 1)
            left = ("bin", "=" if op == "==" else op, left, right)
        return left

    def unary(self) -> tuple:
        if self.peek("-"):
            self.take()
            return ("neg", self.unary())
        if self.peek("NOT"):
            raise DaxError("NOT is not translated")
        return self.primary()

    def primary(self) -> tuple:
        kind, text = self.take()
        if kind == "num":
            return ("num", text)
        if kind == "str":
            return ("str", text[1:-1].replace('""', '"'))
        if kind == "col":
            table, _, column = text.partition("[")
            return ("col", _unquote(table.strip()), column[:-1])
        if kind == "mref":
            return ("mref", text[1:-1])
        if kind == "table":
            return ("table", _unquote(text))
        if text == "(":
            node = self.binary(0)
            self.expect(")")
            return node
        if text == "{":
            items = [self.binary(0)]
            while self.peek(","):
                self.take()
                items.append(self.binary(0))
            self.expect("}")
            return ("list", items)
        if kind == "name":
            if self.peek("("):
                self.take()
                args = []
                if not self.peek(")"):
                    args.append(self.binary(0))
                    while self.peek(","):
                        self.take()
                        args.append(self.binary(0))
                self.expect(")")
                return ("call", text.upper(), args)
            if text.upper() in ("TRUE", "FALSE"):
                return ("bool", text.upper())
            return ("table", text)
        raise DaxError(f"unexpected `{text}`")


def parse_dax(text: str) -> tuple:
    return _Parser(text).parse()


def _function_names(node: tuple) -> list[str]:
    out = []
    if node[0] == "call":
        out.append(node[1])
    for child in node[1:]:
        if isinstance(child, tuple):
            out += _function_names(child)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, tuple):
                    out += _function_names(item)
    return out


# -- translation ----------------------------------------------------------------------------


class _Unsupported(Exception):
    pass


@dataclass
class _Table:
    name: str
    model: dict[str, Any]
    columns: dict[str, dict[str, Any]]  # Power BI column -> {dimension, source}


class _Translator:
    def __init__(self, tables: dict[str, _Table], measures: dict[str, dict[str, Any]]) -> None:
        self.tables = tables
        self.dax = measures  # Power BI measure name -> {name, expression, description, table}
        self.done: dict[str, str] = {}  # Power BI measure name -> Cosmos member name
        self.failed: dict[str, str] = {}
        self.metrics: list[dict[str, Any]] = []
        self.measure_names: set[str] = set()
        self.warnings: list[str] = []
        self.semantic_warned: set[str] = set()
        self.helpers = 0
        # Cosmos metrics that read an input at an earlier period, directly or through others.
        self.reads_earlier: set[str] = set()
        self.alternate_warned: set[str] = set()
        self._measures: list[str] = []  # the Power BI measures being translated, innermost last
        self._in_progress: set[str] = set()

    # Members -----------------------------------------------------------------------------

    def member(self, pbi_name: str) -> str:
        if pbi_name in self.done:
            return self.done[pbi_name]
        if pbi_name in self.failed:
            raise _Unsupported(f"depends on `{pbi_name}`, which was not imported")
        if pbi_name not in self.dax:
            raise _Unsupported(f"references the unknown measure `{pbi_name}`")
        if pbi_name in self._in_progress:
            raise _Unsupported(f"circular reference through `{pbi_name}`")
        self.translate(pbi_name)
        if pbi_name not in self.done:
            raise _Unsupported(f"depends on `{pbi_name}`, which was not imported")
        return self.done[pbi_name]

    def is_measure(self, member: str) -> bool:
        return member in self.measure_names

    def measure_agg(self, member: str) -> str:
        for table in self.tables.values():
            for item in table.model.get("measures", []):
                if item["name"] == member:
                    return item["agg"]
        raise _Unsupported(f"measure `{member}` was not imported")

    def column(self, table: str, column: str) -> tuple[_Table, dict[str, Any]]:
        t = self.tables.get(table)
        if t is None:
            raise _Unsupported(f"table `{table}` was not imported")
        info = t.columns.get(column)
        if info is None:
            raise _Unsupported(f"column `{table}[{column}]` was not imported")
        return t, info

    def add_measure(self, table: _Table, item: dict[str, Any]) -> str:
        name = item["name"]
        while name in self.measure_names or name in {m["name"] for m in self.metrics}:
            name = f"{name}_2"
        item["name"] = name
        table.model.setdefault("measures", []).append(item)
        self.measure_names.add(name)
        return name

    def aggregation(self, node: tuple, name: str | None, description: str | None) -> str:
        """Registers ``SUM(T[c])``-style calls as a measure and returns its name."""
        _, func, args = node
        if func == "COUNTROWS":
            if len(args) != 1 or args[0][0] != "table":
                raise _Unsupported("COUNTROWS over an expression")
            table = self.tables.get(args[0][1])
            if table is None:
                raise _Unsupported(f"table `{args[0][1]}` was not imported")
            item: dict[str, Any] = {"agg": "count"}
        else:
            if len(args) != 1 or args[0][0] != "col":
                raise _Unsupported(f"{func} over an expression")
            table, info = self.column(args[0][1], args[0][2])
            item = {"agg": _AGGS[func], "expr": info["source"]}
        if name is None:
            self.helpers += 1
            name = f"{ident(table.name)}_{item['agg']}_{self.helpers}"
        item = {"name": name, **item}
        if item.get("expr") == name:
            item.pop("expr")
        if description:
            item["description"] = description
        return self.add_measure(table, item)

    def row_aggregate(
        self, node: tuple, home: _Table | None, name: str, description: str | None
    ) -> str:
        """Registers ``SUMX``, ``AVERAGEX``, ``MINX`` or ``MAXX`` (``T``, <row arithmetic>)
        over the measure's own table as a ``sum``, ``avg``, ``min`` or ``max`` measure whose
        ``expr`` is the row expression, and returns its name. The row expression is NULL
        exactly where DAX gives BLANK, so the rows AVG, MIN and MAX skip are the rows
        AVERAGEX, MINX and MAXX skip. ``avg`` averages as a double on every dialect, as DAX
        does, even over integer columns (see the planner)."""
        func, args = node[1], node[2]
        if len(args) != 2:
            raise _Unsupported(f"{func} takes a table and a row expression")
        if args[0][0] != "table":
            raise _Unsupported(f"{func} over a table expression instead of a table name")
        if home is None or args[0][1] != home.name:
            raise _Unsupported(f"{func} over `{args[0][1]}`, which is not the measure's own table")
        item: dict[str, Any] = {
            "name": name,
            "agg": _ITERATORS[func],
            "expr": self.row_expression(args[1], home, func)[0],
        }
        if description:
            item["description"] = description
        return self.add_measure(home, item)

    def row_expression(
        self, node: tuple, table: _Table, func: str = "SUMX"
    ) -> tuple[str, bool, int]:
        """An iterator's row expression as portable SQL: (sql, can be null, precedence of its
        top operator: 0 for an operand, 1 for ``*``, 2 for ``+``/``-``). The SQL is NULL on
        exactly the rows where DAX gives BLANK, which AVERAGEX, MINX and MAXX skip.
        Multiplication keeps NULL as DAX keeps BLANK (``BLANK * 5`` is BLANK). In DAX
        ``BLANK + 5`` is 5 (a row that counts, not a skipped one) and ``BLANK + BLANK`` is
        BLANK, so ``+`` and ``-`` coalesce a missing side only. Division is not translated:
        DAX returns infinity or BLANK where SQL raises an error, returns NULL or truncates
        integers."""
        kind = node[0]
        if kind == "num":
            return node[1], False, 0
        if kind == "neg" and node[1][0] == "num":
            return f"(-{node[1][1]})", False, 0
        if kind in ("col", "mref"):
            owner, column = (node[1], node[2]) if kind == "col" else (table.name, node[1])
            if kind == "mref" and column in self.dax:
                raise _Unsupported(f"{func} row expression references the measure `[{column}]`")
            if owner != table.name:
                raise _Unsupported(
                    f"{func} row expression references `{owner}[{column}]`, a column of another table"
                )
            _, info = self.column(owner, column)
            if info["spec"]["type"] != "number":
                raise _Unsupported(f"{func} over the non-numeric column `{owner}[{column}]`")
            if not _SQL_IDENT.match(info["source"]):
                raise _Unsupported(
                    f"{func} over `{owner}[{column}]`, whose source column `{info['source']}` "
                    "is not a plain SQL identifier"
                )
            return info["source"], True, 0
        if kind == "bin" and node[1] == "/":
            raise _Unsupported(
                f"{func} row expression divides; DAX division returns infinity or BLANK where "
                "SQL raises an error, returns NULL or truncates integers"
            )
        if kind == "bin" and node[1] in ("+", "-", "*"):
            op, level = node[1], 1 if node[1] == "*" else 2
            left, left_null, left_level = self.row_expression(node[2], table, func)
            right, right_null, right_level = self.row_expression(node[3], table, func)
            # Left-associative: same-level operators on the left need no parentheses.
            lhs = f"({left})" if left_level > level else left
            rhs = f"({right})" if right_level and right_level >= level else right
            if op == "*" or not (left_null or right_null):
                return f"{lhs} {op} {rhs}", left_null or right_null, level
            if not right_null:
                return f"COALESCE({left}, 0) {op} {rhs}", False, level
            if not left_null:
                return f"{lhs} {op} COALESCE({right}, 0)", False, level
            negated = rhs if op == "+" else f"-({right})" if right_level else f"-{right}"
            return f"COALESCE({lhs} {op} {rhs}, {left}, {negated})", True, 0
        if kind == "neg":
            raise _Unsupported(
                f"{func} row expression negates a column or an expression; only negative "
                "numbers are translated"
            )
        raise _Unsupported(f"{func} row expression is not arithmetic over the table's columns")

    def operand(self, node: tuple, hint: str | None = None) -> str:
        """A measure reference or an inline aggregation, as a Cosmos member name."""
        if node[0] == "mref":
            return self.member(node[1])
        if node[0] == "call" and (node[1] in _AGGS or node[1] == "COUNTROWS"):
            return self.aggregation(node, hint, None)
        raise _Unsupported("operand is not a measure or a simple aggregation")

    # Expressions -------------------------------------------------------------------------

    def arithmetic(self, node: tuple) -> str:
        kind = node[0]
        if kind == "num":
            return node[1]
        if kind in ("mref", "call") and not (kind == "call" and node[1] == "DIVIDE"):
            return "{" + self.operand(node) + "}"
        if kind == "call":  # DIVIDE inside arithmetic: the engine divides safely
            args = node[2]
            if len(args) < 2:
                raise _Unsupported("DIVIDE needs two arguments")
            self.alternate_result(args)
            num, den = (self.arithmetic(a) for a in args[:2])
            # Parenthesized as a whole: `[A] / DIVIDE([B], [C])` is a / (b / c).
            return f"({_parenthesized(num)} / {_parenthesized(den)})"
        if kind == "neg":
            return f"-({self.arithmetic(node[1])})"
        if kind == "bin" and node[1] in ("+", "-", "*", "/"):
            return f"({self.arithmetic(node[2])} {node[1]} {self.arithmetic(node[3])})"
        raise _Unsupported("expression is not arithmetic over measures")

    def condition(self, node: tuple, table_of_measure: _Table | None) -> str:
        kind = node[0]
        if kind == "call" and node[1] == "KEEPFILTERS" and len(node[2]) == 1:
            return self.condition(node[2][0], table_of_measure)
        if kind == "bin" and node[1] in ("&&", "||"):
            op = "AND" if node[1] == "&&" else "OR"
            left = self.condition(node[2], table_of_measure)
            right = self.condition(node[3], table_of_measure)
            return f"({left} {op} {right})"
        if kind == "bin" and node[1] in ("=", "<>", "<", ">", "<=", ">=", "IN"):
            left, right, op = node[2], node[3], node[1]
            if left[0] != "col":
                left, right = right, left
                op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)
            if left[0] != "col":
                raise _Unsupported("filter does not compare a column")
            table, info = self.column(left[1], left[2])
            dims = table.model.setdefault("dimensions", [])
            if info["spec"] not in dims:
                dims.append(info["spec"])
            ref = "{" + f"{table.model['name']}.{info['dimension']}" + "}"
            if op == "IN":
                if right[0] != "list":
                    raise _Unsupported("IN without a literal list")
                values = ", ".join(self.literal(v) for v in right[1])
                return f"{ref} IN ({values})"
            return f"{ref} {op} {self.literal(right)}"
        raise _Unsupported("filter is not a simple column condition")

    def literal(self, node: tuple) -> str:
        if node[0] == "str":
            return "'" + node[1].replace("'", "''") + "'"
        if node[0] == "num":
            return node[1]
        if node[0] == "neg" and node[1][0] == "num":
            return f"-{node[1][1]}"
        if node[0] == "bool":
            return node[1]
        raise _Unsupported("filter value is not a literal")

    # Measures ------------------------------------------------------------------------------

    def translate(self, pbi_name: str) -> None:
        source = self.dax[pbi_name]
        self._in_progress.add(pbi_name)
        self._measures.append(pbi_name)
        try:
            self._translate(pbi_name, source)
        except (_Unsupported, DaxError) as exc:
            self.failed[pbi_name] = str(exc)
            alternate = f"measure `{pbi_name}`: DIVIDE with an alternate result"
            self.warnings = [w for w in self.warnings if not w.startswith(alternate)]
            functions = sorted(set(_safe_functions(source["expression"])) - _TRANSLATABLE)
            hint = f" (DAX {', '.join(functions)} has no equivalent)" if functions else ""
            expr = " ".join(source["expression"].split())
            self.warnings.append(
                f"measure `{pbi_name}` not imported{hint}: {exc}. DAX: {expr[:120]}"
            )
        finally:
            self._measures.pop()
            self._in_progress.discard(pbi_name)

    def _translate(self, pbi_name: str, source: dict[str, Any]) -> None:
        tree = parse_dax(source["expression"])
        name = ident(pbi_name)
        description = source.get("description")
        meta = {"label": pbi_name, **({"description": description} if description else {})}
        home = self.tables.get(source["table"])
        kind = tree[0]

        if kind == "call" and tree[1] in _ITERATORS:
            self.done[pbi_name] = self.row_aggregate(tree, home, name, description)
            return
        if kind == "call" and (tree[1] in _AGGS or tree[1] == "COUNTROWS"):
            if tree[1] == "DISTINCTCOUNT":
                self.warnings.append(
                    f"measure `{pbi_name}`: in DAX a DISTINCTCOUNT counts BLANK as a value, "
                    "while Cosmos `count_distinct` ignores nulls; the imported measure is "
                    "one lower than Power BI whenever the column has nulls. Use "
                    "DISTINCTCOUNTNOBLANK in Power BI to match, or accept the difference"
                )
            self.done[pbi_name] = self.aggregation(tree, name, description)
            return
        if kind == "call" and tree[1] == "DIVIDE":
            args = tree[2]
            if len(args) < 2:
                raise _Unsupported("DIVIDE needs two arguments")
            if not all(_is_operand(a) for a in args[:2]):
                # DIVIDE([m] - [m PY], [m PY]): arithmetic over measures, divided safely.
                self.derived(pbi_name, tree, name, meta)
                return
            self.alternate_result(args)
            numerator = self.operand(args[0], f"{name}_numerator")
            denominator = self.operand(args[1], f"{name}_denominator")
            self.metric(
                pbi_name,
                {
                    "name": name,
                    "type": "ratio",
                    "numerator": numerator,
                    "denominator": denominator,
                    **meta,
                },
            )
            if {numerator, denominator} & self.reads_earlier:
                self.reads_earlier.add(self.done[pbi_name])
            return
        if (
            kind == "call"
            and tree[1] == "CALCULATE"
            and any(a[0] == "call" and a[1] == "DATESINPERIOD" for a in tree[2][1:])
        ):
            self.rolling_window(pbi_name, tree[2], name, meta)
            return
        if (
            kind == "call"
            and tree[1] == "CALCULATE"
            and any(a[0] == "call" and a[1] in _SHIFTS for a in tree[2][1:])
        ):
            self.period_shift(pbi_name, tree[2], name, meta)
            return
        if kind == "call" and tree[1] == "CALCULATE":
            args = tree[2]
            if not args:
                raise _Unsupported("CALCULATE without arguments")
            inner = self.operand(args[0], f"{name}_base")
            filters = [self.condition(a, home) for a in args[1:]]
            if not filters:
                self.metric(
                    pbi_name, {"name": name, "type": "derived", "expr": "{" + inner + "}", **meta}
                )
                if inner in self.reads_earlier:
                    self.reads_earlier.add(self.done[pbi_name])
                return
            if any(not (a[0] == "call" and a[1] == "KEEPFILTERS") for a in args[1:]):
                self.warnings.append(
                    f"measure `{pbi_name}`: in DAX a CALCULATE filter replaces the filter on "
                    "that column, while Cosmos intersects it (like KEEPFILTERS); numbers "
                    "differ only when a query also groups or filters by that column"
                )
            flt = " AND ".join(filters)
            if self.is_measure(inner):
                spec = {"name": name, "type": "simple", "measure": inner, "filter": flt}
            else:
                base = next((m for m in self.metrics if m["name"] == inner), None)
                if base is None or base["type"] not in ("simple", "ratio"):
                    raise _Unsupported("CALCULATE over a metric that is not simple or a ratio")
                spec = {k: v for k, v in base.items() if k not in ("label", "description")}
                spec["name"] = name
                spec["filter"] = f"({base['filter']}) AND {flt}" if base.get("filter") else flt
            self.metric(pbi_name, {**spec, **meta})
            if inner in self.reads_earlier:  # a filtered copy of a ratio over a shifted metric
                self.reads_earlier.add(self.done[pbi_name])
            return
        if kind == "call" and tree[1] in _TO_DATE:
            args = tree[2]
            if len(args) != 2:
                raise _Unsupported(f"{tree[1]} with extra filters or a year-end date")
            inner = self.operand(args[0], f"{name}_base")
            if not self.is_measure(inner):
                raise _Unsupported(f"{tree[1]} over a metric; only measures accumulate")
            agg = self.measure_agg(inner)
            if agg not in WINDOW_AGGS:
                # DAX evaluates the measure over all the dates to date at once; Cosmos
                # accumulates per-period values, which only sums, counts, minima and maxima
                # allow (DEC-21). Importing it would make the model fail validation.
                raise _Unsupported(
                    f"{tree[1]} over `{inner}`, an `{agg}` measure, cannot be accumulated from "
                    "per-period values (only sum, count, min and max can); an average or a "
                    "distinct count to date must be computed over the whole period"
                )
            self.metric(
                pbi_name,
                {
                    "name": name,
                    "type": "cumulative",
                    "measure": inner,
                    "grain_to_date": _TO_DATE[tree[1]],
                    **meta,
                },
            )
            return
        self.derived(pbi_name, tree, name, meta)

    def derived(self, pbi_name: str, tree: tuple, name: str, meta: dict[str, Any]) -> None:
        """Arithmetic over measures as a derived metric. Over a metric that reads an earlier
        period, ``+`` and ``-`` get a warning: DAX counts a BLANK side as zero there."""
        expr = _strip_parens(self.arithmetic(tree))
        self.metric(pbi_name, {"name": name, "type": "derived", "expr": expr, **meta})
        earlier = sorted(set(re.findall(r"\{([^{}]+)\}", expr)) & self.reads_earlier)
        if not earlier:
            return
        self.reads_earlier.add(self.done[pbi_name])
        shifted = self.added_or_subtracted_earlier(tree)
        if shifted:
            self.warnings.append(
                f"measure `{pbi_name}`: adds or subtracts `{shifted}`, which reads an earlier "
                "period. In DAX a BLANK side of + or - counts as zero, while in Cosmos a NULL "
                "side makes the result NULL: where the earlier period has no facts, "
                "`[m] - [m PY]` is [m] in Power BI and NULL in Cosmos (a growth that divides by "
                "the earlier period is BLANK and NULL alike)"
            )

    def alternate_result(self, args: list) -> None:
        """``DIVIDE(a, b, alt)``: imported as a safe division, with a warning unless ``alt``
        is ``BLANK()``. DAX returns ``alt`` where ``b`` is zero or BLANK; Cosmos returns NULL."""
        if len(args) > 3:
            raise _Unsupported("DIVIDE takes at most three arguments")
        if len(args) < 3 or args[2] == ("call", "BLANK", []):
            return
        pbi_name = self._measures[-1]
        if pbi_name in self.alternate_warned:
            return
        self.alternate_warned.add(pbi_name)
        self.warnings.append(
            f"measure `{pbi_name}`: DIVIDE with an alternate result; DAX returns the alternate "
            "result where the denominator is zero or BLANK, while Cosmos returns NULL there"
        )

    def added_or_subtracted_earlier(self, node: tuple) -> str | None:
        """The shifted member that is an operand (or inside an operand) of a ``+`` or ``-``,
        or None. A shifted member only in a DIVIDE denominator, say, does not count."""
        if node[0] == "bin" and node[1] in ("+", "-"):
            for side in node[2:]:
                found = self.shifted_inside(side)
                if found:
                    return found
        children = node[2] if node[0] == "call" else node[1:]
        for child in children:
            if isinstance(child, tuple):
                found = self.added_or_subtracted_earlier(child)
                if found:
                    return found
        return None

    def shifted_inside(self, node: tuple) -> str | None:
        if node[0] == "mref":
            member = self.done.get(node[1])
            return member if member in self.reads_earlier else None
        children = node[2] if node[0] == "call" else node[1:]
        for child in children:
            if isinstance(child, tuple):
                found = self.shifted_inside(child)
                if found:
                    return found
        return None

    def measures_under(self, member: str, seen: set[str] | None = None) -> set[str]:
        """The measures a member reads, refusing what an offset cannot shift."""
        if self.is_measure(member):
            return {member}
        seen = set() if seen is None else seen
        if member in seen:
            return set()
        seen.add(member)
        spec = next((m for m in self.metrics if m["name"] == member), None)
        if spec is None:
            raise _Unsupported(f"`{member}` was not imported")
        if spec["type"] == "cumulative":
            raise _Unsupported(
                f"a period shift of `{member}`, a cumulative metric; offsets of cumulative "
                "metrics are not supported"
            )
        if spec.get("inputs") or member in self.reads_earlier:
            raise _Unsupported(
                f"a period shift of `{member}`, which already reads an earlier period; "
                "offsets do not combine yet"
            )
        if spec["type"] == "simple":
            refs = {spec["measure"]}
        elif spec["type"] == "ratio":
            refs = {spec["numerator"], spec["denominator"]}
        else:
            refs = set(re.findall(r"\{([^{}]+)\}", spec["expr"]))
        out: set[str] = set()
        for ref in refs:
            out |= self.measures_under(ref, seen)
        return out

    def period_shift(self, pbi_name: str, args: list, name: str, meta: dict[str, Any]) -> None:
        """``CALCULATE(m, SAMEPERIODLASTYEAR(D[c]))``, ``DATEADD(D[c], -N, unit)`` or
        ``PARALLELPERIOD(D[c], -N, MONTH)`` as a derived metric reading ``m`` at an earlier
        period (DEC-35). DAX moves the set of dates in context; Cosmos moves the start of the
        query's period. On whole periods of a complete date table both read the whole earlier
        period at the grains the planner accepts for the offset (by month, quarter or year
        for months; by day or week for days): DAX extends a set that ends on a month's last
        day to the end of the shifted month, and PARALLELPERIOD by month returns whole months.
        Every other form stays a warning."""
        if len(args) != 2:
            raise _Unsupported("CALCULATE with a period shift and other filters")
        func, fargs = args[1][1], args[1][2]
        if func == "SAMEPERIODLASTYEAR":
            if len(fargs) != 1:
                raise _Unsupported("SAMEPERIODLASTYEAR takes a date column")
            column, count, unit = fargs[0], 1, "YEAR"
        else:
            if len(fargs) != 3:
                raise _Unsupported(f"{func} takes a date column, a count and a unit")
            column, count_node, unit_node = fargs
            unit = unit_node[1].upper() if unit_node[0] == "table" else ""
            units = ("MONTH", "QUARTER", "YEAR") if func == "PARALLELPERIOD" else ("DAY",)
            if unit not in (*units, *_SHIFT_MONTHS):
                known = ", ".join(dict.fromkeys((*units, *_SHIFT_MONTHS)))
                raise _Unsupported(f"{func} in a unit other than {known}")
            if not (
                count_node[0] == "neg" and count_node[1][0] == "num" and count_node[1][1].isdigit()
            ):
                raise _Unsupported(
                    f"{func} with a count that is not a negative whole number; only shifts to "
                    "an earlier period are translated"
                )
            count = int(count_node[1][1])
            if count == 0:
                raise _Unsupported(f"{func} by zero periods")
            if func == "PARALLELPERIOD" and unit != "MONTH":
                period = unit.lower()
                raise _Unsupported(
                    f"PARALLELPERIOD by {unit} returns the whole earlier {period}: by month, "
                    f"Power BI shows the whole {period} where an offset would read one month. "
                    f"By {period} it is DATEADD(..., -{count}, {unit}), which is imported"
                )
        if column[0] != "col":
            raise _Unsupported(f"{func} over a table expression instead of a date column")
        dates, info = self.column(column[1], column[2])
        if info["spec"]["type"] != "time":
            raise _Unsupported(f"{func} over `{column[1]}[{column[2]}]`, not a date column")
        if unit == "DAY":
            amount, offset_unit = count, "day"
        else:
            amount, offset_unit = count * _SHIFT_MONTHS[unit], "month"
        offset = f"{amount} {offset_unit}" + ("" if amount == 1 else "s")
        before = set(self.measure_names)
        inner = self.operand(args[0], f"{name}_base")
        try:
            self.check_shift_path(func, column, dates, inner)
        except _Unsupported:
            if inner not in before:  # an inline aggregation: leave no helper measure behind
                self.measure_names.discard(inner)
                for table in self.tables.values():
                    table.model["measures"] = [
                        m for m in table.model.get("measures", []) if m["name"] != inner
                    ]
            raise
        dims = dates.model.setdefault("dimensions", [])
        if info["spec"] not in dims:
            dims.append(info["spec"])
        date = f"{dates.model['name']}.{info['dimension']}"
        grains = [g for g, n in OFFSET_GRAINS[offset_unit].items() if amount % n == 0]
        where = " or ".join(f"`{date}__{g}`" for g in grains)
        clamp = (
            "; by day the planner refuses it, and DAX would move each date and clamp it to "
            "the month's end (29, 30 and 31 March a month back are all the last day of February)"
            if offset_unit == "month"
            else ""
        )
        if func == "PARALLELPERIOD":
            partial = (
                "a filter on the date that cuts a period still gives DAX the whole earlier "
                "months, while Cosmos applies the filter to the earlier period's facts too"
            )
        else:
            partial = (
                "a filter on the date that cuts a period, or a date table that ends inside "
                "one, gives DAX a partial earlier period (the same dates moved back), while "
                "Cosmos applies the filter to the earlier period's facts too and otherwise "
                "reads the whole earlier period"
            )
        self.warnings.append(
            f"measure `{pbi_name}`: imported as `{inner}` read {offset} earlier; query it by "
            f"{where} (or by the fact's own date column related to it), where it matches DAX "
            f"on whole periods when `{dates.name}` holds every day of them{clamp}. It differs "
            f"where DAX moves a set of dates: {partial}; grouping or filtering by other "
            f"columns of `{dates.name}` (a weekday) gives different numbers, because Cosmos "
            "reads the earlier period with the same value; and Power BI also shows periods "
            "without facts whose earlier period has some, which Cosmos does not output"
        )
        self.metric(
            pbi_name,
            {
                "name": name,
                "type": "derived",
                "expr": "{" + inner + "}",
                "inputs": [{"metric": inner, "offset": offset}],
                **meta,
            },
        )
        self.reads_earlier.add(self.done[pbi_name])

    def rolling_window(self, pbi_name: str, args: list, name: str, meta: dict[str, Any]) -> None:
        """``CALCULATE(m, DATESINPERIOD(D[c], MAX(D[c]), -N, DAY))`` as a cumulative metric
        with ``window: N days``. DAX takes the N dates ending at the last date in context; the
        engine answers windows at day grain only, where that date is the row's day, so the
        window is D - (N - 1) to D in both. Every other form stays a warning."""
        if len(args) != 2:
            raise _Unsupported("CALCULATE with DATESINPERIOD and other filters")
        call = args[1]
        if len(call[2]) != 4:
            raise _Unsupported("DATESINPERIOD takes a date column, a start, a count and a unit")
        column, anchor, count, unit = call[2]
        if column[0] != "col":
            raise _Unsupported("DATESINPERIOD over a table expression instead of a date column")
        if anchor != ("call", "MAX", [column]):
            raise _Unsupported(
                f"DATESINPERIOD anchored elsewhere than MAX({column[1]}[{column[2]}]), the last "
                "date in context; only that anchor is the row's day"
            )
        if unit[0] != "table" or unit[1].upper() != "DAY":
            raise _Unsupported(
                "DATESINPERIOD in a unit other than DAY; only windows in days are translated"
            )
        if not (count[0] == "neg" and count[1][0] == "num" and count[1][1].isdigit()):
            raise _Unsupported(
                "DATESINPERIOD with a count that is not a negative whole number; only windows "
                "that end at the last date are translated"
            )
        days = int(count[1][1])
        if days == 0:
            raise _Unsupported("DATESINPERIOD over zero days")
        dates, info = self.column(column[1], column[2])
        if info["spec"]["type"] != "time":
            raise _Unsupported(f"DATESINPERIOD over `{column[1]}[{column[2]}]`, not a date column")
        inner = self.operand(args[0], f"{name}_base")
        if not self.is_measure(inner):
            raise _Unsupported("DATESINPERIOD over a metric; only measures roll")
        problem = window_agg_refusal(self.measure_agg(inner))
        if problem:
            raise _Unsupported(f"DATESINPERIOD over `{inner}`: {problem}")
        owner = next(
            t
            for t in self.tables.values()
            if any(m["name"] == inner for m in t.model.get("measures", []))
        )
        keys = {e["name"] for e in dates.model.get("entities", []) if e["type"] != "foreign"}
        foreign = {e["name"] for e in owner.model.get("entities", []) if e["type"] == "foreign"}
        if owner is not dates and not keys & foreign:
            raise _Unsupported(
                f"DATESINPERIOD over `{column[1]}`, which `{owner.name}` does not reach through "
                "an imported relationship"
            )
        dims = dates.model.setdefault("dimensions", [])
        if info["spec"] not in dims:
            dims.append(info["spec"])
        day = f"{dates.model['name']}.{info['dimension']}__day"
        related = " (or the fact's own date column related to it)" if owner is not dates else ""
        self.warnings.append(
            f"measure `{pbi_name}`: imported as a {days}-day window; query it only with "
            f"`{day}`{related}, where it matches DAX on every day that has facts; other date "
            "dimensions give different numbers. Grouping or filtering by other columns of "
            f"`{dates.name}` also gives different numbers: the DAX filter on "
            f"`{column[1]}[{column[2]}]` removes those filters, while Cosmos keeps one window "
            "per value. Power BI also shows days without facts and coarser periods, and a date "
            "slicer does not cut its window, while Cosmos filters the facts first"
        )
        self.metric(
            pbi_name,
            {
                "name": name,
                "type": "cumulative",
                "measure": inner,
                "window": f"{days} day" if days == 1 else f"{days} days",
                **meta,
            },
        )

    def check_shift_path(self, func: str, column: tuple, dates: _Table, inner: str) -> None:
        """Each measure under a period shift lives on a table one imported relationship away
        from the date table, and never on the date table itself."""
        for measure in sorted(self.measures_under(inner)):
            owner = next(
                t
                for t in self.tables.values()
                if any(m["name"] == measure for m in t.model.get("measures", []))
            )
            if owner is dates:
                raise _Unsupported(
                    f"{func} over `{column[1]}[{column[2]}]`, a column of the measure's own "
                    "table: DAX moves only the dates in context that the column holds, so over "
                    "a fact's own dates the earlier period keeps only the days with facts in "
                    "the current one; use a date table"
                )
            keys = {e["name"] for e in dates.model.get("entities", []) if e["type"] != "foreign"}
            foreign = {e["name"] for e in owner.model.get("entities", []) if e["type"] == "foreign"}
            if not keys & foreign:
                raise _Unsupported(
                    f"{func} over `{column[1]}`, which `{owner.name}` does not reach through "
                    "an imported relationship"
                )

    def metric(self, pbi_name: str, spec: dict[str, Any]) -> None:
        while spec["name"] in self.measure_names or spec["name"] in {
            m["name"] for m in self.metrics
        }:
            spec["name"] = f"{spec['name']}_2"
        self.metrics.append(spec)
        self.done[pbi_name] = spec["name"]


_TRANSLATABLE = (
    set(_AGGS)
    | {"COUNTROWS", "DIVIDE", "CALCULATE", "KEEPFILTERS", "BLANK", "DATESINPERIOD"}
    | set(_SHIFTS)
    | set(_ITERATORS)
    | set(_TO_DATE)
)


def _is_operand(node: tuple) -> bool:
    """A measure reference or an inline aggregation, which a ratio metric takes as is."""
    return node[0] == "mref" or (node[0] == "call" and (node[1] in _AGGS or node[1] == "COUNTROWS"))


def _wrapped(expr: str) -> bool:
    """Whether one pair of parentheses encloses the whole expression."""
    if not (expr.startswith("(") and expr.endswith(")")):
        return False
    depth = 0
    for i, char in enumerate(expr):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0 and i < len(expr) - 1:
            return False
    return True


def _strip_parens(expr: str) -> str:
    """``(a + b)`` -> ``a + b``, but ``(a) / (b)`` stays as it is."""
    return expr[1:-1] if _wrapped(expr) else expr


def _parenthesized(expr: str) -> str:
    return expr if _wrapped(expr) else f"({expr})"


def _safe_functions(expression: str) -> list[str]:
    try:
        return _function_names(parse_dax(expression))
    except DaxError:
        return re.findall(r"\b([A-Z][A-Z0-9.]*)\s*\(", expression)


# -- import ------------------------------------------------------------------------------


def import_powerbi(path: str | Path, *, schema: str | None = None) -> ImportResult:
    model = read_model(path)
    warnings: list[str] = []
    extensions: dict[str, Any] = {}
    tables: dict[str, _Table] = {}

    for table in model.get("tables", []):
        tname = table["name"]
        where = f"table `{tname}`"
        if table.get("calculationGroup") is not None:
            warnings.append(f"{where}: calculation groups are not imported")
            continue
        sources = [p.get("source", {}) for p in table.get("partitions", [])]
        if any(s.get("type") == "calculated" for s in sources):
            warnings.append(f"{where}: calculated tables are not imported (DAX would have to run)")
            continue
        physical = next((t for t in map(physical_table, sources) if t), None)
        if physical is None:
            physical = f"{schema}.{ident(tname)}" if schema else ident(tname)
            if sources:
                warnings.append(
                    f"{where}: the source table could not be read from the partition; assumed "
                    f"`{physical}`, check it"
                )
        name = ident(tname)
        spec: dict[str, Any] = {"name": name, "table": physical}
        if table.get("description"):
            spec["description"] = table["description"]
        columns: dict[str, dict[str, Any]] = {}
        for column in table.get("columns", []):
            cname = column["name"]
            if column.get("type") == "calculated" or "expression" in column:
                warnings.append(
                    f"{where}: calculated column `{cname}` is not imported; compute it in the "
                    "source table or as a dimension `expr`"
                )
                continue
            if column.get("type") == "rowNumber" or cname.startswith("RowNumber-"):
                continue
            kind = _DATA_TYPES.get(str(column.get("dataType", "string")).lower())
            if kind is None:
                warnings.append(
                    f"{where}: column `{cname}` of type `{column.get('dataType')}` skipped"
                )
                continue
            dim_name, source_column = ident(cname), column.get("sourceColumn", cname)
            dim: dict[str, Any] = {"name": dim_name, "type": kind}
            if source_column != dim_name:
                dim["expr"] = source_column
            if column.get("description"):
                dim["description"] = column["description"]
            # Hidden columns (usually keys) become dimensions only when a filter uses them.
            if not column.get("isHidden"):
                spec.setdefault("dimensions", []).append(dim)
            columns[cname] = {"dimension": dim_name, "source": source_column, "spec": dim}
        if table.get("hierarchies"):
            extensions.setdefault("hierarchies", {})[name] = [
                h["name"] for h in table["hierarchies"]
            ]
        tables[tname] = _Table(tname, spec, columns)

    _relationships(model.get("relationships", []), tables, warnings)

    measures = {
        m["name"]: {
            "name": m["name"],
            "expression": "\n".join(m["expression"])
            if isinstance(m["expression"], list)
            else str(m["expression"]),
            "description": m.get("description"),
            "table": t["name"],
        }
        for t in model.get("tables", [])
        for m in t.get("measures", [])
    }
    translator = _Translator(tables, measures)
    for pbi_name in measures:
        if pbi_name not in translator.done and pbi_name not in translator.failed:
            translator.translate(pbi_name)
    warnings += translator.warnings

    for role in model.get("roles", []):
        for permission in role.get("tablePermissions", []):
            expr = permission.get("filterExpression")
            if expr:
                expr = " ".join(str(expr).split())
                warnings.append(
                    f"role `{role['name']}`: row-level security on `{permission['name']}` "
                    f"({expr}) must be recreated as a row_filter with `$user` fields"
                )
    if model.get("perspectives"):
        warnings.append("perspectives are not imported")

    project = Project.model_validate(
        {
            "version": 1,
            "semantic_models": [t.model for t in tables.values()],
            "metrics": translator.metrics,
            "custom_extensions": {"powerbi": extensions} if extensions else {},
        }
    )
    return ImportResult(project, warnings)


def _relationships(
    relationships: list[dict[str, Any]], tables: dict[str, _Table], warnings: list[str]
) -> None:
    for rel in relationships:
        many, one = rel.get("fromTable"), rel.get("toTable")
        many_col, one_col = rel.get("fromColumn"), rel.get("toColumn")
        label = f"relationship {many}[{many_col}] -> {one}[{one_col}]"
        if rel.get("isActive") is False:
            warnings.append(f"{label}: inactive (used through USERELATIONSHIP); not imported")
            continue
        cardinality = (rel.get("fromCardinality", "many"), rel.get("toCardinality", "one"))
        if cardinality == ("many", "many"):
            warnings.append(f"{label}: many-to-many relationships are not imported")
            continue
        if cardinality == ("one", "many"):
            many, one, many_col, one_col = one, many, one_col, many_col
        if rel.get("crossFilteringBehavior") == "bothDirections":
            warnings.append(
                f"{label}: bidirectional filtering is not imported; Cosmos joins many-to-one "
                "and reaches one-to-many dimensions through a bridge"
            )
        if many not in tables or one not in tables:
            warnings.append(f"{label}: a table was not imported; relationship skipped")
            continue
        try:
            one_info = tables[one].columns[one_col]
            many_info = tables[many].columns[many_col]
        except KeyError:
            warnings.append(f"{label}: a key column was not imported; relationship skipped")
            continue
        entity = ident(one)
        target = tables[one].model.setdefault("entities", [])
        primary = next((e for e in target if e["type"] in ("primary", "unique")), None)
        if primary is None:
            target.append({"name": entity, "type": "primary", "expr": one_info["source"]})
        elif primary.get("expr") != one_info["source"]:
            entity = f"{entity}_{ident(one_col)}"
            if not any(e["name"] == entity for e in target):
                target.append({"name": entity, "type": "unique", "expr": one_info["source"]})
        else:
            entity = primary["name"]
        source = tables[many].model.setdefault("entities", [])
        if any(e["name"] == entity and e["type"] == "foreign" for e in source):
            warnings.append(f"{label}: a second path to `{one}`; relationship skipped")
            continue
        source.append({"name": entity, "type": "foreign", "expr": many_info["source"]})

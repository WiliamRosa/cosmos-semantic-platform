"""Importing Power BI semantic models (ADR 0009): ``.bim`` (TMSL JSON) or TMDL folders.

Tables become semantic models, with the physical table read from the partition (Power Query
navigation, a native SQL query or a legacy query partition). Active many-to-one relationships
become entities. DAX measures are translated when they map onto the Cosmos metric types:

| DAX | Cosmos |
| --- | --- |
| ``SUM``, ``AVERAGE``, ``MIN``, ``MAX``, ``COUNT``, ``DISTINCTCOUNT``, ``DISTINCTCOUNTNOBLANK``, ``COUNTROWS`` | measure |
| ``SUMX(T, ...)`` over the measure's own table, with ``+ - *`` over its numeric columns | ``sum`` measure |
| ``DIVIDE(a, b)`` | ratio metric |
| ``CALCULATE(m, T[c] = "x", ...)`` with simple column filters | metric with ``filter`` |
| ``TOTALYTD``, ``TOTALQTD``, ``TOTALMTD`` | cumulative metric |
| arithmetic over measures (``[A] - [B]``) | derived metric |

Everything else (other iterators such as ``AVERAGEX``; ``SUMX`` over another table, with a
division, a function or ``RELATED``; ``FILTER``, ``ALL``, variables, calculated columns and
tables, calculation groups, row-level security roles) is reported as a warning; nothing is
dropped silently. No DAX is ever executed.
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
from cosmos_semantic.spec import Project

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

    def row_sum(self, node: tuple, home: _Table | None, name: str, description: str | None) -> str:
        """Registers ``SUMX(T, <row arithmetic>)`` over the measure's own table as a ``sum``
        measure whose ``expr`` is the row expression, and returns its name."""
        args = node[2]
        if len(args) != 2:
            raise _Unsupported("SUMX takes a table and a row expression")
        if args[0][0] != "table":
            raise _Unsupported("SUMX over a table expression instead of a table name")
        if home is None or args[0][1] != home.name:
            raise _Unsupported(f"SUMX over `{args[0][1]}`, which is not the measure's own table")
        item: dict[str, Any] = {
            "name": name,
            "agg": "sum",
            "expr": self.row_expression(args[1], home)[0],
        }
        if description:
            item["description"] = description
        return self.add_measure(home, item)

    def row_expression(self, node: tuple, table: _Table) -> tuple[str, bool, int]:
        """A SUMX row expression as portable SQL: (sql, can be null, precedence of its top
        operator: 0 for an operand, 1 for ``*``, 2 for ``+``/``-``). Multiplication keeps NULL
        as DAX keeps BLANK (``BLANK * 5`` is BLANK). In DAX ``BLANK + 5`` is 5 and
        ``BLANK + BLANK`` is BLANK, so ``+`` and ``-`` coalesce a missing side. Division is
        not translated: DAX returns infinity or BLANK where SQL raises an error, returns NULL
        or truncates integers."""
        kind = node[0]
        if kind == "num":
            return node[1], False, 0
        if kind == "neg" and node[1][0] == "num":
            return f"(-{node[1][1]})", False, 0
        if kind in ("col", "mref"):
            owner, column = (node[1], node[2]) if kind == "col" else (table.name, node[1])
            if kind == "mref" and column in self.dax:
                raise _Unsupported(f"SUMX row expression references the measure `[{column}]`")
            if owner != table.name:
                raise _Unsupported(
                    f"SUMX row expression references `{owner}[{column}]`, a column of another table"
                )
            _, info = self.column(owner, column)
            if info["spec"]["type"] != "number":
                raise _Unsupported(f"SUMX over the non-numeric column `{owner}[{column}]`")
            if not _SQL_IDENT.match(info["source"]):
                raise _Unsupported(
                    f"SUMX over `{owner}[{column}]`, whose source column `{info['source']}` "
                    "is not a plain SQL identifier"
                )
            return info["source"], True, 0
        if kind == "bin" and node[1] == "/":
            raise _Unsupported(
                "SUMX row expression divides; DAX division returns infinity or BLANK where "
                "SQL raises an error, returns NULL or truncates integers"
            )
        if kind == "bin" and node[1] in ("+", "-", "*"):
            op, level = node[1], 1 if node[1] == "*" else 2
            left, left_null, left_level = self.row_expression(node[2], table)
            right, right_null, right_level = self.row_expression(node[3], table)
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
                "SUMX row expression negates a column or an expression; only negative "
                "numbers are translated"
            )
        raise _Unsupported("SUMX row expression is not arithmetic over the table's columns")

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
            if len(args) == 3 and args[2] not in (("num", "0"), ("call", "BLANK", [])):
                raise _Unsupported("DIVIDE with an alternate result")
            return f"({self.arithmetic(args[0])}) / ({self.arithmetic(args[1])})"
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
        try:
            self._translate(pbi_name, source)
        except (_Unsupported, DaxError) as exc:
            self.failed[pbi_name] = str(exc)
            functions = sorted(set(_safe_functions(source["expression"])) - _TRANSLATABLE)
            hint = f" (DAX {', '.join(functions)} has no equivalent)" if functions else ""
            expr = " ".join(source["expression"].split())
            self.warnings.append(
                f"measure `{pbi_name}` not imported{hint}: {exc}. DAX: {expr[:120]}"
            )
        finally:
            self._in_progress.discard(pbi_name)

    def _translate(self, pbi_name: str, source: dict[str, Any]) -> None:
        tree = parse_dax(source["expression"])
        name = ident(pbi_name)
        description = source.get("description")
        meta = {"label": pbi_name, **({"description": description} if description else {})}
        home = self.tables.get(source["table"])
        kind = tree[0]

        if kind == "call" and tree[1] == "SUMX":
            self.done[pbi_name] = self.row_sum(tree, home, name, description)
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
            if len(args) == 3 and args[2] not in (("num", "0"), ("call", "BLANK", [])):
                raise _Unsupported("DIVIDE with an alternate result")
            if len(args) < 2:
                raise _Unsupported("DIVIDE needs two arguments")
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
            return
        if kind == "call" and tree[1] in _TO_DATE:
            args = tree[2]
            if len(args) != 2:
                raise _Unsupported(f"{tree[1]} with extra filters or a year-end date")
            inner = self.operand(args[0], f"{name}_base")
            if not self.is_measure(inner):
                raise _Unsupported(f"{tree[1]} over a metric; only measures accumulate")
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
        expr = self.arithmetic(tree)
        if expr.startswith("(") and expr.endswith(")"):
            expr = expr[1:-1]
        self.metric(pbi_name, {"name": name, "type": "derived", "expr": expr, **meta})

    def metric(self, pbi_name: str, spec: dict[str, Any]) -> None:
        while spec["name"] in self.measure_names or spec["name"] in {
            m["name"] for m in self.metrics
        }:
            spec["name"] = f"{spec['name']}_2"
        self.metrics.append(spec)
        self.done[pbi_name] = spec["name"]


_TRANSLATABLE = (
    set(_AGGS)
    | {"COUNTROWS", "DIVIDE", "CALCULATE", "KEEPFILTERS", "BLANK", "SUMX"}
    | set(_TO_DATE)
)


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

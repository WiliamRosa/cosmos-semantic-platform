"""Exporting the business glossary as SKOS/RDF, for data catalogs and thesaurus tools.

The glossary becomes a ``skos:ConceptScheme``: every concept with its labels per language,
definition, hierarchy (``broader``/``narrower``) and ``related`` links, both directions written
out so tools that do no inference still see them. A concept's ``maps_to`` points, through
``rdfs:isDefinedBy``, to an RDF resource for the governed member (metric, measure, dimension,
rule or entity) with its description, owner and, for rules, the effective date; a
``skos:scopeNote`` says the same in words for catalogs that only read SKOS.

Turtle, JSON-LD (expanded form) and N-Triples are written without third-party libraries.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cosmos_semantic.compiler import SemanticGraph

FORMATS = {"turtle": ".ttl", "jsonld": ".jsonld", "ntriples": ".nt"}

SKOS = "http://www.w3.org/2004/02/skos/core#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
DCT = "http://purl.org/dc/terms/"
XSD = "http://www.w3.org/2001/XMLSchema#"
PREFIXES = {"skos": SKOS, "rdf": RDF, "rdfs": RDFS, "dct": DCT, "xsd": XSD}

_KIND_CLASS = {
    "metric": "Metric",
    "measure": "Measure",
    "dimension": "Dimension",
    "rule": "BusinessRule",
    "entity": "Entity",
}


@dataclass(frozen=True)
class IRI:
    value: str


@dataclass(frozen=True)
class Literal:
    value: str
    lang: str | None = None
    datatype: str | None = None


Triple = tuple[IRI, IRI, IRI | Literal]


def _iri_part(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._~-]", lambda m: f"%{ord(m.group()):02X}", name)


class GlossaryExporter:
    def __init__(
        self,
        graph: SemanticGraph,
        *,
        base: str = "urn:cosmos-semantic:glossary:",
        title: str = "Business glossary",
        definition_lang: str = "en",
    ) -> None:
        self.graph = graph
        self.base = base if base.endswith(("/", "#", ":")) else base + "/"
        self.title = title
        self.definition_lang = definition_lang
        self.vocab = self.base + "vocab#"

    # IRIs ---------------------------------------------------------------------------------

    def scheme(self) -> IRI:
        return IRI(self.base + "scheme")

    def concept(self, name: str) -> IRI:
        return IRI(f"{self.base}concept/{_iri_part(name)}")

    def member(self, kind: str, name: str) -> IRI:
        return IRI(f"{self.base}{kind}/{_iri_part(name)}")

    def term(self, local: str) -> IRI:
        return IRI(self.vocab + local)

    # Triples --------------------------------------------------------------------------

    def triples(self) -> list[Triple]:
        g = self.graph
        concepts = g.project.glossary
        names = {c.concept for c in concepts}
        out: list[Triple] = []
        add = out.append
        scheme = self.scheme()
        add((scheme, IRI(RDF + "type"), IRI(SKOS + "ConceptScheme")))
        add((scheme, IRI(DCT + "title"), Literal(self.title, "en")))
        add(
            (
                scheme,
                IRI(DCT + "modified"),
                Literal(dt.date.today().isoformat(), datatype=XSD + "date"),
            )
        )

        narrower: dict[str, list[str]] = {}
        related: dict[str, set[str]] = {}
        for c in concepts:
            if c.broader in names:
                narrower.setdefault(c.broader, []).append(c.concept)
            for other in c.related:
                if other in names:
                    related.setdefault(c.concept, set()).add(other)
                    related.setdefault(other, set()).add(c.concept)

        members: dict[tuple[str, str], IRI] = {}
        for c in concepts:
            me = self.concept(c.concept)
            add((me, IRI(RDF + "type"), IRI(SKOS + "Concept")))
            add((me, IRI(SKOS + "inScheme"), scheme))
            add((me, IRI(SKOS + "notation"), Literal(c.concept)))
            for lang, label in sorted(c.pref_label.items()):
                add((me, IRI(SKOS + "prefLabel"), Literal(label, lang)))
            for lang, labels in sorted(c.alt_labels.items()):
                for label in labels:
                    add((me, IRI(SKOS + "altLabel"), Literal(label, lang)))
            if c.definition:
                add((me, IRI(SKOS + "definition"), Literal(c.definition, self.definition_lang)))
            if c.broader in names:
                add((me, IRI(SKOS + "broader"), self.concept(c.broader)))
            else:
                add((me, IRI(SKOS + "topConceptOf"), scheme))
                add((scheme, IRI(SKOS + "hasTopConcept"), me))
            for child in narrower.get(c.concept, []):
                add((me, IRI(SKOS + "narrower"), self.concept(child)))
            for other in sorted(related.get(c.concept, set())):
                add((me, IRI(SKOS + "related"), self.concept(other)))
            if c.maps_to:
                kind, _, target = c.maps_to.partition(":")
                if kind == "metric" and target in g.measure_owner:
                    kind = "measure"
                key = (kind, target)
                if key not in members:
                    members[key] = self.member(kind, target)
                    out += self._member(kind, target, members[key])
                add((me, IRI(RDFS + "isDefinedBy"), members[key]))
                add((me, IRI(SKOS + "scopeNote"), Literal(self._note(kind, target), "en")))
        return out

    def _describe(self, kind: str, name: str) -> tuple[str | None, str | None, Any]:
        """(description, owner, effective date) of a governed member, as far as known."""
        g = self.graph
        if kind == "metric" and name in g.metrics:
            m = g.metrics[name]
            return m.description, m.owner, None
        if kind == "measure" and name in g.measure_owner:
            model = g.models[g.measure_owner[name]]
            return model.measures[name].description, model.spec.owner, None
        if kind == "rule" and name in g.rules:
            r = g.rules[name]
            return r.description, r.owner, r.effective_from
        if kind == "dimension" and "." in name:
            model_name, _, dim = name.partition(".")
            model = g.models.get(model_name)
            if model and dim in model.dimensions:
                return model.dimensions[dim].description, model.spec.owner, None
        return None, None, None

    def _member(self, kind: str, name: str, me: IRI) -> list[Triple]:
        description, owner, since = self._describe(kind, name)
        out: list[Triple] = [
            (me, IRI(RDF + "type"), self.term(_KIND_CLASS.get(kind, "Member"))),
            (me, IRI(RDFS + "label"), Literal(name)),
            (me, IRI(DCT + "identifier"), Literal(f"{kind}:{name}")),
        ]
        if description:
            out.append((me, IRI(DCT + "description"), Literal(description, "en")))
        if owner:
            out.append((me, self.term("owner"), Literal(owner)))
        if since:
            out.append((me, IRI(DCT + "valid"), Literal(since.isoformat(), datatype=XSD + "date")))
        return out

    def _note(self, kind: str, name: str) -> str:
        description, owner, _ = self._describe(kind, name)
        text = f"Defined by the {kind} `{name}` in the Cosmos Semantic model"
        if description:
            text += f": {description}"
        if owner:
            text += f" (owner: {owner})"
        return text + "."

    # Serializers --------------------------------------------------------------------------

    def serialize(self, fmt: str = "turtle") -> str:
        triples = self.triples()
        if fmt == "turtle":
            return _turtle(triples)
        if fmt == "ntriples":
            return "".join(f"{_nt(s)} {_nt(p)} {_nt(o)} .\n" for s, p, o in triples)
        if fmt == "jsonld":
            return json.dumps(_jsonld(triples), ensure_ascii=False, indent=2) + "\n"
        raise ValueError(f"unknown format `{fmt}`; use one of {', '.join(FORMATS)}")


def _escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _nt(term: IRI | Literal) -> str:
    if isinstance(term, IRI):
        return f"<{term.value}>"
    text = f'"{_escape(term.value)}"'
    if term.lang:
        return f"{text}@{term.lang}"
    if term.datatype:
        return f"{text}^^<{term.datatype}>"
    return text


def _short(iri: IRI) -> str:
    for prefix, namespace in PREFIXES.items():
        local = iri.value.removeprefix(namespace)
        if local != iri.value and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", local):
            return f"{prefix}:{local}"
    return f"<{iri.value}>"


def _turtle(triples: list[Triple]) -> str:
    lines = [f"@prefix {p}: <{ns}> ." for p, ns in PREFIXES.items()]
    subjects: dict[IRI, list[tuple[IRI, IRI | Literal]]] = {}
    for s, p, o in triples:
        subjects.setdefault(s, []).append((p, o))
    for subject, pairs in subjects.items():
        lines.append("")
        by_predicate: dict[IRI, list[str]] = {}
        for p, o in pairs:
            text = _short(o) if isinstance(o, IRI) else _nt(o)
            if isinstance(o, Literal) and o.datatype:
                text = f'"{_escape(o.value)}"^^{_short(IRI(o.datatype))}'
            by_predicate.setdefault(p, []).append(text)
        parts = []
        for p, objects in by_predicate.items():
            name = "a" if p.value == RDF + "type" else _short(p)
            parts.append(f"    {name} " + ",\n        ".join(objects))
        lines.append(f"{_short(subject)}\n" + " ;\n".join(parts) + " .")
    return "\n".join(lines) + "\n"


def _jsonld(triples: list[Triple]) -> list[dict[str, Any]]:
    """Expanded JSON-LD: valid without a context, readable by any JSON-LD processor."""
    nodes: dict[str, dict[str, Any]] = {}
    for s, p, o in triples:
        node = nodes.setdefault(s.value, {"@id": s.value})
        if p.value == RDF + "type":
            node.setdefault("@type", []).append(o.value)
            continue
        if isinstance(o, IRI):
            value: dict[str, Any] = {"@id": o.value}
        else:
            value = {"@value": o.value}
            if o.lang:
                value["@language"] = o.lang
            elif o.datatype:
                value["@type"] = o.datatype
        node.setdefault(p.value, []).append(value)
    return list(nodes.values())


def export_glossary(graph: SemanticGraph, fmt: str = "turtle", **options: Any) -> str:
    return GlossaryExporter(graph, **options).serialize(fmt)

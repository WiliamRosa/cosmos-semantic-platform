"""SKOS/RDF export of the glossary: valid RDF in every format, SKOS integrity, catalog links."""

from __future__ import annotations

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCTERMS, RDF, RDFS, SKOS, XSD

from cosmos_semantic.compiler import compile_project
from cosmos_semantic.interchange.skos import FORMATS, export_glossary
from cosmos_semantic.spec import Project

RDFLIB_FORMAT = {"turtle": "turtle", "jsonld": "json-ld", "ntriples": "nt"}
BASE = "https://data.example.com/glossary/"
G = Namespace(BASE)


def parse(graph, fmt="turtle", **options) -> Graph:
    rdf = Graph()
    rdf.parse(data=export_glossary(graph, fmt, **options), format=RDFLIB_FORMAT[fmt])
    return rdf


def test_every_format_gives_the_same_graph(graph):
    graphs = [parse(graph, fmt, base=BASE) for fmt in FORMATS]
    assert len(graphs[0]) > 50
    for other in graphs[1:]:
        assert isomorphic(graphs[0], other)


def test_concepts_labels_and_hierarchy(graph):
    rdf = parse(graph, base=BASE)
    concepts = set(rdf.subjects(RDF.type, SKOS.Concept))
    assert concepts == {G[f"concept/{c.concept}"] for c in graph.project.glossary}
    active = G["concept/active_customer"]
    assert (active, SKOS.prefLabel, Literal("Cliente ativo", lang="pt")) in rdf
    assert (active, SKOS.altLabel, Literal("active buyer", lang="en")) in rdf
    # Both directions are written, for tools without inference.
    assert (active, SKOS.broader, G["concept/customer"]) in rdf
    assert (G["concept/customer"], SKOS.narrower, active) in rdf
    tops = set(rdf.objects(G.scheme, SKOS.hasTopConcept))
    assert G["concept/customer"] in tops and active not in tops
    assert all((c, SKOS.inScheme, G.scheme) in rdf for c in concepts)


def test_skos_integrity(graph):
    """S14: at most one prefLabel per language; prefLabel and altLabel are disjoint (S13)."""
    rdf = parse(graph, base=BASE)
    for concept in rdf.subjects(RDF.type, SKOS.Concept):
        prefs = list(rdf.objects(concept, SKOS.prefLabel))
        assert len({p.language for p in prefs}) == len(prefs)
        assert not set(prefs) & set(rdf.objects(concept, SKOS.altLabel))
        for narrower in rdf.objects(concept, SKOS.narrower):
            assert (narrower, SKOS.broader, concept) in rdf
        for other in rdf.objects(concept, SKOS.related):
            assert (other, SKOS.related, concept) in rdf


def test_concepts_link_to_governed_members(graph):
    rdf = parse(graph, base=BASE)
    rule = G["rule/active_customer"]
    assert (G["concept/active_customer"], RDFS.isDefinedBy, rule) in rdf
    assert (rule, RDF.type, URIRef(BASE + "vocab#BusinessRule")) in rdf
    assert (rule, URIRef(BASE + "vocab#owner"), Literal("sales-leadership")) in rdf
    assert (rule, DCTERMS.valid, Literal("2026-07-01", datatype=XSD.date)) in rdf
    # `metric:revenue` in the glossary is a measure in the model.
    revenue = G["measure/revenue"]
    assert (G["concept/gross_revenue"], RDFS.isDefinedBy, revenue) in rdf
    assert (revenue, URIRef(BASE + "vocab#owner"), Literal("digital-commerce")) in rdf
    note = str(rdf.value(G["concept/net_revenue"], SKOS.scopeNote))
    assert "metric `net_revenue`" in note and "controllership" in note


def test_escaping_and_related_links(graph):
    data = graph.project.model_dump(mode="json")
    data["glossary"].append(
        {
            "concept": "tricky",
            "pref_label": {"en": 'Say "hi"\\now', "pt": "Ação\tlinha"},
            "definition": "Line one\nline two",
            "related": ["customer"],
        }
    )
    other = compile_project(Project.model_validate(data), strict=False)
    for fmt in FORMATS:
        rdf = parse(other, fmt, base=BASE)
        tricky = G["concept/tricky"]
        assert (tricky, SKOS.prefLabel, Literal('Say "hi"\\now', lang="en")) in rdf
        assert (tricky, SKOS.definition, Literal("Line one\nline two", lang="en")) in rdf
        assert (G["concept/customer"], SKOS.related, tricky) in rdf


def test_default_base_and_unknown_format(graph):
    rdf = parse(graph)
    assert (URIRef("urn:cosmos-semantic:glossary:scheme"), RDF.type, SKOS.ConceptScheme) in rdf
    with pytest.raises(ValueError, match="unknown format"):
        export_glossary(graph, "rdfxml")

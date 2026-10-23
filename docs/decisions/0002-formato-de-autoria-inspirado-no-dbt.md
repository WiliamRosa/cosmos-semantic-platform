# ADR 0002: Formato de autoria inspirado no dbt Semantic Layer

- **Status:** aceita (revisada em 15/10/2026 após o [ADR 0004](0004-motor-semantico-proprio.md); complementada em 22/10/2026 pelo [ADR 0007](0007-conectores-origens-identidade-e-importacao.md))
- **Data:** 14/10/2026

## Contexto

O dbt Semantic Layer, com o motor MetricFlow, usa entidades para inferir joins e tem tipos de métrica explícitos, o que torna a modelagem mais simples e menos sujeita a erro do que declarar joins e métricas compostas à mão, como no Cube.

O MetricFlow está sob Apache 2.0 desde a versão 0.209.0, mas hoje funciona como biblioteca acoplada a um projeto dbt e a um adapter dbt ([repositório](https://github.com/dbt-labs/metricflow)).

## Decisão

Criar um formato de autoria próprio, com os conceitos do dbt Semantic Layer (entidades, tipos de métrica), que alimenta o **motor semântico próprio** do produto. O MetricFlow serve como **referência de design**, não como dependência. O mesmo modelo pode ser exportado para o YAML do Cube, para o YAML do dbt e para o Ossie. Especificação em [modelo semântico](../modelo-semantico.md).

## Alternativas consideradas

| Alternativa | Por que não |
| --- | --- |
| Adotar o YAML do Cube como formato | Prende o produto ao modelo de outro fornecedor e não traz entidades nem tipos de métrica |
| Usar o MetricFlow como motor | Exige projeto dbt e adapter dbt para funcionar |
| Usar o Ossie como formato de autoria | O padrão ainda é rascunho e não tem entidades nem tipos de métrica; serve melhor como formato de troca |

## Consequências

- O formato precisa de versionamento próprio, para evoluir sem quebrar projetos existentes.
- Cada exportação (Cube, dbt, Ossie) precisa de testes que comparem os resultados com o motor próprio.
- Conceitos que não têm equivalente direto em algum destino de exportação geram aviso na exportação, nunca perda silenciosa.

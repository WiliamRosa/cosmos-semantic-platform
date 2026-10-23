# ADR 0003: Apache Ossie como formato de intercâmbio

- **Status:** aceita
- **Data:** 14/10/2026

## Contexto

O produto precisa seguir o padrão aberto do mercado para não aprisionar o usuário e para conversar com outras ferramentas. O Open Semantic Interchange (OSI), lançado em 2025, virou o Apache Ossie, em incubação na Apache Software Foundation, com participação de dbt Labs, Snowflake, Cube e AtScale. A especificação está em rascunho (`0.2.0.dev0`) e tem mecanismo de extensão (`custom_extensions`).

## Decisão

O Ossie é o **formato de importação e exportação** do produto, não o formato de autoria. Todo modelo pode ser exportado em Ossie e reimportado sem perda, usando `custom_extensions` para o que o padrão não cobre. Detalhes em [interoperabilidade com Ossie](../interoperabilidade-ossie.md).

## Consequências

- O compilador tem um módulo de mapeamento isolado, testado por versão do schema do Ossie.
- O teste de ciclo completo (exportar, reimportar, comparar) faz parte do CI.
- Quando o padrão estabilizar e cobrir entidades e tipos de métrica, reavaliar se ele pode virar também o formato de autoria.

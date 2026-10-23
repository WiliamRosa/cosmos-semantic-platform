# Interoperabilidade: Ossie, dbt e Cube

O [Apache Ossie](https://github.com/open-semantic-interchange/OSI), antes chamado Open Semantic Interchange (OSI), é o padrão aberto para trocar modelos semânticos entre ferramentas. Foi lançado em setembro de 2025 pela Snowflake, Salesforce e dbt Labs, está em incubação na Apache Software Foundation sob licença Apache 2.0, e reúne participantes como Cube, AtScale, Collibra e Starburst. Ver [ADR 0003](decisions/0003-ossie-como-formato-de-intercambio.md).

## Estado do padrão

| Item | Situação em outubro de 2026 |
| --- | --- |
| Versão em desenvolvimento | `0.2.0.dev0`, rascunho, o schema ainda pode mudar |
| Última versão publicada | `0.1.1` |
| Formato | JSON e YAML, com schema em `core-spec/` |
| Conversores de referência | dbt, GoodData, Polaris, Salesforce |
| Extensões | `custom_extensions` em todos os níveis, com `vendor_name` e `data` em JSON |

Fonte: [especificação do Ossie](https://github.com/open-semantic-interchange/OSI/blob/main/core-spec/spec.md).

## Mapeamento com o formato de autoria

| Formato de autoria | Ossie | Observação |
| --- | --- | --- |
| Projeto | Semantic model (raiz) | `version`, `name`, `datasets`, `relationships`, `metrics` |
| `semantic_models[]` | `datasets[]` | `table` corresponde a `source` |
| Entidade `primary` | `primary_key` do dataset | |
| Entidade `unique` | `unique_keys` do dataset | |
| Entidade `foreign` | `relationships[]` explícitas, com `from`, `to`, `from_columns` e `to_columns` | Na exportação, os joins inferidos viram relações explícitas; na importação, relações viram entidades |
| `dimensions[]` | `fields[]` com `dimension` (e `is_time` para tempo) | |
| `measures[]` | `metrics[]` com expressão de agregação | O Ossie não separa medida de métrica; a separação fica numa extensão |
| `metrics[]` | `metrics[]` | Tipo da métrica (razão, cumulativa) guardado em `custom_extensions` |
| `description` | `description` | |
| `row_filters` e `sensitivity` | `custom_extensions` | Políticas de acesso e LGPD não têm campo nativo no Ossie |
| Glossário: `alt_labels` e `definition` | `ai_context` (`synonyms`, `instructions`) do membro ligado pelo `maps_to` | Campo nativo do Ossie |
| Glossário: conceitos, hierarquia e rótulos por idioma | `custom_extensions` na raiz do modelo | O Ossie não tem um glossário próprio |
| Regras de negócio | `fields[]` com a expressão compilada em cada dataset de `applies_to` | Dono, vigência, tabela de decisão e testes ficam em `custom_extensions` |

## Regras

1. **Expressões:** o Ossie guarda expressões por dialeto (`expression.dialects`). Só o dialeto `OSSIE_SQL_2026` tem semântica definida pelo padrão; os demais são texto opaco. Na exportação, o produto escreve `OSSIE_SQL_2026` e, quando útil, o dialeto da origem. Na importação, prefere `OSSIE_SQL_2026`.
2. **Extensões:** tudo o que o Ossie não representa (tipo de métrica, separação entre medida e métrica, janela cumulativa) vai em `custom_extensions` com o `vendor_name` do produto. Um modelo exportado e reimportado não pode perder informação.
3. **Métricas entre datasets:** o Ossie permite métricas que atravessam datasets, e o motor próprio também, porque agrega cada medida no próprio grão. A importação as aceita; só a exportação para o Cube gera aviso para elas.
4. **Ciclo completo:** exportar e reimportar o mesmo modelo deve gerar exatamente o mesmo modelo de autoria. Isso vira um teste automático.

## Importação na fase 1

A fase 1 importa três formatos e converte tudo para o formato do Cosmos ([ADR 0007](decisions/0007-conectores-origens-identidade-e-importacao.md)). Nenhuma conversão perde informação em silêncio: o que não tem equivalente vai para `custom_extensions` ou vira aviso no relatório da importação.

| Formato | O que entra | Limite |
| --- | --- | --- |
| dbt | `semantic_models` e `metrics`, no formato atual e no legado | Lê o YAML sem executar o projeto dbt; filtros com `Dimension(...)` viram a sintaxe `{dimensao}` |
| Ossie | O documento inteiro, incluindo `custom_extensions` | Só o dialeto `OSSIE_SQL_2026` tem semântica garantida |
| Cube | YAML estático: cubes, dimensões, medidas, joins e segmentos | Modelos em JavaScript e YAML com templates Jinja ou Python geram aviso e não são importados |

## Importação do Cube

O Cube é a camada semântica open source mais adotada, então importar dele é o caminho de migração mais curto para novos usuários. Junto com a exportação, fecha a ida e volta com o Cube.

| No Cube | No Cosmos | Conversão |
| --- | --- | --- |
| `cubes` com `sql_table` ou `sql` | Modelo semântico | Direta |
| Dimensão com `primary_key: true` | Entidade primária | Direta |
| `joins` com igualdade simples (`{CUBE}.customer_id = {customers}.id`) | Entidade estrangeira | Direta; condições complexas geram aviso |
| Medidas `count`, `count_distinct`, `sum`, `avg`, `min`, `max` | Medida do mesmo tipo | Direta |
| Medida `number` que referencia outras medidas | Métrica derivada | Exige ler a expressão SQL |
| `segments` | Regra de negócio | Entra com dono e vigência pendentes; a validação avisa até serem preenchidos |
| `rolling_window` e medidas em várias etapas | Métrica acumulada (fora do MVP) | Aviso e `custom_extensions` |
| `pre_aggregations` e `views` | Sem equivalente no MVP | Guardados em `custom_extensions` |
| Controle de acesso no `cube.js` | `row_filters` e `sensitivity` | Manual: é código JavaScript; o relatório lista as regras a recriar |

Importar um modelo do Cube e exportá-lo de volta deve gerar o mesmo YAML estático, exceto o que o relatório registrou como aviso; isso vira teste automático, como no Ossie.

## Riscos

- O padrão ainda está em rascunho. O compilador deve isolar o mapeamento numa camada própria, com testes por versão do schema.
- O repositório do Ossie já tem conversor de referência para o dbt; avaliar se ele substitui parte do exportador de dbt do produto.
- O dbt reformulou o formato YAML em 2026 e mantém o anterior como legado. O importador de dbt fica isolado e testado por versão do formato.

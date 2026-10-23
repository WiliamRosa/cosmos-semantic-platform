# Modelo semântico

O usuário escreve num formato YAML próprio, com o vocabulário do dbt Semantic Layer (entidades, dimensões, medidas e tipos de métrica) e uma camada de ontologia (glossário SKOS e regras de negócio) por cima. O motor próprio lê esse modelo, planeja cada consulta e gera o SQL da origem. Modelos de dbt, Ossie e Cube entram por importação, e o mesmo modelo pode ser exportado para Ossie, Cube e dbt. Ver [ADR 0002](decisions/0002-formato-de-autoria-inspirado-no-dbt.md), [ADR 0004](decisions/0004-motor-semantico-proprio.md) e [ADR 0007](decisions/0007-conectores-origens-identidade-e-importacao.md).

## Versão e JSON Schema

Cada arquivo começa com `version: 1`. O JSON Schema do formato fica em `schema/cosmos-semantic.v1.schema.json`, gerado a partir dos modelos Pydantic do compilador (`cosmos schema`). Com ele, o VS Code e outros editores validam e completam o YAML sem plugin:

```yaml
# yaml-language-server: $schema=../../schema/cosmos-semantic.v1.schema.json
version: 1
```

O vocabulário do dbt fica para que quem já usa dbt leia o modelo de primeira, mas o Cosmos não depende do formato do dbt, que foi reformulado em 2026 e mantém o anterior como legado.

## Exemplo completo

```yaml
version: 1

semantic_models:
  - name: orders
    table: public.orders
    description: Pedidos de venda, um registro por pedido
    entities:
      - name: order
        type: primary
        expr: order_id
      - name: customer
        type: foreign
        expr: customer_id
    dimensions:
      - name: ordered_at
        type: time
      - name: status
        type: categorical
    measures:
      - name: order_count
        agg: count
      - name: revenue
        agg: sum
        expr: amount
        description: Valor bruto dos pedidos
      - name: discounts
        agg: sum
        expr: discount

  - name: customers
    table: public.customers
    entities:
      - name: customer
        type: primary
        expr: customer_id
    dimensions:
      - name: region
        type: categorical
      - name: segment
        type: categorical
      - name: email
        type: categorical
        sensitivity: personal
    measures:
      - name: customer_count
        agg: count_distinct
        expr: customer_id
    row_filters:
      - name: por_regiao
        filter: "{region} = $user.region"

metrics:
  - name: completed_revenue
    type: simple
    measure: revenue
    filter: "{status} = 'completed'"
  - name: average_order_value
    type: ratio
    numerator: revenue
    denominator: order_count
  - name: net_revenue
    type: derived
    expr: "{revenue} - {discounts}"
  - name: revenue_month_to_date
    type: cumulative
    measure: revenue
    grain_to_date: month
```

## Elementos

### Semantic model

Uma tabela ou view da origem.

| Campo | Obrigatório | Descrição |
| --- | --- | --- |
| `name` | Sim | Nome único no projeto |
| `table` | Sim | Tabela ou view na origem |
| `description` | Não | Texto para pessoas e para agentes de IA |
| `data_source` | Não | Nome da origem, quando há mais de uma |
| `entities` | Não | Chaves que ligam modelos entre si |
| `dimensions` | Não | Atributos para agrupar e filtrar |
| `measures` | Não | Agregações básicas |
| `row_filters` | Não | Filtros aplicados conforme o contexto do usuário |

### Entidades

Em vez de declarar joins, o usuário declara chaves.

| Tipo | Significado |
| --- | --- |
| `primary` | Identifica uma linha do modelo; no máximo um modelo por nome de entidade |
| `unique` | Valor único no modelo, sem ser a chave principal |
| `foreign` | Referência à entidade primária ou única de outro modelo |

Uma entidade `foreign` num modelo A e a mesma entidade `primary` ou `unique` num modelo B formam uma aresta do grafo de joins. O planejador percorre esse grafo, inclusive com vários saltos (pedidos → clientes → regiões). Caminho ambíguo é erro.

### Dimensões

`type` aceita `categorical`, `time`, `number` e `boolean`. `expr` é opcional; sem ele, usa-se a coluna com o mesmo nome. `sensitivity` aceita `personal` (dado pessoal) e `sensitive` (dado pessoal sensível, como saúde): essas colunas nunca chegam a um agente com o valor bruto, só agregadas ou mascaradas.

### Medidas

`agg` aceita `count`, `count_distinct`, `sum`, `avg`, `min` e `max`. Nomes de medidas são únicos no projeto.

### Métricas

| Tipo | Campos | Comportamento |
| --- | --- | --- |
| `simple` | `measure`, `filter` opcional | A medida, com filtro opcional |
| `ratio` | `numerator`, `denominator` | Divisão protegida contra zero; cada lado agregado no próprio grão |
| `derived` | `expr` com referências `{medida}` | Expressão sobre medidas já agregadas |
| `cumulative` | `measure` e `window` ou `grain_to_date` | Janela móvel ou acumulado no período |

Como o planejador agrega cada medida no próprio grão antes de combinar, razão e derivada podem usar medidas de modelos diferentes sem multiplicação de linhas.

### Filtros por linha

`row_filters` usa `{dimensao}` para membros do modelo e `$user.<campo>` para declarações do token do usuário. Todos os filtros do modelo são aplicados sempre que ele participa de uma consulta.

### Contexto do usuário

`user_context` declara cada campo de `$user` e de qual declaração do token ele vem, porque cada provedor de identidade usa nomes diferentes. Se faltar uma declaração exigida, a consulta é negada.

```yaml
user_context:
  region:
    claim: region        # nome da declaração no token do provedor
    required: true       # sem ela, a consulta é negada
```

## Glossário (SKOS)

O glossário descreve os conceitos do negócio no estilo [SKOS](https://www.w3.org/TR/skos-reference/), o padrão do W3C para vocabulários. Cada conceito aponta para o membro do modelo ou para a regra que o define. Ver [ADR 0006](decisions/0006-ontologia-no-mvp.md).

```yaml
glossary:
  - concept: cliente
    pref_label: { pt: Cliente, en: Customer }
    alt_labels: { pt: [comprador, consumidor] }
    definition: Pessoa ou empresa que fez pelo menos um pedido
    maps_to: entity:customer

  - concept: cliente_ativo
    pref_label: { pt: Cliente ativo, en: Active customer }
    alt_labels: { pt: [cliente recorrente, comprador ativo] }
    broader: cliente
    definition: Cliente com compra nos últimos 90 dias
    maps_to: rule:cliente_ativo

  - concept: receita_liquida
    pref_label: { pt: Receita líquida, en: Net revenue }
    alt_labels: { pt: [faturamento líquido] }
    definition: Receita bruta menos descontos
    maps_to: metric:net_revenue
```

| Campo | Equivalente SKOS | Uso |
| --- | --- | --- |
| `concept` | Identificador do conceito | Nome único no projeto |
| `pref_label` | `skos:prefLabel` | Nome oficial, por idioma |
| `alt_labels` | `skos:altLabel` | Sinônimos que o agente reconhece |
| `definition` | `skos:definition` | Definição em linguagem natural |
| `broader` | `skos:broader` | Hierarquia "é um tipo de" |
| `related` | `skos:related` | Ligação entre conceitos sem hierarquia (opcional) |
| `maps_to` | Extensão própria | Liga o conceito a `entity:`, `dimension:`, `metric:` ou `rule:` |

## Regras de negócio

Regras de negócio são lógica com dono, versão e testes, reaproveitável em vários modelos.

```yaml
business_rules:
  - name: cliente_ativo
    kind: predicate
    owner: diretoria-comercial
    effective_from: 2026-01-01
    description: Cliente com compra nos últimos 90 dias
    inputs:
      ultima_compra: time
    logic: "{ultima_compra} >= CURRENT_DATE - INTERVAL '90' DAY"
    applies_to:
      - model: customers
        bind: { ultima_compra: last_order_at }
    tests:
      - given: { ultima_compra: 2026-10-01 }
        today: 2026-10-16
        expect: true
      - given: { ultima_compra: 2026-05-01 }
        today: 2026-10-16
        expect: false

  - name: faixa_cliente
    kind: classification
    owner: diretoria-comercial
    effective_from: 2026-01-01
    inputs:
      receita_12m: number
    decision_table:
      - when: "{receita_12m} >= 100000"
        then: Ouro
      - when: "{receita_12m} >= 20000"
        then: Prata
      - otherwise: Bronze
    applies_to:
      - model: customer_summary
        bind: { receita_12m: revenue_12m }
```

| `kind` | Resultado | Vira no motor | Uso típico |
| --- | --- | --- | --- |
| `predicate` | Verdadeiro ou falso | Filtro nomeado e dimensão booleana | `filter: "rule:cliente_ativo"` numa métrica ou numa consulta |
| `classification` | Categoria | Dimensão categórica (`CASE WHEN`) | Agrupar por faixa do cliente |
| `derived` | Valor | Dimensão numérica | Margem do item |

| Campo | Obrigatório | Descrição |
| --- | --- | --- |
| `name`, `kind` | Sim | Nome único e tipo |
| `owner` | Sim | Área ou pessoa responsável pela regra |
| `effective_from` | Sim | Desde quando a versão atual vale |
| `inputs` | Sim | Entradas nomeadas, com tipo |
| `logic` ou `decision_table` | Sim | Expressão, ou tabela de decisão (só em `classification`) |
| `applies_to` | Sim | Modelos onde a regra vale, com o mapeamento das entradas para colunas ou dimensões |
| `tests` | Recomendado | Exemplos de entrada e saída esperada, executados em CI |

**Como uma pergunta atravessa a ontologia:** "quantos compradores ativos temos no Sul?" → o sinônimo "comprador ativo" leva ao conceito `cliente_ativo` → que é definido pela regra `cliente_ativo` → que vira um filtro em `customers`:

```sql
SELECT COUNT(DISTINCT customers.customer_id) AS customer_count
FROM public.customers AS customers
WHERE customers.last_order_at >= CURRENT_DATE - INTERVAL '90' DAY   -- regra cliente_ativo
  AND customers.region = 'Sul'
```

**Limites:** as regras usam só colunas da linha (classificações sobre agregados exigem um modelo de resumo, como `customer_summary`); as consultas usam sempre a versão vigente da regra; o glossário não faz inferência lógica.

## O SQL que o motor gera

Os exemplos abaixo são ilustrativos do resultado esperado, em Postgres. O mesmo plano gera SQL em outros dialetos pelo SQLGlot.

**Pedido simples:** receita e ticket médio por região do cliente, por mês.

```sql
SELECT
  DATE_TRUNC('MONTH', orders.ordered_at) AS ordered_at__month,
  customers.region AS customers__region,
  SUM(orders.amount) AS revenue,
  1.0 * SUM(orders.amount) / NULLIF(COUNT(*), 0) AS average_order_value
FROM public.orders AS orders
LEFT JOIN public.customers AS customers
  ON orders.customer_id = customers.customer_id
WHERE customers.region = 'Sul'   -- filtro por linha do usuário
GROUP BY 1, 2
```

**Pedido com medidas de modelos diferentes:** receita (de `orders`) e número de clientes (de `customers`) por região. Juntar e somar direto multiplicaria linhas; por isso cada medida é agregada no próprio grão.

```sql
WITH orders_agg AS (
  SELECT customers.region AS customers__region, SUM(orders.amount) AS revenue
  FROM public.orders AS orders
  LEFT JOIN public.customers AS customers
    ON orders.customer_id = customers.customer_id
  GROUP BY 1
),
customers_agg AS (
  SELECT customers.region AS customers__region, COUNT(DISTINCT customers.customer_id) AS customer_count
  FROM public.customers AS customers
  GROUP BY 1
)
SELECT
  COALESCE(o.customers__region, c.customers__region) AS customers__region,
  o.revenue,
  c.customer_count
FROM orders_agg AS o
FULL OUTER JOIN customers_agg AS c
  ON o.customers__region = c.customers__region
```

## Exportação para o Cube

Quem precisa de cache, pré-agregações ou conexão com BI pode exportar o modelo para o Cube. O exemplo acima gera, entre outros, este cube:

```yaml
cubes:
  - name: orders
    sql_table: public.orders
    joins:
      - name: customers
        relationship: many_to_one
        sql: "{CUBE}.customer_id = {customers}.customer_id"
    dimensions:
      - name: order
        sql: order_id
        type: number
        primary_key: true
      - name: ordered_at
        sql: ordered_at
        type: time
      - name: status
        sql: status
        type: string
    measures:
      - name: revenue
        type: sum
        sql: amount
      - name: completed_revenue
        type: sum
        sql: amount
        filters:
          - sql: "{CUBE}.status = 'completed'"
      - name: average_order_value
        type: number
        sql: "1.0 * {revenue} / NULLIF({order_count}, 0)"
      - name: revenue_month_to_date
        type: sum
        sql: amount
        rolling_window:
          type: to_date
          granularity: month
```

O que o Cube não representa do mesmo jeito gera aviso na exportação:

- métricas de razão ou derivadas que combinam modelos diferentes, porque no Cube uma medida pertence a um cube;
- `row_filters` e `sensitivity`, que no Cube viram configuração (`query_rewrite`) e não modelo.

## Validações

- Nomes duplicados num modelo, e de medidas ou métricas no projeto.
- Entidade `primary` ou `unique` repetida em mais de um modelo.
- Caminho de join ambíguo entre dois modelos.
- Métrica que referencia medida ou dimensão inexistente, ou sem os campos do seu tipo.
- Conceito cujo `maps_to` aponta para membro ou regra inexistente; ciclo na hierarquia `broader`.
- Mesmo sinônimo em dois conceitos diferentes: aviso, porque o agente não saberia qual escolher.
- Regra com entrada não mapeada em algum modelo de `applies_to`, ou mapeada para coluna de tipo incompatível.
- Tabela de decisão sem `otherwise`.
- Teste de regra cujo resultado difere do esperado.

## Fora do MVP

- Métricas de conversão (funil).
- Time spine para séries sem buracos em dias sem dados.
- Joins `one_to_many` como ponto de partida de consultas.

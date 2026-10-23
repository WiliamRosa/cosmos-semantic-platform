# ADR 0007: Conectores, origens, identidade e importação de dbt, Ossie e Cube

- **Status:** aceita
- **Data:** 22/10/2026

## Contexto

Antes de começar o código, quatro questões estavam abertas: a biblioteca dos conectores, a ordem das origens e os dados de teste, quem emite o token que alimenta os filtros por linha, e até que ponto o formato de autoria segue o dbt. Os critérios foram competitividade, diferencial, modernização e flexibilidade.

## Decisão

| Questão | Decisão |
| --- | --- |
| Conectores | Interface `Connector` própria que sempre devolve resultados em Arrow. ADBC é a implementação padrão; SQLAlchemy é a reserva para origens sem driver ADBC maduro |
| Origens | Postgres para desenvolver (roda no `docker compose`, testes gratuitos no CI); Databricks é a primeira origem validada, no gate 1; SQL Server entra na fase 1 e completa o gate 2 |
| Dados de teste | Gerador sintético no repositório, com Faker em pt_BR (e-commerce com CPF, e-mail e um campo sensível); TPC-H em escala maior para desempenho. O dataset Olist fica de fora porque a licença é não comercial |
| Identidade | O Cosmos é servidor de recursos OIDC: valida tokens de um provedor externo pelo JWKS (Entra ID primeiro; Keycloak, Okta e Auth0 pelo mesmo caminho) e não emite tokens. Chaves de API ligadas a um contexto fixo só no modo de desenvolvimento |
| Contexto do usuário | O modelo declara em `user_context` cada campo de `$user` e de qual declaração do token ele vem; se faltar uma declaração exigida, a consulta é negada. Agentes consultam com o token do usuário (fluxo on-behalf-of) |
| Formato de autoria | YAML próprio com o vocabulário do dbt, versionado (`version: 1`) e com JSON Schema gerado dos modelos Pydantic |
| Importação na fase 1 | dbt (formato atual e legado), Ossie e Cube (só YAML estático) |

As 20 consultas de referência cobrem as armadilhas do motor: fan-out com medidas em grãos diferentes (4), métrica simples e por granularidade (3), joins inferidos (3), razão e derivadas (3), filtros por dimensão e regra de negócio (3), filtro por linha e mascaramento de LGPD (2) e perguntas só com sinônimos do glossário (2).

## Importação do Cube

| No Cube | No Cosmos | Conversão |
| --- | --- | --- |
| `cubes` com `sql_table` ou `sql` | Modelo semântico | Direta |
| Dimensão com `primary_key: true` | Entidade primária | Direta |
| `joins` com igualdade simples | Entidade estrangeira | Direta; condições complexas geram aviso |
| Medidas `count`, `count_distinct`, `sum`, `avg`, `min`, `max` | Medida do mesmo tipo | Direta |
| Medida `number` que referencia outras medidas | Métrica derivada | Exige ler a expressão SQL |
| `segments` | Regra de negócio | Dono e vigência ficam pendentes; a validação avisa |
| `rolling_window` e medidas em várias etapas | Métrica acumulada (fora do MVP) | Aviso e `custom_extensions` |
| `pre_aggregations` e `views` | Sem equivalente no MVP | `custom_extensions` |
| Controle de acesso no `cube.js` | `row_filters` e `sensitivity` | Manual; o relatório da importação lista as regras a recriar |

Modelos em JavaScript e YAML com templates Jinja ou Python geram aviso e não são importados, porque exigiriam executar código do usuário.

## Alternativas consideradas

| Alternativa | Por que não |
| --- | --- |
| Só SQLAlchemy | Converte resultados linha a linha em Python; o mercado está migrando para Arrow (a Databricks já usa ADBC por padrão no Power BI) |
| Emitir tokens próprios | Duplica o provedor de identidade do cliente e contraria a especificação de autorização do MCP, que trata o servidor como servidor de recursos OAuth |
| Ler o YAML do dbt sem alteração como formato oficial | O dbt reformulou o formato em 2026 e mantém o anterior como legado; o Cosmos ficaria refém dessas mudanças e sem lugar para `sensitivity`, `row_filters`, glossário e regras |
| Importar o Cube só na fase 2 | Recomendação inicial; a importação foi antecipada para a fase 1 para servir de caminho de migração desde o MVP |

## Consequências

- A fase 1 passa de 11 a 13 para 14 a 16 semanas (estimativa a validar no gate 1).
- A fase 0 valida o protótipo em Postgres e Databricks, não mais em Postgres e SQL Server.
- O segredo de JWT no Azure Key Vault é substituído pelas chaves do modo de desenvolvimento; o Entra ID entra na implantação como provedor padrão.
- Importar um modelo do Cube e exportá-lo de volta deve gerar o mesmo YAML estático, exceto o que foi registrado como aviso; isso vira teste automático, como no Ossie.
- Complementa o [ADR 0002](0002-formato-de-autoria-inspirado-no-dbt.md), que agora também inclui importação, e não só exportação.

# Cosmos Semantic

> Repositório: `cosmos-semantic-platform`. Licença: open core, com o núcleo sob Apache 2.0 ([ADR 0005](docs/decisions/0005-open-core.md)).

Camada semântica para agentes de IA e aplicações, com **motor próprio**: formato YAML próprio com o vocabulário do [dbt Semantic Layer](https://docs.getdbt.com/docs/use-dbt-semantic-layer/dbt-sl), versionado e com JSON Schema, geração de SQL para cada origem e interoperabilidade nativa com o [Apache Ossie](https://github.com/open-semantic-interchange/OSI) (antigo Open Semantic Interchange, OSI). O [Cube](https://github.com/cube-js/cube) e o dbt são **origens de importação e destinos de exportação**, não dependências.

**Status:** fases 0 e 1 implementadas. O motor, a ontologia (glossário e regras de negócio), a API REST, o servidor MCP, a identidade OIDC e a importação de dbt, Ossie e Cube estão prontos. As consultas de referência passam em DuckDB, Postgres e SQL Server; a validação no Databricks depende de um SQL warehouse configurado. Ver [roadmap](docs/roadmap.md).

## Começando

Requer Python 3.11+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras

# Gera dados sintéticos do e-commerce num DuckDB local (data/ecommerce.duckdb)
uv run cosmos demo-data -c examples/ecommerce/connections.yml

# Valida o modelo de exemplo
uv run cosmos validate -m examples/ecommerce/semantic

# Consulta: receita e ticket médio por status
uv run cosmos query revenue average_order_value --by orders.status \
  -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# Métricas de modelos diferentes, com filtro por linha do usuário; --show-sql mostra o SQL gerado
uv run cosmos query revenue paid_value customer_count --by customers.region -u region=Sul --show-sql \
  -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# Regras de negócio como filtro e como agrupamento
uv run cosmos query customer_count --where "{rule.cliente_ativo}" --by customers.segment -u region=Sudeste   -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml

# Testes das regras de negócio (no DuckDB, sem acessar as origens) e consultas de referência
uv run cosmos test -m examples/ecommerce/semantic
uv run cosmos reference

# Importar modelos de outras ferramentas
uv run cosmos import cube caminho/do/modelo -o semantic/importado.yml
uv run cosmos import dbt caminho/do/projeto/models --schema analytics -o semantic/dbt.yml
uv run cosmos import ossie modelo.yaml -o semantic/ossie.yml
```

### API REST e MCP

`cosmos serve` sobe a API REST em `/v1` e o servidor MCP em `/mcp` no mesmo processo; a documentação interativa fica em `/docs`.

```bash
# Produção: tokens do provedor OIDC (o Cosmos só valida, não emite)
export COSMOS_OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
export COSMOS_OIDC_AUDIENCE=api://cosmos-semantic
# Desenvolvimento: chaves de API ligadas a um contexto fixo (arquivo com `keys: {<chave>: {subject, user}}`)
export COSMOS_DEV_API_KEYS=dev-keys.yml

uv run cosmos serve -m examples/ecommerce/semantic -c examples/ecommerce/connections.yml
curl -X POST localhost:8080/v1/query -H "X-API-Key: <chave>" -H "content-type: application/json"   -d '{"metrics": ["revenue"], "dimensions": ["customers.segment"]}'
```

Para clientes MCP locais (Claude Desktop, Claude Code), use o transporte stdio: `cosmos mcp -m ... -c ...` com `COSMOS_API_KEY` definida. As ferramentas são `search_concepts`, `explain`, `list_members`, `query` e `show_sql`. Cada chamada vale para o usuário dono do token; dados pessoais nunca chegam a agentes.

### Origens

Postgres local: `docker compose -p cosmos-semantic -f deploy/docker-compose.yml up -d`, depois `cosmos demo-data --target postgres` e `cosmos reference --source postgres`. Para rodar os testes também no Postgres, defina `COSMOS_PG_URI=postgresql://cosmos:cosmos@localhost:55432/cosmos`.

SQL Server local: `docker compose -p cosmos-semantic -f deploy/docker-compose.yml --profile sqlserver up -d` e `COSMOS_MSSQL_URI='mssql+pymssql://sa:Cosmos!Passw0rd@localhost:14330/master'`. Cada origem aceita `schema_map` em `connections.yml` (por exemplo `public: dbo`) para usar o mesmo modelo em esquemas diferentes.

Databricks: defina `DATABRICKS_HTTP_PATH` (SQL warehouse) e `DATABRICKS_TOKEN` com `DATABRICKS_SERVER_HOSTNAME`, ou `DATABRICKS_CONFIG_PROFILE` com um perfil do Databricks CLI.

O editor valida e completa os arquivos do modelo pelo JSON Schema em [schema/](schema/), referenciado na primeira linha de cada arquivo (`# yaml-language-server: $schema=...`). Depois de mudar `spec.py`, regenere com `uv run cosmos schema -o schema/cosmos-semantic.v1.schema.json`.

## Em uma frase

Defina entidades, métricas, conceitos de negócio e regras de negócio uma vez, e qualquer agente de IA ou aplicação consulta o mesmo número, com o SQL gerado e executado direto na sua origem de dados, e com o modelo exportável para Ossie, Cube e dbt.

## Princípios

1. **O motor é nosso.** Modelo semântico, planejador de métricas e geração de SQL são o núcleo do produto.
2. **Compatível, não dependente.** Cube, dbt e Ossie são formatos de exportação e importação.
3. **Consulta direto na origem.** Sem infraestrutura intermediária no MVP; cache vem depois.
4. **Um diferencial por fase.** O escopo só cresce depois de validado.
5. **Núcleo aberto.** Tudo o que define ou calcula um número é Apache 2.0; a parte paga cobre identidade corporativa, auditoria, operação gerenciada e suporte.

## Documentação

| Documento | Conteúdo |
| --- | --- |
| [Visão do produto](docs/visao-do-produto.md) | Problema, público, posicionamento, versões grátis e paga |
| [Arquitetura](docs/arquitetura.md) | Componentes, planejador de consultas, segurança, implantação |
| [Modelo semântico](docs/modelo-semantico.md) | O formato de autoria, o SQL gerado e as exportações |
| [Interoperabilidade](docs/interoperabilidade-ossie.md) | Mapeamento com o Apache Ossie e importação de dbt e Cube |
| [Roadmap](docs/roadmap.md) | Fases, escopo do MVP e critérios de avanço |
| [Decisões de arquitetura](docs/decisions/) | Registros das decisões (ADRs); a 0004 substitui a 0001, a 0005 define nome e licença, a 0006 coloca a ontologia no MVP, a 0007 define conectores, origens, identidade e importação |

## Estrutura do repositório

```text
src/cosmos_semantic/
  spec.py            formato de autoria v1 (Pydantic); fonte do JSON Schema
  loader.py          leitura dos arquivos YAML
  compiler.py        validação e grafo semântico, com joins inferidos das entidades
  expressions.py     referências {membro} e $user, e proteção dos filtros de quem consulta
  planner.py         plano de consulta e SQL por dialeto (SQLGlot)
  connectors.py      DuckDB, Postgres (ADBC), Databricks e SQL Server (SQLAlchemy), sempre em Arrow
  engine.py          modelo + conexões -> consultas executadas
  ontology.py        regras de negócio (dimensões virtuais e testes) e glossário SKOS
  catalog.py         membros, busca de conceitos e explicações
  auth.py            validação de tokens OIDC e chaves de desenvolvimento
  service.py         consulta com auditoria, compartilhada por REST e MCP
  api.py             API REST (FastAPI)
  mcp_server.py      servidor MCP
  interchange/       importação de dbt, Ossie e Cube
  demo_data.py       dados sintéticos em pt_BR
  reference.py       execução das consultas de referência
  cli.py             comando `cosmos`
schema/              JSON Schema do formato
examples/ecommerce/  modelo e conexões de exemplo
reference/           as 20 consultas de referência
tests/fixtures/      modelos de exemplo em dbt, Ossie e Cube
deploy/              docker compose local
docs/                documentação e ADRs
```

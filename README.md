# Cosmos Semantic

> Repositório: `cosmos-semantic-platform`. Licença: open core, com o núcleo sob Apache 2.0 ([ADR 0005](docs/decisions/0005-open-core.md)).

Camada semântica para agentes de IA e aplicações, com **motor próprio**: formato YAML próprio com o vocabulário do [dbt Semantic Layer](https://docs.getdbt.com/docs/use-dbt-semantic-layer/dbt-sl), versionado e com JSON Schema, geração de SQL para cada origem e interoperabilidade nativa com o [Apache Ossie](https://github.com/open-semantic-interchange/OSI) (antigo Open Semantic Interchange, OSI). O [Cube](https://github.com/cube-js/cube) e o dbt são **origens de importação e destinos de exportação**, não dependências.

**Status:** fase de documentação. Ainda não há código de implementação.

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

## Estrutura planejada do repositório

```text
semantic/            modelos semânticos do usuário (formato de autoria)
engine/              compilador, planejador de consultas e geração de SQL
connectors/          conexões com as origens
api/                 API REST
mcp_server/          servidor MCP para agentes de IA
exporters/           Ossie, Cube e dbt
deploy/              docker compose local e infraestrutura do Azure
docs/                esta documentação
```

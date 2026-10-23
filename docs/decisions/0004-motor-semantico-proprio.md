# ADR 0004: Motor semântico próprio, com o Cube como destino de exportação

- **Status:** aceita
- **Data:** 15/10/2026
- **Substitui:** [ADR 0001](0001-distribuicao-do-cube-core.md)

## Contexto

O ADR 0001 escolheu distribuir o Cube Core e construir o valor por cima. Ao detalhar a solução, apareceram dois problemas:

1. **O produto vivia na sombra do Cube.** Numa camada semântica, o motor que transforma um pedido de métrica em SQL é o núcleo do produto. Construir sobre o Cube significava competir com o Cube usando o núcleo dele, e qualquer diferencial por cima (como entidades e tipos de métrica no estilo do dbt) poderia ser copiado por eles numa versão.
2. **A pilha ficou complexa.** Formato próprio, compilador para o YAML do Cube, Cube API, refresh worker, Cube Store e as camadas próprias: muita coisa para uma pessoa operar, e parte dela não era sua.

## Decisão

Inverter a relação: **o motor semântico é próprio, e o Cube passa a ser um dos destinos de exportação**, ao lado do dbt e do Apache Ossie.

- O núcleo é do produto: modelo semântico, planejador de métricas com inferência de joins e geração de SQL por dialeto.
- As consultas rodam direto na origem, sem infraestrutura intermediária no MVP.
- As interfaces iniciais são MCP e REST, voltadas para agentes de IA e aplicações.
- Quem precisar de cache, pré-agregações ou conexão com ferramentas de BI pode exportar o modelo para o Cube.

## Peças open source usadas como base

| Peça | Licença | Uso |
| --- | --- | --- |
| [SQLGlot](https://github.com/tobymao/sqlglot) | MIT | Construir e traduzir SQL para cada dialeto (SQL Server, Oracle, Postgres, Databricks, Snowflake e outros) |
| [MetricFlow](https://github.com/dbt-labs/metricflow) | Apache 2.0 (desde 0.209.0) | Referência de design para planejamento de métricas e joins; não é dependência |
| [Cube](https://github.com/cube-js/cube) | Apache 2.0 | Formato de destino da exportação; não é dependência de execução |

## Consequências

**Positivas**

- O núcleo e a propriedade intelectual são do produto.
- A arquitetura do MVP fica menor: compilador, planejador, conectores, REST e MCP.
- O produto passa a ser compatível com o Cube, em vez de dependente dele.

**Negativas**

- Sem cache nem pré-agregações no MVP: cada consulta vai à origem.
- Sem endpoint SQL compatível com Postgres no MVP, então Power BI e Tableau ficam para depois ou via exportação para o Cube.
- A correção do SQL gerado vira responsabilidade do produto, inclusive casos difíceis como a multiplicação de linhas em joins (fan-out).

## Riscos e mitigações

| Risco | Mitigação |
| --- | --- |
| SQL gerado incorreto em joins com várias tabelas | Agregar cada medida no próprio grão antes de juntar, como o MetricFlow; suíte de consultas de referência comparadas com SQL escrito à mão |
| Diferenças entre dialetos | SQLGlot e testes por origem suportada |
| Desempenho em bases grandes sem cache | Empurrar agregação para a origem, limitar linhas devolvidas, cache de resultados na fase 3 |
| Escopo crescer de novo | Um diferencial por fase, além do núcleo |

# Roadmap

As durações são estimativas para uma pessoa e devem ser recalibradas ao fim de cada fase. Regra do projeto: um diferencial por fase, além do núcleo.

## Fase 0: fundação (3 a 4 semanas)

- Especificação v1 do formato de autoria (`version: 1`), com JSON Schema gerado dos modelos Pydantic e o exemplo completo de [modelo semântico](modelo-semantico.md).
- Gerador de dados sintéticos em pt_BR (e-commerce com clientes, pedidos, itens, pagamentos e vendedores, incluindo CPF, e-mail e um campo sensível) e um conjunto de 20 consultas de referência escritas à mão em SQL.
- Interface de conectores em Arrow, com ADBC por padrão e SQLAlchemy como reserva.
- Protótipo do planejador para métricas simples num único modelo, gerando SQL para Postgres e Databricks.

**Critério de avanço:** o protótipo reproduz o resultado das consultas de referência de um único modelo em Postgres e Databricks.

## Fase 1: MVP (14 a 16 semanas)

- Planejador completo: joins inferidos com vários saltos, os 4 tipos de métrica, agregação no próprio grão para evitar multiplicação de linhas.
- Origens: Postgres, Databricks e SQL Server.
- API REST e servidor MCP (`search_concepts`, `explain`, `list_members`, `query`, `show_sql`).
- Identidade por provedor OIDC externo (Entra ID primeiro), com `user_context` mapeando declarações do token; filtros por linha e classificação de LGPD (`sensitivity`) como segurança básica.
- Importação de dbt (formato atual e legado), Ossie e Cube (YAML estático) ([ADR 0007](decisions/0007-conectores-origens-identidade-e-importacao.md)).
- **Diferencial da fase: ontologia.** Glossário SKOS (conceitos, sinônimos, hierarquia, rótulos em português e inglês) e regras de negócio (`predicate`, `classification` com tabela de decisão, `derived`) com dono, vigência e testes em CI ([ADR 0006](decisions/0006-ontologia-no-mvp.md)).
- CLI com `validate`, `test` e `query`, e validação em CI.

**Critério de avanço:** as 20 consultas de referência, inclusive as que cruzam modelos e as que usam regras de negócio, dão o mesmo resultado via REST, via MCP e pelo SQL escrito à mão, nas três origens; e um agente responde corretamente a perguntas feitas só com sinônimos do glossário.

### Consultas de referência

| Categoria | Consultas |
| --- | --- |
| Fan-out: duas medidas em grãos diferentes (pedido, item, pagamento) | 4 |
| Métrica simples e por granularidade de tempo | 3 |
| Join com várias tabelas inferido pelas entidades | 3 |
| Métricas de razão e derivadas | 3 |
| Filtros por dimensão e por regra de negócio (`cliente_ativo`) | 3 |
| Filtro por linha vindo do token e mascaramento de `personal` e `sensitive` | 2 |
| Pergunta feita só com sinônimos do glossário, via agente | 2 |

O TPC-H em escala maior cobre desempenho. O dataset Olist fica de fora porque a licença é não comercial.

## Fase 2: abertura (6 semanas)

- **Diferencial da fase:** exportação Apache Ossie, com teste de ciclo completo (a importação entrou na fase 1).
- Exportação para o YAML do Cube e do dbt, com teste de ida e volta para o Cube.
- Servidor MCP remoto com OAuth, pronto para o diretório de conectores do Claude.
- Publicação do núcleo open source (Apache 2.0, contribuições com DCO), com documentação de instalação.

**Critério de avanço:** primeiros usuários externos rodando a versão grátis com modelos próprios.

## Fase 3: produto pago (contínuo)

- Cache de resultados.
- Endpoint SQL compatível com Postgres, para Power BI, Tableau e Excel.
- Plano de controle: editor, playground, conexões, usuários.
- Hospedagem gerenciada no Azure, SSO com Entra ID, auditoria.
- Candidatos a diferencial, um de cada vez: importação de modelos do Power BI, testes de reconciliação de métricas, custo por métrica e por agente, consulta com regras de negócio de vigência passada, exportação do glossário em SKOS/RDF para catálogos de dados.

## Decisões em aberto

- [x] Nome do produto: Cosmos Semantic ([ADR 0005](decisions/0005-open-core.md)).
- [x] Licença: open core, núcleo sob Apache 2.0 ([ADR 0005](decisions/0005-open-core.md)).
- [x] Conflito de marca com o Azure Cosmos DB: nome mantido; repositório `cosmos-semantic-platform`.
- [x] Conectores, ordem das origens, identidade e formato de autoria ([ADR 0007](decisions/0007-conectores-origens-identidade-e-importacao.md)).
- [ ] Limites da edição hospedada gratuita e modelo de preço da edição comercial (revisar no futuro).
- [ ] Tecnologia do cache de resultados (fase 3).
- [ ] Construir o endpoint SQL ou manter a exportação para o Cube como caminho oficial para BI.

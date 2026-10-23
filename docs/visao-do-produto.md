# Visão do produto

## Problema

Empresas definem a mesma métrica várias vezes: no dashboard do Power BI, na planilha do financeiro, no SQL do analista e, agora, no prompt do agente de IA. Cada versão dá um número um pouco diferente. Quando um agente de IA lê o schema cru do banco, o problema piora: ele adivinha o que significa `vl_tot_liq` e erra com confiança.

Uma camada semântica resolve isso definindo métricas, dimensões e relacionamentos uma vez e servindo o mesmo resultado para todas as ferramentas. O mercado validou a ideia: Databricks, Snowflake, dbt, Cube, AtScale, Dremio e Microsoft lançaram ou reforçaram camadas semânticas voltadas para IA entre 2025 e 2026.

## Proposta

Uma camada semântica com motor próprio que:

- tem um **formato de autoria simples**, inspirado no dbt Semantic Layer, com entidades que inferem joins e tipos de métrica de primeira classe;
- traz uma **ontologia de negócio desde o MVP**: glossário SKOS com sinônimos e hierarquia, e regras de negócio com dono, vigência e testes, legíveis por gente de negócio;
- **gera SQL para cada origem** e executa direto nela, sem infraestrutura intermediária;
- serve **agentes de IA via MCP** e aplicações via REST;
- fala **Apache Ossie** nativamente e **exporta para Cube e dbt**;
- começa com uma **versão gratuita**.

## Público

| Perfil | O que procura |
| --- | --- |
| Time de IA | Contexto confiável para agentes, via MCP |
| Engenheiro de analytics | Modelar métricas em git, com revisão e validação em CI |
| Desenvolvedor de aplicações | API de métricas para produtos e analytics embarcado |

Recorte inicial: empresas de porte médio que querem colocar agentes de IA sobre os próprios dados e ainda não pagam um produto semântico corporativo. Times de BI entram quando houver endpoint SQL ou via exportação para o Cube.

## Posicionamento

| Ferramenta | Força | Onde este produto se diferencia |
| --- | --- | --- |
| [Cube](https://cube.dev/pricing) | Motor maduro, cache, plano grátis, conector oficial no Claude | Formato de autoria mais simples e Ossie nativo; o Cube é destino de exportação, não concorrente direto na execução |
| [dbt Semantic Layer](https://docs.getdbt.com/docs/use-dbt-semantic-layer/sl-faqs) | Métricas versionadas no projeto dbt; suporta Snowflake, BigQuery, Databricks, Redshift, Postgres e Trino | Não exige projeto dbt; suporta origens que o dbt não suporta, como SQL Server e Oracle |
| [AtScale](https://atlan.com/know/ai-agent/semantic-layer/atscale-vs-context-layer/) | Virtualização e conexão nativa com Power BI e Excel | Plano gratuito e preço acessível para empresa média |
| [Dremio](https://www.dremio.com/newsroom/sap-to-acquire-dremio-to-unify-sap-and-non-sap-data-to-power-agentic-ai/) | Motor federado, MCP open source com busca semântica | Independente: a Dremio foi comprada pela SAP em 2026 |
| [Microsoft Fabric IQ](https://letsdatascience.com/news/microsoft-expands-fabric-iq-as-a-context-layer-for-ai-agents-e9a90bd8) | Ontologia e semântica sobre OneLake e Power BI | Não exige Fabric; neutro entre plataformas |

Com o mercado se consolidando em torno das grandes plataformas, a **neutralidade** volta a ser argumento: o produto não pertence a nenhuma plataforma de dados.

O diferencial de adoção é a **ontologia**. O Genie da Databricks tem expressões de negócio presas a um Genie Agent, o Fabric IQ tem ontologia ainda em preview e dentro do Fabric, e a Palantir oferece ontologia com funções só para empresa grande. O Cosmos Semantic entrega glossário e regras de negócio versionados, testados e neutros, no núcleo aberto ([ADR 0006](decisions/0006-ontologia-no-mvp.md)).

## Edições (open core)

O Cosmos Semantic é open core ([ADR 0005](decisions/0005-open-core.md)): tudo o que define ou calcula um número é Apache 2.0.

| Recurso | Núcleo aberto (auto-hospedado) | Hospedado gratuito | Comercial |
| --- | --- | --- | --- |
| Formato de autoria, motor e validação | Sim | Sim | Sim |
| API REST e servidor MCP | Sim | Sim, com limites de uso | Sem limites |
| Ontologia: glossário SKOS e regras de negócio | Sim | Sim | Sim |
| Regras de LGPD e filtros por linha | Sim | Sim | Sim |
| Importação e exportação Ossie, Cube e dbt | Sim | Sim | Sim |
| Cache de resultados e endpoint SQL para BI (fase 3) | Sim | Sim | Sim |
| Origens de dados | Sem limite | Limitadas | Sem limite |
| Hospedagem | Por conta do usuário | Gerenciada | Gerenciada no Azure |
| SSO, SCIM e auditoria corporativa | Não | Não | Sim |
| Controle de acesso por perfil no plano de controle | Não | Não | Sim |
| Suporte e SLA | Comunidade | Comunidade | Contratual |

Os limites da edição hospedada gratuita são hipóteses a validar com os primeiros usuários.

## Não objetivos (por enquanto)

- Cache, pré-agregações e endpoint SQL compatível com Postgres no MVP.
- Importação de modelos do Power BI (candidata a diferencial depois do MVP).
- Pacotes semânticos prontos para ERPs.
- Ferramenta de transformação de dados (ETL ou dbt).

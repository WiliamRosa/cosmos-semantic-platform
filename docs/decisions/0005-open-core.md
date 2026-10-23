# ADR 0005: Nome Cosmos Semantic e licenciamento open core

- **Status:** aceita
- **Data:** 16/10/2026

## Contexto

O produto precisava de nome e de um modelo de licença. Para a licença, as opções eram Apache 2.0 em tudo, uma licença source-available (BSL ou Elastic License), ou open core. O objetivo declarado é que a comunidade tenha o máximo de acesso para usar e alterar o núcleo, mantendo espaço para um produto pago.

## Decisão

**Nome:** Cosmos Semantic. O repositório se chama `cosmos-semantic-platform`.

**Licença:** open core.

| Parte | Licença | Conteúdo |
| --- | --- | --- |
| Núcleo | Apache 2.0 | Formato de autoria, compilador, planejador, gerador de SQL, conectores, API REST, servidor MCP, regras de LGPD, importação e exportação Ossie, Cube e dbt |
| Recursos comerciais | Proprietária | SSO e SCIM, auditoria corporativa, controle de acesso por perfil no plano de controle, multi-tenancy gerenciado, hospedagem e suporte |

Princípios para decidir onde cada recurso fica:

1. **Tudo o que define ou calcula um número fica no núcleo.** Se a comunidade não puder auditar e alterar como uma métrica é calculada, o produto perde confiança.
2. **Recursos comerciais são organizacionais, não analíticos:** identidade corporativa, auditoria, operação gerenciada, suporte.
3. **Nada sai do núcleo depois de entrar.** Mover um recurso aberto para a parte paga quebra a confiança da comunidade.

## Contribuições

- Contribuições externas ao núcleo usam o Developer Certificate of Origin (DCO), assinatura em cada commit, sem exigir cessão de direitos.
- O código comercial fica em repositório separado, que depende do núcleo como qualquer outro usuário.

## Consequências

- A versão gratuita auto-hospedada é o próprio núcleo, sem limites artificiais de origens ou uso.
- Limites de uso só existem na versão hospedada gratuita, que roda na infraestrutura do produto.
- Qualquer empresa pode oferecer o núcleo como serviço; esse risco é aceito em troca de adoção. A defesa está na qualidade do produto hospedado e nos recursos comerciais.
- Antes de registrar domínio e logo, verificar conflito de marca com o Azure Cosmos DB, da Microsoft.

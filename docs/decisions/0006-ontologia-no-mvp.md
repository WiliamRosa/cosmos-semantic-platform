# ADR 0006: Ontologia no MVP, com glossário SKOS e regras de negócio

- **Status:** aceita
- **Data:** 16/10/2026

## Contexto

Agentes de IA precisam de contexto de negócio, não só de métricas. O usuário pergunta por "compradores ativos", e o agente precisa chegar a uma regra definida e a um filtro SQL correto. O mercado caminha nessa direção:

| Referência | O que mostra |
| --- | --- |
| Genie Ontology e "SQL expressions" do Genie (Databricks) | Filtros, medidas e campos calculados com significado de negócio, mas presos a um Genie Agent |
| Microsoft Fabric IQ | Ontologia sobre OneLake e Power BI, com o item de ontologia ainda em preview |
| Palantir Foundry Ontology | Objetos, ligações e funções de negócio como parte da ontologia, para empresa grande |
| DMN (padrão OMG) | Tabelas de decisão legíveis por gente de negócio |

Nenhuma camada semântica independente trata regras de negócio como objetos com dono, versão e testes, ligados a um glossário. A ontologia foi escolhida como o diferencial que convence o usuário a adotar o produto, e por isso entra no MVP.

## Decisão

O MVP inclui dois elementos novos no modelo semântico. Especificação em [modelo semântico](../modelo-semantico.md#glossário-skos).

1. **Glossário no estilo SKOS:** conceitos com rótulo preferido, sinônimos, definição e hierarquia ("é um tipo de"), com rótulos em mais de um idioma. Não é uma ontologia formal OWL, que seria pesada demais para o cliente manter.
2. **Regras de negócio** (`business_rules`), de três tipos: `predicate` (verdadeiro ou falso), `classification` (categoria, inclusive por tabela de decisão no estilo DMN) e `derived` (valor calculado). Cada regra tem dono, data de vigência, testes e é ligada a modelos por mapeamento de colunas.

O servidor MCP ganha ferramentas para buscar conceitos por sinônimo e explicar uma regra em linguagem natural.

## Limites assumidos

- **Regras por linha.** Uma regra só usa colunas e dimensões da linha. Classificações que dependem de agregados (como receita dos últimos 12 meses) exigem uma coluna ou modelo de resumo já calculado.
- **Vale a definição vigente.** `effective_from` registra desde quando a regra vale, e o histórico fica no git, mas as consultas sempre usam a versão atual. Consultar com uma versão antiga fica para depois.
- **Sem inferência lógica.** O glossário é navegável (sinônimos, hierarquia), mas o motor não faz raciocínio automático sobre ele.

## Consequências

- A ontologia é o diferencial da fase 1. A classificação de LGPD continua no MVP como segurança básica.
- A fase 1 passa de 8 a 10 para 11 a 13 semanas.
- Os testes das regras rodam em CI sem acessar a origem: o SQLGlot traduz a lógica para DuckDB, que avalia os exemplos localmente.
- Glossário e regras ficam no núcleo open source, porque definem o que um número significa ([ADR 0005](0005-open-core.md)).

# ADR 0001: Distribuição do Cube Core, sem fork

- **Status:** substituída pelo [ADR 0004](0004-motor-semantico-proprio.md) em 15/10/2026
- **Data:** 14/10/2026

> Esta decisão foi revertida: o motor passou a ser próprio, e o Cube virou um destino de exportação. O texto abaixo fica como registro histórico.

## Contexto

O produto quer oferecer uma camada semântica universal comparável ao Cube. Havia dois caminhos:

| Caminho | Descrição |
| --- | --- |
| A. Distribuição do Cube Core | Usar o Cube Core como motor e construir o valor próprio por cima |
| B. Motor próprio | Escrever compilador, conectores, cache e endpoint SQL próprios, com bibliotecas open source |

O Cube Core tem o backend sob Apache 2.0 e o cliente sob MIT ([repositório](https://github.com/cube-js/cube)). Ele já entrega API REST, GraphQL, endpoint SQL compatível com Postgres, cache, pré-agregações e cerca de 30 origens de dados oficiais ([lista](https://docs.cube.dev/admin/connect-to-data/data-sources)).

## Decisão

Seguir o caminho A: o Cube Core é uma dependência em versão fixada, executada a partir das imagens oficiais (`cubejs/cube` e `cubejs/cubestore`). O código do Cube não é copiado nem modificado. O valor próprio fica em:

- formato de autoria e compilador ([ADR 0002](0002-formato-de-autoria-inspirado-no-dbt.md));
- interoperabilidade Ossie ([ADR 0003](0003-ossie-como-formato-de-intercambio.md));
- servidor MCP, plano de controle e hospedagem gerenciada.

## Consequências

**Positivas**

- Chegar a um produto funcional em semanas, não anos.
- Herdar origens que o dbt Semantic Layer não suporta, como SQL Server, Oracle, MySQL e Microsoft Fabric.
- Atualizar o motor puxando novas versões, sem conflito de merge.

**Negativas**

- O motor é igual ao do Cube, então desempenho não é diferencial.
- Recursos exclusivos do Cube Cloud ficam fora, como a conexão DAX/XMLA com o Power BI.
- O ritmo e as decisões do projeto Cube afetam o produto.

## Riscos e mitigações

| Risco | Mitigação |
| --- | --- |
| O Cube mudar a licença em versões futuras, como Elastic e HashiCorp fizeram | Versão fixada; o modelo canônico é o formato de autoria e o Ossie, não o YAML do Cube, então o motor pode ser trocado |
| Uso indevido de marca | Não usar "Cube" no nome do produto; citar o Cube Core apenas como componente, com os avisos de licença exigidos pela Apache 2.0 |
| Atualização do Cube quebrar o modelo gerado | Suíte de regressão antes de cada atualização |
| Recurso necessário existir só no Cube Cloud | Implementar por cima, na camada própria, ou aceitar a lacuna e documentar |

## Obrigações da licença Apache 2.0

- Manter os avisos de copyright e licença do Cube em qualquer redistribuição.
- Incluir um arquivo de avisos de terceiros na distribuição do produto.
- Indicar quando houver modificações em arquivos do Cube; com a decisão de não modificar, isso não deve ocorrer.

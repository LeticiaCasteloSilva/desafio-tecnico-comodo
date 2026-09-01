# Case Técnico | Cômoda

Repositório com as três entregas do case: ingestão de dados via API, análise SQL
sobre um funil de vendas e classificação de leads com LLM.

## Estrutura

```
parte1_ingestao/     coleta de repositórios da API do GitHub
dados/               CSVs e JSON fornecidos com o case (partes 2 e 3)
saida/               artefatos gerados pelos scripts (fora do versionamento)
```

## Como rodar

Requer Python 3.11 ou superior.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # preencha as credenciais
```

### Credenciais

As credenciais ficam no `.env`, que está no `.gitignore` e nunca é versionado.
O `.env.example` documenta as variáveis necessárias, sem valores.

| Variável | Onde obter | Necessária para |
|---|---|---|
| `GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) | Parte 1 |
| `GITHUB_ORG` | — (padrão: `vercel`) | Parte 1 |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | Parte 3 |

Para ler repositórios públicos o token do GitHub **não precisa de nenhum escopo
marcado** — pode ser gerado sem permissão alguma, que é o mais seguro. Sem token
o script roda, mas a API limita a 60 requisições/hora; com token, 5.000.

---

## Parte 1 | Ingestão via API

```bash
.venv/bin/python parte1_ingestao/coleta_repos.py
```

Coleta todos os repositórios públicos de uma organização do GitHub e grava
nome, descrição, linguagem principal, estrelas, forks, data de criação e data
de atualização em `saida/repos_<org>_<data>.csv`.

### Resultado da execução

Organização `vercel`, executada em 01/09/2026:

```
pagina 1: 100 repositórios (acumulado: 100)
pagina 2: 100 repositórios (acumulado: 200)
pagina 3:  39 repositórios (acumulado: 239)
SUCESSO — vercel — 239 repositórios — 4.0s
```

Validação do CSV gerado: 239 linhas, 239 nomes únicos, nenhum nome vazio,
nenhuma data malformada, nenhuma contagem não-numérica. Os 45 registros sem
descrição e 27 sem linguagem são campos legitimamente nulos na API
(repositórios sem descrição ou sem código detectável), não falha de parsing.

### Decisões

**Por que CSV.**  
O destino do arquivo é um relatório que uma pessoa abre às 9h — CSV abre direto em Excel ou Sheets, sem
etapa intermediária. Os dados são achatados e homogêneos, sem aninhamento que
justificasse JSONL, e o volume (centenas de linhas) não justifica SQLite nem a
necessidade de query no destino. O nome do arquivo carrega a data, o que dá
histórico diário sem sobrescrever a coleta anterior.

**O contexto de operação guiou o resto.** O enunciado diz que o script roda às
6h, sem ninguém acompanhando, e alimenta um relatório aberto às 9h. Isso torna
duas coisas mais importantes do que seriam num script interativo: nunca produzir
um arquivo corrompido, e nunca falhar em silêncio.

**Escrita atômica.** O CSV é gravado num arquivo temporário e só substitui o
final quando a coleta termina inteira, via `Path.replace()`, que é atômico no
mesmo sistema de arquivos. Se a execução falhar no meio, o arquivo do dia
anterior permanece intacto: o relatório das 9h lê um dado desatualizado, e não
um CSV truncado que passaria por completo.

**Paginação pelo header `Link`.** A API informa na própria resposta se existe
próxima página, o que é mais confiável do que contar páginas ou parar num limite
fixo. Um conjunto de nomes já vistos descarta duplicatas, que a paginação pode
produzir se algum repositório for criado durante a coleta.

**Retry com backoff.** Erros transitórios — 5xx, timeout, falha de rede — são
repetidos até 5 vezes, com a espera dobrando a partir de 2 segundos. Rate limit
(403/429) é tratado à parte: o script aguarda o tempo que a própria API informa
em `Retry-After` ou `X-RateLimit-Reset`, em vez de adivinhar, limitado a 15
minutos para não ficar dormindo em silêncio. Já 401 e 404 falham na hora, sem
repetição: token inválido e organização inexistente não melhoram com nova
tentativa.

**Sinalização de falha.** Saída com código 0 em caso de sucesso e 1 em caso de
falha — é o que um cron consegue detectar para disparar alerta. O log vai para
stdout com timestamp, e ao final o script imprime a organização, a contagem de
registros e o caminho do arquivo, atendendo ao requisito de deixar claro quantos
registros foram processados.

### Comportamento verificado em falha

| Cenário | Resultado |
|---|---|
| Organização inexistente | Erro explícito, código de saída 1 |
| Token inválido | `token invalido ou expirado (401)`, código de saída 1 |
| CSV anterior após falha | Preservado, sem arquivo temporário órfão |

### O que ficou de fora

- **Retenção dos arquivos diários.** Os CSVs acumulam em `saida/` sem limpeza.
  Em produção isso precisaria de política de retenção.
- **Alerta ativo em caso de falha.** O código de saída é o gancho, mas quem
  dispara e-mail ou mensagem é o cron ou o orquestrador, fora do escopo do
  script.
- **Persistência em banco.** O destino é um arquivo local, como o case pede. Em
  produção, o caminho natural seria gravar numa tabela com chave
  `(data_coleta, repositorio)` — o que daria histórico consultável, tornaria a
  re-execução idempotente e permitiria comparar coletas entre dias — mantendo o
  CSV como exportação para o relatório. Fora do escopo aqui porque o enunciado
  pede arquivo local e o volume atual não justifica a infraestrutura.
- **Coleta incremental.** O script busca todos os repositórios a cada execução,
  sem consultar o que já foi coletado antes. Com 239 repositórios em 4 segundos,
  manter esse estado custaria mais do que economiza.

---

## Parte 2 | SQL sobre os dados de funil

```bash
.venv/bin/python parte2_sql/executar.py
```

As queries estão em [`parte2_sql/consultas.sql`](parte2_sql/consultas.sql). O
script apenas executa esse arquivo e formata a saída — o SQL é a entrega.

Usei **DuckDB** porque ele consulta os CSVs diretamente, sem etapa de carga.
Isso elimina uma fonte de erro: não existe um passo de importação onde tipos
possam ser convertidos errado, e qualquer pessoa reproduz os números rodando um
comando sobre os arquivos originais.

### Integridade dos dados

Antes das análises, uma query de controle mede quanto do dado ficou de fora.
Ela existe porque os totais das outras queries **não** fecham com os arquivos, e
sem isso não haveria como saber se a diferença é intencional:

| Métrica | Qtd |
|---|---|
| Leads no arquivo | 478 |
| Leads sem campanha | 6 |
| Leads com campanha inexistente (`cmp_999`) | 4 |
| **Leads atribuídos (base das análises)** | **468** |
| Vendas no arquivo | 49 |
| Vendas sem lead correspondente | 0 |
| Vendas de leads sem campanha (fora do rateio) | 1 |
| **Vendas atribuídas (base das análises)** | **48** |
| Vendas fechadas após 30/06 | 31 |

Os 10 leads sem origem rastreável (2,1% do total) ficam fora das métricas por
campanha, porque não há gasto atribuível a eles. Isso tem uma consequência que
precisa estar visível: **uma venda real de R$ 44.909,27 (VD510) não entra em
nenhuma campanha**. A receita atribuída soma R$ 1.753.074,88, contra
R$ 1.797.984,15 no arquivo de vendas.

### Premissas

**Período.** O investimento cobre 01/05 a 30/06, mas as vendas vão até 18/09.
Considerei **todas as 49 vendas**, e não apenas as 18 fechadas dentro da janela
de mídia. Móveis planejados têm ciclo de venda longo: um lead de junho fecha em
agosto, e cortar em 30/06 descartaria 31 vendas que aquele investimento gerou —
fazendo o custo por venda parecer quase três vezes maior do que é. A contrapartida
é que o denominador (gasto) está fechado e o numerador (vendas) ainda pode
crescer: **estes custos por venda tendem a melhorar** conforme o funil matura.

**Funil.** O campo `etapa_atual` diz onde o lead **está**, não por onde **passou**.

A regra que usei: as etapas são ordenadas (`novo` → `em_atendimento` →
`qualificado` → `briefing_realizado` → `proposta` → `vendido`), e um lead conta
para todas as etapas até a sua posição atual. Um lead em `proposta` é contado em
`novo`, `em_atendimento`, `qualificado`, `briefing_realizado` e `proposta`.

O problema está nos 133 leads em `perdido`. Essa etapa não fica em lugar nenhum
da escada — um lead pode ter sido perdido logo no primeiro contato ou depois de
receber a proposta, e o dado não distingue os dois casos. Como não há onde
encaixá-los, eles ficam fora de todas as etapas.

O efeito é que **os números do funil são um piso, não o valor real**. Se um lead
chegou à proposta e depois foi perdido, a linha `proposta` não o conta. As etapas
finais são as mais afetadas, porque é onde há mais leads perdidos acumulados.
Isso se resolve na origem, com um campo registrando a etapa máxima atingida.

### 1. Custo por lead e custo por venda

Ordenado do melhor para o pior custo por venda:

| Campanha | Plataforma | Gasto | Leads | Vendas | Custo/lead | Custo/venda | Conversão |
|---|---|---:|---:|---:|---:|---:|---:|
| PESQUISA_MOVEIS_PLANEJADOS_MARCA | Google | 3.679,14 | 40 | 13 | 91,98 | **283,01** | 32,5% |
| LEADS_APARTAMENTO_NOVO | Meta | 7.357,81 | 71 | 11 | 103,63 | 668,89 | 15,5% |
| REMARKETING_VISITANTES_SITE | Meta | 7.062,61 | 71 | 8 | 99,47 | 882,83 | 11,3% |
| PLANEJADOS_DORMITORIO_FRIO | Meta | 11.260,30 | 96 | 7 | 117,29 | 1.608,61 | 7,3% |
| PESQUISA_MOVEIS_PLANEJADOS_GENERICO | Google | 11.596,14 | 95 | 5 | 122,06 | 2.319,23 | 5,3% |
| PLANEJADOS_COZINHA_FRIO | Meta | 10.689,91 | 95 | 4 | 112,53 | **2.672,48** | 4,2% |

O custo por lead é homogêneo — varia de R$ 92 a R$ 122, um fator de 1,3. O custo
por venda varia por um fator de **9,4**. Ou seja: as campanhas compram leads a
preços parecidos, mas a qualidade desses leads é radicalmente diferente. Otimizar
por custo por lead levaria à conclusão oposta da correta.

### 2. Funil por campanha

São seis campanhas por seis etapas; o resultado completo sai na execução do
script. Abaixo, as duas pontas do ranking de custo por venda, onde o contraste
fica visível:

| Etapa | `cmp_004` (marca) | perda | `cmp_001` (cozinha frio) | perda |
|---|---:|---:|---:|---:|
| novo | 34 | — | 73 | — |
| em_atendimento | 29 | 14,7% | 56 | 23,3% |
| qualificado | 27 | 6,9% | 34 | 39,3% |
| briefing_realizado | 18 | 33,3% | 12 | 64,7% |
| proposta | 16 | 11,1% | 6 | 50,0% |
| vendido | 13 | 18,8% | 4 | 33,3% |

`cmp_001` gera o dobro de leads e converte um terço. A perda se concentra na
passagem para `briefing_realizado` (64,7%), que é onde o lead precisa investir
tempo — sinal de intenção fraca, coerente com uma campanha de público frio.

### 3. Ticket médio e receita

| Campanha | Vendas | Receita | Ticket médio | ROAS |
|---|---:|---:|---:|---:|
| PESQUISA_MOVEIS_PLANEJADOS_MARCA | 13 | 517.203,67 | 39.784,90 | 140,6 |
| LEADS_APARTAMENTO_NOVO | 11 | 404.065,89 | 36.733,26 | 54,9 |
| PLANEJADOS_DORMITORIO_FRIO | 7 | 251.637,65 | 35.948,24 | 22,4 |
| REMARKETING_VISITANTES_SITE | 8 | 235.551,73 | 29.443,97 | 33,4 |
| PESQUISA_MOVEIS_PLANEJADOS_GENERICO | 5 | 192.034,78 | 38.406,96 | 16,6 |
| PLANEJADOS_COZINHA_FRIO | 4 | 152.581,16 | 38.145,29 | 14,3 |

**Qual campanha traz o cliente de maior valor.** `PESQUISA_MOVEIS_PLANEJADOS_MARCA`
lidera nas duas leituras, mas por margens muito diferentes. Em ticket médio,
R$ 39.784,90 contra R$ 38.406,96 da segunda colocada — 3,6% de diferença, sobre
amostras de 4 a 13 vendas, onde um único contrato move a média em milhares de
reais. Em receita total, R$ 517 mil contra R$ 404 mil — 28% de folga.

A diferença entre essas duas margens é a resposta. Os tickets médios ficam todos
entre R$ 29 mil e R$ 40 mil, um fator de 1,35, e a ordem entre eles não é
confiável com amostras desse tamanho. As receitas separam as campanhas por um
fator de 3,4. **O que distingue as campanhas não é o valor de cada cliente, é
quantos clientes elas fecham** — `cmp_004` não vende mais caro, vende mais.

### Confiança nos números

**O que sustenta os números.** As três tabelas cruzam sem sobras: as 49 vendas
têm lead correspondente, e os 49 leads marcados como `vendido` são exatamente os
que têm venda registrada. Nenhuma venda é anterior à criação do seu lead, nenhum
valor é nulo ou negativo, nenhum identificador se repete. Por fim, refiz todos os
cálculos em Python puro, sem SQL, partindo dos CSVs originais: os valores batem
em todas as campanhas.

**Confiabilidade por métrica.**

| Métrica | Confiável? | Por quê |
|---|---|---|
| Custo por venda | Sim | Separa as campanhas por um fator de 9,4, muito acima do ruído |
| Custo por lead | Sim | Baseado em 40 a 96 leads por campanha |
| Ticket médio | **Não** | Com 4 a 13 vendas, o intervalo de 95% da média vai de ±24% a ±67% — os tickets estão todos dentro do intervalo uns dos outros, e qualquer ordem entre eles é compatível com os dados |
| Etapas do funil | Parcial | São um piso: leads perdidos não aparecem nas etapas que chegaram a alcançar |

**Dois vieses do dado, que valem para todas as métricas acima.** As vendas ainda
não terminaram de fechar — leads de junho podem virar contrato depois de
setembro, melhorando o custo por venda de forma desigual entre campanhas. E a
atribuição é de último clique: cada lead carrega uma campanha só, então quem viu
o anúncio no Meta e depois buscou a marca no Google dá todo o crédito à busca.
Esse segundo viés infla `cmp_004` e deprecia as campanhas de topo de funil — é a
razão pela qual a recomendação abaixo não propõe cortar as piores colocadas.

### Sobre realocar verba

O desalinhamento entre verba e retorno é o achado mais acionável:

| Campanha | Custo/venda | Fatia da verba |
|---|---:|---:|
| `cmp_004` — busca pela marca | R$ 283 | 7,1% |
| `cmp_006` — apartamento novo | R$ 669 | 14,2% |
| `cmp_003` — remarketing | R$ 883 | 13,7% |
| `cmp_002` — dormitório frio | R$ 1.609 | 21,8% |
| `cmp_005` — busca genérica | R$ 2.319 | 22,5% |
| `cmp_001` — cozinha frio | R$ 2.673 | 20,7% |

As duas piores campanhas consomem 43% do orçamento; a melhor, 7,1%.

Isso **não** significa mover a verba para `cmp_004`. Ela captura quem já procura
a Cômodo pelo nome, e esse público tem tamanho fixo: dobrar o investimento não
dobra o número de pessoas buscando a marca. É a campanha mais eficiente
justamente porque colhe demanda que outra coisa gerou.

Duas ações que os dados sustentam:

- **`cmp_005` (busca genérica, R$ 2.319 por venda, 22,5% da verba)** é a candidata
  mais clara a revisão. Ela disputa termos genéricos no Google, onde o custo por
  clique é alto e a intenção do usuário é difusa. Revisar palavras-chave e página
  de destino antes de manter o aporte;
- **`cmp_006` (R$ 669 por venda, 71 leads)** combina o segundo melhor custo por
  venda com volume relevante, e é a única entre as eficientes que pode escalar.
  Vale testar aumento gradual, medindo se o custo por venda se mantém.

Já `cmp_001` e `cmp_002` são as campanhas de público frio: caras por venda, mas é
delas que sai a demanda que `cmp_004` colhe depois. Como a atribuição é de último
clique, o retorno delas está sistematicamente subestimado nestes números — cortá-las
com base nesta tabela seria decidir a partir de um viés conhecido.

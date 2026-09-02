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

---

## Parte 3 | Classificação de leads com LLM

```bash
.venv/bin/python parte3_classificacao/classificar.py
.venv/bin/python parte3_classificacao/classificar.py --conversa CV001   # uma só
```

Lê as 15 conversas, chama o modelo uma vez por conversa e grava uma linha JSON
por conversa em `saida/classificacoes.jsonl`. Modelo: `gpt-4.1`, temperatura 0.

O prompt está em
[`prompts/classificacao.md`](parte3_classificacao/prompts/classificacao.md),
fora do código, com seções `## sistema` e `## usuario`. Versionado em arquivo
próprio porque mudança de critério muda a distribuição das classificações: ao ver
a proporção de `quente` variar, é o histórico do Git que permite saber se o funil
mudou ou se o prompt mudou.

### Resultado

15 conversas classificadas, nenhuma falha, cerca de 30 segundos.

| Classe | Qtd | Conversas |
|---|---:|---|
| quente | 6 | CV001, CV008, CV009, CV011, CV013, CV015 |
| morno | 2 | CV003, CV006 |
| frio | 5 | CV002, CV007, CV010, CV012, CV014 |
| fora_do_perfil | 2 | CV004, CV005 |

Exemplo de saída (CV015):

```json
{
  "conversa_id": "CV015",
  "classificacao": "quente",
  "prioridade": 1,
  "confianca": "alta",
  "sinais": ["comprei 3 apartamentos",
             "entrega das chaves foi mes passado",
             "150 mil pros tres",
             "amanha as 10"],
  "orcamento_mencionado": 150000.0,
  "prazo_mencionado": null,
  "ambientes": ["dormitorio"],
  "proxima_acao": "Agendar videochamada para amanhã às 10",
  "resumo_para_o_vendedor": "Três apartamentos comprados para investimento, chaves entregues mês passado, orçamento de R$ 150 mil. Videochamada agendada para amanhã às 10.",
  "status": "ok",
  "campanha_id": "cmp_006"
}
```

### Escolha do modelo

`gpt-4.1`, com `temperature=0`. Testei também `gpt-4o`, `gpt-5.6-terra` e
`gpt-5.6-sol`, medindo quantas das 15 conversas saem iguais em execuções
repetidas — classificação que muda sozinha não serve para ordenar fila de
vendedor. O `gpt-4.1` foi o único com as 15 estáveis em três execuções.

Duas descobertas do teste valem registro. A família `gpt-5.x` **não aceita
`temperature=0`** (rejeita com HTTP 400, só permite o padrão 1), o que a torna
menos estável nesta tarefa: no `gpt-5.6-terra`, uma conversa oscilou entre
`quente` e `morno` entre execuções — mudança de classe altera o que o vendedor
faz. E o `gpt-5.6-sol`, modelo de raciocínio, levou mais de 10 minutos onde os
outros levam menos de um, inviável para volume sem paralelização.

O script envia `temperature` apenas aos modelos que a aceitam, o que permite
trocar de modelo sem editar a chamada.

Uma ressalva sobre o método, porque é fácil errar aqui: `temperature=0` reduz a
variação mas **não garante determinismo**. As duas primeiras execuções deram
resultado idêntico e eu tratei isso como reprodutibilidade — só na terceira, e
comparando modelos, a variação apareceu. Medir estabilidade exige mais de duas
amostras.

### Critérios de classificação

O prompt avalia cinco fatores objetivos: **imóvel disponível**, **orçamento
declarado**, **prazo definido**, **informação técnica** (planta, metragem) e
**aceite de avanço** (visita, medição, reunião). A classe sai da contagem, não de
uma descrição em prosa — isso reduz a margem de interpretação e torna auditável
por que um lead caiu numa classe.

Quatro decisões moldaram esses critérios, e as três primeiras vieram de erros
observados nos resultados:

**Desqualificação acontece antes da contagem.** Numa versão anterior, o piso de
R$ 12.000 era uma cláusula dentro dos critérios de classe, e a contagem o
atropelava: um lead com imóvel, metragem e orçamento de R$ 8 mil era classificado
como frio em vez de fora do perfil. Hoje a checagem de desqualificação é um passo
que encerra a análise.

**Orçamento declarado é obrigatório para `quente`.** É o que separa projeto real
de intenção. Sem essa regra, um lead com projeto grande mas sem valor definido
ocupava o horário mais caro do time. A ausência de orçamento impede a promoção a
quente, mas nunca rebaixa para frio — precisou ficar explícito, porque a
classificação de uma conversa oscilava entre morno e frio justamente aí.

**P1 é escasso por definição.** Numa versão anterior, todos os leads quentes
receberam P1 — uma fila em que tudo é urgente não ordena nada. P1 hoje exige
gatilho declarado: prazo de até 30 dias, decisão na semana, concorrência com
data, ou volume excepcional. O resultado atual distribui 2 em P1 e 4 em P2.

**A conversa do lead é entrada não confiável.** As instruções ficam em tags
nomeadas e a transcrição entra dentro de `<conversa>`, separando o que é
instrução do que é texto de terceiro — reduz a chance de o modelo tratar o
conteúdo da conversa como comando.

### Decisões de schema

Além dos campos pedidos no enunciado, incluí quatro:

- **`confianca`** (`alta`/`media`/`baixa`) — separa "este lead é frio" de "esta
  conversa é curta demais para eu saber". São situações diferentes: a primeira o
  vendedor descarta, a segunda ele precisa cutucar. Sem esse campo as duas viram
  `frio` e ficam indistinguíveis. Mede a informação disponível, não a qualidade
  do lead: um frio com motivo declarado ("estou pesquisando para daqui a 2 anos")
  é confiança alta. Duas das 15 conversas saíram com confiança baixa;
- **`orcamento_mencionado`** e **`prazo_mencionado`** — os dois dados que mais
  pesam na priorização de um ticket de R$ 15 a 75 mil. Em campo próprio, dão
  filtro e ordenação sem precisar reler o resumo. Numa faixa ("12 a 15 mil"), o
  prompt registra o limite inferior: a decisão de atendimento usa o pior caso;
- **`ambientes`** — permite rotear para o especialista certo e dimensionar a
  proposta antes do primeiro contato;
- **`status`** (`ok`/`erro`) — distingue classificação real de registro produzido
  por falha, o que torna o arquivo reprocessável.

### Garantia de JSON válido

O enunciado exige JSON válido em todos os casos. São quatro camadas: a chamada
usa `response_format={"type": "json_object"}`; se ainda assim vier texto em volta
ou bloco markdown, o parser extrai o objeto; todo campo passa por normalização
(classe fora do vocabulário vira `frio`, prioridade não-numérica vira 5, `sinais`
que veio como string vira lista vazia, e o `conversa_id` é sempre reescrito a
partir da entrada em vez do eco do modelo); e se as 3 tentativas falharem, sai um
registro completo com `status: "erro"` e o motivo, no mesmo schema — quem consome
o arquivo tem um caminho de leitura só.

Cada camada foi verificada com entrada correspondente. O tratamento de falha, em
particular, foi testado em condição real duas vezes: a primeira execução ocorreu
com a conta sem créditos (429), e depois um teste com `gpt-5.6` foi rejeitado por
causa da temperatura (400). Nos dois casos saíram 15 JSONs válidos com o motivo
preservado, sem exceção levantada — foi assim que diagnostiquei o segundo
problema em segundos.

### Como avaliar a qualidade em produção

> *Como você saberia, daqui a três meses e com 4.000 conversas processadas, se
> essa classificação está funcionando bem ou não?*

O erro seria medir "acurácia" contra um gabarito, porque não existe gabarito: a
classificação certa é a que faz o vendedor ganhar dinheiro. Três frentes:

**1. Contra o desfecho real (a única medida que importa).** Cada conversa tem um
lead, e cada lead tem uma etapa no funil. Passados os meses, dá para cruzar a
classificação com o que aconteceu de fato. A pergunta é se a classificação
ordena: a taxa de conversão dos `quente` precisa ser consistentemente maior que a
dos `morno`, e a destes maior que a dos `frio`.

O número que eu acompanharia é a **taxa de conversão por classe, medida
mensalmente**. Com os dados atuais como referência, `quente` deveria converter
acima de 30% e `frio` abaixo de 5%. Se as faixas se aproximarem, a classificação
parou de discriminar e virou ruído — mesmo que "pareça" certa em leitura manual.

Duas medidas complementares:

- **Vendas perdidas em `frio`** — receita fechada por leads classificados como
  frio ou fora do perfil. É o custo real dos falsos negativos, em reais. Se
  passar de 5% da receita do mês, o prompt está descartando gente que compra;
- **Ocupação indevida em `quente`** — proporção de leads quentes que morrem sem
  chegar a proposta. Mede o desperdício do horário mais caro do time.

**2. Contra o julgamento humano (amostral).** Toda semana, 20 conversas sorteadas
são reclassificadas por um vendedor, sem ver a saída do modelo. Compara-se a
concordância. É trabalhoso, então funciona por amostra — mas é o que detecta erro
sistemático antes do desfecho aparecer, que leva semanas.

Aqui vale medir separadamente a concordância nos casos de **confiança baixa**: se
o modelo acerta quando diz que tem certeza e erra quando diz que não tem, o campo
está calibrado e dá para automatizar a triagem só nos de confiança alta.

**3. Estabilidade da distribuição.** As proporções de cada classe, semana a
semana. Um salto sem mudança correspondente no mix de campanhas indica que algo
mudou no sistema, não no mercado — atualização do modelo pela OpenAI, mudança no
prompt, ou mudança no comportamento dos atendentes.

**O que dispararia alerta:**

| Sinal | Limiar | Por quê |
|---|---|---|
| Conversão de `quente` cai | Abaixo de 20% por 2 semanas | A classe perdeu poder de discriminação |
| Receita vinda de `frio`/`fora_do_perfil` | Acima de 5% do mês | Falsos negativos custando dinheiro |
| Distribuição de uma classe muda | Mais de 10 pontos percentuais vs. média de 4 semanas | Mudança no sistema, não no mercado |
| Confiança baixa | Acima de 25% das conversas | Conversas chegando curtas demais, ou prompt inadequado ao que mudou |
| Falhas de chamada | Acima de 2% no dia | Problema de integração |
| Concordância com o vendedor | Abaixo de 70% na amostra semanal | Critério desalinhado do time |

**O que eu construiria primeiro.** Com 4.000 conversas, nada disso funciona sem
guardar o histórico: cada classificação precisa registrar a versão do prompt, o
modelo e a data. Sem isso, é impossível saber se a métrica piorou porque o prompt
mudou ou porque o mercado mudou — e essa é a primeira pergunta que alguém faz
quando o número cai. É a razão de o prompt já estar versionado em arquivo próprio
desde a primeira versão.

### O que ficou de fora

- **Processamento paralelo.** As 15 conversas rodam em sequência, cerca de 30 segundos.
  Para 4.000, seria necessário paralelizar com controle de rate limit;
- **Persistência do histórico de classificações.** O arquivo é sobrescrito a cada
  execução. O plano de avaliação acima depende de guardar versão do prompt e
  modelo por classificação, o que pede um banco;
- **Cache por conversa.** Reprocessar as 15 refaz todas as chamadas. Com volume
  maior, valeria pular conversas já classificadas cujo texto não mudou.

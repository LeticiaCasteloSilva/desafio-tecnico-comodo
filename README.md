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

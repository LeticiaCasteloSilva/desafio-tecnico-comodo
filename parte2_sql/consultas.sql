-- Parte 2 | Analise do funil de vendas
--
-- Executar com DuckDB, a partir da raiz do projeto:
--   duckdb < parte2_sql/consultas.sql
-- ou pelo script que formata os resultados:
--   .venv/bin/python parte2_sql/executar.py
--
-- Os CSVs sao lidos diretamente pelo DuckDB, sem etapa de carga.
--
-- Premissas (detalhadas no README):
--   1. Investimento cobre 01/05 a 30/06; vendas vao ate 18/09. Todas as 49
--      vendas sao consideradas, porque o ciclo de venda e mais longo que a
--      janela de midia e cortar em 30/06 descartaria 31 vendas reais.
--   2. Leads sem campanha (6) e com cmp_999 (4) ficam fora das metricas por
--      campanha: nao ha gasto atribuivel a eles. Sao quantificados na query 0.
--   3. etapa_atual e o estado presente do lead, nao o historico. A query 2
--      assume etapas ordenadas e conta como "alcancou X" todo lead cuja etapa
--      atual seja X ou posterior. Leads perdidos ficam fora da escada.


-- Fontes reutilizadas pelas demais consultas.
CREATE OR REPLACE VIEW investimento AS
    SELECT * FROM read_csv_auto('dados/investimento_midia.csv');

CREATE OR REPLACE VIEW leads AS
    SELECT * FROM read_csv_auto('dados/leads.csv');

CREATE OR REPLACE VIEW vendas AS
    SELECT * FROM read_csv_auto('dados/vendas.csv');

-- Uma linha por campanha, com o gasto total do periodo.
CREATE OR REPLACE VIEW gasto_campanha AS
    SELECT
        campanha_id,
        any_value(plataforma)    AS plataforma,
        any_value(nome_campanha) AS nome_campanha,
        SUM(gasto)               AS gasto_total,
        SUM(impressoes)          AS impressoes,
        SUM(cliques)             AS cliques
    FROM investimento
    GROUP BY campanha_id;

-- Apenas os leads com origem rastreavel ate uma campanha com gasto registrado.
CREATE OR REPLACE VIEW leads_atribuidos AS
    SELECT l.*
    FROM leads l
    JOIN gasto_campanha g ON g.campanha_id = l.campanha_id;


-- ---------------------------------------------------------------------------
-- 0 | Controle de integridade
--
-- Roda antes das demais para deixar explicito quanto do dado ficou de fora e
-- por que. Sem isso, os totais das outras queries nao fecham com os arquivos
-- e nao ha como saber se a diferenca e intencional.
-- ---------------------------------------------------------------------------
SELECT 'leads no arquivo'                AS metrica, COUNT(*) AS qtd FROM leads
UNION ALL
SELECT 'leads sem campanha',             COUNT(*) FROM leads WHERE campanha_id IS NULL
UNION ALL
SELECT 'leads com campanha inexistente', COUNT(*) FROM leads l
    WHERE l.campanha_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM gasto_campanha g WHERE g.campanha_id = l.campanha_id)
UNION ALL
SELECT 'leads atribuidos (base das analises)', COUNT(*) FROM leads_atribuidos
UNION ALL
SELECT 'vendas no arquivo',              COUNT(*) FROM vendas
UNION ALL
SELECT 'vendas sem lead correspondente', COUNT(*) FROM vendas v
    WHERE NOT EXISTS (SELECT 1 FROM leads l WHERE l.lead_id = v.lead_id)
UNION ALL
-- consequencia da exclusao dos leads sem origem: uma venda real fica fora
-- das metricas por campanha. Explicitada aqui para os totais fecharem.
SELECT 'vendas de leads sem campanha (fora do rateio)', COUNT(*) FROM vendas v
    JOIN leads l ON l.lead_id = v.lead_id
    WHERE NOT EXISTS (SELECT 1 FROM gasto_campanha g WHERE g.campanha_id = l.campanha_id)
UNION ALL
SELECT 'vendas atribuidas (base das analises)', COUNT(*) FROM vendas v
    JOIN leads_atribuidos l ON l.lead_id = v.lead_id
UNION ALL
SELECT 'vendas fechadas apos 30/06',     COUNT(*) FROM vendas WHERE data_fechamento > DATE '2026-06-30';


-- ---------------------------------------------------------------------------
-- 1 | Custo por lead e custo por venda, por campanha
--
-- Ordenado do melhor para o pior custo por venda. Campanhas sem venda ficam
-- no fim (custo por venda indefinido, nao zero).
-- ---------------------------------------------------------------------------
WITH leads_por_campanha AS (
    SELECT campanha_id, COUNT(*) AS leads
    FROM leads_atribuidos
    GROUP BY campanha_id
),
vendas_por_campanha AS (
    SELECT l.campanha_id, COUNT(*) AS vendas, SUM(v.valor_contrato) AS receita
    FROM vendas v
    JOIN leads_atribuidos l ON l.lead_id = v.lead_id
    GROUP BY l.campanha_id
)
SELECT
    g.campanha_id,
    g.plataforma,
    g.nome_campanha,
    ROUND(g.gasto_total, 2)                             AS gasto,
    COALESCE(l.leads, 0)                                AS leads,
    COALESCE(v.vendas, 0)                               AS vendas,
    ROUND(g.gasto_total / NULLIF(l.leads, 0), 2)        AS custo_por_lead,
    ROUND(g.gasto_total / NULLIF(v.vendas, 0), 2)       AS custo_por_venda,
    ROUND(100.0 * v.vendas / NULLIF(l.leads, 0), 1)     AS taxa_conversao_pct
FROM gasto_campanha g
LEFT JOIN leads_por_campanha  l ON l.campanha_id = g.campanha_id
LEFT JOIN vendas_por_campanha v ON v.campanha_id = g.campanha_id
ORDER BY custo_por_venda ASC NULLS LAST;


-- ---------------------------------------------------------------------------
-- 2 | Funil por campanha: alcance de cada etapa e perda entre etapas
--
-- etapa_atual guarda o estado presente, nao o caminho percorrido. Assumindo as
-- etapas ordenadas, um lead "alcancou" toda etapa ate a sua posicao atual.
-- Leads perdidos nao entram na escada: nao ha registro de onde pararam.
-- ---------------------------------------------------------------------------
WITH ordem_etapas(etapa, posicao) AS (
    VALUES ('novo', 1),
           ('em_atendimento', 2),
           ('qualificado', 3),
           ('briefing_realizado', 4),
           ('proposta', 5),
           ('vendido', 6)
),
leads_posicionados AS (
    SELECT l.campanha_id, o.posicao
    FROM leads_atribuidos l
    JOIN ordem_etapas o ON o.etapa = l.etapa_atual
),
alcance AS (
    -- para cada etapa, quantos leads chegaram nela ou passaram dela
    SELECT
        lp.campanha_id,
        o.posicao,
        o.etapa,
        COUNT(*) AS leads_que_alcancaram
    FROM leads_posicionados lp
    JOIN ordem_etapas o ON lp.posicao >= o.posicao
    GROUP BY lp.campanha_id, o.posicao, o.etapa
)
SELECT
    a.campanha_id,
    g.nome_campanha,
    a.posicao,
    a.etapa,
    a.leads_que_alcancaram,
    LAG(a.leads_que_alcancaram) OVER j                  AS etapa_anterior,
    ROUND(100.0 * (LAG(a.leads_que_alcancaram) OVER j - a.leads_que_alcancaram)
          / NULLIF(LAG(a.leads_que_alcancaram) OVER j, 0), 1) AS perda_pct
FROM alcance a
JOIN gasto_campanha g ON g.campanha_id = a.campanha_id
WINDOW j AS (PARTITION BY a.campanha_id ORDER BY a.posicao)
ORDER BY a.campanha_id, a.posicao;


-- ---------------------------------------------------------------------------
-- 3 | Ticket medio e receita total por campanha
--
-- "Cliente de maior valor" tem duas leituras, e elas discordam: maior ticket
-- medio (valor por contrato) e maior receita total (volume x valor). As duas
-- colunas ficam lado a lado, com o ranking de cada uma.
-- ---------------------------------------------------------------------------
WITH receita AS (
    SELECT
        l.campanha_id,
        COUNT(*)                    AS vendas,
        SUM(v.valor_contrato)       AS receita_total,
        AVG(v.valor_contrato)       AS ticket_medio,
        MIN(v.valor_contrato)       AS menor_contrato,
        MAX(v.valor_contrato)       AS maior_contrato
    FROM vendas v
    JOIN leads_atribuidos l ON l.lead_id = v.lead_id
    GROUP BY l.campanha_id
)
SELECT
    g.campanha_id,
    g.plataforma,
    g.nome_campanha,
    r.vendas,
    ROUND(r.receita_total, 2)   AS receita_total,
    ROUND(r.ticket_medio, 2)    AS ticket_medio,
    ROUND(r.menor_contrato, 2)  AS menor_contrato,
    ROUND(r.maior_contrato, 2)  AS maior_contrato,
    ROUND(r.receita_total / NULLIF(g.gasto_total, 0), 2) AS roas,
    RANK() OVER (ORDER BY r.ticket_medio  DESC) AS rank_ticket,
    RANK() OVER (ORDER BY r.receita_total DESC) AS rank_receita
FROM receita r
JOIN gasto_campanha g ON g.campanha_id = r.campanha_id
ORDER BY r.receita_total DESC;

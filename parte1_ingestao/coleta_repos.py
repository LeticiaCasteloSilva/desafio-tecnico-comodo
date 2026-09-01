"""Coleta todos os repositorios publicos de uma organizacao no GitHub.

Projetado para rodar sem supervisao (cron, 6h da manha). As decisoes de
resiliencia estao documentadas no README; em resumo:

  - falha e sinalizada pelo exit code (0 = sucesso, 1 = falha), para o cron
    conseguir alertar;
  - o arquivo de saida so e substituido se a coleta terminar completa, para
    o relatorio das 9h nunca ler um arquivo pela metade;
  - erros transitorios (429, 5xx, timeout) sao repetidos com backoff.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

API = "https://api.github.com"
POR_PAGINA = 100
MAX_TENTATIVAS = 5
TIMEOUT = 30

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "saida"

COLUNAS = [
    "nome",
    "descricao",
    "linguagem_principal",
    "estrelas",
    "forks",
    "criado_em",
    "atualizado_em",
]

log = logging.getLogger("coleta_repos")


def configurar_log() -> None:
    """Log em stdout com timestamp, para o cron capturar em arquivo."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


class FalhaNaColeta(Exception):
    """Erro que impede a coleta de terminar completa."""


def montar_sessao(token: str | None) -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "desafio-tecnico-comoda/1.0",
        }
    )
    if token:
        sessao.headers["Authorization"] = f"Bearer {token}"
    return sessao


def esperar_rate_limit(resposta: requests.Response) -> float:
    """Quanto esperar quando a API sinaliza limite atingido.

    O GitHub responde 403 ou 429 com Retry-After ou com o timestamp de reset
    em X-RateLimit-Reset. Respeitar isso e mais confiavel do que adivinhar.
    """
    retry_after = resposta.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return float(retry_after)

    if resposta.headers.get("X-RateLimit-Remaining") == "0":
        reset = resposta.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            faltam = int(reset) - time.time()
            # o teto de 15 min evita o script dormir por horas em silencio
            return max(0.0, min(faltam + 1, 900.0))
    return 0.0


def buscar_pagina(sessao: requests.Session, url: str, params: dict | None) -> requests.Response:
    """GET com retry e backoff exponencial para erros transitorios."""
    espera = 2.0
    ultimo_erro = ""

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resposta = sessao.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as erro:
            ultimo_erro = f"erro de rede: {erro}"
        else:
            if resposta.status_code == 200:
                return resposta

            # 404 e 401 nao adianta repetir: org errada ou token invalido
            if resposta.status_code == 404:
                raise FalhaNaColeta(f"organizacao nao encontrada em {resposta.url}")
            if resposta.status_code == 401:
                raise FalhaNaColeta("token invalido ou expirado (401)")

            if resposta.status_code in (403, 429):
                pausa = esperar_rate_limit(resposta)
                if pausa > 0:
                    log.warning(
                        "rate limit atingido, aguardando %.0fs (tentativa %d/%d)",
                        pausa,
                        tentativa,
                        MAX_TENTATIVAS,
                    )
                    time.sleep(pausa)
                    continue

            ultimo_erro = f"HTTP {resposta.status_code}"
            if resposta.status_code < 500 and resposta.status_code not in (403, 429):
                raise FalhaNaColeta(f"{ultimo_erro} em {resposta.url}")

        if tentativa < MAX_TENTATIVAS:
            log.warning(
                "%s; nova tentativa em %.0fs (%d/%d)",
                ultimo_erro,
                espera,
                tentativa,
                MAX_TENTATIVAS,
            )
            time.sleep(espera)
            espera *= 2

    raise FalhaNaColeta(f"falhou apos {MAX_TENTATIVAS} tentativas: {ultimo_erro}")


def extrair_campos(repo: dict) -> dict:
    """Somente os campos pedidos no case, com nomes estaveis."""
    return {
        "nome": repo.get("name") or "",
        "descricao": (repo.get("description") or "").replace("\n", " ").strip(),
        "linguagem_principal": repo.get("language") or "",
        "estrelas": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "criado_em": repo.get("created_at") or "",
        "atualizado_em": repo.get("updated_at") or "",
    }


def coletar(sessao: requests.Session, org: str) -> list[dict]:
    """Percorre a paginacao ate o fim, seguindo o header Link."""
    url = f"{API}/orgs/{org}/repos"
    params = {"per_page": POR_PAGINA, "type": "public", "sort": "full_name"}
    repos: list[dict] = []
    vistos: set[str] = set()
    pagina = 0

    while url:
        pagina += 1
        resposta = buscar_pagina(sessao, url, params)
        lote = resposta.json()

        if not isinstance(lote, list):
            raise FalhaNaColeta(f"resposta inesperada da API na pagina {pagina}")

        for repo in lote:
            nome = repo.get("name")
            # a paginacao pode repetir itens se algo mudar no meio da coleta
            if nome and nome not in vistos:
                vistos.add(nome)
                repos.append(extrair_campos(repo))

        log.info("pagina %d: %d repositorios (acumulado: %d)", pagina, len(lote), len(repos))

        # o header Link e a fonte de verdade do "existe proxima pagina"
        url = resposta.links.get("next", {}).get("url")
        params = None  # a URL do Link ja carrega os parametros

    return repos


def gravar_csv(repos: list[dict], destino: Path) -> None:
    """Escrita atomica: grava em .tmp e so entao substitui o arquivo final.

    Assim o relatorio das 9h nunca encontra um CSV truncado por uma execucao
    que morreu no meio.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(destino.suffix + ".tmp")

    with temporario.open("w", newline="", encoding="utf-8") as arquivo:
        escritor = csv.DictWriter(arquivo, fieldnames=COLUNAS)
        escritor.writeheader()
        escritor.writerows(repos)

    temporario.replace(destino)


def main() -> int:
    configurar_log()
    load_dotenv(RAIZ / ".env")

    org = os.getenv("GITHUB_ORG", "vercel").strip()
    token = (os.getenv("GITHUB_TOKEN") or "").strip()

    if not token:
        log.warning(
            "GITHUB_TOKEN ausente: a API limita a 60 req/h sem autenticacao "
            "e a coleta provavelmente vai falhar. Configure o .env."
        )

    inicio = time.monotonic()
    log.info("iniciando coleta da organizacao '%s'", org)

    try:
        sessao = montar_sessao(token)
        repos = coletar(sessao, org)

        if not repos:
            raise FalhaNaColeta(f"nenhum repositorio retornado para '{org}'")

        carimbo = datetime.now(timezone.utc).strftime("%Y%m%d")
        destino = SAIDA / f"repos_{org}_{carimbo}.csv"
        gravar_csv(repos, destino)

    except FalhaNaColeta as erro:
        log.error("COLETA FALHOU: %s", erro)
        log.error("nenhum arquivo foi substituido; o dado anterior segue intacto")
        return 1
    except Exception as erro:  # rede, disco, parsing inesperado
        log.exception("COLETA FALHOU por erro inesperado: %s", erro)
        return 1

    duracao = time.monotonic() - inicio
    log.info("-" * 60)
    log.info("SUCESSO")
    log.info("organizacao......: %s", org)
    log.info("repositorios.....: %d", len(repos))
    log.info("arquivo..........: %s", destino)
    log.info("duracao..........: %.1fs", duracao)
    log.info("-" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

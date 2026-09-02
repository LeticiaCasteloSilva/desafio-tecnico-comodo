"""Classifica conversas de pre-vendas com um LLM.

Le dados/conversas_prevendas.json, chama o modelo uma vez por conversa e grava
uma linha JSON por conversa em saida/classificacoes.jsonl.

Garantia central: SEMPRE sai um JSON valido por conversa, inclusive quando a API
falha ou o modelo devolve algo que nao e JSON. Nesses casos o registro vem com
"status": "erro" e a classificacao neutra, para o consumidor conseguir distinguir
o que foi classificado do que precisa de retentativa.

Uso, a partir da raiz do projeto:
    .venv/bin/python parte3_classificacao/classificar.py
    .venv/bin/python parte3_classificacao/classificar.py --conversa CV001
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

RAIZ = Path(__file__).resolve().parent.parent
CONVERSAS = RAIZ / "dados" / "conversas_prevendas.json"
PROMPT = Path(__file__).resolve().parent / "prompts" / "classificacao.md"
SAIDA = RAIZ / "saida" / "classificacoes.jsonl"

MODELO = "gpt-4.1"
TEMPERATURA = 0  # classificacao deve variar o minimo possivel entre execucoes
MAX_TENTATIVAS = 3
TIMEOUT = 120

# Os modelos da familia gpt-5 aceitam apenas a temperatura padrao (1) e rejeitam
# qualquer outro valor com HTTP 400. Enviar o parametro so onde ele e aceito.
MODELOS_SEM_TEMPERATURA = ("gpt-5",)

CLASSES = {"quente", "morno", "frio", "fora_do_perfil"}
CONFIANCAS = {"alta", "media", "baixa"}

log = logging.getLogger("classificar")


def configurar_log() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def carregar_prompt() -> tuple[str, str]:
    """Separa o arquivo de prompt nas secoes '## sistema' e '## usuario'.

    O prompt fica versionado como markdown para o diff do Git mostrar mudancas
    de criterio de forma legivel.
    """
    texto = PROMPT.read_text(encoding="utf-8")
    partes = re.split(r"^## (sistema|usuario)\s*$", texto, flags=re.MULTILINE)

    secoes: dict[str, str] = {}
    for i in range(1, len(partes) - 1, 2):
        secoes[partes[i]] = partes[i + 1].strip()

    if "sistema" not in secoes or "usuario" not in secoes:
        raise ValueError(f"{PROMPT.name} precisa das secoes '## sistema' e '## usuario'")

    return secoes["sistema"], secoes["usuario"]


def formatar_transcricao(conversa: dict) -> str:
    return "\n".join(f"[{m['de']}] {m['texto']}" for m in conversa["mensagens"])


def registro_de_erro(conversa: dict, motivo: str) -> dict:
    """Resposta valida para quando nao foi possivel classificar.

    Mantem o mesmo schema do caso de sucesso: quem consome o arquivo nao precisa
    de dois caminhos de leitura. A classificacao neutra e 'frio'/prioridade 5
    para o lead nao ser promovido por engano, e o status permite reprocessar.
    """
    return {
        "conversa_id": conversa["conversa_id"],
        "classificacao": "frio",
        "prioridade": 5,
        "confianca": "baixa",
        "sinais": [],
        "orcamento_mencionado": None,
        "prazo_mencionado": None,
        "ambientes": [],
        "proxima_acao": "Revisar manualmente: classificacao automatica indisponivel",
        "resumo_para_o_vendedor": "Nao foi possivel classificar esta conversa automaticamente.",
        "status": "erro",
        "erro": motivo,
    }


def extrair_json(bruto: str) -> dict | None:
    """Tenta ler JSON da resposta do modelo, tolerando enfeites comuns."""
    texto = bruto.strip()

    # o modelo as vezes embrulha em bloco de codigo, mesmo instruido a nao fazer
    if texto.startswith("```"):
        texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto, flags=re.MULTILINE).strip()

    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:
        # ultima tentativa: pegar o primeiro objeto entre chaves
        inicio, fim = texto.find("{"), texto.rfind("}")
        if inicio == -1 or fim <= inicio:
            return None
        try:
            dados = json.loads(texto[inicio : fim + 1])
        except json.JSONDecodeError:
            return None

    return dados if isinstance(dados, dict) else None


def normalizar(dados: dict, conversa: dict) -> dict:
    """Forca o schema esperado, corrigindo o que o modelo entregou fora do combinado.

    Sem isso, um campo com tipo errado quebraria o consumidor do arquivo. Aqui
    todo desvio vira um valor valido, e a conversa segue classificada.
    """
    classificacao = str(dados.get("classificacao", "")).strip().lower()
    if classificacao not in CLASSES:
        classificacao = "frio"

    try:
        prioridade = int(dados.get("prioridade", 5))
    except (TypeError, ValueError):
        prioridade = 5
    prioridade = min(max(prioridade, 1), 5)

    confianca = str(dados.get("confianca", "")).strip().lower()
    if confianca not in CONFIANCAS:
        confianca = "baixa"

    sinais = dados.get("sinais") or []
    if not isinstance(sinais, list):
        sinais = []
    sinais = [str(s) for s in sinais[:5]]

    ambientes = dados.get("ambientes") or []
    if not isinstance(ambientes, list):
        ambientes = []
    ambientes = [str(a).strip().lower() for a in ambientes]

    orcamento: float | None
    try:
        bruto = dados.get("orcamento_mencionado")
        orcamento = float(bruto) if bruto is not None else None
    except (TypeError, ValueError):
        orcamento = None

    def texto_de(campo: str, padrao: str) -> str:
        valor = dados.get(campo)
        return str(valor).strip() if valor else padrao

    return {
        "conversa_id": conversa["conversa_id"],  # nunca confiar no eco do modelo
        "classificacao": classificacao,
        "prioridade": prioridade,
        "confianca": confianca,
        "sinais": sinais,
        "orcamento_mencionado": orcamento,
        "prazo_mencionado": (
            str(dados["prazo_mencionado"]).strip()
            if dados.get("prazo_mencionado")
            else None
        ),
        "ambientes": ambientes,
        "proxima_acao": texto_de("proxima_acao", "Revisar manualmente"),
        "resumo_para_o_vendedor": texto_de("resumo_para_o_vendedor", "Sem resumo."),
        "status": "ok",
        "campanha_id": conversa.get("campanha_id"),
    }


def classificar(cliente: OpenAI, conversa: dict, sistema: str, molde: str) -> dict:
    """Uma conversa, com retry. Nunca levanta excecao: sempre devolve um dict."""
    usuario = molde.format(
        conversa_id=conversa["conversa_id"],
        campanha_id=conversa.get("campanha_id", "desconhecida"),
        transcricao=formatar_transcricao(conversa),
    )

    espera = 2.0
    ultimo_erro = "desconhecido"

    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            parametros = {
                "model": MODELO,
                "timeout": TIMEOUT,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": sistema},
                    {"role": "user", "content": usuario},
                ],
            }
            if not MODELO.startswith(MODELOS_SEM_TEMPERATURA):
                parametros["temperature"] = TEMPERATURA

            resposta = cliente.chat.completions.create(**parametros)
            bruto = resposta.choices[0].message.content or ""

        except Exception as erro:  # rede, rate limit, autenticacao, indisponibilidade
            ultimo_erro = f"{type(erro).__name__}: {erro}"
            log.warning(
                "%s: falha na chamada (%d/%d) — %s",
                conversa["conversa_id"],
                tentativa,
                MAX_TENTATIVAS,
                ultimo_erro,
            )
        else:
            dados = extrair_json(bruto)
            if dados is not None:
                return normalizar(dados, conversa)

            ultimo_erro = "resposta nao e JSON valido"
            log.warning(
                "%s: resposta invalida (%d/%d)",
                conversa["conversa_id"],
                tentativa,
                MAX_TENTATIVAS,
            )

        if tentativa < MAX_TENTATIVAS:
            time.sleep(espera)
            espera *= 2

    log.error("%s: nao foi classificada — %s", conversa["conversa_id"], ultimo_erro)
    return registro_de_erro(conversa, ultimo_erro)


def gravar(registros: list[dict], destino: Path) -> None:
    """Escrita atomica, pelo mesmo motivo da Parte 1."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(destino.suffix + ".tmp")

    with temporario.open("w", encoding="utf-8") as arquivo:
        for registro in registros:
            arquivo.write(json.dumps(registro, ensure_ascii=False) + "\n")

    temporario.replace(destino)


def resumir(registros: list[dict]) -> None:
    total = len(registros)
    erros = [r for r in registros if r["status"] == "erro"]
    baixa = [r for r in registros if r["confianca"] == "baixa" and r["status"] == "ok"]

    contagem: dict[str, int] = {}
    for r in registros:
        contagem[r["classificacao"]] = contagem.get(r["classificacao"], 0) + 1

    log.info("-" * 60)
    log.info("conversas processadas: %d", total)
    log.info("classificadas com sucesso: %d", total - len(erros))
    log.info("falhas: %d", len(erros))
    for classe in sorted(contagem):
        log.info("  %-15s %d", classe, contagem[classe])
    log.info("confianca baixa: %d", len(baixa))
    log.info("arquivo: %s", SAIDA)
    log.info("-" * 60)


def main() -> int:
    configurar_log()
    load_dotenv(RAIZ / ".env")

    parser = argparse.ArgumentParser(description="Classifica conversas de pre-vendas")
    parser.add_argument("--conversa", help="classifica apenas uma conversa (ex: CV001)")
    args = parser.parse_args()

    chave = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not chave:
        log.error("OPENAI_API_KEY ausente: configure o .env")
        return 1

    try:
        sistema, molde = carregar_prompt()
        conversas = json.loads(CONVERSAS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        log.error("nao foi possivel iniciar: %s", erro)
        return 1

    if args.conversa:
        conversas = [c for c in conversas if c["conversa_id"] == args.conversa]
        if not conversas:
            log.error("conversa %s nao encontrada", args.conversa)
            return 1

    cliente = OpenAI(api_key=chave)
    log.info("classificando %d conversa(s) com %s", len(conversas), MODELO)

    registros = [classificar(cliente, c, sistema, molde) for c in conversas]

    gravar(registros, SAIDA)
    resumir(registros)

    # falha parcial ainda produz arquivo, mas sinaliza para o orquestrador
    return 1 if any(r["status"] == "erro" for r in registros) else 0


if __name__ == "__main__":
    sys.exit(main())

"""Executa as consultas de parte2_sql/consultas.sql e imprime os resultados.

O SQL e a entrega; este script existe para rodar o arquivo e formatar a
saida de forma legivel, sem depender do CLI do DuckDB estar instalado.

Uso, a partir da raiz do projeto:
    .venv/bin/python parte2_sql/executar.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

RAIZ = Path(__file__).resolve().parent.parent
CONSULTAS = Path(__file__).resolve().parent / "consultas.sql"

TITULOS = {
    0: "0 | Controle de integridade",
    1: "1 | Custo por lead e custo por venda, por campanha",
    2: "2 | Funil por campanha: alcance e perda entre etapas",
    3: "3 | Ticket medio e receita por campanha",
}


def separar_comandos(sql: str) -> list[str]:
    """Divide o arquivo em comandos.

    Os comentarios sao removidos ANTES do split: eles contem ';' no meio do
    texto, que quebraria a divisao dos comandos.
    """
    sem_comentarios = "\n".join(
        linha
        for linha in sql.splitlines()
        if linha.strip() and not linha.strip().startswith("--")
    )
    return [
        comando.strip()
        for comando in sem_comentarios.split(";")
        if comando.strip()
    ]


def main() -> int:
    if not CONSULTAS.exists():
        print(f"arquivo nao encontrado: {CONSULTAS}", file=sys.stderr)
        return 1

    # os caminhos dentro do SQL sao relativos a raiz do projeto
    conexao = duckdb.connect()
    conexao.execute(f"SET file_search_path = '{RAIZ}'")

    consultas_executadas = 0

    for comando in separar_comandos(CONSULTAS.read_text(encoding="utf-8")):
        resultado = conexao.execute(comando)

        # CREATE VIEW nao produz resultado para exibir
        if comando.lstrip().upper().startswith("CREATE"):
            continue

        titulo = TITULOS.get(consultas_executadas, f"consulta {consultas_executadas}")
        print()
        print("=" * 78)
        print(titulo)
        print("=" * 78)
        print(resultado.df().to_string(index=False))
        consultas_executadas += 1

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

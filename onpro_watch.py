#!/usr/bin/env python3
"""
Onpro Watch - Vigia de mudança de status multi-empresa
Consulta GET /meta/status-log/by-empresa (polling incremental por id) e avisa
no WhatsApp a cada mudança de status detectada. Sem quantidade produzida por
enquanto — ver memória do projeto sobre Nossa Fruta / op_centro_snapshot.

Reaproveita onpro_alerta.py (HTTP, envio, formatação, config de empresas).

Sem dependências externas — só a biblioteca padrão do Python 3.9+.

Uso:
    python3 onpro_watch.py --vigiar                    # residente, polling contínuo
    python3 onpro_watch.py --once --dry-run            # um ciclo isolado, sem enviar
    python3 onpro_watch.py --baseline                  # marca tudo como visto, sem enviar
    python3 onpro_watch.py --once --input eventos.json --empresa 12345678000199 --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from onpro_alerta import (
    API_URL, RODAPE, TZ,
    carregar_empresas, enviar_whatsapp, http_json, parse_dias, produto_curto,
    EMPRESAS_FILE,
)

POLL = int(os.getenv("ONPRO_POLL", "60"))               # segundos entre consultas
JANELA = os.getenv("ONPRO_JANELA", "07:00-19:00")       # janela de vigilância
DIAS = os.getenv("ONPRO_DIAS", "1-5")
WATCH_STATE_DIR = Path(os.getenv("ONPRO_WATCH_STATE", "/var/tmp/onpro_watch"))

STATUS_ROTULO = {"1": "Pendente", "2": "Iniciada", "3": "PARADA", "4": "Finalizada"}
STATUS_EMOJI = {"1": "⏸️", "2": "▶️", "3": "⚠️", "4": "✅"}

log = logging.getLogger("onpro_watch")
_parar = False


# ----------------------------------------------------------------------------
# Estado (cursor incremental por empresa — mais simples que o do Frosty porque
# a API já filtra por id > desde_id, não tem duplicata pra deduplicar aqui)
# ----------------------------------------------------------------------------
def _state_file(cnpj: str) -> Path:
    return WATCH_STATE_DIR / cnpj / "state.json"


def ler_estado(cnpj: str) -> dict:
    try:
        e = json.loads(_state_file(cnpj).read_text())
        e.setdefault("ultimo_id", 0)
        e.setdefault("inicializado", False)
        return e
    except (OSError, json.JSONDecodeError):
        return {"ultimo_id": 0, "inicializado": False}


def gravar_estado(cnpj: str, estado: dict) -> None:
    arquivo = _state_file(cnpj)
    try:
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        tmp = arquivo.with_suffix(".tmp")
        tmp.write_text(json.dumps(estado))
        tmp.replace(arquivo)  # troca atômica: não corrompe se morrer no meio
    except OSError as e:
        log.error("não consegui gravar o estado em %s: %s", arquivo, e)


# ----------------------------------------------------------------------------
# Busca
# ----------------------------------------------------------------------------
def buscar_eventos(cnpj: str, desde_id: int, caminho: str | None = None) -> list[dict]:
    if caminho:
        log.info("lendo payload local: %s", caminho)
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        eventos = dados if isinstance(dados, list) else dados.get("eventos", [])
        return [e for e in eventos if e.get("id", 0) > desde_id]
    if not API_URL:
        raise RuntimeError("ONPRO_API_URL não definida no ambiente")
    url = (f"{API_URL.rstrip('/')}/meta/status-log/by-empresa"
           f"?empresa={cnpj}&desde_id={desde_id}&limit=500")
    log.debug("consultando API: %s", url)
    resultado = http_json(url)
    return resultado or []


# ----------------------------------------------------------------------------
# Mensagem
# ----------------------------------------------------------------------------
def hora_de(ts) -> str:
    m = re.search(r"T(\d{2}):(\d{2})", str(ts))
    return f"{m.group(1)}:{m.group(2)}" if m else "--:--"


def _linha_evento(ev: dict) -> str:
    status = str(ev.get("status_novo"))
    emoji = STATUS_EMOJI.get(status, "🔔")
    rotulo = STATUS_ROTULO.get(status, status or "?")
    numero = ev.get("cod_ordem") or ev.get("id_meta")
    produto = produto_curto(ev.get("descricao_produto") or "")
    return f"{emoji} *{rotulo}* • OP {numero} — {produto}"


def montar_mensagem(eventos: list[dict], empresa_nome: str, agora: datetime | None = None) -> str:
    agora = agora or datetime.now(TZ)

    if len(eventos) == 1:
        ev = eventos[0]
        status = str(ev.get("status_novo"))
        emoji, rotulo = STATUS_EMOJI.get(status, "🔔"), STATUS_ROTULO.get(status, status or "?")
        numero = ev.get("cod_ordem") or ev.get("id_meta")
        linhas = [
            f"{emoji} *OP {numero} {rotulo.upper()}* — {empresa_nome} — {hora_de(ev.get('data'))}",
            produto_curto(ev.get("descricao_produto") or ""),
        ]
        if status == "3":
            linhas.append("\n⚠️ _Verificar o motivo da interrupção._")
        return "\n".join(linhas) + RODAPE

    linhas = [f"🔔 *ATUALIZAÇÃO DE PRODUÇÃO — {empresa_nome} — {agora.strftime('%H:%M')}*", ""]
    for ev in eventos:
        linhas.append(_linha_evento(ev))
        linhas.append(f"  _{hora_de(ev.get('data'))}_")
    return "\n".join(linhas) + RODAPE


# ----------------------------------------------------------------------------
# Ciclo
# ----------------------------------------------------------------------------
def ciclo_empresa(empresa: dict, *, dry_run: bool = False, baseline: bool = False,
                   entrada: str | None = None) -> int:
    cnpj, nome, grupo = empresa["cnpj"], empresa["nome"], empresa["grupo_whatsapp"]
    estado = ler_estado(cnpj)

    try:
        eventos = buscar_eventos(cnpj, estado["ultimo_id"], entrada)
    except Exception as e:
        log.error("[%s] falha ao consultar a API: %s", nome, e)
        return 1

    # Primeira execução: NÃO despeja o histórico do dia inteiro no grupo.
    if baseline or not estado["inicializado"]:
        maior = max((e["id"] for e in eventos), default=estado["ultimo_id"])
        estado.update(ultimo_id=maior, inicializado=True)
        gravar_estado(cnpj, estado)
        log.info("[%s] baseline definido: %d evento(s) marcado(s) como visto, nada enviado",
                 nome, len(eventos))
        return 0

    if not eventos:
        log.debug("[%s] sem novidades (cursor em id=%d)", nome, estado["ultimo_id"])
        return 0

    log.info("[%s] %d nova(s) mudança(s): %s", nome, len(eventos),
             ", ".join(f"OP {e.get('cod_ordem')}:{e.get('status_novo')}" for e in eventos))

    texto = montar_mensagem(eventos, nome)

    if dry_run:
        print("\n" + "-" * 60 + f"\n[{nome}]\n{texto}\n" + "-" * 60)
        return 0

    try:
        resp = enviar_whatsapp(texto, grupo)
        log.info("[%s] enviado (id=%s)", nome, (resp.get("key") or {}).get("id", "?"))
    except Exception as e:
        # NÃO avança o cursor: a próxima volta tenta de novo
        log.error("[%s] falha ao enviar, eventos seguem pendentes: %s", nome, e)
        return 2

    estado["ultimo_id"] = max(e["id"] for e in eventos)
    gravar_estado(cnpj, estado)
    return 0


def ciclo_todas(empresas: list[dict], **kwargs) -> int:
    pior = 0
    for empresa in empresas:
        try:
            codigo = ciclo_empresa(empresa, **kwargs)
        except Exception as e:
            log.exception("[%s] erro não tratado: %s", empresa.get("nome"), e)
            codigo = 1
        pior = max(pior, codigo)
    return pior


# ----------------------------------------------------------------------------
# Janela de vigilância
# ----------------------------------------------------------------------------
def parse_janela(txt: str) -> tuple[tuple[int, int], tuple[int, int]]:
    ini, _, fim = txt.partition("-")
    def hm(p):
        h, _, m = p.strip().partition(":")
        return int(h), int(m or 0)
    return hm(ini), hm(fim)


def dentro_da_janela(agora: datetime, ini, fim, dias) -> bool:
    if agora.weekday() not in dias:
        return False
    atual = (agora.hour, agora.minute)
    return ini <= atual < fim


def proxima_abertura(agora: datetime, ini, dias) -> datetime:
    for delta in range(8):
        dia = agora + timedelta(days=delta)
        if dia.weekday() not in dias:
            continue
        alvo = dia.replace(hour=ini[0], minute=ini[1], second=0, microsecond=0)
        if alvo > agora:
            return alvo
    raise RuntimeError("janela inválida")


def _sinal(signum, _frame):
    global _parar
    _parar = True
    log.info("sinal %s recebido — encerrando", signal.Signals(signum).name)


def vigiar(empresas: list[dict]) -> int:
    ini, fim = parse_janela(JANELA)
    dias = parse_dias(DIAS)
    signal.signal(signal.SIGTERM, _sinal)
    signal.signal(signal.SIGINT, _sinal)
    log.info("vigia ativo | janela=%s | dias=%s | intervalo=%ds | fuso=%s | empresas=%d",
             JANELA, DIAS, POLL, TZ.key, len(empresas))

    while not _parar:
        agora = datetime.now(TZ)

        if not dentro_da_janela(agora, ini, fim, dias):
            abre = proxima_abertura(agora, ini, dias)
            log.info("fora da janela — dormindo até %s", abre.strftime("%d/%m %H:%M"))
            while not _parar and datetime.now(TZ) < abre:
                time.sleep(min(5, max(0.5, (abre - datetime.now(TZ)).total_seconds())))
            continue

        try:
            ciclo_todas(empresas)
        except Exception as e:
            log.exception("erro não tratado no ciclo: %s", e)

        alvo = time.monotonic() + POLL
        while not _parar and time.monotonic() < alvo:
            time.sleep(min(5, max(0.5, alvo - time.monotonic())))

    log.info("vigia encerrado")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Vigia de mudança de status multi-empresa")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--vigiar", action="store_true", help="polling contínuo na janela")
    g.add_argument("--once", action="store_true", help="executa um ciclo e sai")
    g.add_argument("--baseline", action="store_true",
                   help="marca o estado atual como visto sem enviar nada")
    ap.add_argument("--empresa", help="roda só pra um CNPJ (em vez de todas as empresas.json)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--input", help="lê eventos de um arquivo JSON (use com --empresa)")
    ap.add_argument("--empresas-file", default=str(EMPRESAS_FILE),
                    help="caminho do arquivo de config das empresas (padrão: %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.input and not args.empresa:
        log.error("--input exige --empresa (não dá pra simular payload de várias empresas de uma vez)")
        return 2

    try:
        empresas = carregar_empresas(Path(args.empresas_file))
    except RuntimeError as e:
        log.error("%s", e)
        return 2

    if args.empresa:
        empresas = [e for e in empresas if e["cnpj"] == args.empresa]
        if not empresas:
            log.error("empresa %s não encontrada em %s", args.empresa, args.empresas_file)
            return 2

    if args.baseline:
        return ciclo_todas(empresas, baseline=True, entrada=args.input)
    if args.once or args.dry_run:
        return ciclo_todas(empresas, dry_run=args.dry_run, entrada=args.input)
    return vigiar(empresas)


if __name__ == "__main__":
    sys.exit(main())

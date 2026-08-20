#!/usr/bin/env python3
"""
Onpro Alerta - Monitoramento de produção multi-empresa
Avisa no WhatsApp quando nenhuma OP foi iniciada até o momento, listando as OPs
programadas do dia. Uma tabela só (`cadastro_meta`, via GET /meta/by-empresa da
api-fast-onimus), N empresas, um processo.

Sem dependências externas — só a biblioteca padrão do Python 3.9+.

Uso:
    python3 onpro_alerta.py                            # roda pra todas as empresas de empresas.json
    python3 onpro_alerta.py --empresa 12345678000199    # só uma empresa
    python3 onpro_alerta.py --dry-run                   # imprime, não envia
    python3 onpro_alerta.py --simular sem-inicio         # força o cenário de alerta com dados reais
    python3 onpro_alerta.py --input sample.json --empresa 12345678000199 --dry-run
    python3 onpro_alerta.py --agendar                    # fica residente, dispara em ONPRO_HORARIOS

Agendamento (ONPRO_HORARIOS/ONPRO_DIAS) é global por padrão, mas cada empresa em
empresas.json pode sobrescrever com os campos opcionais "horarios"/"dias" (mesmo
formato das env vars).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Configuração (tudo sobrescrevível por variável de ambiente)
# ----------------------------------------------------------------------------
TZ = ZoneInfo(os.getenv("ONPRO_TZ", "America/Fortaleza"))

API_URL = os.getenv("ONPRO_API_URL", "")  # base da api-fast-onimus, ex: http://api.onimus.com.br:8148
EVO_URL = os.getenv("EVOLUTION_URL", "https://evo.onimus.com.br/message/sendText/Onimus")
EVO_APIKEY = os.getenv("EVOLUTION_APIKEY", "")

TIMEOUT = int(os.getenv("ONPRO_TIMEOUT", "30"))
TENTATIVAS = int(os.getenv("ONPRO_RETRIES", "3"))
MAX_LISTA = int(os.getenv("ONPRO_MAX_LISTA", "8"))
STATE_DIR = Path(os.getenv("ONPRO_STATE", "/var/tmp/onpro_alerta"))
EMPRESAS_FILE = Path(os.getenv("ONPRO_EMPRESAS", "empresas.json"))

# Agendador interno (usado com --agendar). Ex: "07:30" ou "07:30,12:00,17:00"
HORARIOS = os.getenv("ONPRO_HORARIOS", "07:30")
DIAS = os.getenv("ONPRO_DIAS", "1-5")  # 1=segunda ... 7=domingo

RODAPE = "\n\n_Sistema de Monitoramento Onimus_"

# status de cadastro_meta: '1' pode iniciar, '2' em execução, '3' parada, '4' finalizada
STATUS_PENDENTE = "1"
STATUS_DESC = {"1": "Pendente", "2": "Em Andamento", "3": "Parada", "4": "Finalizada"}

log = logging.getLogger("onpro_alerta")


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def http_json(url: str, *, method: str = "GET", body: dict | None = None,
              headers: dict | None = None):
    """GET/POST com retry e backoff exponencial."""
    dados = json.dumps(body).encode() if body is not None else None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if dados:
        hdrs.setdefault("Content-Type", "application/json")

    ultimo_erro: Exception | None = None
    for tentativa in range(1, TENTATIVAS + 1):
        req = urllib.request.Request(url, data=dados, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                bruto = resp.read().decode("utf-8", errors="replace")
                return json.loads(bruto) if bruto.strip() else None
        except urllib.error.HTTPError as e:
            corpo = e.read().decode("utf-8", errors="replace")[:400]
            ultimo_erro = RuntimeError(f"HTTP {e.code}: {corpo}")
            # 4xx (exceto 429) não adianta repetir
            if 400 <= e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            ultimo_erro = e

        if tentativa < TENTATIVAS:
            espera = 2 ** tentativa
            log.warning("tentativa %d/%d falhou (%s) — repetindo em %ds",
                        tentativa, TENTATIVAS, ultimo_erro, espera)
            time.sleep(espera)

    raise RuntimeError(f"{method} {url} falhou após {TENTATIVAS} tentativas: {ultimo_erro}")


def buscar_ops(cnpj: str, data: str, caminho: str | None = None) -> list[dict]:
    if caminho:
        log.info("lendo payload local: %s", caminho)
        dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
        return dados if isinstance(dados, list) else dados.get("ops", [])
    if not API_URL:
        raise RuntimeError("ONPRO_API_URL não definida no ambiente")
    url = (f"{API_URL.rstrip('/')}/meta/by-empresa"
           f"?empresa={cnpj}&data_inicio={data}&data_final={data}&limit=500")
    log.debug("consultando API: %s", url)
    resultado = http_json(url)
    return resultado or []


def enviar_whatsapp(texto: str, grupo: str) -> dict:
    if not EVO_APIKEY:
        raise RuntimeError("EVOLUTION_APIKEY não definida no ambiente")
    log.info("enviando WhatsApp para %s (%d caracteres)", grupo, len(texto))
    return http_json(
        EVO_URL,
        method="POST",
        body={"number": grupo, "text": texto},
        headers={"apikey": EVO_APIKEY},
    ) or {}


# ----------------------------------------------------------------------------
# Formatação
# ----------------------------------------------------------------------------
MINUSCULAS = {"de", "da", "do", "das", "dos", "e", "com", "em", "para"}


def num(v) -> float:
    try:
        return float(str(v if v is not None else 0).replace(",", "."))
    except ValueError:
        return 0.0


def fmt_num(n: float) -> str:
    return f"{round(n):,}".replace(",", ".")


def titulo(txt: str) -> str:
    palavras = re.sub(r"\s+", " ", str(txt)).strip().lower().split(" ")
    return " ".join(
        p if i > 0 and p in MINUSCULAS else p.capitalize()
        for i, p in enumerate(palavras)
    )


def produto_curto(p: str) -> str:
    t = titulo(str(p).split(" - ")[0])
    t = re.sub(r"(\d)\s?(ml|kg|g|l)\b", lambda m: m.group(1) + m.group(2).upper(), t, flags=re.I)
    return t[:42].strip()


def linha_op(op: dict) -> str:
    numero = op.get("cod_ordem") or op.get("id")
    return f"• *OP {numero}* — {produto_curto(op.get('descricao_produto') or '')}"


def listar(ops: list[dict]) -> str:
    linhas = [linha_op(op) for op in ops[:MAX_LISTA]]
    resto = len(ops) - MAX_LISTA
    if resto > 0:
        linhas.append(f"_...e mais {resto} OP{'s' if resto > 1 else ''}_")
    return "\n".join(linhas)


# ----------------------------------------------------------------------------
# Montagem da mensagem
# ----------------------------------------------------------------------------
def alguma_iniciada(ops: list[dict]) -> bool:
    """Iniciada = qualquer OP que não esteja mais no status inicial (pendente)."""
    return any(str(op.get("status")) != STATUS_PENDENTE for op in ops)


def montar_alerta(ops: list[dict], empresa_nome: str, agora: datetime | None = None) -> tuple[str, dict]:
    """Retorna (texto, totais). Só chamar quando alguma_iniciada(ops) for False."""
    agora = agora or datetime.now(TZ)
    hora = agora.strftime("%H:%M")
    data_hoje = agora.strftime("%d/%m/%Y")

    total = len(ops)
    plural = "s" if total != 1 else ""
    pendentes = [o for o in ops if str(o.get("status")) == STATUS_PENDENTE] or ops

    texto = "\n".join([
        f"🚨 *ALERTA DE PRODUÇÃO — {hora}*",
        "",
        f"Nenhuma ordem de produção foi iniciada até o momento na unidade *{empresa_nome}*.",
        "",
        f"📋 *{total}* OP{plural} programada{plural} para hoje ({data_hoje}):",
        listar(pendentes),
        "",
        "⚠️ Verificar com a equipe de produção o motivo do não início.",
    ]) + RODAPE

    return texto, {"total": total}


# ----------------------------------------------------------------------------
# Empresas (config multi-tenant)
# ----------------------------------------------------------------------------
def carregar_empresas(caminho: Path) -> list[dict]:
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"arquivo de empresas não encontrado: {caminho}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{caminho} não é um JSON válido: {e}")

    obrigatorios = {"cnpj", "nome", "grupo_whatsapp"}
    for i, emp in enumerate(dados):
        faltando = obrigatorios - emp.keys()
        if faltando:
            raise RuntimeError(f"{caminho}[{i}]: faltando campo(s) {sorted(faltando)}")
        try:
            horarios_dias_empresa(emp)
        except ValueError as e:
            raise RuntimeError(f"{caminho}[{i}] ({emp.get('nome')}): {e}")
    return dados


def horarios_dias_empresa(empresa: dict) -> tuple[list[tuple[int, int]], set[int]]:
    """horarios/dias por empresa, com fallback pro default global (ONPRO_HORARIOS/ONPRO_DIAS)."""
    horarios = parse_horarios(empresa.get("horarios") or HORARIOS)
    dias = parse_dias(empresa.get("dias") or DIAS)
    return horarios, dias


# ----------------------------------------------------------------------------
# Anti-duplicata (por empresa)
# ----------------------------------------------------------------------------
def _chave(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()[:16]


def _state_file(cnpj: str) -> Path:
    return STATE_DIR / cnpj / "state.json"


def _ler_estado(cnpj: str) -> dict:
    try:
        return json.loads(_state_file(cnpj).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def ja_enviada(cnpj: str, texto: str) -> bool:
    estado = _ler_estado(cnpj)
    hoje = datetime.now(TZ).strftime("%Y-%m-%d")
    return estado.get("data") == hoje and _chave(texto) in estado.get("hashes", [])


def marcar_enviada(cnpj: str, texto: str) -> None:
    """Só grava DEPOIS do envio confirmado — senão uma falha de rede
    silenciaria a próxima execução."""
    hoje = datetime.now(TZ).strftime("%Y-%m-%d")
    estado = _ler_estado(cnpj)
    if estado.get("data") != hoje:
        estado = {"data": hoje, "hashes": []}
    estado["hashes"] = (estado.get("hashes", []) + [_chave(texto)])[-20:]
    arquivo = _state_file(cnpj)
    try:
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(json.dumps(estado))
    except OSError as e:
        log.warning("não consegui gravar o estado em %s: %s", arquivo, e)


# ----------------------------------------------------------------------------
# Execução por empresa
# ----------------------------------------------------------------------------
MARCA_TESTE = "🧪 _MENSAGEM DE TESTE — pode ignorar_\n\n"


def simular_sem_inicio(ops: list[dict]) -> list[dict]:
    """Finge que nenhuma OP começou, mantendo as OPs reais do dia.
    Só para teste — a mensagem sai marcada como tal."""
    d = copy.deepcopy(ops)
    for op in d:
        op["status"] = STATUS_PENDENTE
    return d


def executar_empresa(empresa: dict, *, dry_run: bool = False, entrada: str | None = None,
                      dedupe: bool = True, simular: str | None = None) -> int:
    cnpj, nome, grupo = empresa["cnpj"], empresa["nome"], empresa["grupo_whatsapp"]
    hoje = datetime.now(TZ).strftime("%Y-%m-%d")

    try:
        ops = buscar_ops(cnpj, hoje, entrada)
    except Exception as e:
        log.error("[%s] falha ao obter dados: %s", nome, e)
        return 1

    if not ops:
        log.info("[%s] nenhuma OP programada para hoje — nada a enviar", nome)
        return 0

    if simular == "sem-inicio":
        log.warning("[%s] MODO SIMULAÇÃO: forçando cenário 'nenhuma OP iniciada'", nome)
        ops = simular_sem_inicio(ops)

    if alguma_iniciada(ops):
        log.info("[%s] produção já iniciada — nada a enviar", nome)
        return 0

    texto, totais = montar_alerta(ops, nome)
    if simular:
        texto = MARCA_TESTE + texto
    log.info("[%s] totais: %s", nome, totais)

    if dry_run:
        print("\n" + "-" * 60)
        print(f"[{nome}]")
        print(texto)
        print("-" * 60)
        return 0

    if dedupe and ja_enviada(cnpj, texto):
        log.info("[%s] mensagem idêntica já enviada hoje — ignorando", nome)
        return 0

    try:
        resp = enviar_whatsapp(texto, grupo)
        log.info("[%s] enviado (id=%s)", nome, (resp.get("key") or {}).get("id", "?"))
        marcar_enviada(cnpj, texto)
        return 0
    except Exception as e:
        log.error("[%s] falha ao enviar: %s", nome, e)
        return 2


def executar_todas(empresas: list[dict], **kwargs) -> int:
    pior = 0
    for empresa in empresas:
        try:
            codigo = executar_empresa(empresa, **kwargs)
        except Exception as e:
            log.exception("[%s] erro não tratado: %s", empresa.get("nome"), e)
            codigo = 1
        pior = max(pior, codigo)
    return pior


# ----------------------------------------------------------------------------
# Agendador interno (para rodar como container de longa duração)
# ----------------------------------------------------------------------------
_parar = False


def _sinal(signum, _frame):
    global _parar
    _parar = True
    log.info("sinal %s recebido — encerrando", signal.Signals(signum).name)


def parse_horarios(txt: str) -> list[tuple[int, int]]:
    saida = []
    for parte in txt.split(","):
        parte = parte.strip()
        if not parte:
            continue
        h, _, m = parte.partition(":")
        h, m = int(h), int(m or 0)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"horário fora da faixa em {txt!r}: {parte!r}")
        saida.append((h, m))
    if not saida:
        raise ValueError(f"ONPRO_HORARIOS inválido: {txt!r}")
    return sorted(set(saida))


def parse_dias(txt: str) -> set[int]:
    """1=segunda ... 7=domingo. Aceita '1-5', '1,3,5', '1-5,7'."""
    dias: set[int] = set()
    for parte in txt.split(","):
        parte = parte.strip()
        if "-" in parte:
            a, b = parte.split("-")
            dias.update(range(int(a), int(b) + 1))
        elif parte:
            dias.add(int(parte))
    if not dias or not dias <= set(range(1, 8)):
        raise ValueError(f"ONPRO_DIAS inválido: {txt!r}")
    return {d - 1 for d in dias}  # converte para weekday() do Python (0=segunda)


def proxima_execucao(agora: datetime, horarios, dias) -> datetime:
    for delta in range(8):
        dia = agora + timedelta(days=delta)
        if dia.weekday() not in dias:
            continue
        for h, m in horarios:
            alvo = dia.replace(hour=h, minute=m, second=0, microsecond=0)
            if alvo > agora:
                return alvo
    raise RuntimeError("nenhum horário futuro encontrado")


def esta_na_hora(agora: datetime, horarios, dias) -> bool:
    return agora.weekday() in dias and (agora.hour, agora.minute) in horarios


def agendar(empresas: list[dict], *, dedupe: bool = True) -> int:
    # (empresa, horarios, dias) resolvido uma vez — cada empresa pode ter seu
    # próprio ONPRO_HORARIOS/ONPRO_DIAS via empresas.json, senão usa o default global.
    agenda = [(empresa, *horarios_dias_empresa(empresa)) for empresa in empresas]

    signal.signal(signal.SIGTERM, _sinal)
    signal.signal(signal.SIGINT, _sinal)
    log.info("agendador ativo | fuso=%s | empresas=%d", TZ.key, len(empresas))
    for empresa, horarios, dias in agenda:
        log.info("  [%s] horários=%s dias=%s", empresa["nome"],
                 empresa.get("horarios") or HORARIOS, empresa.get("dias") or DIAS)

    while not _parar:
        agora = datetime.now(TZ)
        alvo = min(proxima_execucao(agora, h, d) for _, h, d in agenda)
        log.info("próxima execução: %s", alvo.strftime("%d/%m/%Y %H:%M"))

        while not _parar and datetime.now(TZ) < alvo:
            time.sleep(min(5, max(0.5, (alvo - datetime.now(TZ)).total_seconds())))

        if _parar:
            break

        agora = datetime.now(TZ).replace(second=0, microsecond=0)
        devidas = [empresa for empresa, h, d in agenda if esta_na_hora(agora, h, d)]
        if devidas:
            log.info("--- disparo agendado: %s ---", ", ".join(e["nome"] for e in devidas))
            try:
                executar_todas(devidas, dedupe=dedupe)
            except Exception as e:  # nunca deixa o agendador morrer
                log.exception("erro não tratado na execução: %s", e)
        time.sleep(61)  # evita disparar duas vezes no mesmo minuto

    log.info("agendador encerrado")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Alerta de produção multi-empresa via WhatsApp")
    ap.add_argument("--empresa", help="roda só pra um CNPJ (em vez de todas as empresas.json)")
    ap.add_argument("--dry-run", action="store_true", help="imprime sem enviar")
    ap.add_argument("--input", help="lê o payload de um arquivo JSON em vez da API (use com --empresa)")
    ap.add_argument("--sem-dedupe", action="store_true", help="ignora o controle de duplicata")
    ap.add_argument("--simular", choices=["sem-inicio"],
                    help="usa os dados reais mas força o cenário de alerta (marca como teste)")
    ap.add_argument("--agendar", action="store_true",
                    help="fica residente e dispara nos horários de ONPRO_HORARIOS")
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

    if args.agendar:
        return agendar(empresas, dedupe=not args.sem_dedupe)

    return executar_todas(empresas, dry_run=args.dry_run, entrada=args.input,
                          dedupe=not args.sem_dedupe, simular=args.simular)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -- coding: utf-8 --
"""
bot_escanteios_rp_vip_plus_multi_v2_economico.py

VIP Plus — Central de Estratégias Múltiplas com economia de requests:
- Ignora jogos < 25' para reduzir consumo (~70%+)
- Intervalo de varredura dinâmico conforme nº de jogos (modo econômico)
- Estratégias independentes (HT, FT, Over 2ºT, Campo Pequeno, Jogo Aberto)
- Poisson para sugerir linhas
- Link Bet365 por busca otimizada
- Anti-spam por fixture/estratégia
- Tratamento de limites (HTTP 429) com backoff
- Mensagens no Telegram com MarkdownV2 + escape seguro (sem erro 400)

ENV (Render → Environment):
- API_FOOTBALL_KEY : chave oficial dashboard.api-football.com
- TOKEN            : token do Telegram (BotFather)
- TELEGRAM_CHAT_ID : id do grupo/canal (ex: -1003188916464)
- SCAN_INTERVAL    : base em segundos (default 120)
- RENOTIFY_MINUTES : minutos entre reavisos (default 10)
- LOG_LEVEL        : INFO ou DEBUG
"""

import os
import re
import time
import math
import logging
import threading
import urllib.parse
from collections import defaultdict
from typing import Dict, Any, List, Optional

import requests
from flask import Flask, request, jsonify

# ========================= LOG / ENV =========================

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot_escanteios_rp_vip_multi_v2_economico')

API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY')
TOKEN = os.getenv('TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SCAN_INTERVAL_BASE = int(os.getenv('SCAN_INTERVAL', '120'))      # intervalo base (s)
RENOTIFY_MINUTES = int(os.getenv('RENOTIFY_MINUTES', '10'))      # anti-spam

if not API_FOOTBALL_KEY:
    raise ValueError("⚠️ API_FOOTBALL_KEY não definida.")
if not TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("⚠️ Defina TOKEN e TELEGRAM_CHAT_ID.")

# ===================== API FOOTBALL CONFIG ===================

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ===================== PARÂMETROS DE ANÁLISE =================

HT_WINDOW = (30, 40)  # alvo HT
FT_WINDOW = (80, 90)  # alvo FT

MIN_PRESSURE_SCORE = 0.5
ATTACKS_MIN = 5
ATTACKS_DIFF = 4
DANGER_MIN = 5
DANGER_DIFF = 3

# Estádios "apertados" / favorecem cantos (expansível)
SMALL_STADIUMS = {
    'loftus road','vitality stadium','kenilworth road','turf moor',
    'bramall lane','ewood park','the den','carrow road',
    'bet365 stadium','pride park','liberty stadium','fratton park',
}

# Anti-spam: {fixture_id: {signal_key: last_ts}}
sent_signals: Dict[int, Dict[str, float]] = defaultdict(dict)

# Counters opcionais (diagnóstico)
request_count = 0
last_rate_headers = {}

# ====================== ESCAPE MARKDOWNV2 =====================

MDV2_SPECIALS = r'[_*\[\]()~`>#+\-=|{}.!]'

def escape_markdown(text: Any) -> str:
    """
    Escapa os caracteres especiais do Telegram MarkdownV2.
    Use APENAS em campos dinâmicos (nomes de times, ligas, números, estádios).
    Não escape URLs cruas (deixe-as fora desta função).
    """
    s = str(text) if text is not None else ""
    return re.sub(MDV2_SPECIALS, r'\\\g<0>', s)

# ============================ FLASK ===========================

app = Flask(__name__)

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'status': 'ok',
        'service': 'Bot Escanteios RP VIP Plus — Multi v2 (Econômico)',
        'scan_interval_base': SCAN_INTERVAL_BASE,
        'renotify_minutes': RENOTIFY_MINUTES
    }), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    logger.debug("Update Telegram (webhook): %s", str(data)[:500])
    return jsonify({"status": "ok"}), 200

# ====================== FUNÇÕES UTIL / POISSON ======================

def poisson_pmf(k: int, lam: float) -> float:
    try:
        return (lam**k) * math.exp(-lam) / math.factorial(k) if k >= 0 else 0.0
    except Exception:
        return 0.0

def poisson_cdf_le(k: int, lam: float) -> float:
    return sum(poisson_pmf(i, lam) for i in range(0, int(k) + 1))

def poisson_tail_ge(k: int, lam: float) -> float:
    return 1.0 if k <= 0 else 1.0 - poisson_cdf_le(k - 1, lam)

def predict_corners_and_line_metrics(current_total: int, lam_remaining: float, candidate_line) -> Dict[str, float]:
    is_fractional = isinstance(candidate_line, float) and (candidate_line % 1) != 0
    if is_fractional:
        required = int(math.floor(candidate_line) + 1)
        p_win = poisson_tail_ge(required - current_total, lam_remaining)
        p_push = 0.0
        p_lose = 1.0 - p_win
    else:
        L = int(candidate_line)
        required = L + 1
        p_win = poisson_tail_ge(required - current_total, lam_remaining)
        k_eq = L - current_total
        p_push = poisson_pmf(k_eq, lam_remaining) if k_eq >= 0 else 0.0
        p_lose = 1.0 - p_win - p_push
    return {'line': float(candidate_line), 'p_win': p_win, 'p_push': p_push, 'p_lose': p_lose}

def evaluate_candidate_lines(current_total: int, lam: float, lines_to_check=None) -> List[Dict[str, float]]:
    lines_to_check = lines_to_check or [3.5, 4.0, 4.5, 5.0, 5.5]
    results = [predict_corners_and_line_metrics(current_total, lam, L) for L in lines_to_check]
    results.sort(key=lambda x: x['p_win'], reverse=True)
    return results

# ===================== TELEGRAM / LINKS =====================

def send_telegram_message(text: str):
    """
    Envia mensagem em MarkdownV2 seguro.
    Atenção: não inclua URLs dentro de campos escapados; deixe a URL "crua".
    """
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.warning("Erro Telegram %s: %s", r.status_code, r.text[:400])
    except Exception as e:
        logger.exception("Erro ao enviar Telegram: %s", e)

def build_bet365_link(fixture: Dict[str, Any]) -> str:
    """
    Link por busca otimizada (não escape a URL).
    """
    home = fixture.get('teams', {}).get('home', {}).get('name', '') or ''
    away = fixture.get('teams', {}).get('away', {}).get('name', '') or ''
    league = fixture.get('league', {}).get('name', '') or ''
    query = f"site:bet365.com {home} x {away} {league}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

def build_vip_message(fixture: Dict[str, Any], strategy_title: str, metrics: Dict[str, Any],
                      best_lines: List[Dict[str, float]]) -> str:
    """
    Monta a mensagem com todos os campos dinâmicos escapados para MarkdownV2.
    Sem usar tags HTML; todos os textos dinâmicos passam por escape_markdown().
    """
    teams = fixture.get('teams', {})
    home = escape_markdown(teams.get('home', {}).get('name', '?'))
    away = escape_markdown(teams.get('away', {}).get('name', '?'))
    minute = escape_markdown(metrics.get('minute', 0))
    goals = fixture.get('goals', {})
    score = f"{goals.get('home','-')} x {goals.get('away','-')}"
    score = escape_markdown(score)

    total_corners = escape_markdown(metrics.get('total_corners'))
    home_c = escape_markdown(metrics.get('home_corners'))
    away_c = escape_markdown(metrics.get('away_corners'))
    home_att = escape_markdown(metrics.get('home_attacks'))
    away_att = escape_markdown(metrics.get('away_attacks'))
    home_d = escape_markdown(metrics.get('home_danger'))
    away_d = escape_markdown(metrics.get('away_danger'))

    pressure_note = "Pressão detectada" if metrics.get('pressure') else "Pressão fraca"
    pressure_note = escape_markdown(pressure_note)
    stadium_small = "✅" if metrics.get('small_stadium') else "❌"
    strategy_title_md = escape_markdown(strategy_title)

    # Linhas Poisson (escapar números com ponto)
    lines_txt = []
    for ln in best_lines[:3]:
        line = f"{ln['line']:.1f}"
        pwin = f"{ln['p_win']*100:.0f}"
        ppush = f"{ln['p_push']*100:.0f}"
        lines_txt.append(f"Linha {escape_markdown(line)} → Win {escape_markdown(pwin)}% \\| Push {escape_markdown(ppush)}%")

    bet_link = build_bet365_link(fixture)  # NÃO escapar URL

    parts = [
        f"📣 {strategy_title_md}",
        f"🏟 Jogo: {home} x {away}",
        f"⏱ Minuto: {minute}  \\|  ⚽ Placar: {score}",
        f"⛳ Cantos: {total_corners} \\(H:{home_c} \\- A:{away_c}\\)",
        f"⚡ Ataques: H:{home_att}  A:{away_att}",
        f"🔥 Ataques perigosos: H:{home_d}  A:{away_d}",
        f"🏟 Estádio pequeno: {stadium_small}  \\|  {pressure_note}",
        "",
        "Top linhas sugeridas \\(Poisson\\):",
        *lines_txt,
        "",
        # Em MarkdownV2 o link cru funciona bem, sem escapar:
        f"🔗 Bet365: {bet_link}",
    ]
    return "\n".join(parts)

# ====================== API HELPERS / RATE =====================

def api_get(endpoint: str, params: Optional[dict] = None, timeout: int = 20) -> Optional[dict]:
    """
    Wrapper com logging, contagem de requests e captura de headers de rate limit.
    Em 429 (Too Many Requests), aplica backoff e retorna None.
    """
    global request_count, last_rate_headers
    url = f"{API_BASE}/{endpoint}"
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
        request_count += 1

        # Captura de headers de rate (se fornecidos)
        last_rate_headers = {
            'x-ratelimit-requests-remaining': r.headers.get('x-ratelimit-requests-remaining'),
            'x-ratelimit-requests-limit': r.headers.get('x-ratelimit-requests-limit'),
            'x-ratelimit-minutely-remaining': r.headers.get('x-ratelimit-minutely-remaining'),
            'x-ratelimit-minutely-limit': r.headers.get('x-ratelimit-minutely-limit'),
        }

        if r.status_code == 429:
            logger.warning("⚠️ 429 Too Many Requests em %s (headers=%s) — backoff 30s", endpoint, last_rate_headers)
            time.sleep(30)
            return None

        if r.status_code != 200:
            logger.warning("Erro API %s: %s %s", endpoint, r.status_code, r.text[:400])
            return None

        data = r.json()
        if isinstance(data, dict) and data.get("errors"):
            logger.error("⚠️ Erro retornado pela API em %s: %s", endpoint, data["errors"])
        return data
    except Exception as e:
        logger.exception("❌ Erro ao conectar à API-Football (%s): %s", endpoint, e)
        return None

from time import time, sleep

LAST_REQUEST = 0
MIN_INTERVAL = 0.8  # segundos entre chamadas (~75 por minuto)

def safe_request(url, headers, params=None):
    """
    Gerencia a taxa de requisições automaticamente.
    Garante que a API não seja chamada mais rápido que o permitido.
    """
    global LAST_REQUEST
    now = time()
    elapsed = now - LAST_REQUEST
    if elapsed < MIN_INTERVAL:
        sleep(MIN_INTERVAL - elapsed)
    LAST_REQUEST = time()

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        return response
    except Exception as e:
        logger.error(f"❌ Erro na requisição segura: {e}")
        return None

def get_fixture_statistics(fixture_id):
    try:
        url = f"{API_BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        resp = safe_request(url, headers=HEADERS, params=params)
        if not resp or resp.status_code != 200:
            logger.warning("⚠️ Erro na API em fixtures/statistics: %s", resp.text if resp else "sem resposta")
            return None
        data = resp.json()
        return data.get("response", [])
    except Exception as e:
        logger.exception("Erro em get_fixture_statistics: %s", e)
        return None

# ====================== EXTRAÇÃO / PRESSÃO =====================

def extract_basic_stats(fixture: Dict[str, Any], stats_resp: List[Dict[str, Any]]):
    """
    Extrai corners/attacks/danger por time, usando IDs corretos (robusto).
    """
    teams = fixture.get('teams', {})
    home_id = teams.get('home', {}).get('id')
    away_id = teams.get('away', {}).get('id')
    home = {'corners': 0, 'attacks': 0, 'danger': 0}
    away = {'corners': 0, 'attacks': 0, 'danger': 0}

    for entry in stats_resp:
        team = entry.get('team', {}) or {}
        stats_list = entry.get('statistics', []) or []
        target = None
        if team.get('id') == home_id:
            target = home
        elif team.get('id') == away_id:
            target = away
        else:
            continue
        for s in stats_list:
            t = str(s.get('type', '')).lower()
            val = s.get('value')
            try:
                v = int(float(str(val).replace('%', ''))) if val is not None else 0
            except Exception:
                v = 0
            if 'corner' in t:
                target['corners'] = v
            elif 'attack' in t and 'danger' not in t:
                target['attacks'] = v
            elif 'on goal' in t or 'danger' in t or 'shots on goal' in t:
                target['danger'] = v

    return home, away

def pressure_score(home: Dict[str, int], away: Dict[str, int]):
    h_att, a_att = home['attacks'], away['attacks']
    h_d, a_d = home['danger'], away['danger']
    if (h_att + a_att) < ATTACKS_MIN or (h_d + a_d) < DANGER_MIN:
        return 0.0, 0.0
    score_home = (
        0.35 * min(1, max(0, (h_att - a_att) / ATTACKS_DIFF)) +
        0.55 * min(1, max(0, (h_d - a_d) / DANGER_DIFF)) +
        0.10 * min(1, (h_att + h_d) / 20)
    )
    score_away = (
        0.35 * min(1, max(0, (a_att - h_att) / ATTACKS_DIFF)) +
        0.55 * min(1, max(0, (a_d - h_d) / DANGER_DIFF)) +
        0.10 * min(1, (a_att + a_d) / 20)
    )
    return score_home, score_away

# ======================= ESTRATÉGIAS VIP =======================

def verificar_estrategias(fixture: Dict[str, Any], metrics: Dict[str, Any]) -> List[str]:
    """
    Retorna uma lista de títulos de estratégias que bateram (cada uma é independente).
    """
    sinais = []
    minuto = metrics['minute']
    total_cantos = metrics['total_corners']
    home_gols = fixture.get('goals', {}).get('home', 0) or 0
    away_gols = fixture.get('goals', {}).get('away', 0) or 0

    # 1) HT - Casa Empatando
    if 30 <= minuto <= 40 and metrics['pressure'] and home_gols == away_gols:
        sinais.append("Estratégia HT - Casa Empatando")

    # 2) FT - Reação da Casa (perdendo com pressão)
    if 75 <= minuto <= 88 and metrics['pressure'] and home_gols < away_gols:
        sinais.append("Estratégia FT - Reação da Casa")

    # 3) FT - Over Cantos 2º Tempo (pressão + cantos relativamente baixos)
    if 70 <= minuto <= 90 and metrics['pressure'] and total_cantos <= 8:
        sinais.append("Estratégia FT - Over Cantos 2º Tempo")

    # 4) Campo Pequeno + Pressão (25'–90')
    if metrics['small_stadium'] and metrics['pressure'] and 25 <= minuto <= 90:
        sinais.append("Estratégia Campo Pequeno + Pressão")

    # 5) Jogo Aberto (Ambos pressionam) a partir de 30'
    if metrics['home_attacks'] >= 8 and metrics['away_attacks'] >= 8 and minuto >= 30:
        sinais.append("Estratégia Jogo Aberto (Ambos pressionam)")

    return sinais

# ========================= ANTI-SPAM ==========================

def should_notify(fixture_id: int, signal_key: str) -> bool:
    now = time.time()
    last = sent_signals[fixture_id].get(signal_key, 0)
    if now - last >= RENOTIFY_MINUTES * 60:
        sent_signals[fixture_id][signal_key] = now
        return True
    return False

# ========================= MAIN LOOP ==========================
def main_loop():
    logger.info("🔁 Loop econômico iniciado. Base: %ss (renotify=%s min).", SCAN_INTERVAL_BASE, RENOTIFY_MINUTES)
    logger.info("🟢 Loop econômico ativo: aguardando jogos ao vivo...")

    while True:
        try:
            fixtures = get_live_fixtures()
            total = len(fixtures)

            if total == 0:
                logger.debug("Sem partidas ao vivo no momento. (req: %s, rate: %s)", request_count, last_rate_headers)
                time.sleep(SCAN_INTERVAL_BASE)
                continue

            # Intervalo dinâmico conforme nº de jogos (modo econômico)
            scan_interval = SCAN_INTERVAL_BASE if total < 20 else SCAN_INTERVAL_BASE + 60
            logger.debug("🎯 %d jogos ao vivo | intervalo=%ds | req=%s | rate=%s",
                         total, scan_interval, request_count, last_rate_headers)

            for fixture in fixtures:
                fixture_id = fixture.get('fixture', {}).get('id')
                if not fixture_id:
                    continue

                minute = fixture.get('fixture', {}).get('status', {}).get('elapsed', 0) or 0

                # ECONOMIA: ignorar jogos antes de 25' (nossas estratégias não disparam antes disso)
                if minute < 25:
                    logger.debug("⏳ Ignorado fixture=%s (min %s < 25')", fixture_id, minute)
                    continue

                # Buscar estatísticas somente após 25'
                stats_resp = get_fixture_statistics(fixture_id)
                if not stats_resp:
                    logger.debug("Sem estatísticas para fixture=%s no momento.", fixture_id)
                    continue

                # Extrair dados corretos por ID de time
                home, away = extract_basic_stats(fixture, stats_resp)

                # Cálculo de pressão
                score_home, score_away = pressure_score(home, away)

                total_corners = (home['corners'] or 0) + (away['corners'] or 0)
                metrics = {
                    'minute': minute,
                    'home_corners': home['corners'],
                    'away_corners': away['corners'],
                    'home_attacks': home['attacks'],
                    'away_attacks': away['attacks'],
                    'home_danger': home['danger'],
                    'away_danger': away['danger'],
                    'pressure': (score_home > MIN_PRESSURE_SCORE) or (score_away > MIN_PRESSURE_SCORE),
                    'small_stadium': (fixture.get('fixture', {}).get('venue', {}).get('name', '').lower() in SMALL_STADIUMS),
                    'total_corners': total_corners
                }

                # (Apenas para log)
                if HT_WINDOW[0] <= minute <= HT_WINDOW[1]:
                    janela = 'HT'
                elif FT_WINDOW[0] <= minute <= FT_WINDOW[1]:
                    janela = 'FT'
                else:
                    janela = 'LIVE'

                # Poisson (lam simples; calibrar depois se desejar com histórico)
                best_lines = evaluate_candidate_lines(total_corners, lam=1.5)

                # Checar MULTI-ESTRATÉGIAS independentes
                estrategias = verificar_estrategias(fixture, metrics)
                if not estrategias:
                    logger.debug("Sem critério: janela=%s pressão=%s fixture=%s", janela, metrics['pressure'], fixture_id)
                else:
                    for strat_title in estrategias:
                        # chave anti-flood: estratégia + total cantos
                        signal_key = f"{strat_title}_{total_corners}"
                        if should_notify(fixture_id, signal_key):
                            msg = build_vip_message(fixture, strat_title, metrics, best_lines)
                            send_telegram_message(msg)
                            logger.info("📤 Sinal enviado [%s] fixture=%s minuto=%s", strat_title, fixture_id, minute)

                            # Contador de sinais enviados
                            signals_sent = signals_sent + 1 if 'signals_sent' in locals() else 1

        # ======= RESUMO DA VARREDURA =======
            try:
                logger.info(
                    "📊 Resumo da varredura: %d jogos analisados | %d sinais enviados | próxima varredura em %ds",
                    total,
                    signals_sent if 'signals_sent' in locals() else 0,
                    scan_interval
                )
                signals_sent = 0  # reinicia contador

                # Atualiza os dados do painel VIP (/status)
                atualizar_metricas(total, last_rate_headers)

            except Exception as e:
                logger.exception("Erro ao finalizar resumo da varredura: %s", e)

            # pausa até a próxima varredura
            time.sleep(scan_interval)

        except Exception as e:
            logger.exception("Erro no loop principal: %s", e)
            time.sleep(SCAN_INTERVAL_BASE)

# ========================= STATUS COMMAND (VIP) ==========================
from datetime import datetime

# Marca o tempo inicial do bot (uptime)
START_TIME = datetime.now()

# Variáveis globais auxiliares
LAST_SCAN_TIME = None
LAST_API_STATUS = "⏳ Aguardando..."
LAST_RATE_USAGE = "0%"
TOTAL_VARRIDURAS = 0

def get_status_message():
    """Gera mensagem de status com dados VIP"""
    try:
        uptime = datetime.now() - START_TIME
        horas, resto = divmod(uptime.seconds, 3600)
        minutos, _ = divmod(resto, 60)

        # Recupera métricas globais do loop
        jogos = globals().get("total", 0)
        sinais = globals().get("signals_sent", 0)
        api_status = globals().get("LAST_API_STATUS", "✅ OK")
        rate_usage = globals().get("LAST_RATE_USAGE", "0%")
        varridas = globals().get("TOTAL_VARRIDURAS", 0)

        last_scan = globals().get("LAST_SCAN_TIME")
        if last_scan:
            last_scan = last_scan.strftime("%H:%M:%S")
        else:
            last_scan = "Ainda não realizada"

        msg = (
            "📊 Status Bot Escanteios RP VIP Plus\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 Tempo online: {horas}h {minutos}min\n"
            f"⚽ Jogos varridos: {jogos}\n"
            f"🚩 Sinais enviados: {sinais}\n"
            f"🔁 Varreduras realizadas: {varridas}\n"
            f"⏱️ Última varredura: {last_scan}\n"
            f"🧮 Próxima varredura: {SCAN_INTERVAL_BASE}s\n"
            f"🌐 Status API: {api_status}\n"
            f"📉 Uso da API: {rate_usage}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "🤖 Versão Multi v2 Econômico"
        )
        return msg

    except Exception as e:
        return f"❌ Erro ao gerar status: {e}"

# ================================================================
# 📡 Atualiza métricas globais durante o loop principal

def atualizar_metricas(loop_total, req_headers):
    """Atualiza dados para o comando /status"""
    global LAST_SCAN_TIME, LAST_API_STATUS, LAST_RATE_USAGE, TOTAL_VARRIDURAS

    LAST_SCAN_TIME = datetime.now()
    TOTAL_VARRIDURAS += 1

    # Status da API
    if "X-RateLimit-Remaining" in req_headers:
        restante = int(req_headers.get("X-RateLimit-Remaining", 0))
        limite = int(req_headers.get("X-RateLimit-Limit", 1))
        uso = 100 - int((restante / limite) * 100)
        LAST_RATE_USAGE = f"{uso}% usado"
        LAST_API_STATUS = "✅ OK" if uso < 90 else "⚠️ Alto consumo"
    else:
        LAST_API_STATUS = "❌ Sem cabeçalhos (erro API)"
        LAST_RATE_USAGE = "Indefinido"

# ================================================================
# 🔗 Handler /status — via webhook (Flask)

from flask import request, jsonify

@app.route(f"/{TOKEN}/status", methods=["POST"])
def telegram_status_webhook():
    data = request.get_json()
    message = data.get("message", {})
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")

    if text.strip().lower() == "/status":
        status_msg = get_status_message()
        send_telegram_message(status_msg)
    return jsonify({"ok": True})
# =================================================================

# =========================== START ============================

if __name__ == "__main__":
    logger.info("🚀 Iniciando Bot Escanteios RP VIP Plus — Multi v2 (Econômico)")
    try:
        boot_msg = "🤖 Bot Escanteios RP VIP Plus — Multi v2 \\(Econômico\\) ativo\\. Ignorando jogos < 25' e otimizando consumo\\."
        send_telegram_message(boot_msg)
    except Exception:
        pass
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)

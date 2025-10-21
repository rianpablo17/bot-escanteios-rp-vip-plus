#!/usr/bin/env python3
# -- coding: utf-8 --
"""
bot_escanteios_rp_vip_plus_multi_v1.py
RP VIP Plus — Central de Estratégias Múltiplas (HT/FT, Over 2ºT, Campo Pequeno, Jogo Aberto),
varredura ao vivo e envio de sinais no Telegram. Compatível com Render (Flask + thread em background).

Requisitos de ENV (Render → Environment):
- API_FOOTBALL_KEY : sua chave oficial da dashboard.api-football.com
- TOKEN            : token do BotFather
- TELEGRAM_CHAT_ID : id do grupo/canal (ex.: -1001234567890)
- SCAN_INTERVAL    : opcional (padrão 300s)
- RENOTIFY_MINUTES : opcional (padrão 10 min)
"""

import os
import time
import math
import logging
import threading
import urllib.parse
from collections import defaultdict
from typing import Dict, Any, List

import requests
from flask import Flask, request, jsonify

# ---------- LOG / ENV ----------
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot_escanteios_rp_vip_plus_multi')

API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY')
TOKEN = os.getenv('TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SCAN_INTERVAL = int(os.getenv('SCAN_INTERVAL', '120'))      # segundos entre varreduras
RENOTIFY_MINUTES = int(os.getenv('RENOTIFY_MINUTES', '10')) # evitar repetição de sinais

if not API_FOOTBALL_KEY:
    raise ValueError("⚠️ API_FOOTBALL_KEY não definida no Environment do Render.")
if not TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("⚠️ Defina TOKEN e TELEGRAM_CHAT_ID no Environment do Render.")

# ---------- API FOOTBALL (OFICIAL) ----------
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ---------- ESTRATÉGIAS / PARÂMETROS ----------
HT_WINDOW = (30, 40)   # janela alvo HT
FT_WINDOW = (80, 90)   # janela alvo FT

MIN_PRESSURE_SCORE = 0.5
ATTACKS_MIN = 5         # volume mínimo somado para liberar cálculo de pressão
ATTACKS_DIFF = 4        # diferença de ataques para pontuar
DANGER_MIN = 5          # volume mínimo de ataques perigosos
DANGER_DIFF = 3         # diferença de ataques perigosos para pontuar

# Estádios (exemplos) com dimensão/ambiente que favorece cantos (lista pode ser expandida)
SMALL_STADIUMS = {
    'loftus road','vitality stadium','kenilworth road','turf moor',
    'bramall lane','ewood park','the den','carrow road',
    'bet365 stadium','pride park','liberty stadium','fratton park',
}

# memória anti-spam: {fixture_id: {signal_key: last_ts}}
sent_signals = defaultdict(dict)

# ---------- FLASK (healthcheck Render) ----------
app = Flask(__name__)

@app.route('/', methods=['GET'])
def root():
    return jsonify({'status': 'ok', 'service': 'Bot Escanteios RP VIP Plus — Multi v1'}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

# Webhook opcional do Telegram (não necessário para envio de sinais)
@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    logger.debug("Update Telegram (webhook): %s", str(data)[:500])
    return jsonify({"status": "ok"}), 200

# ---------- POISSON HELPERS ----------
def poisson_pmf(k, lam):
    try:
        return (lam**k) * math.exp(-lam) / math.factorial(k) if k >= 0 else 0.0
    except Exception:
        return 0.0

def poisson_cdf_le(k, lam):
    return sum(poisson_pmf(i, lam) for i in range(0, int(k) + 1))

def poisson_tail_ge(k, lam):
    return 1.0 if k <= 0 else 1.0 - poisson_cdf_le(k - 1, lam)

# ---------- API HELPERS ----------
def api_get(endpoint, params=None, timeout=20):
    url = f"{API_BASE}/{endpoint}"
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
        if r.status_code != 200:
            logger.warning("Erro API-Football %s: %s %s", endpoint, r.status_code, r.text[:300])
            return None
        data = r.json()
        if isinstance(data, dict) and data.get("errors"):
            logger.error("⚠️ Erro retornado pela API em %s: %s", endpoint, data["errors"])
        return data
    except Exception as e:
        logger.exception("❌ Erro ao conectar à API-Football (%s): %s", endpoint, e)
        return None

def get_live_fixtures() -> List[Dict[str, Any]]:
    data = api_get("fixtures", params={"live": "all"})
    fixtures = (data or {}).get('response', []) or []
    logger.debug("Fixtures ao vivo: %d", len(fixtures))
    return fixtures

def get_fixture_statistics(fixture_id: int) -> List[Dict[str, Any]]:
    data = api_get("fixtures/statistics", params={"fixture": fixture_id})
    return (data or {}).get('response', []) or []

# ---------- STATS EXTRACTION ----------
def extract_basic_stats(fixture, stats_resp):
    teams = fixture.get('teams', {})
    home_id = teams.get('home', {}).get('id')
    away_id = teams.get('away', {}).get('id')
    home = {'corners': 0, 'attacks': 0, 'danger': 0}
    away = {'corners': 0, 'attacks': 0, 'danger': 0}

    for entry in stats_resp:
        team = entry.get('team', {}) or {}
        stats_list = entry.get('statistics', []) or []
        if not team:
            continue
        target = home if team.get('id') == home_id else away
        for s in stats_list:
            t = str(s.get('type', '')).lower()
            val = s.get('value')
            try:
                # valores podem vir como "12" ou "12%" ou None
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

# ---------- PRESSURE SCORE ----------
def pressure_score(home, away):
    h_att, a_att = home['attacks'], away['attacks']
    h_d, a_d = home['danger'], away['danger']

    # requisito mínimo de volume
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

# ---------- LINES (Poisson) ----------
def predict_corners_and_line_metrics(current_total, lam_remaining, candidate_line):
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
    return {'line': candidate_line, 'p_win': p_win, 'p_push': p_push, 'p_lose': p_lose}

def evaluate_candidate_lines(current_total, lam, lines_to_check=None):
    lines_to_check = lines_to_check or [3.5, 4.0, 4.5, 5.0, 5.5]
    results = [predict_corners_and_line_metrics(current_total, lam, L) for L in lines_to_check]
    results.sort(key=lambda x: x['p_win'], reverse=True)
    return results

# ---------- LINKS (Bet365 via busca otimizada) ----------
def build_bet365_link(fixture: Dict[str, Any]) -> str:
    """
    A Bet365 não fornece URL pública por fixture id. Geramos um link de busca
    que abre o resultado do jogo direto no site da Bet365.
    """
    home = fixture.get('teams', {}).get('home', {}).get('name', '') or ''
    away = fixture.get('teams', {}).get('away', {}).get('name', '') or ''
    league = fixture.get('league', {}).get('name', '') or ''
    query = f"site:bet365.com {home} x {away} {league}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

# ---------- TELEGRAM ----------
def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.warning("Erro Telegram %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        logger.exception("Erro ao enviar Telegram: %s", e)

def build_vip_message(fixture, strategy_title: str, metrics: Dict[str, Any], best_lines: List[Dict[str, float]]):
    teams = fixture.get('teams', {})
    home = teams.get('home', {}).get('name', '?')
    away = teams.get('away', {}).get('name', '?')
    minute = metrics.get('minute', 0) or 0
    goals = fixture.get('goals', {})
    score = f"{goals.get('home','-')} x {goals.get('away','-')}"
    lines_txt = [f"Linha {ln['line']} → Win {ln['p_win']*100:.0f}% | Push {ln['p_push']*100:.0f}%" for ln in best_lines[:3]]
    pressure_note = 'Pressão detectada' if metrics.get('pressure') else 'Pressão fraca'
    stadium_small = '✅' if metrics.get('small_stadium') else '❌'
    bet_link = build_bet365_link(fixture)

    txt = [
        f"📣 <b>{strategy_title}</b>",
        f"🏟 <b>Jogo:</b> {home} x {away}",
        f"⏱ <b>Minuto:</b> {minute}  |  ⚽ <b>Placar:</b> {score}",
        f"⛳ <b>Cantos:</b> {metrics.get('total_corners')} (H:{metrics.get('home_corners')} - A:{metrics.get('away_corners')})",
        f"⚡ <b>Ataques:</b> H:{metrics.get('home_attacks')}  A:{metrics.get('away_attacks')}",
        f"🔥 <b>Ataques perigosos:</b> H:{metrics.get('home_danger')}  A:{metrics.get('away_danger')}",
        f"🏟 <b>Estádio pequeno:</b> {stadium_small}  |  {pressure_note}",
        "",
        "<b>Top linhas sugeridas (Win/Push):</b>",
        *lines_txt,
        "",
        f"🔗 <b>Bet365:</b> {bet_link}"
    ]
    return "\n".join(txt)

# ---------- ANTI-SPAM ----------
def should_notify(fixture_id: int, signal_key: str) -> bool:
    now = time.time()
    last = sent_signals[fixture_id].get(signal_key, 0)
    if now - last >= RENOTIFY_MINUTES * 60:
        sent_signals[fixture_id][signal_key] = now
        return True
    return False

# ---------- CENTRAL DE ESTRATÉGIAS ----------
def verificar_estrategias(fixture: Dict[str, Any], metrics: Dict[str, Any]) -> List[str]:
    """
    Retorna lista de títulos de estratégias que bateram (cada uma é independente).
    """
    sinais = []
    minuto = metrics['minute']
    total_cantos = metrics['total_corners']
    home_gols = fixture.get('goals', {}).get('home', 0) or 0
    away_gols = fixture.get('goals', {}).get('away', 0) or 0

    # 1️⃣ Estratégia HT - Casa Empatando
    if 30 <= minuto <= 40 and metrics['pressure'] and home_gols == away_gols:
        sinais.append("Estratégia HT - Casa Empatando")

    # 2️⃣ Estratégia FT - Reação da Casa (time da casa perdendo com pressão)
    if 75 <= minuto <= 88 and metrics['pressure'] and home_gols < away_gols:
        sinais.append("Estratégia FT - Reação da Casa")

    # 3️⃣ Estratégia FT - Over Cantos 2º Tempo (jogo ainda com cantos baixos + pressão)
    if 70 <= minuto <= 90 and metrics['pressure'] and total_cantos <= 8:
        sinais.append("Estratégia FT - Over Cantos 2º Tempo")

    # 4️⃣ Estratégia Campo Pequeno + Pressão (do 10' até o fim)
    if metrics['small_stadium'] and metrics['pressure'] and 10 <= minuto <= 90:
        sinais.append("Estratégia Campo Pequeno + Pressão")

    # 5️⃣ Estratégia Jogo Aberto (ambos pressionam) a partir de 30'
    if metrics['home_attacks'] >= 8 and metrics['away_attacks'] >= 8 and minuto >= 30:
        sinais.append("Estratégia Jogo Aberto (Ambos pressionam)")

    return sinais

# ---------- LOOP PRINCIPAL ----------
def main_loop():
    logger.info("🔁 Loop de varredura iniciado (intervalo: %ss).", SCAN_INTERVAL)
    while True:
        try:
            fixtures = get_live_fixtures()
            if not fixtures:
                logger.debug("Sem partidas ao vivo no momento.")
                time.sleep(SCAN_INTERVAL)
                continue

            for fixture in fixtures:
                fixture_id = fixture.get('fixture', {}).get('id')
                if not fixture_id:
                    continue

                minute = fixture.get('fixture', {}).get('status', {}).get('elapsed', 0) or 0
                stats_resp = get_fixture_statistics(fixture_id)
                if not stats_resp:
                    logger.debug("Sem estatísticas disponíveis (results=0) para fixture=%s.", fixture_id)

                home, away = extract_basic_stats(fixture, stats_resp)

                # cálculo de pressão
                score_home, score_away = pressure_score(home, away)
                total_corners = (home['corners'] or 0) + (away['corners'] or 0)

                # métricas consolidadas
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

                # janela auxiliar (só para log)
                if HT_WINDOW[0] <= minute <= HT_WINDOW[1]:
                    janela = 'HT'
                elif FT_WINDOW[0] <= minute <= FT_WINDOW[1]:
                    janela = 'FT'
                else:
                    janela = 'LIVE'

                # linhas Poisson (lam suposição simples; pode calibrar futuramente por histórico)
                best_lines = evaluate_candidate_lines(total_corners, lam=1.5)

                # checa todas as estratégias independentes
                estrategias = verificar_estrategias(fixture, metrics)
                if not estrategias:
                    logger.debug("Sem critério para sinal: janela=%s pressão=%s fixture=%s", janela, metrics['pressure'], fixture_id)
                else:
                    for strat_title in estrategias:
                        # chave anti-spam por estratégia + total de cantos (evitar flood)
                        signal_key = f"{strat_title}_{total_corners}"
                        if should_notify(fixture_id, signal_key):
                            msg = build_vip_message(fixture, strat_title, metrics, best_lines)
                            send_telegram_message(msg)
                            logger.info("Sinal enviado [%s] fixture=%s minuto=%s", strat_title, fixture_id, minute)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.exception("Erro no loop principal: %s", e)
            time.sleep(SCAN_INTERVAL)

# ---------- START ----------
if __name__ == "__main__":
    logger.info("🚀 Iniciando Bot Escanteios RP VIP Plus — Multi Estratégias v1...")
    # mensagem de boot (uma vez)
    try:
        send_telegram_message("🤖 Bot Escanteios RP VIP Plus (Multi Estratégias v1) iniciado. Varrendo jogos ao vivo...")
    except Exception:
        pass
    # thread da varredura
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    # servidor web p/ Render (mantém serviço vivo)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
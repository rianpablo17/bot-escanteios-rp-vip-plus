#!/usr/bin/env python3
# -- coding: utf-8 --
"""
bot_escanteios_rp_vip_plus_final_v3.py
RP VIP Plus — Estratégias HT/FT de escanteios, varredura ao vivo e envio de sinais no Telegram.
Compatível com Render (Flask + thread em background).
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
logger = logging.getLogger('bot_escanteios_rp-vip_plus')

API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY')
TOKEN = os.getenv('TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
SCAN_INTERVAL = int(os.getenv('SCAN_INTERVAL', '120'))  # seg entre varreduras
RENOTIFY_MINUTES = int(os.getenv('RENOTIFY_MINUTES', '10'))

if not API_FOOTBALL_KEY:
    raise ValueError("⚠️ API_FOOTBALL_KEY não definida no Environment do Render.")
if not TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("⚠️ Defina TOKEN e TELEGRAM_CHAT_ID no Environment do Render.")

# ---------- API FOOTBALL ----------
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ---------- ESTRATÉGIAS (mantidas do seu script) ----------
HT_WINDOW = (35, 40)   # minuto corrido do jogo (0-90)
FT_WINDOW = (80, 90)

MIN_PRESSURE_SCORE = 0.5
ATTACKS_MIN = 5
ATTACKS_DIFF = 4
DANGER_MIN = 5
DANGER_DIFF = 3
SMALL_STADIUMS = [
    'loftus road','vitality stadium','kenilworth road','turf moor',
    'crowd','bramall lane','ewood park'
]

# memória de envio para evitar spam
sent_signals = defaultdict(dict)  # {fixture_id: {signal_key: last_ts}}

# ---------- FLASK (saúde Render) ----------
app = Flask(__name__)

@app.route('/', methods=['GET'])
def root():
    return jsonify({'status': 'ok', 'service': 'Bot Escanteios RP VIP Plus'}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

# Webhook opcional do Telegram (mantido, mas não obrigatório para envio)
@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    logger.debug("Update Telegram (webhook): %s", str(data)[:500])
    return jsonify({"status": "ok"}), 200

# ---------- POISSON HELPERS (mantidos) ----------
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
def get_live_fixtures() -> List[Dict[str, Any]]:
    url = f"{API_BASE}/fixtures?live=all"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            logger.warning('Erro API-Football /fixtures: %s %s', r.status_code, r.text[:300])
            return []
        data = r.json()
        fixtures = data.get('response', []) or []
        logger.debug("Fixtures ao vivo: %d", len(fixtures))
        return fixtures
    except Exception as e:
        logger.exception('Erro ao buscar fixtures: %s', e)
        return []

def get_fixture_statistics(fixture_id: int) -> List[Dict[str, Any]]:
    url = f"{API_BASE}/fixtures/statistics?fixture={fixture_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            logger.warning('Erro API-Football /statistics: %s %s', r.status_code, r.text[:300])
            return []
        return r.json().get('response', []) or []
    except Exception as e:
        logger.exception('Erro ao buscar statistics: %s', e)
        return []

# ---------- STATS EXTRACTION (mantido, com defensivas) ----------
def extract_basic_stats(fixture, stats_resp):
    teams = fixture.get('teams', {})
    home_id = teams.get('home', {}).get('id')
    away_id = teams.get('away', {}).get('id')
    home = {'corners': 0, 'attacks': 0, 'danger': 0}
    away = {'corners': 0, 'attacks': 0, 'danger': 0}

    for entry in stats_resp:
        team = entry.get('team', {}) or {}
        stats_list = entry.get('statistics', []) or []
        target = home if team.get('id') == home_id else away
        for s in stats_list:
            t = str(s.get('type', '')).lower()
            val = s.get('value') or 0
            try:
                val = int(float(str(val).replace('%', '')))
            except Exception:
                val = 0
            if 'corner' in t:
                target['corners'] = val
            elif 'attack' in t and 'danger' not in t:
                target['attacks'] = val
            elif 'on goal' in t or 'danger' in t or 'shots on goal' in t:
                target['danger'] = val
    return home, away

# ---------- PRESSURE (mantido) ----------
def pressure_score(home, away):
    h_att, a_att = home['attacks'], away['attacks']
    h_d, a_d = home['danger'], away['danger']

    # requisito mínimo de volume
    if (h_att + a_att) < ATTACKS_MIN or (h_d + a_d) < DANGER_MIN:
        return 0.0, 0.0

    # score de pressão simples ponderado
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

# ---------- PREDICTION (mantido) ----------
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

# ---------- LINKS (Bet365 por busca) ----------
def build_bet365_link(fixture: Dict[str, Any]) -> str:
    """
    A Bet365 não fornece URL pública por fixture id. Geramos um link de busca
    que abre a página de resultados da Bet365 para o confronto/competição.
    """
    home = fixture.get('teams', {}).get('home', {}).get('name', '') or ''
    away = fixture.get('teams', {}).get('away', {}).get('name', '') or ''
    league = fixture.get('league', {}).get('name', '') or ''
    query = f"site:bet365.com {home} x {away} {league}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

# ---------- TELEGRAM ----------
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.warning("Erro Telegram %s: %s", r.status_code, r.text[:300])
    except Exception as e:
        logger.exception("Erro ao enviar Telegram: %s", e)

def build_vip_message(fixture, window_key, metrics, best_lines):
    teams = fixture.get('teams', {})
    home = teams.get('home', {}).get('name', '?')
    away = teams.get('away', {}).get('name', '?')
    minute = fixture.get('fixture', {}).get('status', {}).get('elapsed', 0) or 0
    goals = fixture.get('goals', {})
    score = f"{goals.get('home','-')} x {goals.get('away','-')}"
    lines_txt = [f"Linha {ln['line']} → Win {ln['p_win']*100:.0f}% | Push {ln['p_push']*100:.0f}%" for ln in best_lines[:3]]
    pressure_note = 'Pressão detectada' if metrics.get('pressure') else 'Pressão fraca'
    stadium_small = '✅' if metrics.get('small_stadium') else '❌'
    bet_link = build_bet365_link(fixture)

    txt = [
        f"📣 <b>SINAL VIP PLUS {window_key}</b>",
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
                home, away = extract_basic_stats(fixture, stats_resp)

                # métricas
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

                # linha Poisson (lam suposição simples — você pode calibrar)
                best_lines = evaluate_candidate_lines(total_corners, lam=1.5)

                # janela (mantida)
                if HT_WINDOW[0] <= minute <= HT_WINDOW[1]:
                    window_key = 'HT'
                elif FT_WINDOW[0] <= minute <= FT_WINDOW[1]:
                    window_key = 'FT'
                else:
                    window_key = 'LIVE'

                # chave de sinal (inclui janela e total para evitar repetição desnecessária)
                signal_key = f"{window_key}_{total_corners}"

                # regra simples: só notifica se houver “pressão” na janela alvo
                if window_key in ('HT', 'FT') and metrics['pressure']:
                    if should_notify(fixture_id, signal_key):
                        msg = build_vip_message(fixture, window_key, metrics, best_lines)
                        send_telegram_message(msg)
                        logger.info("Sinal enviado [%s] fixture=%s minuto=%s", signal_key, fixture_id, minute)
                else:
                    logger.debug("Sem critério para sinal: janela=%s pressão=%s", window_key, metrics['pressure'])

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            logger.exception("Erro no loop principal: %s", e)
            time.sleep(SCAN_INTERVAL)

# ---------- START ----------
if __name__ == "__main__":
    logger.info("🚀 Iniciando Bot Escanteios RP VIP Plus...")
    # mensagem de boot (uma vez)
    try:
        send_telegram_message("🤖 Bot Escanteios RP VIP Plus iniciado. Varrendo jogos ao vivo...")
    except Exception:
        pass
    # thread da varredura
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    # servidor web p/ Render
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
#!/usr/bin/env python3
# -- coding: utf-8 --
"""
Bot Escanteios RP VIP Plus — Multi v2 (Econômico) • ULTRA Sensível v3 (Premium)
- Envia no máximo 1 sinal por período (HT e FT) = 2 por jogo
- Minuto suavizado (não retrocede, nem salta muito)
- Backoff quando a API não retorna estatísticas (economia de cota)
- Mantém TODAS as estratégias e layout VIP sem Poisson
- /status e /debug via webhook do Telegram

ENV:
- API_FOOTBALL_KEY, TOKEN, TELEGRAM_CHAT_ID, (opcional) TELEGRAM_ADMIN_ID
- SCAN_INTERVAL (default 300), RENOTIFY_MINUTES (default 3)
"""

import os
import re
import time
import math
import logging
import threading
import urllib.parse
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
import pytz

import requests
from flask import Flask, request, jsonify

# ========================= LOG / ENV =========================
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot_escanteios_rp_vip_multi_v2_economico')

API_FOOTBALL_KEY   = os.getenv('API_FOOTBALL_KEY')
TOKEN              = os.getenv('TOKEN')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID')
TELEGRAM_ADMIN_ID  = os.getenv('TELEGRAM_ADMIN_ID')
SCAN_INTERVAL_BASE = int(os.getenv('SCAN_INTERVAL', '300'))  # ← 300s por padrão
RENOTIFY_MINUTES   = int(os.getenv('RENOTIFY_MINUTES', '3'))

if not API_FOOTBALL_KEY:
    raise ValueError("⚠️ API_FOOTBALL_KEY não definida.")
if not TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("⚠️ Defina TOKEN e TELEGRAM_CHAT_ID.")

# ===================== STATUS (antes das rotas) ==============
START_TIME = int(time.time())
LAST_SCAN_TIME: Optional[datetime] = None
LAST_API_STATUS = "⏳ Aguardando..."
LAST_RATE_USAGE = "0%"
TOTAL_VARRIDURAS = 0
total = 0  # jogos na última varredura

# ===================== API CONFIG ============================
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ===================== PARÂMETROS ============================
HT_WINDOW = (29.8, 42)   # Janela HT
FT_WINDOW = (69.8, 93)   # Janela FT

# Thresholds (mantidos)
MIN_PRESSURE_SCORE = 0.18
ATTACKS_MIN_SUM    = 10
DANGER_MIN_SUM     = 5
MIN_TOTAL_SHOTS    = 4

# Estádios "apertados"
SMALL_STADIUMS = {
    'loftus road','vitality stadium','kenilworth road','turf moor',
    'bramall lane','ewood park','the den','carrow road',
    'bet365 stadium','pride park','liberty stadium','fratton park',
}

# Anti-spam memória e controle de período (um por período)
sent_signals: Dict[int, Dict[str, float]] = defaultdict(dict)
sent_period: Dict[int, set] = defaultdict(set)        # {fixture_id: {"HT","FT"}}
last_elapsed_seen: Dict[int, float] = {}              # suavização do minuto
no_stats_backoff_until: Dict[int, float] = {}         # evitar pedir stats por Xs

# Diagnóstico
request_count = 0
last_rate_headers: Dict[str, str] = {}

# ====================== ESCAPE MARKDOWNV2 =====================
MDV2_SPECIALS = r'[_*\[\]()~`>#+\-=|{}.!]'

def escape_markdown(text: Any) -> str:
    s = str(text) if text is not None else ""
    return re.sub(MDV2_SPECIALS, lambda m: "\\" + m.group(0), s)

# ============================ FLASK ===========================
app = Flask(__name__)

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'status': 'ok',
        'service': 'Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA Sensível v3',
        'scan_interval_base': SCAN_INTERVAL_BASE,
        'renotify_minutes': RENOTIFY_MINUTES
    }), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

# ====================== TELEGRAM WEBHOOK ======================
@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        message = data.get('message') or data.get('edited_message') or {}
        text = (message.get('text') or '').strip().lower()
        chat_id = str(message.get('chat', {}).get('id', TELEGRAM_CHAT_ID))

        if text == '/status':
            uptime = int(time.time() - START_TIME)
            horas = uptime // 3600
            minutos = (uptime % 3600) // 60

            total_jogos = globals().get("total", 0)
            varreduras = globals().get("TOTAL_VARRIDURAS", 0)
            api_status = globals().get("LAST_API_STATUS", "✅ OK")
            uso_api = globals().get("LAST_RATE_USAGE", "Indefinido")
            last_scan_dt: Optional[datetime] = globals().get("LAST_SCAN_TIME")
            if last_scan_dt:
                tz = pytz.timezone("America/Sao_Paulo")
                last_scan_local = last_scan_dt.astimezone(tz) if last_scan_dt.tzinfo else tz.localize(last_scan_dt)
                last_scan_txt = last_scan_local.strftime("%H:%M:%S")
            else:
                last_scan_txt = "Ainda não realizada"

            resposta = (
                "📊 Status Bot Escanteios RP VIP Plus\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                f"🕒 Tempo online: {horas}h {minutos}min\n"
                f"⚽ Jogos varridos: {total_jogos}\n"
                f"🔁 Varreduras realizadas: {varreduras}\n"
                f"⏱️ Última varredura: {last_scan_txt}\n"
                f"🌐 Status API: {api_status}\n"
                f"📉 Uso da API: {uso_api}\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "🤖 Versão: Multi v2 Econômico ULTRA Sensível v3"
            )
            send_telegram_message_plain(resposta)

        elif text == '/debug':
            resposta = (
                "🧩 Modo Debug\n"
                f"📦 Requests enviados: {request_count}\n"
                f"⏱ Intervalo base: {SCAN_INTERVAL_BASE}s\n"
                f"📡 Headers API: {last_rate_headers}"
            )
            send_telegram_message_plain(resposta)

    except Exception as e:
        logger.exception("❌ Erro no processamento do webhook: %s", e)

    return jsonify({"ok": True}), 200

# ====================== TELEGRAM HELPERS ======================
def _tg_send(chat_id: str, text: str, parse_mode: Optional[str] = None, disable_web_page_preview: bool = True) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": disable_web_page_preview}
    if parse_mode in ("MarkdownV2", "HTML"):
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200 and parse_mode:
            fallback_payload = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": True}
            requests.post(url, json=fallback_payload, timeout=20)
    except Exception as e:
        logger.exception("Erro ao enviar mensagem para o Telegram: %s", e)

def send_telegram_message(text: str) -> None:
    _tg_send(TELEGRAM_CHAT_ID, text, parse_mode="MarkdownV2", disable_web_page_preview=True)

def send_telegram_message_plain(text: str) -> None:
    _tg_send(TELEGRAM_CHAT_ID, text, parse_mode=None, disable_web_page_preview=True)

def send_admin_message(text: str) -> None:
    if TELEGRAM_ADMIN_ID:
        _tg_send(TELEGRAM_ADMIN_ID, text, parse_mode="MarkdownV2", disable_web_page_preview=True)

# ===================== API CALLS =====================
def safe_request(url: str, headers: Dict[str, str], params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    global request_count, last_rate_headers
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        request_count += 1
        last_rate_headers = {
            'x-ratelimit-requests-remaining': response.headers.get('x-ratelimit-requests-remaining'),
            'x-ratelimit-requests-limit': response.headers.get('x-ratelimit-requests-limit'),
            'x-ratelimit-minutely-remaining': response.headers.get('x-ratelimit-minutely-remaining'),
            'x-ratelimit-minutely-limit': response.headers.get('x-ratelimit-minutely-limit'),
        }
        if response.status_code == 200:
            return response.json()
        logger.warning("⚠️ Erro API-Football %s: %s", response.status_code, response.text)
        return None
    except requests.exceptions.Timeout:
        logger.warning("⚠️ Timeout na requisição para %s", url)
        return None
    except Exception as e:
        logger.exception("Erro em safe_request: %s", e)
        return None

def get_live_fixtures() -> List[Dict[str, Any]]:
    try:
        url = f"{API_BASE}/fixtures"
        params = {"live": "all"}
        data = safe_request(url, headers=HEADERS, params=params)
        if not data:
            logger.warning("⚠️ Erro ao buscar fixtures ao vivo (sem resposta ou falha na API)")
            return []
        fixtures = data.get("response", [])
        logger.debug("📡 %d partidas ao vivo encontradas.", len(fixtures))
        return fixtures
    except Exception as e:
        logger.exception("Erro em get_live_fixtures: %s", e)
        return []

def get_fixture_statistics(fixture_id: int) -> Optional[List[Dict[str, Any]]]:
    try:
        # Backoff (economia): se falhou recentemente, aguarda
        now = time.time()
        if fixture_id in no_stats_backoff_until and now < no_stats_backoff_until[fixture_id]:
            return None

        url = f"{API_BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        data = safe_request(url, headers=HEADERS, params=params)

        if not data:
            # ativa backoff de 240s quando a API não retorna
            no_stats_backoff_until[fixture_id] = now + 240
            logger.warning("⚠️ Stats sem resposta. Backoff 240s para fixture=%s", fixture_id)
            return None

        stats = data.get("response", [])
        if not stats:
            no_stats_backoff_until[fixture_id] = now + 180
            logger.debug("Sem estatísticas para fixture=%s (backoff curto).", fixture_id)
            return None

        return stats
    except Exception as e:
        logger.exception("Erro em get_fixture_statistics: %s", e)
        return None

# ===================== EXTRACT STATS =====================
STAT_ALIASES = {
    'corners': ['corner', 'corners'],
    'attacks': ['attack', 'attacks'],
    'danger':  ['dangerous attack', 'dangerous attacks'],  # separado de shots
    'shots':   ['shot', 'shots', 'total shots', 'shots on target', 'shots on goal'],
    'pos':     ['possession', 'ball possession']
}

def extract_value(stat_type: str, stat_label: str, value) -> Optional[int]:
    stat_label = (stat_label or '').lower()
    for alias in STAT_ALIASES.get(stat_type, []):
        if alias in stat_label:
            try:
                return int(float(str(value).replace('%', '').strip()))
            except Exception:
                return 0
    return None

def extract_basic_stats(fixture: Dict[str, Any], stats_resp: List[Dict[str, Any]]):
    teams = fixture.get('teams', {})
    home_id = teams.get('home', {}).get('id')
    away_id = teams.get('away', {}).get('id')

    home = {'corners': 0, 'attacks': 0, 'danger': 0, 'shots': 0, 'pos': 50}
    away = {'corners': 0, 'attacks': 0, 'danger': 0, 'shots': 0, 'pos': 50}

    for entry in stats_resp or []:
        team = entry.get('team', {}) or {}
        stats_list = entry.get('statistics', []) or []
        team_id = team.get('id')

        if team_id == home_id:
            target = home
        elif team_id == away_id:
            target = away
        else:
            continue

        for s in stats_list:
            label = str(s.get('type', '')).lower()
            val = s.get('value')
            for key in STAT_ALIASES.keys():
                v = extract_value(key, label, val)
                if v is not None:
                    target[key] = v
                    break

    return home, away

# ===================== PRESSURE VIP =====================
def pressure_score_vip(home: Dict[str, int], away: Dict[str, int]) -> Tuple[float, float]:
    def norm(x, a):
        try:
            return max(0.0, min(1.0, x / float(a)))
        except Exception:
            return 0.0

    if (home['attacks'] + away['attacks']) < 1 or (home['danger'] + away['danger']) < 1:
        return 0.0, 0.0

    h = (0.25 * norm(home['attacks'] - away['attacks'], 10) +
         0.45 * norm(home['danger']  - away['danger'],  8) +
         0.20 * norm(home['shots']   - away['shots'],   4) +
         0.10 * norm(home['pos']     - away['pos'],    20))
    a = (0.25 * norm(away['attacks'] - home['attacks'], 10) +
         0.45 * norm(away['danger']  - home['danger'],  8) +
         0.20 * norm(away['shots']   - home['shots'],   4) +
         0.10 * norm(away['pos']     - home['pos'],    20))
    return h, a

# ======================= ESTRATÉGIAS VIP (mantidas) =======================
def verificar_estrategias_vip(fixture: Dict[str, Any], metrics: Dict[str, Any]):
    estrategias = []
    minuto = metrics['minute']
    total_cantos = metrics['total_corners']
    home_gols = fixture.get('goals', {}).get('home', 0) or 0
    away_gols = fixture.get('goals', {}).get('away', 0) or 0

    press_home = metrics['press_home']
    press_away = metrics['press_away']

    # Originais
    if HT_WINDOW[0] <= minuto <= HT_WINDOW[1] and home_gols == away_gols and press_home >= MIN_PRESSURE_SCORE:
        estrategias.append("HT - Casa Empatando")
    if 70 <= minuto <= 86.8 and home_gols < away_gols and press_home >= MIN_PRESSURE_SCORE:
        estrategias.append("FT - Reação da Casa")
    if 70 <= minuto <= 88.8 and max(press_home, press_away) >= MIN_PRESSURE_SCORE and total_cantos <= 8:
        estrategias.append("FT - Over Cantos 2º Tempo")
    if metrics['small_stadium'] and max(press_home, press_away) >= MIN_PRESSURE_SCORE and 25 <= minuto <= 89.8:
        estrategias.append("Campo Pequeno + Pressão")
    if minuto >= 30 and press_home >= 0.30 and press_away >= 0.30:
        estrategias.append("Jogo Aberto (Ambos pressionam)")
    if 35 <= minuto <= 79.8:
        if press_home > press_away + 0.10 and home_gols < away_gols:
            estrategias.append("Favorito em Perigo (Casa)")
        if press_away > press_home + 0.10 and away_gols < home_gols:
            estrategias.append("Favorito em Perigo (Fora)")

    # Avançadas
    if (press_home >= 1.36 and metrics['home_danger'] >= 5.8 and metrics['home_pos'] >= 59.5 and
        home_gols <= away_gols and 18.8 <= minuto <= 38.6):
        estrategias.append("Pressão Mandante Dominante")
    if (total_cantos <= 4.3 and (metrics['home_danger'] + metrics['away_danger']) >= 9.4 and
        (metrics['home_shots'] + metrics['away_shots']) >= 1.8 and
        press_home < 1.95 and press_away < 1.95 and minuto <= 43.8):
        estrategias.append("Jogo Vivo Sem Cantos")
    if ((metrics['home_shots'] + metrics['away_shots']) < 4.8 and
        abs(metrics['home_pos'] - metrics['away_pos']) <= 9.8 and
        (metrics['home_danger'] + metrics['away_danger']) < 4.7 and minuto >= 24.5):
        estrategias.append("Jogo Travado (Under Corner Asiático)")
    if (press_home >= 1.18 and press_away >= 1.18 and
        (metrics['home_danger'] + metrics['away_danger']) >= 9.6 and
        (metrics['home_shots'] + metrics['away_shots']) >= 4.6 and
        19.5 <= minuto <= 79.5):
        estrategias.append("Pressão Alternada (Ambos Atacando)")

    # Composite 3/5
    home_g = fixture.get('goals', {}).get('home', 0) or 0
    away_g = fixture.get('goals', {}).get('away', 0) or 0
    cond_attacks  = (metrics['home_attacks'] + metrics['away_attacks']) >= ATTACKS_MIN_SUM
    cond_danger   = (metrics['home_danger']  + metrics['away_danger'])  >= DANGER_MIN_SUM
    cond_pressure = max(metrics['press_home'], metrics['press_away'])   >= MIN_PRESSURE_SCORE
    cond_score    = ((home_g == away_g) or
                    (metrics['press_home'] > metrics['press_away'] and home_g < away_g) or
                    (metrics['press_away'] > metrics['press_home'] and away_g < home_g))
    cond_window   = (HT_WINDOW[0] <= minuto <= HT_WINDOW[1]) or (FT_WINDOW[0] <= minuto <= FT_WINDOW[1])
    true_count = sum([cond_attacks, cond_danger, cond_pressure, cond_score, cond_window])
    composite_ok = true_count >= 3

    logger.debug("🧩 Composite (Setup 3/5): Ataques=%s | Perigo=%s | Pressão=%s | Placar=%s | Janela=%s → %d/5 | Estratégias VIP: %d/10",
                 cond_attacks, cond_danger, cond_pressure, cond_score, cond_window, true_count, len(estrategias))

    return estrategias, composite_ok

# ========================= ANTI-SPAM ==========================
def should_notify(fixture_id: int, signal_key: str) -> bool:
    now = time.time()
    last = sent_signals[fixture_id].get(signal_key, 0)
    if now - last >= RENOTIFY_MINUTES * 60:
        sent_signals[fixture_id][signal_key] = now
        return True
    return False

# ===================== VIP MESSAGE =====================
def build_bet365_link(fixture: Dict[str, Any]) -> str:
    home = (fixture.get("teams", {}) or {}).get("home", {}).get("name", "") or ""
    away = (fixture.get("teams", {}) or {}).get("away", {}).get("name", "") or ""
    league = (fixture.get("league", {}) or {}).get("name", "") or ""
    query = f"site:bet365.com {home} x {away} {league}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

def _format_minute(elapsed: Any) -> str:
    try:
        return f"{float(elapsed):.0f}'"
    except Exception:
        return str(elapsed)

def build_signal_message_vip(fixture: Dict[str, Any], estrategias: List[str], metrics: Dict[str, Any]) -> str:
    teams = fixture.get("teams", {}) or {}
    league = (fixture.get("league", {}) or {}).get("name", "?")
    home_team = (teams.get("home", {}) or {}).get("name", "?")
    away_team = (teams.get("away", {}) or {}).get("name", "?")
    goals = fixture.get("goals", {}) or {}
    score = f"{goals.get('home', '-')} x {goals.get('away', '-')}"
    minute_txt = _format_minute(metrics.get("minute", 0))

    total_corners = metrics.get("total_corners", 0)
    home_c = metrics.get("home_corners", 0)
    away_c = metrics.get("away_corners", 0)
    home_att = metrics.get("home_attacks", 0)
    away_att = metrics.get("away_attacks", 0)
    home_d = metrics.get("home_danger", 0)
    away_d = metrics.get("away_danger", 0)
    home_sh = metrics.get("home_shots", 0)
    away_sh = metrics.get("away_shots", 0)
    home_pos = metrics.get("home_pos", 0)
    away_pos = metrics.get("away_pos", 0)
    press_home = f"{metrics.get('press_home', 0.0):.2f}"
    press_away = f"{metrics.get('press_away', 0.0):.2f}"
    stadium_small = "✅" if metrics.get("small_stadium") else "❌"

    # Título: HT/FT
    minute_val = metrics.get("minute", 0)
    if HT_WINDOW[0] <= minute_val <= HT_WINDOW[1]:
        title = "ALERTA ESTRATÉGIA: HT — Asiáticos/Limite"
    elif FT_WINDOW[0] <= minute_val <= FT_WINDOW[1]:
        title = "ALERTA ESTRATÉGIA: FT — Asiáticos/Limite"
    else:
        title = "ALERTA ESTRATÉGIA — Asiáticos/Limite"

    estrategias_block = " • ".join(estrategias) if estrategias else "Setup 3/5 válido"
    bet_link = build_bet365_link(fixture)

    msg = (
        f"📣 {title}\n"
        f"🏟️ {home_team} x {away_team}\n"
        f"🏆 {league}\n"
        f"⏱️ {minute_txt} | ⚽ Placar: {score}\n"
        f"⛳ Cantos: {total_corners} ({home_team}: {home_c} • {away_team}: {away_c})\n"
        f"🔥 Pressão: {home_team} {press_home} | {away_team} {press_away}\n"
        f"⚡ Ataques: {home_team} {home_att} | {away_team} {away_att}\n"
        f"🔥 Perigosos: {home_team} {home_d} | {away_team} {away_d}\n"
        f"🥅 Finalizações: {home_team} {home_sh} | {away_team} {away_sh}\n"
        f"🎯 Posse: {home_team} {home_pos}% | {away_team} {away_pos}%\n"
        f"🏟️ Estádio pequeno: {stadium_small}\n\n"
        f"📌 Estratégias ativas: {estrategias_block}\n\n"
        f"🔗 Bet365:\n{bet_link}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return msg

# ========================= UTIL: MINUTO/PERÍODO =========================
def get_period(minute: float) -> str:
    return "HT" if minute <= 45 else "FT"

def smooth_minute(fixture_id: int, raw: float) -> float:
    """Garante minuto não regressivo e sem saltos >5 entre varreduras."""
    raw = float(raw or 0.0)
    prev = last_elapsed_seen.get(fixture_id, 0.0)
    # não retrocede
    if raw < prev:
        raw = prev
    # evita saltos muito grandes
    if raw - prev > 5.0:
        raw = prev + 5.0
    # limita entre 0 e 95
    raw = max(0.0, min(95.0, raw))
    last_elapsed_seen[fixture_id] = raw
    return round(raw, 1)

# ========================= MÉTRICAS STATUS ====================
def atualizar_metricas(loop_total: int, req_headers: Dict[str, str]):
    global LAST_SCAN_TIME, LAST_API_STATUS, LAST_RATE_USAGE, TOTAL_VARRIDURAS
    LAST_SCAN_TIME = datetime.now(pytz.timezone("America/Sao_Paulo"))
    TOTAL_VARRIDURAS += 1
    k = { (key or '').lower(): str(val) for key, val in (req_headers or {}).items() }
    if 'x-ratelimit-minutely-remaining' in k and 'x-ratelimit-minutely-limit' in k:
        try:
            restante = int(k.get('x-ratelimit-minutely-remaining','0') or '0')
            limite   = int(k.get('x-ratelimit-minutely-limit','1') or '1')
            uso = 100 - int((restante / max(1, limite)) * 100)
            LAST_RATE_USAGE = f"{uso}% usado"
            LAST_API_STATUS = "✅ OK" if uso < 90 else "⚠️ Alto consumo"
        except Exception:
            LAST_API_STATUS = "⚠️ Cabeçalhos inválidos"
            LAST_RATE_USAGE = "Indefinido"
    else:
        LAST_API_STATUS = "⚠️ Cabeçalhos ausentes"
        LAST_RATE_USAGE = "Indefinido"

# ========================= MAIN LOOP ==========================
def main_loop():
    logger.info("🔁 Loop econômico iniciado. Base: %ss (renotify=%s min).", SCAN_INTERVAL_BASE, RENOTIFY_MINUTES)
    logger.info("🟢 Loop econômico ativo: aguardando jogos ao vivo...")

    global total
    signals_sent = 0

    while True:
        try:
            fixtures = get_live_fixtures()
            total = len(fixtures)

            if total == 0:
                logger.debug("Sem partidas ao vivo no momento. (req=%s, rate=%s)", request_count, last_rate_headers)
                time.sleep(SCAN_INTERVAL_BASE)
                atualizar_metricas(0, last_rate_headers)
                continue

            scan_interval = SCAN_INTERVAL_BASE if total < 20 else SCAN_INTERVAL_BASE + 60
            logger.debug("🎯 %d jogos ao vivo | intervalo=%ds | req=%s | rate=%s",
                         total, scan_interval, request_count, last_rate_headers)

            for fixture in fixtures:
                fixture_id = fixture.get('fixture', {}).get('id')
                if not fixture_id:
                    continue

                # Minuto do jogo (suavizado)
                minute_raw = fixture.get('fixture', {}).get('status', {}).get('elapsed', 0) or 0
                minute = smooth_minute(fixture_id, float(minute_raw))
                period = get_period(minute)

                # Ignorar partidas muito cedo (tolerância 18.8')
                if minute < 18.8:
                    logger.debug("⏳ Ignorado fixture=%s (min %.1f < 18.8')", fixture_id, minute)
                    continue

                # Já enviei sinal neste período? (um por período)
                if period in sent_period[fixture_id]:
                    logger.debug("🔒 Já sinalizado neste período %s (fixture=%s). Pulando.", period, fixture_id)
                    continue

                stats_resp = get_fixture_statistics(fixture_id)
                if not stats_resp:
                    logger.debug("Sem estatísticas para fixture=%s no momento.", fixture_id)
                    continue

                home, away = extract_basic_stats(fixture, stats_resp)
                press_home, press_away = pressure_score_vip(home, away)

                total_corners = (home['corners'] or 0) + (away['corners'] or 0)
                total_shots = (home['shots'] or 0) + (away['shots'] or 0)

                metrics = {
                    'minute': minute,
                    'home_corners': home['corners'], 'away_corners': away['corners'],
                    'home_attacks': home['attacks'], 'away_attacks': away['attacks'],
                    'home_danger': home['danger'], 'away_danger': away['danger'],
                    'home_shots': home['shots'], 'away_shots': away['shots'],
                    'home_pos': home['pos'], 'away_pos': away['pos'],
                    'press_home': press_home, 'press_away': press_away,
                    'small_stadium': (fixture.get('fixture', {}).get('venue', {}).get('name', '').lower() in SMALL_STADIUMS),
                    'total_corners': total_corners,
                    'total_shots': total_shots
                }

                estrategias, composite_ok = verificar_estrategias_vip(fixture, metrics)
                if not estrategias and not composite_ok:
                    logger.debug("IGNORADO fixture=%s minuto=%.1f | press(H)=%.2f/A=%.2f | att=%s | dang=%s | shots=%s",
                                 fixture_id, minute, press_home, press_away,
                                 metrics['home_attacks'] + metrics['away_attacks'],
                                 metrics['home_danger'] + metrics['away_danger'],
                                 total_shots)
                    continue

                # Regra: 1ºT exige 3 estratégias | 2ºT exige 4
                limite_estrategias = 3 if minute <= 45 else 4
                strat_title = f"{len(estrategias)}/{limite_estrategias} Estratégias Ativas" if estrategias else "Setup 3/5 — Asiáticos/Limite"
                signal_key = f"{period}{strat_title}{total_corners}"

                if (len(estrategias) >= limite_estrategias or composite_ok) and should_notify(fixture_id, signal_key):
                    msg = build_signal_message_vip(fixture, estrategias, metrics)
                    send_telegram_message_plain(msg)
                    signals_sent += 1
                    sent_period[fixture_id].add(period)  # marca que já enviou neste período
                    logger.info("📤 Sinal enviado (%s): %d estratégias [%s] fixture=%s minuto=%.1f",
                                period, len(estrategias), ", ".join(estrategias[:5]), fixture_id, minute)
                else:
                    logger.debug("❌ Apenas %d estratégias (%s). Aguardando mais sinais fortes...",
                                 len(estrategias), ", ".join(estrategias))

            # --- Resumo da varredura ---
            try:
                logger.info("📊 Resumo: %d jogos analisados | %d sinais enviados | próxima em %ds",
                            total, signals_sent, scan_interval)
                atualizar_metricas(total, last_rate_headers)
                signals_sent = 0
            except Exception as e:
                logger.exception("Erro ao finalizar resumo da varredura: %s", e)

            time.sleep(scan_interval)

        except Exception as e:
            logger.exception("Erro no loop principal: %s", e)
            time.sleep(SCAN_INTERVAL_BASE)

# =========================== START ============================
if __name__ == "__main__":
    logger.info("🚀 Iniciando Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA Sensível v3")
    try:
        boot_msg = "🤖 Bot VIP ULTRA ativo. Ignorando jogos < 18.8', 1 sinal por período e minuto suavizado."
        send_telegram_message_plain(boot_msg)
    except Exception:
        pass

    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
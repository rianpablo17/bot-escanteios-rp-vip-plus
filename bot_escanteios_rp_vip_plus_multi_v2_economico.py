#!/usr/bin/env python3
# -- coding: utf-8 --
"""
Bot Escanteios RP VIP Plus — Multi v2 (Econômico) • ULTRA Sensível v3
- Dispara quando qualquer 3 de 5 condições principais forem verdadeiras
- Thresholds ajustados para teste (mais sensível)
- Mantém todas as estratégias originais (HT, FT, Campo Pequeno, Jogo Aberto, Favorito em Perigo)
- Mantém anti-spam, /status e logs

ENV:
- API_FOOTBALL_KEY, TOKEN, TELEGRAM_CHAT_ID, (opcional) TELEGRAM_ADMIN_ID
- SCAN_INTERVAL (default 120), RENOTIFY_MINUTES (default 5)
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
SCAN_INTERVAL_BASE = int(os.getenv('SCAN_INTERVAL', '120'))
RENOTIFY_MINUTES   = int(os.getenv('RENOTIFY_MINUTES', '5'))

if not API_FOOTBALL_KEY:
    raise ValueError("⚠️ API_FOOTBALL_KEY não definida.")
if not TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("⚠️ Defina TOKEN e TELEGRAM_CHAT_ID.")

# ===================== API CONFIG ===================
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ===================== PARÂMETROS ====================
HT_WINDOW = (30, 42)   # Janela HT (mais ampla)
FT_WINDOW = (70, 92)   # Janela FT (mais ampla)

# Thresholds sensíveis (modo de teste)
MIN_PRESSURE_SCORE = 0.20  # mais sensível
ATTACKS_MIN_SUM    = 12
DANGER_MIN_SUM     = 6
MIN_TOTAL_SHOTS    = 5

# Estádios "apertados"
SMALL_STADIUMS = {
    'loftus road','vitality stadium','kenilworth road','turf moor',
    'bramall lane','ewood park','the den','carrow road',
    'bet365 stadium','pride park','liberty stadium','fratton park',
}

# Anti-spam storage
sent_signals: Dict[int, Dict[str, float]] = defaultdict(dict)

# Diagnostics
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
    """
    Webhook oficial do Telegram.
    - Recebe updates de mensagens (privadas e grupos)
    - Responde a /status e /debug sem quebrar o fluxo do bot
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        logger.debug("📩 Update Telegram recebido: %s", str(data)[:500])

        message = data.get('message') or data.get('edited_message') or {}
        text = (message.get('text') or '').strip().lower()
        chat_id = str(message.get('chat', {}).get('id', TELEGRAM_CHAT_ID))

        # --- Comando /status ---
        if text == '/status':
            from datetime import datetime
            uptime = int(time.time() - START_TIME)
            horas = uptime // 3600
            minutos = (uptime % 3600) // 60

            total_jogos = globals().get("total", 0)
            varreduras = globals().get("TOTAL_VARRIDURAS", 0)
            api_status = globals().get("LAST_API_STATUS", "✅ OK")
            uso_api = globals().get("LAST_RATE_USAGE", "Indefinido")

            resposta = (
                "📊 Status Bot Escanteios RP VIP Plus\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                f"🕒 Tempo online: {horas}h {minutos}min\n"
                f"⚽ Jogos varridos: {total_jogos}\n"
                f"🔁 Varreduras realizadas: {varreduras}\n"
                f"🌐 Status API: {api_status}\n"
                f"📉 Uso da API: {uso_api}\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "🤖 Versão: Multi v2 Econômico ULTRA Sensível v3"
            )
            _tg_send(chat_id, resposta)
            logger.info("📨 /status respondido com sucesso (%s)", chat_id)

        # --- Comando /debug ---
        elif text == '/debug':
            resposta = (
                "🧩 Modo Debug ativo\n"
                f"📦 Requests enviados: {request_count}\n"
                f"⏱ Último intervalo: {SCAN_INTERVAL_BASE}s\n"
                f"📡 Headers API: {last_rate_headers}"
            )
            _tg_send(chat_id, resposta)
            logger.info("📨 /debug respondido com sucesso (%s)", chat_id)

    except Exception as e:
        logger.exception("❌ Erro no processamento do webhook: %s", e)

    # ⚠️ Sempre retornar 200 para evitar bloqueio do Telegram
    return jsonify({"ok": True}), 200

# ====================== TELEGRAM HELPERS =====================
def _tg_send(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.warning("Erro Telegram %s: %s", r.status_code, r.text[:400])
    except Exception as e:
        logger.exception("Erro ao enviar Telegram: %s", e)

def send_telegram_message(text: str):
    _tg_send(TELEGRAM_CHAT_ID, text)

def send_admin_message(text: str):
    if TELEGRAM_ADMIN_ID:
        _tg_send(TELEGRAM_ADMIN_ID, text)

# ====================== RATE LIMIT SAFE ======================
LAST_REQUEST = 0.0
MIN_INTERVAL = 0.8  # ~75/min

def safe_request(url, headers, params=None):
    global LAST_REQUEST, request_count, last_rate_headers
    now = time.time()
    elapsed = now - LAST_REQUEST
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    LAST_REQUEST = time.time()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        request_count += 1
        last_rate_headers = {
            'x-ratelimit-requests-remaining': resp.headers.get('x-ratelimit-requests-remaining'),
            'x-ratelimit-requests-limit': resp.headers.get('x-ratelimit-requests-limit'),
            'x-ratelimit-minutely-remaining': resp.headers.get('x-ratelimit-minutely-remaining'),
            'x-ratelimit-minutely-limit': resp.headers.get('x-ratelimit-minutely-limit'),
        }
        if resp.status_code == 429:
            logger.warning("⚠️ 429 Too Many Requests — backoff 30s | headers=%s", last_rate_headers)
            time.sleep(30)
            return None
        return resp
    except Exception as e:
        logger.error("❌ Erro na requisição segura: %s", e)
        return None

# ===================== API CALLS =====================
def get_live_fixtures():
    try:
        url = f"{API_BASE}/fixtures"
        params = {"live": "all"}
        resp = safe_request(url, headers=HEADERS, params=params)
        if not resp or resp.status_code != 200:
            logger.warning("⚠️ Erro ao buscar fixtures ao vivo: %s", resp.text if resp else "sem resposta")
            return []
        data = resp.json()
        return data.get("response", []) or []
    except Exception as e:
        logger.exception("Erro em get_live_fixtures: %s", e)
        return []

def get_fixture_statistics(fixture_id):
    try:
        url = f"{API_BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        resp = safe_request(url, headers=HEADERS, params=params)
        if not resp or resp.status_code != 200:
            logger.warning("⚠️ Erro em fixtures/statistics: %s", resp.text if resp else "sem resposta")
            return None
        data = resp.json()
        return data.get("response", [])
    except Exception as e:
        logger.exception("Erro em get_fixture_statistics: %s", e)
        return None

# ===================== POISSON =====================
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

# ===================== EXTRACT STATS =====================
STAT_ALIASES = {
    'corners': ['corner', 'corners'],
    'attacks': ['attack'],
    'danger':  ['danger', 'dangerous', 'dangerous attack', 'shots on goal', 'on goal'],
    'shots':   ['shot', 'shots', 'total shots', 'shots on target', 'shots on goal'],
    'pos':     ['possession', 'ball possession']
}

def extract_value(stat_type: str, t: str, val) -> Optional[int]:
    t_low = t.lower()
    for alias in STAT_ALIASES[stat_type]:
        if alias in t_low:
            try:
                return int(float(str(val).replace('%', '')))
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
        target = home if team.get('id') == home_id else away if team.get('id') == away_id else None
        if not target:
            continue
        for s in stats_list:
            t = str(s.get('type', '')).lower()
            val = s.get('value')

            v = extract_value('corners', t, val)
            if v is not None:
                target['corners'] = v
                continue

            v = extract_value('attacks', t, val)
            if v is not None:
                target['attacks'] = v
                continue

            v = extract_value('danger', t, val)
            if v is not None:
                target['danger'] = v
                continue

            v = extract_value('shots', t, val)
            if v is not None:
                target['shots'] = v
                continue

            v = extract_value('pos', t, val)
            if v is not None:
                target['pos'] = v
                continue

    return home, away

# ===================== PRESSURE VIP =====================
def pressure_score_vip(home: Dict[str, int], away: Dict[str, int]) -> Tuple[float, float]:
    def norm(x, a):
        try:
            return max(0.0, min(1.0, x / float(a)))
        except Exception:
            return 0.0

    # somatórios (filtro mínimo) — aqui bem permissivo
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

# ======================= ESTRATÉGIAS VIP =======================
def verificar_estrategias_vip(fixture: Dict[str, Any], metrics: Dict[str, Any]) -> List[str]:
    sinais = []
    minuto = metrics['minute']
    total_cantos = metrics['total_corners']
    home_gols = fixture.get('goals', {}).get('home', 0) or 0
    away_gols = fixture.get('goals', {}).get('away', 0) or 0

    press_home = metrics['press_home']
    press_away = metrics['press_away']

    # 1) HT - Casa Empatando (30–42, pressão da casa)
    if HT_WINDOW[0] <= minuto <= HT_WINDOW[1] and home_gols == away_gols and press_home >= MIN_PRESSURE_SCORE:
        sinais.append("Estratégia HT - Casa Empatando")

    # 2) FT - Reação da Casa (70–88, perdendo + pressão da casa)
    if 70 <= minuto <= 88 and home_gols < away_gols and press_home >= MIN_PRESSURE_SCORE:
        sinais.append("Estratégia FT - Reação da Casa")

    # 3) FT - Over Cantos 2º Tempo (70–90, pressão de qualquer lado + cantos totais ainda baixos)
    if 70 <= minuto <= 90 and max(press_home, press_away) >= MIN_PRESSURE_SCORE and total_cantos <= 8:
        sinais.append("Estratégia FT - Over Cantos 2º Tempo")

    # 4) Campo Pequeno + Pressão (25’–90’)
    if metrics['small_stadium'] and max(press_home, press_away) >= MIN_PRESSURE_SCORE and 25 <= minuto <= 90:
        sinais.append("Estratégia Campo Pequeno + Pressão")

    # 5) Jogo Aberto (Ambos pressionam) a partir de 30'
    if minuto >= 30 and press_home >= 0.30 and press_away >= 0.30:
        sinais.append("Estratégia Jogo Aberto (Ambos pressionam)")

    # 6) Favorito em Perigo (lado mais pressionando está perdendo)
    if 35 <= minuto <= 80:
        if press_home > press_away + 0.10 and home_gols < away_gols:
            sinais.append("Favorito em Perigo (Casa)")
        if press_away > press_home + 0.10 and away_gols < home_gols:
            sinais.append("Favorito em Perigo (Fora)")

    return sinais

# ============ COMPOSITE TRIGGER (3/5 condições) ===============
def composite_trigger_check(fixture: Dict[str, Any], metrics: Dict[str, Any]) -> bool:
    """True se 3 de 5 condições forem satisfeitas."""
    minute = metrics['minute']
    home_g = fixture.get('goals', {}).get('home', 0) or 0
    away_g = fixture.get('goals', {}).get('away', 0) or 0

    cond_attacks = (metrics['home_attacks'] + metrics['away_attacks']) >= ATTACKS_MIN_SUM
    cond_danger  = (metrics['home_danger'] + metrics['away_danger'])   >= DANGER_MIN_SUM
    cond_pressure= max(metrics['press_home'], metrics['press_away'])    >= MIN_PRESSURE_SCORE
    cond_score   = (home_g == away_g) or (
        (metrics['press_home'] > metrics['press_away'] and home_g < away_g) or
        (metrics['press_away'] > metrics['press_home'] and away_g < home_g)
    )
    cond_window  = (HT_WINDOW[0] <= minute <= HT_WINDOW[1]) or (FT_WINDOW[0] <= minute <= FT_WINDOW[1])

    true_count = sum([cond_attacks, cond_danger, cond_pressure, cond_score, cond_window])
    logger.debug("Composite: attacks=%s danger=%s pressure=%s score=%s window=%s -> %d/5",
                 cond_attacks, cond_danger, cond_pressure, cond_score, cond_window, true_count)
    return true_count >= 3

# ===================== VIP MESSAGE / LINKS =====================
def build_bet365_link(fixture: Dict[str, Any]) -> str:
    home = fixture.get('teams', {}).get('home', {}).get('name', '') or ''
    away = fixture.get('teams', {}).get('away', {}).get('name', '') or ''
    league = fixture.get('league', {}).get('name', '') or ''
    query = f"site:bet365.com {home} x {away} {league}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

def build_vip_message(fixture: Dict[str, Any], strategy_title: str, metrics: Dict[str, Any],
                      best_lines: List[Dict[str, float]]) -> str:
    teams = fixture.get('teams', {})
    home = escape_markdown(teams.get('home', {}).get('name', '?'))
    away = escape_markdown(teams.get('away', {}).get('name', '?'))
    minute = escape_markdown(metrics.get('minute', 0))
    goals = fixture.get('goals', {})
    score = escape_markdown(f"{goals.get('home','-')} x {goals.get('away','-')}")

    total_corners = escape_markdown(metrics.get('total_corners'))
    home_c = escape_markdown(metrics.get('home_corners'))
    away_c = escape_markdown(metrics.get('away_corners'))
    home_att = escape_markdown(metrics.get('home_attacks'))
    away_att = escape_markdown(metrics.get('away_attacks'))
    home_d = escape_markdown(metrics.get('home_danger'))
    away_d = escape_markdown(metrics.get('away_danger'))
    home_sh = escape_markdown(metrics.get('home_shots'))
    away_sh = escape_markdown(metrics.get('away_shots'))
    home_pos = escape_markdown(metrics.get('home_pos'))
    away_pos = escape_markdown(metrics.get('away_pos'))

    press_home = escape_markdown(f"{metrics['press_home']:.2f}")
    press_away = escape_markdown(f"{metrics['press_away']:.2f}")

    stadium_small = "✅" if metrics.get('small_stadium') else "❌"
    strategy_title_md = escape_markdown(strategy_title)

    lines_txt = []
    for ln in best_lines[:3]:
        line = f"{ln['line']:.1f}"
        pwin = f"{ln['p_win']*100:.0f}"
        ppush = f"{ln['p_push']*100:.0f}"
        lines_txt.append(f"Linha {escape_markdown(line)} → Win {escape_markdown(pwin)}% \\| Push {escape_markdown(ppush)}%")

    bet_link = build_bet365_link(fixture)

    parts = [
        f"📣 {strategy_title_md}",
        f"🏟 Jogo: {home} x {away}",
        f"⏱ Minuto: {minute}  \\|  ⚽ Placar: {score}",
        f"⛳ Cantos: {total_corners} \\(H:{home_c} \\- A:{away_c}\\)",
        f"⚡ Ataques: H:{home_att}  A:{away_att}  \\|  🔥 Perigosos: H:{home_d}  A:{away_d}",
        f"🥅 Chutes: H:{home_sh}  A:{away_sh}  \\|  🎯 Posse: H:{home_pos}%  A:{away_pos}%",
        f"📊 Pressão: H:{press_home}  A:{press_away}  \\|  🏟 Estádio pequeno: {stadium_small}",
        "",
        "Top linhas sugeridas \\(Poisson\\)\\:",
        *lines_txt,
        "",
        f"🔗 Bet365: {bet_link}",
    ]
    return "\n".join(parts)

# ========================= ANTI-SPAM ==========================
def should_notify(fixture_id: int, signal_key: str) -> bool:
    now = time.time()
    last = sent_signals[fixture_id].get(signal_key, 0)
    if now - last >= RENOTIFY_MINUTES * 60:
        sent_signals[fixture_id][signal_key] = now
        return True
    return False

# ========================= STATUS VIP ==========================
START_TIME = datetime.now()
LAST_SCAN_TIME = None
LAST_API_STATUS = "⏳ Aguardando..."
LAST_RATE_USAGE = "0%"
TOTAL_VARRIDURAS = 0

def get_status_message():
    try:
        uptime = datetime.now() - START_TIME
        horas, resto = divmod(uptime.seconds, 3600)
        minutos, _ = divmod(resto, 60)
        jogos = globals().get("total", 0)
        varridas = globals().get("TOTAL_VARRIDURAS", 0)
        last_scan = globals().get("LAST_SCAN_TIME")
        last_scan_txt = last_scan.strftime("%H:%M:%S") if last_scan else "Ainda não realizada"
        api_status = globals().get("LAST_API_STATUS", "✅ OK")
        rate_usage = globals().get("LAST_RATE_USAGE", "0%")
        msg = (
            "📊 Status Bot Escanteios RP VIP Plus\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 Tempo online: {horas}h {minutos}min\n"
            f"⚽ Jogos varridos: {jogos}\n"
            f"🔁 Varreduras realizadas: {varridas}\n"
            f"⏱️ Última varredura: {last_scan_txt}\n"
            f"🧮 Próxima varredura: {SCAN_INTERVAL_BASE}s\n"
            f"🌐 Status API: {api_status}\n"
            f"📉 Uso da API: {rate_usage}\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "🤖 Versão Multi v2 Econômico ULTRA Sensível v3"
        )
        return msg
    except Exception as e:
        return f"❌ Erro ao gerar status: {e}"

def atualizar_metricas(loop_total, req_headers):
    global LAST_SCAN_TIME, LAST_API_STATUS, LAST_RATE_USAGE, TOTAL_VARRIDURAS
    LAST_SCAN_TIME = datetime.now()
    TOTAL_VARRIDURAS += 1
    k = { (key or '').lower(): str(val) for key, val in (req_headers or {}).items() }
    if 'x-ratelimit-minutely-remaining' in k and 'x-ratelimit-minutely-limit' in k:
        try:
            restante = int(k.get('x-ratelimit-minutely-remaining','0'))
            limite   = int(k.get('x-ratelimit-minutely-limit','1'))
            uso = 100 - int((restante / max(1,limite)) * 100)
            LAST_RATE_USAGE = f"{uso}% usado"
            LAST_API_STATUS = "✅ OK" if uso < 90 else "⚠️ Alto consumo"
        except Exception:
            LAST_API_STATUS = "⚠️ Cabeçalhos inválidos"
            LAST_RATE_USAGE = "Indefinido"
    else:
        LAST_API_STATUS = "❌ Sem cabeçalhos \\(erro API\\)"
        LAST_RATE_USAGE = "Indefinido"

@app.route(f"/{TOKEN}/status", methods=["POST"])
def telegram_status_webhook():
    data = request.get_json() or {}
    message = data.get("message", {})
    text = (message.get("text", "") or "").strip().lower()
    if text == "/status":
        status_msg = get_status_message()
        send_telegram_message(status_msg)
    return jsonify({"ok": True})

# ========================= MAIN LOOP ==========================
def main_loop():
    logger.info("🔁 Loop econômico iniciado. Base: %ss (renotify=%s min).", SCAN_INTERVAL_BASE, RENOTIFY_MINUTES)
    logger.info("🟢 Loop econômico ativo: aguardando jogos ao vivo...")

    global total  # para /status
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
                minute = fixture.get('fixture', {}).get('status', {}).get('elapsed', 0) or 0

                # ECONOMIA: ignorar < 25'
                if minute < 25:
                    logger.debug("⏳ Ignorado fixture=%s (min %s < 25')", fixture_id, minute)
                    continue

                stats_resp = get_fixture_statistics(fixture_id)
                if not stats_resp:
                    logger.debug("Sem estatísticas para fixture=%s no momento.", fixture_id)
                    continue

                home, away = extract_basic_stats(fixture, stats_resp)
                press_home, press_away = pressure_score_vip(home, away)

                total_corners = (home['corners'] or 0) + (away['corners'] or 0)
                metrics = {
                    'minute': minute,
                    'home_corners': home['corners'], 'away_corners': away['corners'],
                    'home_attacks': home['attacks'], 'away_attacks': away['attacks'],
                    'home_danger': home['danger'],   'away_danger': away['danger'],
                    'home_shots': home['shots'],     'away_shots': away['shots'],
                    'home_pos': home['pos'],         'away_pos': away['pos'],
                    'press_home': press_home,        'press_away': press_away,
                    'small_stadium': (fixture.get('fixture', {}).get('venue', {}).get('name', '') or '').lower() in SMALL_STADIUMS,
                    'total_corners': total_corners
                }

                # Estratégias específicas
                estrategias = verificar_estrategias_vip(fixture, metrics)

                # Disparo composto (3/5) — não depende da lista acima
                trigger_ok = composite_trigger_check(fixture, metrics)

                if not estrategias and not trigger_ok:
                    logger.debug("IGNORADO fixture=%s min=%s press(H/A)=%.2f/%.2f cantos=%s",
                                 fixture_id, minute, press_home, press_away, total_corners)
                    continue

                # Se houver, envia as específicas
                best_lines = evaluate_candidate_lines(total_corners, lam=1.5)
                for strat_title in estrategias:
                    signal_key = f"{strat_title}_{total_corners}"
                    if should_notify(fixture_id, signal_key):
                        msg = build_vip_message(fixture, strat_title, metrics, best_lines)
                        send_telegram_message(msg)
                        logger.info("📤 Sinal enviado [%s] fixture=%s minuto=%s", strat_title, fixture_id, minute)
                        signals_sent += 1
                        if TELEGRAM_ADMIN_ID:
                            send_admin_message(f"✅ {strat_title} | {fixture.get('teams',{}).get('home',{}).get('name','?')} x {fixture.get('teams',{}).get('away',{}).get('name','?')} | {minute}'")

                # E também o gatilho composto genérico
                if trigger_ok:
                    signal_key = f"Trigger 3-de-5_{total_corners}"
                    if should_notify(fixture_id, signal_key):
                        msg = build_vip_message(fixture, "Trigger 3\\-de\\-5", metrics, best_lines)
                        send_telegram_message(msg)
                        logger.info("📤 Sinal enviado [Trigger 3-de-5] fixture=%s minuto=%s", fixture_id, minute)
                        signals_sent += 1
                        if TELEGRAM_ADMIN_ID:
                            send_admin_message(f"✅ Trigger 3-de-5 | {fixture.get('teams',{}).get('home',{}).get('name','?')} x {fixture.get('teams',{}).get('away',{}).get('name','?')} | {minute}'")

            # Resumo da varredura
            try:
                logger.info("📊 Resumo: %d jogos analisados | %d sinais enviados | próxima em %ds",
                            total, signals_sent, scan_interval)
                signals_sent = 0
                atualizar_metricas(total, last_rate_headers)
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
        boot = (
            "🤖 Bot VIP ULTRA ativo\\. Ignorando jogos < 25' e usando pressão dinâmica\\.\n"
            "🧠 Modo sensível: dispara quando 3 de 5 condições batem\\."
        )
        send_telegram_message(boot)
        if TELEGRAM_ADMIN_ID:
            send_admin_message("🔐 Logs privados habilitados\\.")
    except Exception:
        pass
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
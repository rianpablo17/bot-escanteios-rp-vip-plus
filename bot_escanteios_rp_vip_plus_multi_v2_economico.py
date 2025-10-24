#!/usr/bin/env python3
# -- coding: utf-8 --
"""
Bot Escanteios RP VIP Plus — Multi v2 (Econômico) • ULTRA Sensível v3
- Dispara quando qualquer 3 de 5 condições principais forem verdadeiras
- Thresholds ajustados para teste (mais sensível)
- Mantém estratégias (HT, FT, Campo Pequeno, Jogo Aberto, Favorito em Perigo)
- Anti-spam, /status e /debug via webhook do Telegram
ENV:
- API_FOOTBALL_KEY, TOKEN, TELEGRAM_CHAT_ID, (opcional) TELEGRAM_ADMIN_ID
- SCAN_INTERVAL (default 120), RENOTIFY_MINUTES (default 3)
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
SCAN_INTERVAL_BASE = int(os.getenv('SCAN_INTERVAL', '120'))
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
HT_WINDOW = (29.8, 42)   # Janela HT (mais ampla e com tolerância)
FT_WINDOW = (69.8, 93)   # Janela FT (mais ampla e com tolerância)

# Thresholds mais sensíveis (ajuste fino)
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

# Anti-spam
sent_signals: Dict[int, Dict[str, float]] = defaultdict(dict)

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
    """
    Webhook oficial do Telegram.
    - Recebe updates (privados e grupos)
    - Responde a /status e /debug sem quebrar o loop
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        logger.debug("📩 Update Telegram recebido: %s", str(data)[:500])

        message = data.get('message') or data.get('edited_message') or {}
        text = (message.get('text') or '').strip().lower()
        chat_id = str(message.get('chat', {}).get('id', TELEGRAM_CHAT_ID))

        # --- /status ---
        if text == '/status':
            uptime = int(time.time() - START_TIME)
            horas = uptime // 3600
            minutos = (uptime % 3600) // 60

            total_jogos = globals().get("total", 0)
            varreduras = globals().get("TOTAL_VARRIDURAS", 0)
            api_status = globals().get("LAST_API_STATUS", "✅ OK")
            uso_api = globals().get("LAST_RATE_USAGE", "Indefinido")
            last_scan_dt: Optional[datetime] = globals().get("LAST_SCAN_TIME")
            last_scan_txt = last_scan_dt.strftime("%H:%M:%S") if last_scan_dt else "Ainda não realizada"

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
            _tg_send(chat_id, resposta)
            logger.info("📨 /status respondido com sucesso (%s)", chat_id)

        # --- /debug ---
        elif text == '/debug':
            resposta = (
                "🧩 Modo Debug\n"
                f"📦 Requests enviados: {request_count}\n"
                f"⏱ Intervalo base: {SCAN_INTERVAL_BASE}s\n"
                f"📡 Headers API: {last_rate_headers}"
            )
            _tg_send(chat_id, resposta)
            logger.info("📨 /debug respondido com sucesso (%s)", chat_id)

    except Exception as e:
        logger.exception("❌ Erro no processamento do webhook: %s", e)

    # Sempre 200 OK
    return jsonify({"ok": True}), 200

# ====================== TELEGRAM HELPERS ======================
def _tg_send(
    chat_id: str,
    text: str,
    parse_mode: str = "MarkdownV2",
    disable_web_page_preview: bool = True,
) -> None:
    """
    Envia mensagem ao Telegram com segurança.
    Se MarkdownV2 falhar, reenvia como texto simples.
    """
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    # Escapa caracteres especiais se for MarkdownV2
    if parse_mode == "MarkdownV2":
        safe_text = (
            str(text)
            .replace("\\", "\\\\")
            .replace("", "\\")
            .replace("", "\\")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace("(", "\\(")
            .replace(")", "\\)")
            .replace("", "\\")
            .replace("", "\\")
            .replace(">", "\\>")
            .replace("#", "\\#")
            .replace("+", "\\+")
            .replace("-", "\\-")
            .replace("=", "\\=")
            .replace("|", "\\|")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace(".", "\\.")
            .replace("!", "\\!")
        )
    else:
        safe_text = str(text)

    payload = {
        "chat_id": chat_id,
        "text": safe_text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.warning("MarkdownV2 falhou (%s). Reenviando como texto simples...", r.status_code)
            fallback_payload = {
                "chat_id": chat_id,
                "text": str(text),
                "disable_web_page_preview": True,
            }
            requests.post(url, json=fallback_payload, timeout=20)
    except Exception as e:
        logger.exception("Erro ao enviar Telegram: %s", e)


def send_telegram_message(text: str) -> None:
    _tg_send(TELEGRAM_CHAT_ID, text, parse_mode="MarkdownV2", disable_web_page_preview=True)


def send_telegram_message_plain(text: str) -> None:
    """Versão sem Markdown, ideal para sinais limpos."""
    _tg_send(TELEGRAM_CHAT_ID, text, parse_mode=None, disable_web_page_preview=True)


def send_admin_message(text: str) -> None:
    if TELEGRAM_ADMIN_ID:
        _tg_send(TELEGRAM_ADMIN_ID, text, parse_mode="MarkdownV2", disable_web_page_preview=True)

# ===================== API CALLS =====================
def safe_request(url: str, headers: Dict[str, str], params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    """
    Executa uma requisição segura à API-Football e retorna o JSON (ou None em caso de erro).
    """
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            logger.warning("⚠️ Erro API-Football %s: %s", response.status_code, response.text)
            return None
    except Exception as e:
        logger.exception("Erro em safe_request: %s", e)
        return None


def get_live_fixtures():
    try:
        url = f"{API_BASE}/fixtures"
        params = {"live": "all"}
        data = safe_request(url, headers=HEADERS, params=params)
        if not data:
            logger.warning("⚠️ Erro ao buscar fixtures ao vivo (sem resposta ou falha na API)")
            return []
        return data.get("response", []) or []
    except Exception as e:
        logger.exception("Erro em get_live_fixtures: %s", e)
        return []


def get_fixture_statistics(fixture_id):
    try:
        url = f"{API_BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        data = safe_request(url, headers=HEADERS, params=params)
        if not data:
            logger.warning("⚠️ Erro em fixtures/statistics (sem resposta ou falha na API)")
            return None
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
                # posse pode vir com %
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

    # somatórios mínimos — permissivo
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

    # 3) FT - Over Cantos 2º Tempo (70–90, pressão de qualquer lado + cantos ainda baixos)
    if 70 <= minuto <= 90 and max(press_home, press_away) >= MIN_PRESSURE_SCORE and total_cantos <= 8:
        sinais.append("Estratégia FT - Over Cantos 2º Tempo")

    # 4) Campo Pequeno + Pressão (25’–90’)
    if metrics['small_stadium'] and max(press_home, press_away) >= MIN_PRESSURE_SCORE and 25 <= minuto <= 90:
        sinais.append("Estratégia Campo Pequeno + Pressão")

    # 5) Jogo Aberto (Ambos pressionam) a partir de 30'
    if minuto >= 30 and press_home >= 0.30 and press_away >= 0.30:
        sinais.append("Estratégia Jogo Aberto (Ambos pressionam)")

    # 6) Favorito em Perigo (mais pressiona, mas perde)
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

    cond_attacks  = (metrics['home_attacks'] + metrics['away_attacks']) >= ATTACKS_MIN_SUM
    cond_danger   = (metrics['home_danger']  + metrics['away_danger'])  >= DANGER_MIN_SUM
    cond_pressure = max(metrics['press_home'], metrics['press_away'])    >= MIN_PRESSURE_SCORE
    cond_score    = (home_g == away_g) or (
        (metrics['press_home'] > metrics['press_away'] and home_g < away_g) or
        (metrics['press_away'] > metrics['press_home'] and away_g < home_g)
    )
    cond_window   = (HT_WINDOW[0] <= minute <= HT_WINDOW[1]) or (FT_WINDOW[0] <= minute <= FT_WINDOW[1])

    true_count = sum([cond_attacks, cond_danger, cond_pressure, cond_score, cond_window])
    logger.debug("Composite: attacks=%s danger=%s pressure=%s score=%s window=%s -> %d/5",
                 cond_attacks, cond_danger, cond_pressure, cond_score, cond_window, true_count)
    return true_count >= 3

# ===================== VIP MESSAGE / LINKS (PLAIN TEXT LIMPO) =====================
def build_bet365_link(fixture: Dict[str, Any]) -> str:
    home = (fixture.get("teams", {}) or {}).get("home", {}).get("name", "") or ""
    away = (fixture.get("teams", {}) or {}).get("away", {}).get("name", "") or ""
    league = (fixture.get("league", {}) or {}).get("name", "") or ""
    query = f"site:bet365.com {home} x {away} {league}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)


def build_vip_message(
    fixture: Dict[str, Any],
    strategy_title: str,
    metrics: Dict[str, Any],
    best_lines: List[Dict[str, float]],
) -> str:
    teams = fixture.get("teams", {}) or {}
    home = (teams.get("home", {}) or {}).get("name", "?") or "?"
    away = (teams.get("away", {}) or {}).get("name", "?") or "?"

    # Minuto como número com 1 casa, quando possível
    minute_val = metrics.get("minute", 0)
    try:
        minute_txt = f"{float(minute_val):.1f}"
    except Exception:
        minute_txt = str(minute_val)

    goals = fixture.get("goals", {}) or {}
    score = f"{goals.get('home', '-')} x {goals.get('away', '-')}"

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

    # Top linhas (Poisson) — até 3
    lines_txt = []
    for ln in best_lines[:3]:
        line = f"{ln.get('line', 0):.1f}"
        pwin = f"{ln.get('p_win', 0.0) * 100:.0f}"
        ppush = f"{ln.get('p_push', 0.0) * 100:.0f}"
        lines_txt.append(f"Linha {line} → Win {pwin}% | Push {ppush}%")

    bet_link = build_bet365_link(fixture)

    parts = [
        f"📣 {strategy_title}",
        f"🏟 Jogo: {home} x {away}",
        f"⏱ Minuto: {minute_txt} | ⚽ Placar: {score}",
        f"⛳ Cantos: {total_corners} (H:{home_c} - A:{away_c})",
        f"⚡ Ataques: H:{home_att}  A:{away_att} | 🔥 Perigosos: H:{home_d}  A:{away_d}",
        f"🥅 Chutes: H:{home_sh}  A:{away_sh} | 🎯 Posse: H:{home_pos}%  A:{away_pos}%",
        f"📊 Pressão: H:{press_home}  A:{press_away} | 🏟 Estádio pequeno: {stadium_small}",
        "",
        "Top linhas sugeridas (Poisson):",
        *lines_txt,
        "",
        f"🔗 Bet365: {bet_link}",
    ]
    return "\n".join(parts)

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

                # Economia: ignorar partidas muito cedo, com tolerância (18.8)
                minute_raw = fixture.get('fixture', {}).get('status', {}).get('elapsed', 0) or 0
                minute = round(float(minute_raw), 1)

                if minute < 18.8:
                    logger.debug("⏳ Ignorado fixture=%s (min %.1f < 18.8')", fixture_id, minute)
                    continue

                stats_resp = get_fixture_statistics(fixture_id)
                if not stats_resp:
                    logger.debug("Sem estatísticas para fixture=%s no momento.", fixture_id)
                    continue

                home, away = extract_basic_stats(fixture, stats_resp)
                press_home, press_away = pressure_score_vip(home, away)

                total_corners = (home['corners'] or 0) + (away['corners'] or 0)
                total_shots   = (home['shots'] or 0) + (away['shots'] or 0)

                metrics = {
                    'minute': minute,
                    'home_corners': home['corners'], 'away_corners': away['corners'],
                    'home_attacks': home['attacks'], 'away_attacks': away['attacks'],
                    'home_danger': home['danger'],   'away_danger': away['danger'],
                    'home_shots': home['shots'],     'away_shots': away['shots'],
                    'home_pos': home['pos'],         'away_pos': away['pos'],
                    'press_home': press_home,        'press_away': press_away,
                    'small_stadium': (fixture.get('fixture', {}).get('venue', {}).get('name', '').lower() in SMALL_STADIUMS),
                    'total_corners': total_corners,
                    'total_shots': total_shots
                }

                estrategias = verificar_estrategias_vip(fixture, metrics)
                composite_ok = composite_trigger_check(fixture, metrics)

                if not estrategias and not composite_ok:
                    logger.debug("IGNORADO fixture=%s minuto=%s | press(H)=%.2f/A=%.2f | att=%s | dang=%s | shots=%s",
                                 fixture_id, minute, press_home, press_away,
                                 metrics['home_attacks']+metrics['away_attacks'],
                                 metrics['home_danger']+metrics['away_danger'],
                                 total_shots)
                    continue

                # Mensagem Poisson (lam heurístico)
                best_lines = evaluate_candidate_lines(total_corners, lam=1.5)

                # --- Envio das estratégias padrão ---
                if estrategias:
                    for strat_title in estrategias:
                        signal_key = f"{strat_title}_{total_corners}"
                        if should_notify(fixture_id, signal_key):
                            msg = build_vip_message(fixture, strat_title, metrics, best_lines)
                            _tg_send(TELEGRAM_CHAT_ID, msg)
                            signals_sent += 1
                            logger.info("📤 Sinal [%s] fixture=%s minuto=%s", strat_title, fixture_id, minute)

                # --- Envio do setup composto (3/5) ---
                elif composite_ok:
                    strat_title = "Setup 3/5 — Asiáticos/Limite"
                    signal_key = f"{strat_title}_{total_corners}"
                    if should_notify(fixture_id, signal_key):
                        msg = build_vip_message(fixture, strat_title, metrics, best_lines)
                        _tg_send(TELEGRAM_CHAT_ID, msg)
                        signals_sent += 1
                        logger.info("📤 Sinal [3/5 Composite] fixture=%s minuto=%s", fixture_id, minute)

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
        boot_msg = escape_markdown("🤖 Bot VIP ULTRA ativo. Ignorando jogos < 18.8' e usando pressão dinâmica.")
        send_telegram_message(boot_msg)
    except Exception:
        pass

    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
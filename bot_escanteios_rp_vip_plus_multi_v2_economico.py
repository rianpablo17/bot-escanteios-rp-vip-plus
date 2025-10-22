#!/usr/bin/env python3
# -- coding: utf-8 --
"""
bot_escanteios_rp_vip_plus_multi_v2_economico.py
Versão: ULTRA VISUAL VIP+ PRO (com suas regras somadas)
- Mantém SCAN_INTERVAL_BASE = 300s e RENOTIFY_MINUTES = 10
- Estratégias principais baseadas nos seus critérios:
    * Finalizações (shots) somadas >= 6
    * Ataques perigosos (danger) somados >= 20
    * Ataques totais (attacks) somados >= 30
- Janela HT: 30-40
- Janela FT: 75-90
- Bônus: campo pequeno permite thresholds reduzidos
- Evita erros de MarkdownV2, logging detalhado e /status via polling
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

import requests
from flask import Flask, request, jsonify
from datetime import datetime

# ========================= LOG / ENV =========================
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot_escanteios_rp_vip_multi_v2_economico')

API_FOOTBALL_KEY   = os.getenv('API_FOOTBALL_KEY')
TOKEN              = os.getenv('TOKEN')
TELEGRAM_CHAT_ID   = os.getenv('TELEGRAM_CHAT_ID')          # grupo/canal VIP
TELEGRAM_ADMIN_ID  = os.getenv('TELEGRAM_ADMIN_ID')         # opcional: logs privados
SCAN_INTERVAL_BASE = int(os.getenv('SCAN_INTERVAL', '300')) # 300s por padrão
RENOTIFY_MINUTES   = int(os.getenv('RENOTIFY_MINUTES', '10'))

if not API_FOOTBALL_KEY:
    raise ValueError("⚠️ API_FOOTBALL_KEY não definida.")
if not TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("⚠️ Defina TOKEN e TELEGRAM_CHAT_ID.")

# ===================== API CONFIG ===================
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ===================== PARÂMETROS ====================
HT_WINDOW = (30, 40)   # Janela HT
FT_WINDOW = (75, 90)   # Janela FT

# Thresholds solicitados (somados ambos times)
TH_SHOTS_SUM_HTFT = 6     # finalizações somadas mínimas
TH_DANGER_SUM      = 20   # ataques perigosos somados mínimos
TH_ATTACKS_SUM     = 30   # ataques somados mínimos

# Bônus campo pequeno (thresholds reduzidos)
SMALL_STADIUM_THRESHOLDS = {
    'shots': 4,
    'danger': 12,
    'attacks': 20
}

# Anti-spam: {fixture_id: {signal_key: last_ts}}
sent_signals: Dict[int, Dict[str, float]] = defaultdict(dict)

# Diagnóstico de uso
request_count = 0
last_rate_headers: Dict[str, Any] = {}

# ====================== ESCAPE MARKDOWNV2 =====================
MDV2_SPECIALS = r'[_*\[\]\(\)~`>#+\-=|{}.!]'
def escape_markdown(text: Any) -> str:
    s = str(text) if text is not None else ""
    # Replace backslash first to avoid double-escaping
    s = s.replace("\\", "\\\\")
    return re.sub(MDV2_SPECIALS, r'\\\g<0>', s)

# ============================ FLASK ===========================
app = Flask(__name__)

@app.route('/', methods=['GET'])
def root():
    return jsonify({'status': 'ok', 'service': 'Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA',
                    'scan_interval_base': SCAN_INTERVAL_BASE, 'renotify_minutes': RENOTIFY_MINUTES}), 200

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'}), 200

# webhook endpoint (kept but not required if using polling for /status)
@app.route(f'/{TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.get_json(force=True, silent=True) or {}
    logger.debug("Update Telegram (webhook): %s", str(data)[:500])
    return jsonify({"status": "ok"}), 200

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

def safe_request(url: str, headers: Dict[str, str], params: Optional[dict]=None):
    global LAST_REQUEST, request_count, last_rate_headers
    now = time.time()
    elapsed = now - LAST_REQUEST
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    LAST_REQUEST = time.time()
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        request_count += 1
        # capture headers safely
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
def get_live_fixtures() -> List[Dict[str, Any]]:
    """Obtém partidas ao vivo (response list)."""
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

def get_fixture_statistics(fixture_id) -> Optional[List[Dict[str, Any]]]:
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

# ===================== POISSON (mantido para linhas) =====================
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
    'shots':   ['shot', 'shots', 'finalization', 'finalizações', 'finalizacoes', 'chute', 'chutes', 'shots on target'],
    'pos':     ['possession', 'ball possession', 'posse']
}

def extract_value(stat_type: str, t: str, val) -> Optional[int]:
    t_low = t.lower()
    for alias in STAT_ALIASES.get(stat_type, []):
        if alias in t_low:
            try:
                return int(float(str(val).replace('%', '')))
            except Exception:
                return 0
    return None

def extract_basic_stats(fixture: Dict[str, Any], stats_resp: List[Dict[str, Any]]) -> Tuple[Dict[str,int], Dict[str,int]]:
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

# ===================== PRESSURE / STRATEGIES =====================
def is_small_stadium(fixture: Dict[str, Any]) -> bool:
    name = fixture.get('fixture', {}).get('venue', {}).get('name', '') or ''
    return name.strip().lower() in {
        'loftus road','vitality stadium','kenilworth road','turf moor',
        'bramall lane','ewood park','the den','carrow road',
        'bet365 stadium','pride park','liberty stadium','fratton park',
    }

def verificar_estrategias_personalizadas(fixture: Dict[str, Any], metrics: Dict[str, Any]) -> List[str]:
    """
    Estratégias baseadas nos critérios que você enviou (somatórias).
    Retorna lista de títulos que bateram.
    """
    sinais = []
    minuto = metrics['minute']
    total_shots = (metrics.get('home_shots') or 0) + (metrics.get('away_shots') or 0)
    total_danger = (metrics.get('home_danger') or 0) + (metrics.get('away_danger') or 0)
    total_attacks = (metrics.get('home_attacks') or 0) + (metrics.get('away_attacks') or 0)
    total_corners = metrics.get('total_corners') or 0
    small_stadium = metrics.get('small_stadium', False)
    goals_home = fixture.get('goals', {}).get('home', 0) or 0
    goals_away = fixture.get('goals', {}).get('away', 0) or 0
    empate = (goals_home == goals_away)

    # Primary VIP rule (suas thresholds)
    def meets_primary():
        return (total_shots >= TH_SHOTS_SUM_HTFT and
                total_danger >= TH_DANGER_SUM and
                total_attacks >= TH_ATTACKS_SUM)

    # Small stadium fallback (reduz thresholds)
    def meets_small():
        return (total_shots >= SMALL_STADIUM_THRESHOLDS['shots'] and
                total_danger >= SMALL_STADIUM_THRESHOLDS['danger'] and
                total_attacks >= SMALL_STADIUM_THRESHOLDS['attacks'])

    # HT window
    if HT_WINDOW[0] <= minuto <= HT_WINDOW[1]:
        if meets_primary():
            sinais.append("HT — Canto Limite (Pressão Total Somada)")
        elif small_stadium and meets_small():
            sinais.append("HT — Canto Limite (Campo Pequeno — confiança reduzida)")

    # FT window
    if FT_WINDOW[0] <= minuto <= FT_WINDOW[1]:
        if meets_primary():
            sinais.append("FT — Canto Limite (Pressão Total Somada)")
        elif small_stadium and meets_small():
            sinais.append("FT — Canto Limite (Campo Pequeno — confiança reduzida)")

    # Extra: se jogo empatado e altíssimo volume no FT (backup)
    if FT_WINDOW[0] <= minuto <= FT_WINDOW[1] and empate and total_attacks >= (TH_ATTACKS_SUM + 10) and total_danger >= (TH_DANGER_SUM + 10):
        sinais.append("FT — Alta Volumetria Empatado (Pressão Extrema)")

    # Nota: não duplicamos sinais (depois o código remove/filtra por anti-spam)
    return list(dict.fromkeys(sinais))

# ========================= VIP MESSAGE / LINKS =====================
def build_bet365_link(fixture: Dict[str, Any]) -> str:
    home = fixture.get('teams', {}).get('home', {}).get('name', '') or ''
    away = fixture.get('teams', {}).get('away', {}).get('name', '') or ''
    league = fixture.get('league', {}).get('name', '') or ''
    query = f"site:bet365.com {home} x {away} {league}"
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

def build_vip_message(fixture: Dict[str, Any], strategy_title: str, metrics: Dict[str, Any],
                      best_lines: List[Dict[str, float]]) -> str:
    teams = fixture.get('teams', {})
    home_name_raw = teams.get('home', {}).get('name', '?')
    away_name_raw = teams.get('away', {}).get('name', '?')
    home = escape_markdown(home_name_raw)
    away = escape_markdown(away_name_raw)
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

    press_home = metrics.get('press_home', 0.0)
    press_away = metrics.get('press_away', 0.0)
    press_home_md = escape_markdown(f"{press_home:.2f}")
    press_away_md = escape_markdown(f"{press_away:.2f}")

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
        f"📊 Pressão: H:{press_home_md}  A:{press_away_md}  \\|  🏟 Estádio pequeno: {stadium_small}",
        "",
        "Top linhas sugeridas \\(Poisson\\):",
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

# ========================= METRICS / STATUS ==========================
START_TIME = datetime.now()
LAST_SCAN_TIME = None
LAST_API_STATUS = "⏳ Aguardando..."
LAST_RATE_USAGE = "0%"
TOTAL_VARRIDURAS = 0

def atualizar_metricas(loop_total, req_headers):
    global LAST_SCAN_TIME, LAST_API_STATUS, LAST_RATE_USAGE, TOTAL_VARRIDURAS
    LAST_SCAN_TIME = datetime.now()
    TOTAL_VARRIDURAS += 1

    # Headers podem vir em minúsculo: padronizamos chaves
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
        LAST_API_STATUS = "❌ Sem cabeçalhos (erro API)"
        LAST_RATE_USAGE = "Indefinido"

def get_status_message():
    try:
        uptime = datetime.now() - START_TIME
        horas, resto = divmod(uptime.seconds, 3600)
        minutos, _ = divmod(resto, 60)
        jogos = globals().get("total", 0)
        sinais = 0
        api_status = globals().get("LAST_API_STATUS", "✅ OK")
        rate_usage = globals().get("LAST_RATE_USAGE", "0%")
        varridas = globals().get("TOTAL_VARRIDURAS", 0)
        last_scan = globals().get("LAST_SCAN_TIME")
        last_scan = last_scan.strftime("%H:%M:%S") if last_scan else "Ainda não realizada"

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
            "🤖 Versão Multi v2 Econômico ULTRA VISUAL"
        )
        return msg
    except Exception as e:
        return f"❌ Erro ao gerar status: {e}"

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
                # press scores not used directly here (we use sums), but we calculate small metrics
                press_home = 0.0
                press_away = 0.0
                try:
                    # simple proxies (normalized diffs) kept for info
                    att_diff = (home['attacks'] - away['attacks'])
                    danger_diff = (home['danger'] - away['danger'])
                    press_home = max(0.0, min(1.0, 0.25 * (att_diff/10) + 0.45 * (danger_diff/8)))
                    press_away = max(0.0, min(1.0, 0.25 * (-att_diff/10) + 0.45 * (-danger_diff/8)))
                except Exception:
                    pass

                total_corners = (home['corners'] or 0) + (away['corners'] or 0)
                metrics = {
                    'minute': minute,
                    'home_corners': home['corners'], 'away_corners': away['corners'],
                    'home_attacks': home['attacks'], 'away_attacks': away['attacks'],
                    'home_danger': home['danger'],   'away_danger': away['danger'],
                    'home_shots': home['shots'],     'away_shots': away['shots'],
                    'home_pos': home['pos'],         'away_pos': away['pos'],
                    'press_home': press_home,        'press_away': press_away,
                    'small_stadium': is_small_stadium(fixture),
                    'total_corners': total_corners
                }

                # Estratégias baseadas na soma (criterios fornecidos por você)
                estrategias = verificar_estrategias_personalizadas(fixture, metrics)

                if not estrategias:
                    # LOG do motivo principal de ignorar (detalhado)
                    total_shots = (metrics['home_shots'] or 0) + (metrics['away_shots'] or 0)
                    total_danger = (metrics['home_danger'] or 0) + (metrics['away_danger'] or 0)
                    total_attacks = (metrics['home_attacks'] or 0) + (metrics['away_attacks'] or 0)

                    motivos = []
                    if not (HT_WINDOW[0] <= minute <= HT_WINDOW[1] or FT_WINDOW[0] <= minute <= FT_WINDOW[1]):
                        motivos.append("fora_janela")
                    if total_shots < TH_SHOTS_SUM_HTFT:
                        motivos.append(f"shots_baixo({total_shots}<{TH_SHOTS_SUM_HTFT})")
                    if total_danger < TH_DANGER_SUM:
                        motivos.append(f"danger_baixo({total_danger}<{TH_DANGER_SUM})")
                    if total_attacks < TH_ATTACKS_SUM:
                        motivos.append(f"attacks_baixo({total_attacks}<{TH_ATTACKS_SUM})")
                    if metrics['small_stadium']:
                        motivos.append("campo_pequeno_bonus")

                    logger.debug("IGNORADO fixture=%s motivos=%s", fixture_id, " | ".join(motivos) or "sem critério")
                    continue

                # Houve sinais — enviar todos os que passaram
                best_lines = evaluate_candidate_lines(total_corners, lam=1.5)
                for strat_title in estrategias:
                    signal_key = f"{strat_title}_{total_corners}"
                    if should_notify(fixture_id, signal_key):
                        msg = build_vip_message(fixture, strat_title, metrics, best_lines)
                        send_telegram_message(msg)
                        logger.info("📤 Sinal enviado [%s] fixture=%s minuto=%s", strat_title, fixture_id, minute)
                        signals_sent += 1

                        # Log privado resumido (se configurado)
                        short = f"✅ {strat_title} | {fixture.get('teams', {}).get('home', {}).get('name','?')} x {fixture.get('teams', {}).get('away', {}).get('name','?')} | {minute}'"
                        send_admin_message(short)

            # ======= RESUMO DA VARREDURA =======
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

# ======================= COMANDO /STATUS (POLLING) =======================
def monitorar_comandos_telegram():
    """Verifica mensagens recentes do bot para responder /status (polling simples)."""
    logger.info("🧠 Módulo de comandos Telegram iniciado (/status disponível).")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "timeout": 15}, timeout=20)
            data = r.json() if r and r.status_code == 200 else {}
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = (msg.get("text", "") or "").strip().lower()
                chat_id = msg.get("chat", {}).get("id")
                if text == "/status":
                    status_msg = get_status_message()
                    _tg_send(chat_id, status_msg)
        except Exception as e:
            logger.warning("⚠️ Erro ao monitorar comandos Telegram: %s", e)
            time.sleep(5)

# =========================== START ============================
if __name__ == "__main__":
    logger.info("🚀 Iniciando Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA VISUAL")
    try:
        # Mensagem de boot — use escape seguro
        boot_msg = "🤖 Bot VIP ULTRA ativo\\. Ignorando jogos \\< 25' e usando pressão dinâmica \\(soma de indicadores\\)\\."
        send_telegram_message(boot_msg)
        if TELEGRAM_ADMIN_ID:
            send_admin_message("🔐 Logs privados habilitados para ADMIN\\. Vou te avisar dos sinais e motivos de ignorar\\.")
    except Exception:
        pass

    # Inicia loop principal
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()

    # Inicia thread de /status (polling) para responder comandos no grupo/privado
    threading.Thread(target=monitorar_comandos_telegram, daemon=True).start()

    # Roda Flask (para health & webhook se quiser)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
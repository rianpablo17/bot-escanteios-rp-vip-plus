#!/usr/bin/env python3
# -- coding: utf-8 --
"""
Bot Escanteios RP VIP Plus — Multi v2 (Econômico) • ULTRA Sensível v3.1
- Envia no máximo 1 sinal por período (HT e FT)
- Minuto suavizado e consumo otimizado
- Mantém TODAS as estratégias e layout VIP
- Agora mais sensível: 2 estratégias no HT, 3 no FT, composite 2/5
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

# ===================== STATUS ======================
START_TIME = int(time.time())
LAST_SCAN_TIME: Optional[datetime] = None
LAST_API_STATUS = "⏳ Aguardando..."
LAST_RATE_USAGE = "0%"
TOTAL_VARRIDURAS = 0
total = 0

# ===================== API CONFIG ============================
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ===================== PARÂMETROS ============================
HT_WINDOW = (29.8, 42)
FT_WINDOW = (69.8, 93)
MIN_PRESSURE_SCORE = 0.18
ATTACKS_MIN_SUM    = 10
DANGER_MIN_SUM     = 5
MIN_TOTAL_SHOTS    = 4

SMALL_STADIUMS = {
    'loftus road','vitality stadium','kenilworth road','turf moor',
    'bramall lane','ewood park','the den','carrow road',
    'bet365 stadium','pride park','liberty stadium','fratton park',
}

sent_signals: Dict[int, Dict[str, float]] = defaultdict(dict)
sent_period: Dict[int, set] = defaultdict(set)
last_elapsed_seen: Dict[int, float] = {}
no_stats_backoff_until: Dict[int, float] = {}
request_count = 0
last_rate_headers: Dict[str, str] = {}

MDV2_SPECIALS = r'[_*\[\]()~`>#+\-=|{}.!]'
def escape_markdown(text: Any) -> str:
    s = str(text) if text is not None else ""
    return re.sub(MDV2_SPECIALS, lambda m: "\\" + m.group(0), s)

# ============================ FLASK ===========================
app = Flask(__name__)

@app.route('/', methods=['GET'])
def root():
    return jsonify({'status': 'ok', 'service': 'Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA Sensível v3.1'}), 200

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
                "🤖 Versão: Multi v2 Econômico ULTRA Sensível v3.1"
            )
            send_telegram_message_plain(resposta)
    except Exception as e:
        logger.exception("❌ Erro no webhook: %s", e)
    return jsonify({"ok": True}), 200

# ====================== TELEGRAM HELPERS ======================
def _tg_send(chat_id: str, text: str, parse_mode: Optional[str] = None) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        logger.exception("Erro Telegram: %s", e)

def send_telegram_message_plain(text: str) -> None:
    _tg_send(TELEGRAM_CHAT_ID, text, None)

# ===================== API CALLS =====================
def safe_request(url: str, headers: Dict[str, str], params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    global request_count, last_rate_headers
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        request_count += 1
        last_rate_headers = {
            'x-ratelimit-minutely-remaining': response.headers.get('x-ratelimit-minutely-remaining'),
            'x-ratelimit-minutely-limit': response.headers.get('x-ratelimit-minutely-limit')
        }
        if response.status_code == 200:
            return response.json()
        return None
    except Exception:
        return None

def get_live_fixtures() -> List[Dict[str, Any]]:
    url = f"{API_BASE}/fixtures"
    data = safe_request(url, HEADERS, {"live": "all"})
    return data.get("response", []) if data else []

def get_fixture_statistics(fixture_id: int) -> Optional[List[Dict[str, Any]]]:
    now = time.time()
    if fixture_id in no_stats_backoff_until and now < no_stats_backoff_until[fixture_id]:
        return None
    data = safe_request(f"{API_BASE}/fixtures/statistics", HEADERS, {"fixture": fixture_id})
    if not data:
        no_stats_backoff_until[fixture_id] = now + 60   # reduzido de 240s para 60s
        return None
    stats = data.get("response", [])
    if not stats:
        no_stats_backoff_until[fixture_id] = now + 60
        return None
    return stats

# ===================== STATS EXTRAÇÃO =====================
STAT_ALIASES = {
    'corners': ['corner','corners'],
    'attacks': ['attack','attacks'],
    'danger':  ['dangerous attack','dangerous attacks'],
    'shots':   ['shot','shots','total shots','shots on target'],
    'pos':     ['possession','ball possession']
}

def extract_value(stat_type: str, stat_label: str, value) -> Optional[int]:
    stat_label = (stat_label or '').lower()
    for alias in STAT_ALIASES.get(stat_type, []):
        if alias in stat_label:
            try:
                return int(float(str(value).replace('%', '').strip()))
            except:
                return 0
    return None

def extract_basic_stats(fixture: Dict[str, Any], stats_resp: List[Dict[str, Any]]):
    teams = fixture.get('teams', {})
    home_id = teams.get('home', {}).get('id')
    away_id = teams.get('away', {}).get('id')
    home = {'corners':0,'attacks':0,'danger':0,'shots':0,'pos':50}
    away = {'corners':0,'attacks':0,'danger':0,'shots':0,'pos':50}
    for entry in stats_resp or []:
        team_id = entry.get('team', {}).get('id')
        stats_list = entry.get('statistics', []) or []
        target = home if team_id == home_id else away if team_id == away_id else None
        if not target: continue
        for s in stats_list:
            for key in STAT_ALIASES.keys():
                v = extract_value(key, s.get('type',''), s.get('value'))
                if v is not None:
                    target[key] = v
                    break
    return home, away

# ===================== PRESSURE =====================
def pressure_score_vip(home: Dict[str,int], away: Dict[str,int]) -> Tuple[float,float]:
    def norm(x,a): return max(0.0, min(1.0, x/float(a))) if a else 0.0
    if (home['attacks']+away['attacks'])<1 or (home['danger']+away['danger'])<1:
        return 0.0,0.0
    h = (0.25*norm(home['attacks']-away['attacks'],10)+
         0.45*norm(home['danger']-away['danger'],8)+
         0.20*norm(home['shots']-away['shots'],4)+
         0.10*norm(home['pos']-away['pos'],20))
    a = (0.25*norm(away['attacks']-home['attacks'],10)+
         0.45*norm(away['danger']-home['danger'],8)+
         0.20*norm(away['shots']-home['shots'],4)+
         0.10*norm(away['pos']-home['pos'],20))
    return h,a

# ======================= ESTRATÉGIAS =======================
def verificar_estrategias_vip(fixture: Dict[str,Any], metrics: Dict[str,Any]):
    estrategias = []
    minuto = metrics['minute']
    total_cantos = metrics['total_corners']
    home_g = fixture.get('goals', {}).get('home', 0)
    away_g = fixture.get('goals', {}).get('away', 0)
    press_home = metrics['press_home']
    press_away = metrics['press_away']

    # Estratégias principais (inalteradas)
    if HT_WINDOW[0] <= minuto <= HT_WINDOW[1] and home_g == away_g and press_home >= MIN_PRESSURE_SCORE:
        estrategias.append("HT - Casa Empatando")
    if 70 <= minuto <= 86.8 and home_g < away_g and press_home >= MIN_PRESSURE_SCORE:
        estrategias.append("FT - Reação da Casa")
    if 70 <= minuto <= 88.8 and max(press_home, press_away) >= MIN_PRESSURE_SCORE and total_cantos <= 8:
        estrategias.append("FT - Over Cantos 2º Tempo")
    if metrics['small_stadium'] and max(press_home, press_away) >= MIN_PRESSURE_SCORE and 25 <= minuto <= 89.8:
        estrategias.append("Campo Pequeno + Pressão")
    if minuto >= 30 and press_home >= 0.30 and press_away >= 0.30:
        estrategias.append("Jogo Aberto (Ambos pressionam)")
    if 35 <= minuto <= 79.8:
        if press_home > press_away + 0.10 and home_g < away_g:
            estrategias.append("Favorito em Perigo (Casa)")
        if press_away > press_home + 0.10 and away_g < home_g:
            estrategias.append("Favorito em Perigo (Fora)")

    # Composite 2/5 — mais sensível
    cond_attacks  = (metrics['home_attacks'] + metrics['away_attacks']) >= ATTACKS_MIN_SUM
    cond_danger   = (metrics['home_danger']  + metrics['away_danger'])  >= DANGER_MIN_SUM
    cond_pressure = max(metrics['press_home'], metrics['press_away'])   >= MIN_PRESSURE_SCORE
    cond_score    = ((home_g == away_g) or
                    (metrics['press_home'] > metrics['press_away'] and home_g < away_g) or
                    (metrics['press_away'] > metrics['press_home'] and away_g < home_g))
    cond_window   = (HT_WINDOW[0] <= minuto <= HT_WINDOW[1]) or (FT_WINDOW[0] <= minuto <= FT_WINDOW[1])
    true_count = sum([cond_attacks, cond_danger, cond_pressure, cond_score, cond_window])
    composite_ok = true_count >= 2   # antes era 3

    return estrategias, composite_ok

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
    logger.info("🔁 Loop econômico iniciado (v3.1).")
    global total
    signals_sent = 0
    while True:
        try:
            fixtures = get_live_fixtures()
            total = len(fixtures)
            if total == 0:
                time.sleep(SCAN_INTERVAL_BASE)
                continue
            for fixture in fixtures:
                fid = fixture.get('fixture', {}).get('id')
                if not fid: continue
                minute = float(fixture.get('fixture', {}).get('status', {}).get('elapsed', 0) or 0)
                if minute < 18.8: continue
                period = "HT" if minute <= 45 else "FT"
                if period in sent_period[fid]: continue
                stats = get_fixture_statistics(fid)
                if not stats: continue
                home, away = extract_basic_stats(fixture, stats)
                press_home, press_away = pressure_score_vip(home, away)
                metrics = {
                    'minute': minute,
                    'home_corners': home['corners'],'away_corners': away['corners'],
                    'home_attacks': home['attacks'],'away_attacks': away['attacks'],
                    'home_danger': home['danger'],'away_danger': away['danger'],
                    'home_shots': home['shots'],'away_shots': away['shots'],
                    'home_pos': home['pos'],'away_pos': away['pos'],
                    'press_home': press_home,'press_away': press_away,
                    'small_stadium': (fixture.get('fixture', {}).get('venue', {}).get('name','').lower() in SMALL_STADIUMS),
                    'total_corners': (home['corners']+away['corners'])
                }
                estrategias, composite_ok = verificar_estrategias_vip(fixture, metrics)
                limite_estrategias = 2 if minute <= 45 else 3  # menos rigoroso
                signal_key = f"{period}{len(estrategias)}"
                if (len(estrategias) >= limite_estrategias or composite_ok) and should_notify(fid, signal_key):
                    msg = f"📣 Alerta Estratégia {period}\n" \
                          f"{fixture.get('teams',{}).get('home',{}).get('name')} x " \
                          f"{fixture.get('teams',{}).get('away',{}).get('name')} — {len(estrategias)} estratégias\n" \
                          f"⏱️ Minuto {minute:.0f} | Pressões: {press_home:.2f}/{press_away:.2f}"
                    send_telegram_message_plain(msg)
                    signals_sent += 1
                    sent_period[fid].add(period)
            time.sleep(SCAN_INTERVAL_BASE)
        except Exception as e:
            logger.exception("Erro loop: %s", e)
            time.sleep(SCAN_INTERVAL_BASE)

# =========================== START ============================
if __name__ == "__main_ _":
    logger.info("🚀 Iniciando Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA Sensível v3.1")
    try:
        boot_msg = "🤖 Bot VIP ULTRA ativo. Ignorando jogos < 18.8', 1 sinal por período e minuto suavizado."
        send_telegram_message_plain(boot_msg)
    except Exception:
        pass

    # Executa o loop principal em paralelo ao servidor Flask (Render)
    t = threading.Thread(target=main_loop, daemon=True)
    t.start()

    # Mantém o servidor web ativo para healthcheck e /status
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
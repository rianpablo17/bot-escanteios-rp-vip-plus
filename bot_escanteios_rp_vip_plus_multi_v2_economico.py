#!/usr/bin/env python3
# -- coding: utf-8 --
"""
Bot Escanteios RP VIP Plus — Multi v2 (Econômico) • ULTRA Sensível v3.2.2 (NASA)
- Envia no máximo 1 sinal por período (HT e FT) = 2 por jogo
- Minuto suavizado (não retrocede, nem salta muito)
- Backoff quando a API não retorna estatísticas (economia de cota)
- Mantém TODAS as estratégias e layout VIP sem Poisson
- /status e /debug via webhook do Telegram

ENV:
- API_FOOTBALL_KEY, TOKEN, TELEGRAM_CHAT_ID, (opcional) TELEGRAM_ADMIN_ID
- SCAN_INTERVAL (default 45), RENOTIFY_MINUTES (default 3)
"""

import os
import re
import time
import math
import logging
import threading
import urllib.parse
from collections import defaultdict, Counter
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, date
import pytz
import csv
import html

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
SCAN_INTERVAL_BASE = int(os.getenv('SCAN_INTERVAL', '45'))   # ⇦ default agora 45s
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
HT_WINDOW = (29.8, 42.0)   # Janela HT
FT_WINDOW = (69.8, 93.0)   # Janela FT

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

# ====================== ESCAPE HTML / MD =====================
MDV2_SPECIALS = r'[_*\[\]()~`>#+\-=|{}.!]'

def escape_markdown(text: Any) -> str:
    s = str(text) if text is not None else ""
    return re.sub(MDV2_SPECIALS, lambda m: "\\" + m.group(0), s)

def _html(txt: Any) -> str:
    return html.escape(str(txt), quote=False)

# ============================ FLASK ===========================
app = Flask(__name__)

@app.route('/', methods=['GET'])
def root():
    return jsonify({
        'status': 'ok',
        'service': 'Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA Sensível v3.2.2 (NASA)',
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

        # ====================== COMANDOS TELEGRAM ======================
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
                "🤖 Versão: Multi v2 Econômico ULTRA Sensível v3.2.2 (NASA)"
            )
            send_telegram_message_plain(resposta, parse_mode="HTML")

        elif text == '/debug':
            resposta = (
                "🧩 Modo Debug\n"
                f"📦 Requests enviados: {request_count}\n"
                f"⏱ Intervalo base: {SCAN_INTERVAL_BASE}s\n"
                f"📡 Headers API: {last_rate_headers}"
            )
            send_telegram_message_plain(resposta, parse_mode="HTML")

        elif text == '/relatorio':
            gerar_relatorio_diario()
            logger.info("📊 Relatório diário solicitado via Telegram.")

        elif text == '/start':
            send_telegram_message_plain(
                "🤖 Bot Escanteios RP VIP+ ativo!\n\n"
                "📊 Use /relatorio para ver o desempenho do dia.\n"
                "⚙️ Use /status para ver o estado do bot.",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.exception("❌ Erro no processamento do webhook: %s", e)

    return jsonify({"ok": True}), 200

# ====================== TELEGRAM HELPERS ======================
def _tg_send(chat_id: str, text: str, parse_mode: Optional[str] = None, disable_web_page_preview: bool = True) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": disable_web_page_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.warning(f"Telegram resposta {r.status_code}: {r.text}")
            # fallback sem parse_mode
            fallback_payload = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": True}
            requests.post(url, json=fallback_payload, timeout=20)
    except Exception as e:
        logger.exception("Erro ao enviar mensagem para o Telegram: %s", e)

def send_telegram_message(text: str, parse_mode: str = "MarkdownV2") -> None:
    _tg_send(TELEGRAM_CHAT_ID, text, parse_mode=parse_mode, disable_web_page_preview=True)

def send_telegram_message_plain(text: str, parse_mode: Optional[str] = None) -> None:
    _tg_send(TELEGRAM_CHAT_ID, text, parse_mode=parse_mode, disable_web_page_preview=True)

def send_admin_message(text: str) -> None:
    if TELEGRAM_ADMIN_ID:
        _tg_send(TELEGRAM_ADMIN_ID, text, parse_mode="HTML", disable_web_page_preview=True)

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
        now = time.time()
        if fixture_id in no_stats_backoff_until and now < no_stats_backoff_until[fixture_id]:
            return None
        url = f"{API_BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        data = safe_request(url, headers=HEADERS, params=params)
        if not data:
            no_stats_backoff_until[fixture_id] = now + 45  # backoff acelerado
            logger.warning("⚠️ Stats sem resposta. Backoff 45s para fixture=%s", fixture_id)
            return None
        stats = data.get("response", [])
        if not stats:
            no_stats_backoff_until[fixture_id] = now + 45
            logger.debug("Sem estatísticas para fixture=%s (backoff 45s).", fixture_id)
            return None
        return stats
    except Exception as e:
        logger.exception("Erro em get_fixture_statistics: %s", e)
        return None

# ===================== EXTRACT STATS =====================
STAT_ALIASES = {
    'corners': ['corner', 'corners'],
    'attacks': ['attack', 'attacks'],
    'danger':  ['dangerous attack', 'dangerous attacks'],
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

    # Composite 2/5
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

    composite_ok = true_count >= 2
    logger.debug("🧩 Composite 2/5: A=%s | D=%s | P=%s | Placar=%s | Janela=%s → %d/5 | Estratégias=%d",
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

# ===================== VIP MESSAGE HELPERS =====================
def build_bet365_link(fixture: Dict[str, Any]) -> str:
    """
    Gera link direto para Bet365 com o nome do time pesquisável.
    Exemplo: https://www.bet365.com/?q=Sporting
    """
    home = (fixture.get("teams", {}) or {}).get("home", {}).get("name", "") or ""
    # Usa o nome do time da casa para gerar o link direto
    query = urllib.parse.quote_plus(home)
    return f"https://www.bet365.com/?q={query}"

def _periodo_e_tempo(fixture: Dict[str,Any]) -> Tuple[str,str]:
    """
    Usa status.short da API para rotular (HT/FT) e formata tempo como "44' (1ºT)" / "80' (2ºT)".
    """
    status = (fixture.get('fixture',{}) or {}).get('status',{}) or {}
    short = (status.get('short') or '').upper()
    elapsed = status.get('elapsed') or 0

    if short in ('1H','HT'):
        periodo = 'HT'
        tempo_fmt = f"{int(elapsed)}' (1ºT)"
    elif short in ('2H','ET','P'):
        periodo = 'FT'
        tempo_fmt = f"{int(elapsed)}' (2ºT)"
    else:
        # Fallback suave
        periodo = 'HT' if (isinstance(elapsed,int) and elapsed<=45) else 'FT'
        tempo_fmt = f"{int(elapsed)}' (1ºT)" if periodo=='HT' else f"{int(elapsed)}' (2ºT)"
    return periodo, tempo_fmt

# ===================== VIP NASA: COLETA COMPLETA =====================
def _read_json_fast(url: str, headers: dict, timeout=8) -> dict:
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return {}
    except Exception:
        return {}

def _is_probably_reserve_or_uX(league_name: str) -> bool:
    if not league_name:
        return False
    ln = league_name.lower()
    bad = ["u21", "u20", "u19", "reserva", "reserve", "amistoso", "friendly", "women", "sub-"]
    return any(t in ln for t in bad)

def coletar_dados_completos_vip_nasa(fixture_id: int, headers: dict, api_base: str) -> Dict[str, Any]:
    """
    Coleta dados premium:
    - Odds 1x2 (bookmaker=8 Bet365), Standings (home_rank/away_rank), Posse e Cantos (live),
      Acréscimos via events, Selo de verificação e filtro de liga.
    """
    out = {
        "home_rank": "–", "away_rank": "–",
        "odds_home": "-", "odds_draw": "-", "odds_away": "-",
        "home_posse": "?", "away_posse": "?",
        "home_corners": "?", "away_corners": "?",
        "injury_time": "?",
        "dados_verificados": False,
        "liga_confiavel": True,
        "bookmaker_ok": False
    }

    fx = _read_json_fast(f"{api_base}/fixtures?id={fixture_id}", headers)
    if not fx.get("response"):
        return out
    match = fx["response"][0]
    league = match.get("league", {}) or {}
    league_id = league.get("id")
    league_name = league.get("name", "")
    season = league.get("season")

    teams = match.get("teams", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    home_id = home.get("id"); away_id = away.get("id")

    out["liga_confiavel"] = not _is_probably_reserve_or_uX(league_name)

    # Odds (Bet365 = bookmaker 8)
    odds = _read_json_fast(f"{api_base}/odds?fixture={fixture_id}&bookmaker=8", headers)
    try:
        if odds.get("response"):
            book = odds["response"][0]["bookmakers"][0]
            if book.get("id") == 8 or "bet365" in (book.get("name","").lower()):
                bets = book["bets"][0]["values"]
                out["odds_home"] = bets[0].get("odd","-")
                out["odds_draw"] = bets[1].get("odd","-")
                out["odds_away"] = bets[2].get("odd","-")
                out["bookmaker_ok"] = True
    except Exception:
        pass

    # Standings → home_rank / away_rank
    if league_id and season:
        st = _read_json_fast(f"{api_base}/standings?league={league_id}&season={season}", headers)
        try:
            groups = st["response"][0]["league"]["standings"]
            for table in groups:
                for row in table:
                    tid = row["team"]["id"]
                    if tid == home_id: out["home_rank"] = f"{row['rank']}º"
                    if tid == away_id: out["away_rank"] = f"{row['rank']}º"
        except Exception:
            pass

    # Estatísticas live (posse + cantos por lado)
    st_live = _read_json_fast(f"{api_base}/fixtures/statistics?fixture={fixture_id}", headers)
    try:
        if st_live.get("response"):
            h_c, a_c = None, None
            for team_stat in st_live["response"]:
                tid = (team_stat.get("team") or {}).get("id")
                for s in team_stat.get("statistics", []):
                    t = (s.get("type") or "").strip()
                    v = s.get("value")
                    if t == "Ball Possession" and isinstance(v, str):
                        val = v.replace("%","").strip()
                        if tid == home_id: out["home_posse"] = val
                        elif tid == away_id: out["away_posse"] = val
                    if t in ("Corner Kicks","Corners") and isinstance(v,int):
                        if tid == home_id: h_c = v
                        elif tid == away_id: a_c = v
            if h_c is not None: out["home_corners"] = str(h_c)
            if a_c is not None: out["away_corners"] = str(a_c)
    except Exception:
        pass

    # Events → acréscimos
    ev = _read_json_fast(f"{api_base}/fixtures/events?fixture={fixture_id}", headers)
    try:
        extra_max = None
        for e in ev.get("response", []):
            ex = (e.get("time") or {}).get("extra")
            if isinstance(ex, int):
                extra_max = ex if extra_max is None else max(extra_max, ex)
        if extra_max is not None:
            out["injury_time"] = f"{extra_max}'"
    except Exception:
        pass

    # Selo verificado
    if (out["bookmaker_ok"] or out["home_rank"]!="–" or out["away_rank"]!="–") and \
       (out["home_posse"]!="?" or out["home_corners"]!="?" or out["away_corners"]!="?"):
        out["dados_verificados"] = True

    return out

def formatar_mensagem_vip_nasa(match: Dict[str,Any], estrategias: list, st: Dict[str,Any]) -> str:
    home = match['teams']['home']['name']
    away = match['teams']['away']['name']
    league = match['league']['name']
    placar_home = match['goals']['home']; placar_away = match['goals']['away']

    # período/tempo formatado a partir do status
    periodo, tempo_fmt = _periodo_e_tempo(match)

    estrategias_txt = " • ".join(estrategias) if estrategias else "Setup válido (2/5)"
    cpb = "https://cornerprobet.com/analysis/"
    bet = build_bet365_link(match)

    selo = "✅" if st.get("dados_verificados") else "⚠️"
    liga_ok = st.get("liga_confiavel", True)
    liga_txt = "" if liga_ok else " (⚠️ verifique: pode ser reservas/U21)"

    hc = st.get("home_corners", "?")
    ac = st.get("away_corners", "?")  
 
# === acréscimos inteligentes ===
events = get_fixture_events(fixture_id)
injury_time_est = estimate_injury_time(events)
st['injury_time'] = injury_time_est

    msg = f"""
📣 <b>Alerta Estratégia: Asiáticos/Limite - {periodo}</b> 📣
🏟 <b>Jogo:</b> {_html(home)} ({_html(st.get('home_rank','–'))}) x ({_html(st.get('away_rank','–'))}) {_html(away)}
🏆 <b>Competição:</b> {_html(league)}{liga_txt}
🕛 <b>Tempo:</b> {_html(tempo_fmt)}
⚽ <b>Placar:</b> {_html(placar_home)} x {_html(placar_away)}
⛳ <b>Cantos:</b> {_html(hc)} - {_html(ac)}
💥 <b>Posse de Bola:</b> {_html(st.get('home_posse','?'))}% / {_html(st.get('away_posse','?'))}%
⌚ <b>Possíveis acréscimos:</b> {_html(st.get('injury_time','?'))}
📈 <b>Odds 1x2 (pré):</b> {_html(st.get('odds_home','-'))} / {_html(st.get('odds_draw','-'))} / {_html(st.get('odds_away','-'))}

📌 <b>Estratégias ativas:</b> {_html(estrategias_txt)}
🧠 <i>Dados verificados via API-PRO</i> {selo}

🔗 <a href="{cpb}">CornerProBet</a> • <a href="{bet}">Bet365</a>

🚀 <b>Sinal VIP ULTRA PRO NASA ATIVO!</b>
""".strip()
    return msg

def estimate_injury_time(events: List[Dict[str, Any]]) -> str:
    """
    Estima acréscimos entre 1 e 6 minutos conforme a quantidade de eventos.
    """
    try:
        if not events:
            return "–"
        count = len(events)
        # Cálculo simples: quanto mais eventos, maior o acréscimo (máximo 6')
        est = min(6, max(1, count // 5 + 1))
        return f"{est}'"
    except Exception:
        return "–"

# ======================FUNÇÃO DE MENSAGEM VIP (MESMO NOME)=====================
def build_signal_message_vip(match, estrategias, stats):
    """
    Monta a mensagem VIP NASA (HTML) com dados completos.
    - Coleta enriquecida on-demand (rápido e econômico)
    - Mescla sem quebrar valores já calculados no seu 'stats'
    - À prova de erro 400 no Telegram
    """
    try:
        fixture_id = match["fixture"]["id"]
        enriched = coletar_dados_completos_vip_nasa(fixture_id, HEADERS, API_BASE)

        # Mescla com prioridade adequada (protege rank e dados enriquecidos)
        full = dict(enriched)
        for k, v in (stats or {}).items():
            if k in ("home_rank","away_rank"):  # ranking vem da standings, não sobrescrever
                continue
            if v not in (None, "", "?", "-"):
                full[k] = v

        return formatar_mensagem_vip_nasa(match, estrategias, full)

    except Exception as e:
        try:
            return formatar_mensagem_vip_nasa(match, estrategias, stats or {})
        except Exception:
            return f"<b>Alerta Estratégia:</b> falha ao montar mensagem ({_html(e)})"

# ========================= UTIL: MINUTO/PERÍODO =========================
def get_period_by_window(minute: float) -> Optional[str]:
    """Retorna 'HT' se dentro da janela HT, 'FT' se dentro da janela FT, ou None se fora de ambas."""
    if HT_WINDOW[0] <= minute <= HT_WINDOW[1]:
        return "HT"
    if FT_WINDOW[0] <= minute <= FT_WINDOW[1]:
        return "FT"
    return None

def smooth_minute(fixture_id: int, raw: float) -> float:
    """Garante minuto não regressivo e sem saltos >5 entre varreduras."""
    raw = float(raw or 0.0)
    prev = last_elapsed_seen.get(fixture_id, 0.0)
    if raw < prev:          # não retrocede
        raw = prev
    if raw - prev > 5.0:    # evita saltos muito grandes
        raw = prev + 5.0
    raw = max(0.0, min(95.0, raw))  # clamp
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

# ========================== RELATÓRIO DE PERFORMANCE ==========================
RELATORIO_PATH = "relatorio.csv"

def registrar_sinal(fixture: dict, estrategias: list, resultado: str = "⏳") -> None:
    teams = fixture.get("teams", {}) or {}
    home_team = (teams.get("home", {}) or {}).get("name", "?")
    away_team = (teams.get("away", {}) or {}).get("name", "?")
    with open(RELATORIO_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            date.today().isoformat(),
            datetime.now().strftime("%H:%M"),
            f"{home_team} x {away_team}",
            ",".join(estrategias) if estrategias else "Nenhuma",
            resultado
        ])

def atualizar_resultado(jogo: str, resultado: str):
    linhas = []
    with open(RELATORIO_PATH, "r", encoding="utf-8") as f:
        linhas = [linha.strip().split(",") for linha in f.readlines()]
    for linha in linhas:
        if jogo.lower() in linha[2].lower():
            linha[-1] = resultado
    with open(RELATORIO_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(linhas)

def gerar_relatorio_diario():
    try:
        with open(RELATORIO_PATH, "r", encoding="utf-8") as f:
            rows = [r.strip().split(",") for r in f.readlines()]
    except FileNotFoundError:
        send_telegram_message("📊 Nenhum dado disponível ainda no relatório.")
        return

    hoje = date.today().isoformat()
    registros = [r for r in rows if r and r[0] == hoje]
    if not registros:
        send_telegram_message("📊 Nenhum sinal registrado hoje ainda.")
        return

    total_rel = len(registros)
    greens = sum(1 for r in registros if "✅" in r[-1])
    reds = sum(1 for r in registros if "❌" in r[-1])
    pendentes = total_rel - greens - reds
    eficiencia = (greens / total_rel * 100) if total_rel else 0

    estrategias = [e for r in registros for e in r[3].split(",") if e.strip() not in ["Nenhuma", ""]]
    mais_frequentes = Counter(estrategias).most_common(1)
    melhor_estrategia = mais_frequentes[0][0] if mais_frequentes else "—"

    msg = (
        f"📊 Relatório de Performance — Bot Escanteios RP VIP+\n"
        f"🗓️ Período: {datetime.now().strftime('%d/%m/%Y')}\n"
        f"📈 Total de Sinais: {total_rel}\n"
        f"✅ Greens: {greens} ({(greens/total_rel*100):.0f}%)\n"
        f"❌ Reds: {reds} ({(reds/total_rel*100):.0f}%)\n"
        f"⏳ Pendentes: {pendentes}\n"
        f"⚙️ Eficiência Média: {eficiencia:.1f}%\n"
        f"💡 Melhor Estratégia: {melhor_estrategia}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Continue operando no modo VIP — rumo aos 80%+ de acerto!"
    )
    send_telegram_message_plain(msg, parse_mode="HTML")

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

                fixture_info = fixture.get("fixture", {}) or {}
                fixture_status = fixture_info.get("status", {}) or {}
                status_short = (fixture_status.get("short") or "")
                minute_real = fixture_status.get("elapsed", 0) or 0

                if status_short not in ["1H", "2H", "HT"]:  # aceitar HT (intervalo) para formatação, mas não enviaremos fora da janela
                    logger.debug(f"⏩ Ignorando fixture={fixture_id} — status inválido: {status_short}")
                    continue

                if minute_real < 18.8:
                    logger.debug(f"⏳ Ignorado fixture={fixture_id} (min {minute_real:.1f} < 18.8')")
                    continue

                minute = smooth_minute(fixture_id, float(minute_real))

                # 🚫 NOVO: só seguimos se estiver DENTRO de uma janela
                period_by_window = get_period_by_window(minute)
                if period_by_window is None:
                    logger.debug(f"⏩ Ignorado fixture={fixture_id} — fora da janela (min {minute:.1f}).")
                    continue

                period = period_by_window  # 'HT' ou 'FT' pela janela (não pelo minuto simples)

                if period in sent_period[fixture_id]:
                    logger.debug(f"🔒 Já sinalizado neste período {period} (fixture={fixture_id}). Pulando.")
                    continue

                stats_resp = get_fixture_statistics(fixture_id)
                if not stats_resp:
                    logger.debug(f"Sem estatísticas para fixture={fixture_id} no momento.")
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
                    logger.debug(f"IGNORADO fixture={fixture_id} minuto={minute:.1f} | press(H)={press_home:.2f}/A={press_away:.2f}")
                    continue

                limite_estrategias = 2 if period == "HT" else 3
                signal_key = f"{period}{len(estrategias)}{total_corners}"

                # 💬 Envio do Sinal
                if (len(estrategias) >= limite_estrategias or composite_ok) and should_notify(fixture_id, signal_key):
                    try:
                        msg = build_signal_message_vip(fixture, estrategias, metrics)
                        send_telegram_message_plain(msg, parse_mode="HTML")
                        registrar_sinal(fixture, estrategias, "⏳")
                        signals_sent += 1
                        sent_period[fixture_id].add(period)
                        logger.info(f"📤 Sinal enviado ({period}): {len(estrategias)} estratégias fixture={fixture_id} min={minute:.1f}")
                    except Exception as e:
                        logger.error(f"❌ Erro ao enviar sinal: {e}")
                else:
                    logger.debug(f"❌ Estratégias insuficientes ({len(estrategias)}). Aguardando próximo tick...")

            try:
                logger.info(f"📊 Resumo: {total} jogos analisados | {signals_sent} sinais enviados | próxima em {scan_interval}s")
                atualizar_metricas(total, last_rate_headers)
                signals_sent = 0
            except Exception as e:
                logger.exception(f"Erro ao finalizar resumo da varredura: {e}")

            time.sleep(scan_interval)

        except Exception as e:
            logger.exception(f"Erro no loop principal: {e}")
            time.sleep(SCAN_INTERVAL_BASE)

# =========================== START ============================
if __name__ == "__main__":
    logger.info("🚀 Iniciando Bot Escanteios RP VIP Plus — Multi v2 (Econômico) ULTRA Sensível v3.2.2 (NASA)")
    try:
        boot_msg = ("🤖 Bot VIP ULTRA ativo. Janela HT 29.8–42 | FT 69.8–93. "
                    "Minuto suavizado, 1 sinal/tempo, enriquecimento NASA habilitado. "
                    f"Scan {SCAN_INTERVAL_BASE}s.")
        send_telegram_message_plain(boot_msg)
    except Exception:
        pass

    t = threading.Thread(target=main_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), debug=False)
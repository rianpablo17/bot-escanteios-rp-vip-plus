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
SCAN_INTERVAL_BASE = int(os.getenv('SCAN_INTERVAL', '120'))  # ← 120s por padrão
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

# ====================== ESCAPE HTML =====================
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

        # ====================== COMANDOS TELEGRAM ======================

        # 📊 /status -> informações do bot
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
            send_telegram_message_plain(resposta, parse_mode="HTML")

        # 🧩 /debug -> diagnóstico técnico
        elif text == '/debug':
            resposta = (
                "🧩 Modo Debug\n"
                f"📦 Requests enviados: {request_count}\n"
                f"⏱ Intervalo base: {SCAN_INTERVAL_BASE}s\n"
                f"📡 Headers API: {last_rate_headers}"
            )
            send_telegram_message_plain(resposta, parse_mode="HTML")

        # 📈 /relatorio -> gera painel de performance VIP
        elif text == '/relatorio':
            gerar_relatorio_diario()
            logger.info("📊 Relatório diário solicitado via Telegram.")

        # 🟢 /start -> mensagem de boas-vindas
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
    payload = {
        "chat_id": chat_id,
        "text": str(text),
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            logger.warning(f"Telegram resposta {r.status_code}: {r.text}")
            # fallback sem parse_mode
            fallback_payload = {
                "chat_id": chat_id,
                "text": str(text),
                "disable_web_page_preview": True,
            }
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
        # Backoff (economia): se falhou recentemente, aguarda
        now = time.time()
        if fixture_id in no_stats_backoff_until and now < no_stats_backoff_until[fixture_id]:
            return None

        url = f"{API_BASE}/fixtures/statistics"
        params = {"fixture": fixture_id}
        data = safe_request(url, headers=HEADERS, params=params)

        if not data:
            # ajuste v3.1 -> backoff 90s (antes 240s)
            no_stats_backoff_until[fixture_id] = now + 90
            logger.warning("⚠️ Stats sem resposta. Backoff 90s para fixture=%s", fixture_id)
            return None

        stats = data.get("response", [])
        if not stats:
            # ajuste v3.1 -> backoff curto 90s (antes 180s)
            no_stats_backoff_until[fixture_id] = now + 90
            logger.debug("Sem estatísticas para fixture=%s (backoff curto 90s).", fixture_id)
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

    # Composite 3/5  -> ajuste v3.1 para 2/5
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

    # ajuste v3.1 -> composite 2/5 (antes: >= 3)
    composite_ok = true_count >= 2

    logger.debug("🧩 Composite (Setup 2/5): Ataques=%s | Perigo=%s | Pressão=%s | Placar=%s | Janela=%s → %d/5 | Estratégias VIP: %d/10",
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

def build_signal_message_vip_v3(fixture: dict, estrategias: list, metrics: dict) -> str:
    """
    Monta a mensagem do sinal no estilo VIP Pro — completa, detalhada e formatada em 	HTML.
    """

    try:
        # ---------- Dados básicos ----------
        teams = fixture.get("teams", {}) or {}
        league_data = fixture.get("league", {}) or {}
        league = league_data.get("name", "?")
        home_team = (teams.get("home", {}) or {}).get("name", "?")
        away_team = (teams.get("away", {}) or {}).get("name", "?")
        goals = fixture.get("goals", {}) or {}
        score = f"{goals.get('home', '-')} x {goals.get('away', '-')}"
        minute = metrics.get("minute", 0)
        minute_txt = f"{minute:.0f}'"
        period = "HT" if minute <= 45 else "FT"

        # ---------- Estatísticas ----------
        home_c = metrics.get("home_corners", 0)
        away_c = metrics.get("away_corners", 0)
        press_home = f"{metrics.get('press_home', 0.0):.2f}"
        press_away = f"{metrics.get('press_away', 0.0):.2f}"
        home_att = metrics.get("home_attacks", 0)
        away_att = metrics.get("away_attacks", 0)
        home_d = metrics.get("home_danger", 0)
        away_d = metrics.get("away_danger", 0)
        home_sh = metrics.get("home_shots", 0)
        away_sh = metrics.get("away_shots", 0)
        home_pos = metrics.get("home_pos", 0)
        away_pos = metrics.get("away_pos", 0)
        stadium_small = "✅" if metrics.get("small_stadium") else "❌"

        # ---------- Odds e links ----------
        odds_home = metrics.get("odd_home", "-")
        odds_draw = metrics.get("odd_draw", "-")
        odds_away = metrics.get("odd_away", "-")
        bet_link = build_bet365_link(fixture)
        link_cornerprobet = metrics.get("cornerprobet_url", "https://cornerprobet.com/")

        estrategias_block = " • ".join(estrategias) if estrategias else "Setup 2/5 válido"

        # ---------- Lógica de domínio ----------
        if float(press_home) > float(press_away):
            dominio = "mandante"
            favorito = home_team
        elif float(press_home) < float(press_away):
            dominio = "visitante"
            favorito = away_team
        else:
            dominio = "equilibrado"
            favorito = "nenhum"

        recomendacao = (
            f"⚠️ Possível canto ou gol para o {favorito} antes do final do período"
            if dominio != "equilibrado"
            else "⚠️ Jogo equilibrado — monitorar ataques de ambos os lados"
        )

        # ---------- Montagem final ----------
        msg = (
            f"📣 Alerta Estratégia: Asiáticos - {period} 📣\n"
            f"🏟️ Jogo: {home_team} x {away_team}\n"
            f"🏆 Liga: {league}\n"
            f"🕒 Tempo: {minute_txt}\n"
            f"⚽ Placar: {score}\n"
            f"⛳ Cantos: {home_c} - {away_c} (1T)\n"
            f"📈 Odds Pré-Live: {odds_home} / {odds_draw} / {odds_away}\n\n"
            f"📊 Indicadores do Jogo:\n"
            f"• Pressão → {home_team}: {press_home} | {away_team}: {press_away}\n"
            f"• Ataques → {home_att} x {away_att}\n"
            f"• Perigosos → {home_d} x {away_d}\n"
            f"• Finalizações → {home_sh} x {away_sh}\n"
            f"• Posse de Bola → {home_pos}% x {away_pos}%\n"
            f"• Estádio Pequeno: {stadium_small}\n\n"
            f"📌 Estratégias Ativas: {estrategias_block}\n"
            f"📌 Análise: Jogo com domínio {dominio} — {recomendacao}\n\n"
            f"🔗 [CornerProBet]({link_cornerprobet}) | [Bet365]({bet_link})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )

        return msg.strip()

    except Exception as e:
        return f"⚠️ Erro ao montar mensagem: {e}"

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
#==================FUNÇÃO DE MENSAGEM VIP (PROFISSIONAL - HT/FT)=====================
def build_signal_message_vip(match, estrategias, stats):
    """
    Monta a mensagem formatada (HTML) no padrão profissional VIP.
    Detecta automaticamente o período (HT/FT) e aplica layout elegante.
    """
    try:
        # ====== Dados principais do jogo ======
        home = match['teams']['home']['name']
        away = match['teams']['away']['name']
        league = match['league']['name']
        tempo = match['fixture']['status']['elapsed']
        placar_home = match['goals']['home']
        placar_away = match['goals']['away']

        # ====== Estatísticas ======
        cantos_home = stats.get('home_corners', 0)
        cantos_away = stats.get('away_corners', 0)
        injury_time = stats.get('injury_time', '?')
        odds_home = stats.get('odds_home', '-')
        odds_draw = stats.get('odds_draw', '-')
        odds_away = stats.get('odds_away', '-')

        # Links
        link_cornerprobet = stats.get('link_cornerprobet', '')
        link_bet365 = stats.get('link_bet365', '')

        # ====== Identifica período ======
        periodo = "HT" if tempo <= 45 else "FT"

        # ====== Montagem ======
        mensagem = f"""
📣 <b>Alerta Estratégia: Asiáticos/Limite - {periodo} 📣</b>
🏟 <b>Jogo:</b> {home} ({stats.get('pos_home', '–')}º) x ({stats.get('pos_away', '–')}º) {away}
🏆 <b>Competição:</b> {league}
🕛 <b>Tempo:</b> {tempo} '
⚽ <b>Resultado:</b> {placar_home} x {placar_away} (0 x 0 Intervalo)
📈 <b>Odds 1x2 Pre-live:</b> {odds_home} / {odds_draw} / {odds_away}
⛳ <b>Cantos:</b> {cantos_home} - {cantos_away}
- 1ºP: {cantos_home} - {cantos_away}
⌚ <b>Possíveis acréscimos:</b> {injury_time}'

<a href="{link_cornerprobet}">https://cornerprobet.com/analysis/</a>
<a href="{link_bet365}">https://bet365.bet.br/#/AX/K^{away}/</a>

➡️ <b>Detalhes:</b> 👉 Fazer entrada em ESCANTEIOS (mercado asiático)
🚀 <b>Sinal VIP ativo!</b>
"""
        return mensagem.strip()

    except Exception as e:
        return f"<b>Erro ao montar mensagem VIP:</b> {e}"


# ========================= MAIN LOOP ==========================
from collections import defaultdict

# 🔐 Controle global anti-duplicado
sent_period = defaultdict(set)

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
                status_short = fixture_status.get("short", "")
                minute_real = fixture_status.get("elapsed", 0) or 0

                if status_short not in ["1H", "2H"]:
                    logger.debug(f"⏩ Ignorando fixture={fixture_id} — status inválido: {status_short}")
                    continue

                if minute_real < 18.8:
                    logger.debug(f"⏳ Ignorado fixture={fixture_id} (min {minute_real:.1f} < 18.8')")
                    continue

                minute = smooth_minute(fixture_id, float(minute_real))
                period = get_period(minute)

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

                limite_estrategias = 2 if minute <= 45 else 3
                signal_key = f"{period}{len(estrategias)}{total_corners}"

                # ==============================================================
                # 💬 Envio do Sinal
                # ==============================================================
                if (len(estrategias) >= limite_estrategias or composite_ok) and should_notify(fixture_id, signal_key):
                    try:
                        msg = build_signal_message_vip(fixture, estrategias, metrics)
                        send_telegram_message_plain(msg, parse_mode="HTML")
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
# ========================== RELATÓRIO DE PERFORMANCE ==========================
import csv
from datetime import datetime, date
from collections import Counter

RELATORIO_PATH = "relatorio.csv"

# 🔹 Registra cada sinal enviado
def registrar_sinal(fixture: dict, estrategias: list, resultado: str = "⏳") -> None:
    """Salva cada sinal no arquivo relatorio.csv"""
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

# 🔹 Atualiza o resultado manualmente (Green/Red)
def atualizar_resultado(jogo: str, resultado: str):
    """Atualiza um resultado específico no relatório"""
    linhas = []
    with open(RELATORIO_PATH, "r", encoding="utf-8") as f:
        linhas = [linha.strip().split(",") for linha in f.readlines()]
    for linha in linhas:
        if jogo.lower() in linha[2].lower():
            linha[-1] = resultado
    with open(RELATORIO_PATH, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(linhas)

# 🔹 Gera o relatório e envia no Telegram
def gerar_relatorio_diario():
    """Lê o relatorio.csv, calcula estatísticas e envia resumo via Telegram"""
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

    total = len(registros)
    greens = sum(1 for r in registros if "✅" in r[-1])
    reds = sum(1 for r in registros if "❌" in r[-1])
    pendentes = total - greens - reds
    eficiencia = (greens / total * 100) if total else 0

    estrategias = [e for r in registros for e in r[3].split(",") if e.strip() not in ["Nenhuma", ""]]
    mais_frequentes = Counter(estrategias).most_common(1)
    melhor_estrategia = mais_frequentes[0][0] if mais_frequentes else "—"

    msg = (
        f"📊 Relatório de Performance — Bot Escanteios RP VIP+\n"
        f"🗓️ Período: {datetime.now().strftime('%d/%m/%Y')}\n"
        f"📈 Total de Sinais: {total}\n"
        f"✅ Greens: {greens} ({(greens/total*100):.0f}%)\n"
        f"❌ Reds: {reds} ({(reds/total*100):.0f}%)\n"
        f"⏳ Pendentes: {pendentes}\n"
        f"⚙️ Eficiência Média: {eficiencia:.1f}%\n"
        f"💡 Melhor Estratégia: {melhor_estrategia}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Continue operando no modo VIP — rumo aos 80%+ de acerto!"
    )

    send_telegram_message_plain(msg, parse_mode="HTML")
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
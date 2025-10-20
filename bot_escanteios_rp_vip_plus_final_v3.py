#!/usr/bin/env python3
# -- coding: utf-8 --
"""
bot_escanteios_rp_vip_plus_final_v3.py
Versão corrigida e otimizada para Render.
"""

import os
import time
import math
import logging
from collections import defaultdict
from flask import Flask, request, jsonify
import requests

# ---------- CONFIG ----------
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('bot_escanteios_vip_plus')

API_FOOTBALL_KEY = os.getenv('API_FOOTBALL_KEY')
TOKEN = os.getenv('TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# ---------- FLASK APP ----------
app = Flask(__name__)

@app.route('/')
def health():
    return "✅ Bot Escanteios VIP Plus está rodando!", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    return jsonify({"status": "ok"}), 200

# ---------- FUNÇÕES ----------
def enviar_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
        logger.info(f"📩 Enviado: {msg[:50]}...")
    except Exception as e:
        logger.error(f"Erro ao enviar Telegram: {e}")

def buscar_partidas():
    try:
        url = f"{API_BASE}/fixtures?live=all"
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            logger.warning(f"Erro API: {r.status_code}")
            return []
        data = r.json().get("response", [])
        return data
    except Exception as e:
        logger.error(f"Erro ao buscar partidas: {e}")
        return []

def analisar_jogos():
    jogos = buscar_partidas()
    logger.info(f"🔍 {len(jogos)} jogos ao vivo analisados")
    for jogo in jogos:
        try:
            home = jogo["teams"]["home"]["name"]
            away = jogo["teams"]["away"]["name"]
            gols_home = jogo["goals"]["home"]
            gols_away = jogo["goals"]["away"]
            minutos = jogo["fixture"]["status"]["elapsed"] or 0
            escanteios_home = jogo["statistics"][0]["statistics"][0].get("value", 0)
            escanteios_away = jogo["statistics"][1]["statistics"][0].get("value", 0)
            total_esc = escanteios_home + escanteios_away

            # Estratégias
            if 30 <= minutos <= 40 and total_esc <= 6:
                enviar_telegram(f"🚩 <b>FT - Escanteios Possível</b>\n🏟 {home} x {away}\n⏱ {minutos}min\n🔹 Escanteios: {total_esc}")
            elif 35 <= minutos <= 45 and gols_home == gols_away:
                enviar_telegram(f"📣 <b>HT - Casa Empatando</b>\n🏟 {home} x {away}\n⏱ {minutos}min\n🏆 Oportunidade de escanteio")

        except Exception as e:
            logger.error(f"Erro ao analisar jogo: {e}")

# ---------- LOOP ----------
def loop_principal():
    while True:
        analisar_jogos()
        time.sleep(60)  # Atualiza a cada 1 minuto

if __name__ == "__main__":
    # Envia mensagem de inicialização no Telegram
    enviar_telegram("🚀 Bot Escanteios VIP Plus iniciado com sucesso e está monitorando jogos!")

    from threading import Thread
    Thread(target=loop_principal).start()
    app.run(host="0.0.0.0", port=10000)
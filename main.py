#!/usr/bin/env python3
"""
Açaizinho O Original — Sistema de Prestação de Contas
Servidor unificado: Web (Flask) + Bot Telegram
Para deploy no Railway.app
"""

import os
import threading
import asyncio
import logging
from flask import Flask, jsonify, request, send_from_directory
import sqlite3
from datetime import datetime, date
from pathlib import Path

# ==================== CONFIGURAÇÃO ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("DB_PATH", "/data/prestacoes.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ==================== BANCO DE DADOS ====================
def init_db():
    """Inicializa o banco de dados com as tabelas necessárias"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS viagens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_prestacao TEXT NOT NULL,
            solicitante TEXT DEFAULT 'Diogo Machado',
            cargo TEXT DEFAULT 'Diretoria / Executivo',
            destino TEXT,
            motivo TEXT,
            data_inicio TEXT,
            data_fim TEXT,
            status TEXT DEFAULT 'em_andamento',
            observacoes TEXT DEFAULT '',
            data_pagamento TEXT,
            email_enviado INTEGER DEFAULT 0,
            telegram_chat_id TEXT DEFAULT '',
            criado_em TEXT DEFAULT (datetime('now','localtime')),
            atualizado_em TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            viagem_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            descricao TEXT,
            quantidade REAL DEFAULT 1,
            valor_unitario REAL DEFAULT 0,
            valor_total REAL DEFAULT 0,
            valor_pago REAL DEFAULT 0,
            valor_politica REAL DEFAULT 0,
            foto_url TEXT DEFAULT '',
            data_item TEXT,
            criado_em TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (viagem_id) REFERENCES viagens(id)
        )
    """)
    conn.commit()
    conn.close()
    logger.info(f"Banco inicializado: {DB_PATH}")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ==================== FLASK APP ====================
app = Flask(__name__, static_folder="public")

@app.route("/")
def index():
    return send_from_directory("public", "index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "Açaizinho PC"})

@app.route("/api/viagens", methods=["GET"])
def listar_viagens():
    conn = get_db()
    viagens = conn.execute("""
        SELECT v.*, 
            COALESCE(SUM(i.valor_politica),0) as total,
            COALESCE(SUM(i.valor_pago),0) as total_pago,
            COUNT(i.id) as num_itens
        FROM viagens v
        LEFT JOIN itens i ON i.viagem_id = v.id
        GROUP BY v.id
        ORDER BY v.criado_em DESC
    """).fetchall()
    conn.close()
    return jsonify({"viagens": [dict(v) for v in viagens]})

@app.route("/api/viagens", methods=["POST"])
def criar_viagem():
    data = request.json
    now = datetime.now()
    prefixo = f"PC-{now.year}{now.month:02d}-"
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) as n FROM viagens WHERE numero_prestacao LIKE ?", (prefixo + "%",)).fetchone()
    numero = f"{prefixo}{row['n'] + 1:03d}"
    
    hoje = date.today().isoformat()
    conn.execute("""
        INSERT INTO viagens (numero_prestacao, solicitante, cargo, destino, motivo, data_inicio, status, telegram_chat_id)
        VALUES (?, ?, ?, ?, ?, ?, 'em_andamento', ?)
    """, (
        numero,
        data.get("solicitante", "Diogo Machado"),
        data.get("cargo", "Diretoria / Executivo"),
        data.get("destino", "").upper(),
        data.get("motivo", "").upper(),
        data.get("data_inicio", hoje),
        data.get("telegram_chat_id", "")
    ))
    conn.commit()
    viagem = conn.execute("SELECT * FROM viagens WHERE numero_prestacao = ?", (numero,)).fetchone()
    conn.close()
    return jsonify({"sucesso": True, "viagem": dict(viagem)})

@app.route("/api/viagens/<int:vid>", methods=["GET"])
def obter_viagem(vid):
    conn = get_db()
    viagem = conn.execute("SELECT * FROM viagens WHERE id = ?", (vid,)).fetchone()
    itens = conn.execute("SELECT * FROM itens WHERE viagem_id = ? ORDER BY criado_em", (vid,)).fetchall()
    conn.close()
    if not viagem:
        return jsonify({"erro": "Viagem não encontrada"}), 404
    
    totais = _calcular_totais_db(vid)
    return jsonify({
        "viagem": dict(viagem),
        "itens": [dict(i) for i in itens],
        "totais": totais
    })

@app.route("/api/viagens/<int:vid>/itens", methods=["POST"])
def adicionar_item(vid):
    data = request.json
    conn = get_db()
    conn.execute("""
        INSERT INTO itens (viagem_id, tipo, descricao, quantidade, valor_unitario, valor_total, valor_pago, valor_politica, data_item)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        vid,
        data.get("tipo"),
        data.get("descricao"),
        data.get("quantidade", 1),
        data.get("valor_unitario", 0),
        data.get("valor_total", 0),
        data.get("valor_pago", data.get("valor_total", 0)),
        data.get("valor_politica", data.get("valor_total", 0)),
        data.get("data_item")
    ))
    conn.commit()
    item = conn.execute("SELECT * FROM itens WHERE id = last_insert_rowid()").fetchone()
    conn.close()
    return jsonify({"sucesso": True, "item": dict(item)})

@app.route("/api/viagens/<int:vid>/concluir", methods=["POST"])
def concluir_viagem(vid):
    data = request.json or {}
    hoje = date.today().isoformat()
    
    # Calcular próximo dia 15
    d = date.today()
    if d.day < 15:
        data_pagamento = date(d.year, d.month, 15).isoformat()
    else:
        if d.month == 12:
            data_pagamento = date(d.year + 1, 1, 15).isoformat()
        else:
            data_pagamento = date(d.year, d.month + 1, 15).isoformat()
    
    conn = get_db()
    conn.execute("""
        UPDATE viagens SET status='concluida', data_fim=?, data_pagamento=?, atualizado_em=datetime('now','localtime')
        WHERE id=?
    """, (data.get("data_fim", hoje), data_pagamento, vid))
    conn.commit()
    viagem = conn.execute("SELECT * FROM viagens WHERE id = ?", (vid,)).fetchone()
    conn.close()
    totais = _calcular_totais_db(vid)
    return jsonify({"sucesso": True, "viagem": dict(viagem), "totais": totais})

@app.route("/api/viagens/<int:vid>/email-data", methods=["GET"])
def email_data(vid):
    conn = get_db()
    viagem = conn.execute("SELECT * FROM viagens WHERE id = ?", (vid,)).fetchone()
    conn.close()
    if not viagem:
        return jsonify({"erro": "Viagem não encontrada"}), 404
    totais = _calcular_totais_db(vid)
    return jsonify({
        "viagem": dict(viagem),
        "totais": totais,
        "pix": {"chave": "063.291.156-56", "nome": "Diogo Machado Santos", "banco": "Nubank"},
        "data_pagamento": viagem["data_pagamento"]
    })

def _calcular_totais_db(viagem_id):
    conn = get_db()
    itens = conn.execute("SELECT * FROM itens WHERE viagem_id = ?", (viagem_id,)).fetchall()
    conn.close()
    t = {"cafe_manha": 0, "almoco": 0, "jantar": 0, "hospedagem": 0, "km": 0, "extras": 0, "total": 0,
         "cafe_dias": 0, "almoco_dias": 0, "jantar_dias": 0, "hospedagem_noites": 0, "km_rodados": 0, "total_pago": 0}
    for i in itens:
        t["total"] += i["valor_politica"]
        t["total_pago"] += i["valor_pago"]
        if i["tipo"] == "cafe_manha": t["cafe_manha"] += i["valor_politica"]; t["cafe_dias"] += i["quantidade"]
        elif i["tipo"] == "almoco": t["almoco"] += i["valor_politica"]; t["almoco_dias"] += i["quantidade"]
        elif i["tipo"] == "jantar": t["jantar"] += i["valor_politica"]; t["jantar_dias"] += i["quantidade"]
        elif i["tipo"] == "hospedagem": t["hospedagem"] += i["valor_politica"]; t["hospedagem_noites"] += i["quantidade"]
        elif i["tipo"] == "km": t["km"] += i["valor_politica"]; t["km_rodados"] += i["quantidade"]
        else: t["extras"] += i["valor_politica"]
    return t

# ==================== INICIALIZAÇÃO ====================
def start_bot():
    """Inicia o bot Telegram em thread separada com event loop próprio"""
    if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
        logger.warning("⚠️ TELEGRAM_TOKEN ou OPENAI_API_KEY não configurados. Bot não iniciado.")
        return
    
    try:
        # Criar novo event loop para esta thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Importar e iniciar bot
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        
        # Atualizar variáveis do bot
        import bot as telegram_bot
        telegram_bot.TELEGRAM_TOKEN = TELEGRAM_TOKEN
        telegram_bot.OPENAI_API_KEY = OPENAI_API_KEY
        telegram_bot.DB_PATH = DB_PATH
        telegram_bot.client = telegram_bot.AsyncOpenAI(api_key=OPENAI_API_KEY, base_url="https://api.openai.com/v1")
        telegram_bot.migrar_banco()
        
        logger.info("🤖 Iniciando bot Telegram...")
        telegram_bot.main()
    except Exception as e:
        logger.error(f"Erro ao iniciar bot: {e}")

if __name__ == "__main__":
    init_db()
    
    # Iniciar bot em thread separada
    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()
    
    # Iniciar servidor web
    logger.info(f"🌐 Servidor web iniciando na porta {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)

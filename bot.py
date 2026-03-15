#!/usr/bin/env python3
"""
Bot Telegram — Açaizinho O Original
Sistema de Prestação de Contas com IA
Suporta: texto, áudio (transcrição Whisper) e fotos (OCR Vision GPT-4o)
"""

import os
import json
import sqlite3
import asyncio
import tempfile
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from openai import AsyncOpenAI

# ==================== CONFIGURAÇÃO ====================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DB_PATH = os.environ.get("DB_PATH", "/home/ubuntu/acaizinho_pc_v2/prestacoes.db")

# IDs Telegram autorizados (qualquer um pode usar por enquanto)
AUTHORIZED_USERS = []  # Vazio = todos autorizados

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url="https://api.openai.com/v1")

# ==================== TABELA DE VALORES ====================
TABELA = {
    "cafe_manha": {"label": "Café da Manhã", "valor": 30.00, "emoji": "☕"},
    "almoco":     {"label": "Almoço",        "valor": 60.00, "emoji": "🍽️"},
    "jantar":     {"label": "Jantar",        "valor": 90.00, "emoji": "🌙"},
    "hospedagem": {"label": "Hospedagem",    "valor": 500.00, "emoji": "🏨"},
    "km":         {"label": "Quilometragem", "valor": 1.60,  "emoji": "🚗"},
    "extra":      {"label": "Extra",         "valor": 0,     "emoji": "📎"},
}

PIX = {"chave": "063.291.156-56", "nome": "Diogo Machado Santos", "banco": "Nubank"}

# ==================== BANCO DE DADOS ====================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def fmt_brl(val):
    val = float(val or 0)
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_data(d):
    if not d: return "—"
    try:
        y, m, day = str(d).split("-")
        return f"{day}/{m}/{y}"
    except:
        return str(d)

def proximo_dia_15():
    hoje = date.today()
    if hoje.day < 15:
        return date(hoje.year, hoje.month, 15).isoformat()
    else:
        if hoje.month == 12:
            return date(hoje.year + 1, 1, 15).isoformat()
        return date(hoje.year, hoje.month + 1, 15).isoformat()

def gerar_numero():
    conn = get_db()
    now = datetime.now()
    prefixo = f"PC-{now.year}{now.month:02d}-"
    row = conn.execute(
        "SELECT COUNT(*) as n FROM viagens WHERE numero_prestacao LIKE ?",
        (prefixo + "%",)
    ).fetchone()
    seq = row["n"] + 1
    conn.close()
    return f"{prefixo}{seq:03d}"

def calcular_totais(viagem_id):
    conn = get_db()
    itens = conn.execute("SELECT * FROM itens WHERE viagem_id = ?", (viagem_id,)).fetchall()
    conn.close()
    totais = {
        "cafe_manha": 0, "almoco": 0, "jantar": 0, "hospedagem": 0,
        "km": 0, "extras": 0, "total": 0,
        "cafe_dias": 0, "almoco_dias": 0, "jantar_dias": 0,
        "hospedagem_noites": 0, "km_rodados": 0,
        "total_pago": 0
    }
    for item in itens:
        totais["total"] += item["valor_politica"]
        totais["total_pago"] += item["valor_pago"]
        if item["tipo"] == "cafe_manha":
            totais["cafe_manha"] += item["valor_politica"]
            totais["cafe_dias"] += item["quantidade"]
        elif item["tipo"] == "almoco":
            totais["almoco"] += item["valor_politica"]
            totais["almoco_dias"] += item["quantidade"]
        elif item["tipo"] == "jantar":
            totais["jantar"] += item["valor_politica"]
            totais["jantar_dias"] += item["quantidade"]
        elif item["tipo"] == "hospedagem":
            totais["hospedagem"] += item["valor_politica"]
            totais["hospedagem_noites"] += item["quantidade"]
        elif item["tipo"] == "km":
            totais["km"] += item["valor_politica"]
            totais["km_rodados"] += item["quantidade"]
        elif item["tipo"] == "extra":
            totais["extras"] += item["valor_politica"]
    return totais

# ==================== MIGRAÇÃO DO BANCO ====================
def migrar_banco():
    """Adiciona colunas necessárias ao banco existente"""
    conn = get_db()
    # Verificar se colunas existem
    cols = [row[1] for row in conn.execute("PRAGMA table_info(itens)").fetchall()]
    if "valor_pago" not in cols:
        conn.execute("ALTER TABLE itens ADD COLUMN valor_pago REAL DEFAULT 0")
        conn.execute("ALTER TABLE itens ADD COLUMN valor_politica REAL DEFAULT 0")
        conn.execute("ALTER TABLE itens ADD COLUMN foto_url TEXT DEFAULT ''")
        # Migrar dados existentes
        conn.execute("UPDATE itens SET valor_pago = valor_total, valor_politica = valor_total WHERE valor_pago = 0")
        conn.commit()
        logger.info("Banco migrado com sucesso")
    
    # Adicionar coluna telegram_chat_id se não existir
    vcols = [row[1] for row in conn.execute("PRAGMA table_info(viagens)").fetchall()]
    if "telegram_chat_id" not in vcols:
        conn.execute("ALTER TABLE viagens ADD COLUMN telegram_chat_id TEXT DEFAULT ''")
        conn.commit()
    conn.close()

# ==================== IA: INTERPRETAR MENSAGEM ====================
SYSTEM_PROMPT = """Você é o assistente de prestação de contas do Açaizinho O Original.
Sua função é interpretar mensagens de despesas de viagem e retornar JSON estruturado.

TABELA DE VALORES DA POLÍTICA (Diretoria):
- cafe_manha: R$ 30,00 por dia
- almoco: R$ 60,00 por dia  
- jantar: R$ 90,00 por dia
- hospedagem: R$ 500,00 por noite
- km: R$ 1,60 por quilômetro
- extra: valor livre (passagem, uber, pedágio, estacionamento, etc.)

REGRA IMPORTANTE — GASOLINA/COMBUSTÍVEL:
A política de viagem NÃO reembolsa gasolina ou combustível diretamente.
Se o usuário mencionar gasolina, combustível, abastecimento ou posto:
- Retorne acao:"ajuda" com mensagem_usuario explicando que gasolina não é reembolsável
- Informe que o reembolso de transporte é feito por km rodado (R$ 1,60/km)
- Peça que informe quantos km rodou para registrar corretamente

INSTRUÇÕES:
1. Identifique o tipo de despesa
2. Extraia o valor PAGO pelo usuário (o que ele realmente gastou)
3. Calcule o valor da POLÍTICA (baseado na tabela acima)
4. Identifique a quantidade (dias, noites, km)
5. Extraia descrição e data se mencionados

Para abertura de viagem, retorne ação "abrir_viagem" com destino e motivo.
Para fechamento, retorne ação "fechar_viagem".
Para resumo, retorne ação "resumo".
Para despesas, retorne ação "despesa".
Para ajuda, retorne ação "ajuda".
Para cancelar item, retorne ação "cancelar".

SEMPRE retorne JSON válido neste formato:
{
  "acao": "despesa|abrir_viagem|fechar_viagem|resumo|ajuda|cancelar",
  "tipo": "cafe_manha|almoco|jantar|hospedagem|km|extra",
  "quantidade": 1,
  "valor_pago": 0.0,
  "valor_politica": 0.0,
  "descricao": "descrição do item",
  "data": null,
  "destino": null,
  "motivo": null,
  "confianca": "alta|media|baixa",
  "mensagem_usuario": "confirmação amigável em português"
}

Exemplos:
- "almocei e paguei 58 reais" → tipo:almoco, valor_pago:58, valor_politica:60, quantidade:1
- "café da manhã 2 dias" → tipo:cafe_manha, valor_pago:60, valor_politica:60, quantidade:2
- "rodei 120km" → tipo:km, valor_pago:192, valor_politica:192, quantidade:120
- "hotel 2 noites" → tipo:hospedagem, valor_pago:1000, valor_politica:1000, quantidade:2
- "uber R$ 35" → tipo:extra, valor_pago:35, valor_politica:35, quantidade:1
- "gasolina R$ 200" → acao:ajuda, mensagem_usuario:"Gasolina não é reembolsável diretamente. A política paga R$ 1,60/km. Quantos km você rodou?"
- "abasteci R$ 150" → acao:ajuda, mensagem_usuario:"Combustível não entra na prestação. Informe os km rodados para calcular o reembolso de transporte (R$ 1,60/km)."
- "nova viagem São Paulo reunião fornecedores" → acao:abrir_viagem, destino:SÃO PAULO SP, motivo:REUNIÃO COM FORNECEDORES
- "fechar viagem" ou "encerrar" → acao:fechar_viagem
- "resumo" ou "quanto gastei" → acao:resumo
"""

async def interpretar_mensagem(texto: str) -> dict:
    """Usa GPT-4o para interpretar a mensagem e extrair dados estruturados"""
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": texto}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error(f"Erro ao interpretar mensagem: {e}")
        return {"acao": "erro", "mensagem_usuario": "Não entendi. Tente novamente."}

async def transcrever_audio(audio_path: str) -> str:
    """Transcreve áudio usando Whisper"""
    try:
        with open(audio_path, "rb") as f:
            resp = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="pt"
            )
        return resp.text
    except Exception as e:
        logger.error(f"Erro ao transcrever áudio: {e}")
        return ""

async def analisar_foto_comprovante(foto_path: str) -> dict:
    """Analisa foto de comprovante usando GPT-4o Vision"""
    try:
        import base64
        with open(foto_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Analise este comprovante/recibo e extraia as informações.
Retorne JSON com:
{
  "tipo": "cafe_manha|almoco|jantar|hospedagem|km|extra",
  "valor_pago": valor total pago,
  "descricao": "descrição do estabelecimento/serviço",
  "data": "YYYY-MM-DD ou null",
  "estabelecimento": "nome do local",
  "confianca": "alta|media|baixa"
}
Se não conseguir identificar, use tipo "extra" e valor 0."""
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                        }
                    ]
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=300
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error(f"Erro ao analisar foto: {e}")
        return {"tipo": "extra", "valor_pago": 0, "descricao": "Comprovante", "confianca": "baixa"}

# ==================== HANDLERS ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start — boas-vindas"""
    msg = """🍇 *Olá! Sou o assistente de Prestação de Contas do Açaizinho O Original!*

Posso te ajudar a registrar despesas de viagem de forma simples e rápida.

*Como usar:*
✈️ *Abrir viagem:* "Nova viagem São Paulo, reunião com fornecedores"
💰 *Lançar despesa:* "Almoço R$ 58" ou manda um áudio ou foto do comprovante
📊 *Ver resumo:* "Resumo" ou /resumo
✅ *Fechar viagem:* "Fechar viagem" ou /fechar
❓ *Ajuda:* /ajuda

*Tabela de valores (Diretoria):*
☕ Café da Manhã: R$ 30,00
🍽️ Almoço: R$ 60,00
🌙 Jantar: R$ 90,00
🏨 Hospedagem: R$ 500,00/noite
🚗 Quilometragem: R$ 1,60/km

Paz e bem! 🙏"""
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /ajuda"""
    msg = """❓ *Comandos disponíveis:*

/start — Boas-vindas
/nova — Abrir nova viagem
/resumo — Ver resumo da viagem atual
/fechar — Fechar viagem e gerar relatório
/historico — Ver últimas prestações
/ajuda — Esta mensagem

*Exemplos de mensagens:*
• "Nova viagem Curitiba, visita a clientes"
• "Café da manhã hoje"
• "Almocei, paguei 65 reais"
• "Jantar 2 dias"
• "Hotel 3 noites, paguei 450 cada"
• "Rodei 200km"
• "Uber R$ 28"
• "Passagem aérea R$ 380"
• "Fechar viagem"

*Ou simplesmente mande:*
🎤 Um áudio descrevendo a despesa
📸 Uma foto do comprovante/nota fiscal"""
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_nova(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /nova — iniciar nova viagem"""
    await update.message.reply_text(
        "✈️ *Nova Viagem*\n\nMe diga o destino e o motivo da viagem.\n\nExemplo: _\"São Paulo, reunião com fornecedores\"_",
        parse_mode="Markdown"
    )
    context.user_data["aguardando_nova_viagem"] = True

async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /resumo — ver resumo da viagem atual"""
    chat_id = str(update.effective_chat.id)
    conn = get_db()
    viagem = conn.execute(
        "SELECT * FROM viagens WHERE telegram_chat_id = ? AND status = 'em_andamento' ORDER BY criado_em DESC LIMIT 1",
        (chat_id,)
    ).fetchone()
    conn.close()
    
    if not viagem:
        await update.message.reply_text(
            "❌ Nenhuma viagem em andamento.\n\nUse /nova para iniciar uma viagem.",
            parse_mode="Markdown"
        )
        return
    
    await enviar_resumo(update, viagem["id"])

async def cmd_fechar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /fechar — fechar viagem atual"""
    chat_id = str(update.effective_chat.id)
    conn = get_db()
    viagem = conn.execute(
        "SELECT * FROM viagens WHERE telegram_chat_id = ? AND status = 'em_andamento' ORDER BY criado_em DESC LIMIT 1",
        (chat_id,)
    ).fetchone()
    conn.close()
    
    if not viagem:
        await update.message.reply_text("❌ Nenhuma viagem em andamento para fechar.")
        return
    
    await fechar_viagem_handler(update, context, viagem["id"])

async def cmd_historico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /historico — ver últimas prestações"""
    conn = get_db()
    viagens = conn.execute(
        "SELECT v.*, COALESCE(SUM(i.valor_politica),0) as total FROM viagens v LEFT JOIN itens i ON i.viagem_id = v.id GROUP BY v.id ORDER BY v.criado_em DESC LIMIT 5"
    ).fetchall()
    conn.close()
    
    if not viagens:
        await update.message.reply_text("📋 Nenhuma prestação encontrada ainda.")
        return
    
    status_map = {"em_andamento": "⏳ Em andamento", "concluida": "✅ Concluída"}
    msg = "📚 *Últimas Prestações:*\n\n"
    for v in viagens:
        status = status_map.get(v["status"], v["status"])
        msg += f"*{v['numero_prestacao']}* — {v['destino'] or '—'}\n"
        msg += f"  {fmt_data(v['data_inicio'])} → {fmt_data(v['data_fim'] or '')}\n"
        msg += f"  {fmt_brl(v['total'])} | {status}\n\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# ==================== HANDLER PRINCIPAL DE MENSAGENS ====================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa mensagens de texto"""
    texto = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    # Aguardando destino para nova viagem?
    if context.user_data.get("aguardando_destino"):
        context.user_data.pop("aguardando_destino")
        motivo = context.user_data.pop("motivo_pendente", "VIAGEM DE NEGÓCIOS")
        destino = texto.strip().upper()
        # Criar viagem com o destino informado
        dados_ia = {"destino": destino, "motivo": motivo}
        await abrir_viagem_direto(update, context, None, dados_ia)
        return

    # Aguardando nova viagem?
    if context.user_data.get("aguardando_nova_viagem"):
        context.user_data.pop("aguardando_nova_viagem")
        await abrir_viagem_direto(update, context, texto)
        return
    
    # Aguardando confirmação de fechamento?
    if context.user_data.get("aguardando_confirmacao_fechamento"):
        viagem_id = context.user_data.get("viagem_id_fechar")
        if texto.lower() in ["sim", "s", "yes", "confirmar", "✅"]:
            context.user_data.pop("aguardando_confirmacao_fechamento", None)
            context.user_data.pop("viagem_id_fechar", None)
            await executar_fechamento(update, context, viagem_id)
        else:
            context.user_data.pop("aguardando_confirmacao_fechamento", None)
            context.user_data.pop("viagem_id_fechar", None)
            await update.message.reply_text("❌ Fechamento cancelado. Continue lançando despesas!")
        return
    
    # Processar com IA
    await update.message.chat.send_action("typing")
    dados = await interpretar_mensagem(texto)
    await processar_dados_ia(update, context, dados, chat_id)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa mensagens de voz"""
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("🎤 Transcrevendo seu áudio...")
    
    try:
        # Baixar arquivo de áudio
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            audio_path = tmp.name
        
        # Transcrever
        texto = await transcrever_audio(audio_path)
        os.unlink(audio_path)
        
        if not texto:
            await update.message.reply_text("❌ Não consegui entender o áudio. Tente novamente ou escreva a despesa.")
            return
        
        await update.message.reply_text(f"📝 *Entendi:* _{texto}_", parse_mode="Markdown")
        
        # Processar com IA
        dados = await interpretar_mensagem(texto)
        await processar_dados_ia(update, context, dados, chat_id)
        
    except Exception as e:
        logger.error(f"Erro ao processar voz: {e}")
        await update.message.reply_text("❌ Erro ao processar áudio. Tente escrever a despesa.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa fotos de comprovantes"""
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("📸 Analisando o comprovante...")
    
    try:
        # Pegar foto de maior resolução
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            foto_path = tmp.name
        
        # Analisar com Vision
        dados_foto = await analisar_foto_comprovante(foto_path)
        os.unlink(foto_path)
        
        # Verificar viagem ativa
        conn = get_db()
        viagem = conn.execute(
            "SELECT * FROM viagens WHERE telegram_chat_id = ? AND status = 'em_andamento' ORDER BY criado_em DESC LIMIT 1",
            (chat_id,)
        ).fetchone()
        conn.close()
        
        if not viagem:
            await update.message.reply_text(
                f"📸 *Comprovante analisado!*\n\n"
                f"Detectei: {dados_foto.get('descricao', 'Despesa')} — {fmt_brl(dados_foto.get('valor_pago', 0))}\n\n"
                f"❌ Mas você não tem viagem em andamento. Use /nova para iniciar uma viagem primeiro.",
                parse_mode="Markdown"
            )
            return
        
        # Calcular valor da política
        tipo = dados_foto.get("tipo", "extra")
        valor_pago = float(dados_foto.get("valor_pago", 0))
        info_tabela = TABELA.get(tipo, TABELA["extra"])
        
        if tipo == "km":
            valor_politica = valor_pago  # km usa o valor informado
        elif tipo == "extra":
            valor_politica = valor_pago
        else:
            valor_politica = info_tabela["valor"]
        
        # Salvar no banco
        conn = get_db()
        conn.execute("""
            INSERT INTO itens (viagem_id, tipo, descricao, quantidade, valor_unitario, valor_total, valor_pago, valor_politica, data_item)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            viagem["id"], tipo,
            dados_foto.get("descricao", info_tabela["label"]),
            1, valor_politica, valor_politica,
            valor_pago, valor_politica,
            dados_foto.get("data")
        ))
        conn.commit()
        conn.close()
        
        # Feedback
        emoji = info_tabela["emoji"]
        diferenca = valor_politica - valor_pago
        diff_txt = ""
        if abs(diferenca) > 0.01:
            if diferenca > 0:
                diff_txt = f"\n💚 Você economizou {fmt_brl(diferenca)} em relação à política"
            else:
                diff_txt = f"\n⚠️ Acima da política em {fmt_brl(abs(diferenca))}"
        
        msg = (
            f"✅ *Comprovante registrado!*\n\n"
            f"{emoji} *{info_tabela['label']}*\n"
            f"📍 {dados_foto.get('estabelecimento', dados_foto.get('descricao', ''))}\n"
            f"💳 Pago: {fmt_brl(valor_pago)}\n"
            f"📋 Política: {fmt_brl(valor_politica)}"
            f"{diff_txt}\n\n"
            f"_Continue lançando ou diga \"fechar viagem\" quando terminar_"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Erro ao processar foto: {e}")
        await update.message.reply_text("❌ Erro ao analisar comprovante. Tente novamente ou descreva a despesa por texto.")

# ==================== LÓGICA DE NEGÓCIO ====================

async def processar_dados_ia(update, context, dados, chat_id):
    """Processa os dados retornados pela IA"""
    acao = dados.get("acao", "erro")
    
    if acao == "abrir_viagem":
        await abrir_viagem_direto(update, context, None, dados)
    
    elif acao == "despesa":
        await registrar_despesa(update, context, dados, chat_id)
    
    elif acao == "fechar_viagem":
        conn = get_db()
        viagem = conn.execute(
            "SELECT * FROM viagens WHERE telegram_chat_id = ? AND status = 'em_andamento' ORDER BY criado_em DESC LIMIT 1",
            (chat_id,)
        ).fetchone()
        conn.close()
        if viagem:
            await fechar_viagem_handler(update, context, viagem["id"])
        else:
            await update.message.reply_text("❌ Nenhuma viagem em andamento para fechar.")
    
    elif acao == "resumo":
        conn = get_db()
        viagem = conn.execute(
            "SELECT * FROM viagens WHERE telegram_chat_id = ? AND status = 'em_andamento' ORDER BY criza_em DESC LIMIT 1",
            (chat_id,)
        ).fetchone()
        if not viagem:
            viagem = conn.execute(
                "SELECT * FROM viagens WHERE telegram_chat_id = ? ORDER BY criado_em DESC LIMIT 1",
                (chat_id,)
            ).fetchone()
        conn.close()
        if viagem:
            await enviar_resumo(update, viagem["id"])
        else:
            await update.message.reply_text("❌ Nenhuma viagem encontrada. Use /nova para iniciar.")
    
    elif acao == "ajuda":
        # Se a IA retornou uma mensagem_usuario (ex: gasolina), mostrá-la diretamente
        mensagem_ia = dados.get("mensagem_usuario", "").strip()
        if mensagem_ia:
            await update.message.reply_text(
                f"⚠️ {mensagem_ia}",
                parse_mode="Markdown"
            )
        else:
            await cmd_ajuda(update, context)
    
    else:
        # Tentar como despesa direta
        if dados.get("tipo") and dados.get("valor_pago", 0) > 0:
            await registrar_despesa(update, context, dados, chat_id)
        else:
            await update.message.reply_text(
                "🤔 Não entendi bem. Tente:\n"
                "• \"Almoço R$ 58\"\n"
                "• \"Nova viagem São Paulo\"\n"
                "• \"Fechar viagem\"\n"
                "• /ajuda para ver todos os comandos"
            )

async def abrir_viagem_direto(update, context, texto_livre=None, dados_ia=None):
    """Abre uma nova viagem"""
    chat_id = str(update.effective_chat.id)
    
    # Verificar se já tem viagem em andamento
    conn = get_db()
    viagem_ativa = conn.execute(
        "SELECT * FROM viagens WHERE telegram_chat_id = ? AND status = 'em_andamento' LIMIT 1",
        (chat_id,)
    ).fetchone()
    conn.close()
    
    if viagem_ativa:
        keyboard = [
            [InlineKeyboardButton("✅ Sim, fechar e abrir nova", callback_data=f"fechar_e_nova_{viagem_ativa['id']}")],
            [InlineKeyboardButton("❌ Não, continuar viagem atual", callback_data="cancelar")]
        ]
        await update.message.reply_text(
            f"⚠️ Você já tem uma viagem em andamento: *{viagem_ativa['numero_prestacao']}* — {viagem_ativa['destino']}\n\n"
            f"Deseja fechar a viagem atual e abrir uma nova?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        if texto_livre:
            context.user_data["nova_viagem_pendente"] = texto_livre
        return
    
    # Extrair destino e motivo — tratar None em todos os campos
    if dados_ia:
        destino_raw = dados_ia.get("destino") or ""
        motivo_raw  = dados_ia.get("motivo")  or ""
        destino = destino_raw.strip().upper()
        motivo  = motivo_raw.strip().upper()  or "VIAGEM DE NEGÓCIOS"
    elif texto_livre:
        # Usar IA para extrair
        dados = await interpretar_mensagem(f"Abrir viagem: {texto_livre}")
        destino_raw = dados.get("destino") or ""
        motivo_raw  = dados.get("motivo")  or ""
        destino = destino_raw.strip().upper()
        motivo  = motivo_raw.strip().upper()  or "VIAGEM DE NEGÓCIOS"
    else:
        destino = ""
        motivo  = "VIAGEM DE NEGÓCIOS"

    # Se destino não foi informado, perguntar antes de criar
    if not destino:
        context.user_data["aguardando_destino"] = True
        context.user_data["motivo_pendente"] = motivo
        await update.message.reply_text(
            "📍 Para qual cidade/destino é essa viagem?\n\n"
            "_Ex: São Paulo SP, Belo Horizonte MG, Curitiba PR..._",
            parse_mode="Markdown"
        )
        return
    
    numero = gerar_numero()
    hoje = date.today().isoformat()
    
    conn = get_db()
    result = conn.execute("""
        INSERT INTO viagens (numero_prestacao, destino, motivo, data_inicio, status, telegram_chat_id)
        VALUES (?, ?, ?, ?, 'em_andamento', ?)
    """, (numero, destino, motivo, hoje, chat_id))
    conn.commit()
    conn.close()
    
    msg = (
        f"✈️ *Viagem aberta com sucesso!*\n\n"
        f"📋 *Número:* {numero}\n"
        f"📍 *Destino:* {destino}\n"
        f"🎯 *Motivo:* {motivo}\n"
        f"📅 *Início:* {fmt_data(hoje)}\n\n"
        f"Agora é só ir lançando as despesas!\n\n"
        f"*Exemplos:*\n"
        f"• \"Café da manhã\" ou \"Almoço R$ 58\"\n"
        f"• Mande um 🎤 áudio descrevendo a despesa\n"
        f"• Tire uma 📸 foto do comprovante\n\n"
        f"_Quando terminar, diga \"fechar viagem\"_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def registrar_despesa(update, context, dados, chat_id):
    """Registra uma despesa na viagem em andamento"""
    # Verificar viagem ativa
    conn = get_db()
    viagem = conn.execute(
        "SELECT * FROM viagens WHERE telegram_chat_id = ? AND status = 'em_andamento' ORDER BY criado_em DESC LIMIT 1",
        (chat_id,)
    ).fetchone()
    conn.close()
    
    if not viagem:
        await update.message.reply_text(
            "❌ Você não tem viagem em andamento.\n\nUse /nova para iniciar uma viagem primeiro."
        )
        return
    
    tipo = dados.get("tipo", "extra")
    quantidade = float(dados.get("quantidade", 1))
    valor_pago = float(dados.get("valor_pago", 0))
    descricao = dados.get("descricao") or TABELA.get(tipo, {}).get("label", tipo)
    data_item = dados.get("data")
    
    # Calcular valor da política
    info_tabela = TABELA.get(tipo, TABELA["extra"])
    if tipo == "km":
        valor_politica = quantidade * 1.60
        valor_pago = valor_pago if valor_pago > 0 else valor_politica
    elif tipo == "extra":
        valor_politica = valor_pago
    else:
        valor_politica = info_tabela["valor"] * quantidade
        if valor_pago == 0:
            valor_pago = valor_politica
    
    # Salvar no banco
    conn = get_db()
    conn.execute("""
        INSERT INTO itens (viagem_id, tipo, descricao, quantidade, valor_unitario, valor_total, valor_pago, valor_politica, data_item)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        viagem["id"], tipo, descricao,
        quantidade, info_tabela["valor"], valor_politica,
        valor_pago, valor_politica, data_item
    ))
    conn.commit()
    conn.close()
    
    # Feedback
    emoji = info_tabela["emoji"]
    diferenca = valor_politica - valor_pago
    diff_txt = ""
    if abs(diferenca) > 0.01 and tipo not in ["km", "extra"]:
        if diferenca > 0:
            diff_txt = f"\n💚 Economia de {fmt_brl(diferenca)} vs política"
        else:
            diff_txt = f"\n⚠️ {fmt_brl(abs(diferenca))} acima da política"
    
    # Calcular total acumulado
    totais = calcular_totais(viagem["id"])
    
    msg = (
        f"✅ *{descricao} registrado!*\n\n"
        f"{emoji} *Tipo:* {info_tabela['label']}\n"
    )
    if tipo == "km":
        msg += f"🛣️ *Distância:* {int(quantidade)} km\n"
        msg += f"💳 *Valor:* {fmt_brl(valor_politica)}\n"
    else:
        msg += f"💳 *Pago:* {fmt_brl(valor_pago)}\n"
        msg += f"📋 *Política:* {fmt_brl(valor_politica)}\n"
    
    msg += f"{diff_txt}\n"
    msg += f"\n💰 *Total acumulado:* {fmt_brl(totais['total'])}"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def enviar_resumo(update, viagem_id):
    """Envia resumo da viagem"""
    conn = get_db()
    viagem = conn.execute("SELECT * FROM viagens WHERE id = ?", (viagem_id,)).fetchone()
    itens = conn.execute("SELECT * FROM itens WHERE viagem_id = ? ORDER BY criado_em", (viagem_id,)).fetchall()
    conn.close()
    
    if not viagem:
        await update.message.reply_text("❌ Viagem não encontrada.")
        return
    
    totais = calcular_totais(viagem_id)
    
    status_map = {"em_andamento": "⏳ Em andamento", "concluida": "✅ Concluída"}
    
    msg = (
        f"📊 *Resumo — {viagem['numero_prestacao']}*\n\n"
        f"📍 {viagem['destino'] or '—'}\n"
        f"🎯 {viagem['motivo'] or '—'}\n"
        f"📅 {fmt_data(viagem['data_inicio'])} → {fmt_data(viagem['data_fim'] or '')}\n"
        f"Status: {status_map.get(viagem['status'], viagem['status'])}\n\n"
        f"*Despesas:*\n"
    )
    
    if totais["cafe_manha"] > 0:
        msg += f"☕ Café da Manhã ({int(totais['cafe_dias'])}×): {fmt_brl(totais['cafe_manha'])}\n"
    if totais["almoco"] > 0:
        msg += f"🍽️ Almoço ({int(totais['almoco_dias'])}×): {fmt_brl(totais['almoco'])}\n"
    if totais["jantar"] > 0:
        msg += f"🌙 Jantar ({int(totais['jantar_dias'])}×): {fmt_brl(totais['jantar'])}\n"
    if totais["hospedagem"] > 0:
        msg += f"🏨 Hospedagem ({int(totais['hospedagem_noites'])} noite{'s' if totais['hospedagem_noites'] != 1 else ''}): {fmt_brl(totais['hospedagem'])}\n"
    if totais["km"] > 0:
        msg += f"🚗 Quilometragem ({int(totais['km_rodados'])} km): {fmt_brl(totais['km'])}\n"
    if totais["extras"] > 0:
        msg += f"📎 Extras: {fmt_brl(totais['extras'])}\n"
    
    if not any([totais["cafe_manha"], totais["almoco"], totais["jantar"], totais["hospedagem"], totais["km"], totais["extras"]]):
        msg += "_Nenhuma despesa lançada ainda_\n"
    
    msg += f"\n💰 *Total a reembolsar: {fmt_brl(totais['total'])}*"
    
    if totais["total_pago"] > 0 and abs(totais["total_pago"] - totais["total"]) > 0.01:
        msg += f"\n💳 Total pago: {fmt_brl(totais['total_pago'])}"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def fechar_viagem_handler(update, context, viagem_id):
    """Solicita confirmação para fechar a viagem"""
    conn = get_db()
    viagem = conn.execute("SELECT * FROM viagens WHERE id = ?", (viagem_id,)).fetchone()
    conn.close()
    
    totais = calcular_totais(viagem_id)
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmar e Enviar Email", callback_data=f"confirmar_fechar_{viagem_id}"),
        ],
        [
            InlineKeyboardButton("📊 Ver Resumo Primeiro", callback_data=f"ver_resumo_{viagem_id}"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar")
        ]
    ]
    
    msg = (
        f"🏁 *Fechar Viagem {viagem['numero_prestacao']}?*\n\n"
        f"📍 {viagem['destino']}\n"
        f"💰 *Total a reembolsar: {fmt_brl(totais['total'])}*\n\n"
        f"Ao confirmar:\n"
        f"✅ A viagem será marcada como concluída\n"
        f"📧 Email será enviado para o financeiro\n"
        f"💳 PIX: 063.291.156-56 (Nubank)\n"
        f"📅 Pagamento: {fmt_data(proximo_dia_15())}"
    )
    
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def executar_fechamento(update, context, viagem_id):
    """Executa o fechamento da viagem e envia email"""
    conn = get_db()
    hoje = date.today().isoformat()
    data_pagamento = proximo_dia_15()
    
    conn.execute("""
        UPDATE viagens SET status='concluida', data_fim=?, data_pagamento=?, atualizado_em=datetime('now','localtime')
        WHERE id=?
    """, (hoje, data_pagamento, viagem_id))
    conn.commit()
    
    viagem = conn.execute("SELECT * FROM viagens WHERE id = ?", (viagem_id,)).fetchone()
    itens = conn.execute("SELECT * FROM itens WHERE viagem_id = ? ORDER BY tipo, criado_em", (viagem_id,)).fetchall()
    conn.close()
    
    totais = calcular_totais(viagem_id)
    
    # Enviar email via MCP (script externo)
    await enviar_email_relatorio(viagem, itens, totais, data_pagamento)
    
    # Gerar relatório comparativo
    msg = await gerar_relatorio_comparativo(viagem, itens, totais, data_pagamento)
    
    await update.effective_message.reply_text(msg, parse_mode="Markdown")

async def gerar_relatorio_comparativo(viagem, itens, totais, data_pagamento):
    """Gera relatório comparativo valores pagos x política"""
    msg = (
        f"🎉 *Viagem Encerrada com Sucesso!*\n\n"
        f"📋 *{viagem['numero_prestacao']}*\n"
        f"📍 {viagem['destino']}\n"
        f"📅 {fmt_data(viagem['data_inicio'])} → {fmt_data(viagem['data_fim'])}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*RELATÓRIO DE REEMBOLSO*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    # Tabela comparativa
    if totais["cafe_manha"] > 0:
        msg += f"☕ Café da Manhã ({int(totais['cafe_dias'])}×)\n"
        msg += f"   Política: {fmt_brl(totais['cafe_manha'])}\n\n"
    if totais["almoco"] > 0:
        msg += f"🍽️ Almoço ({int(totais['almoco_dias'])}×)\n"
        msg += f"   Política: {fmt_brl(totais['almoco'])}\n\n"
    if totais["jantar"] > 0:
        msg += f"🌙 Jantar ({int(totais['jantar_dias'])}×)\n"
        msg += f"   Política: {fmt_brl(totais['jantar'])}\n\n"
    if totais["hospedagem"] > 0:
        msg += f"🏨 Hospedagem ({int(totais['hospedagem_noites'])} noite{'s' if totais['hospedagem_noites'] != 1 else ''})\n"
        msg += f"   Política: {fmt_brl(totais['hospedagem'])}\n\n"
    if totais["km"] > 0:
        msg += f"🚗 Quilometragem ({int(totais['km_rodados'])} km)\n"
        msg += f"   Política: {fmt_brl(totais['km'])}\n\n"
    if totais["extras"] > 0:
        msg += f"📎 Extras\n"
        msg += f"   Valor: {fmt_brl(totais['extras'])}\n\n"
    
    msg += f"━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 *TOTAL A REEMBOLSAR: {fmt_brl(totais['total'])}*\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += f"*💳 Dados para Pagamento:*\n"
    msg += f"PIX: `063.291.156-56`\n"
    msg += f"Titular: Diogo Machado Santos\n"
    msg += f"Banco: Nubank\n"
    msg += f"📅 *Data: {fmt_data(data_pagamento)}*\n\n"
    msg += f"📧 Email enviado para o financeiro!\n"
    msg += f"_hithiara.ferreira@acaifood.com_\n"
    msg += f"_cópia: diogo.machado@acaifood.com_\n\n"
    msg += f"Paz e bem! 🙏"
    
    return msg

def fmt_brl_email(val):
    val = float(val or 0)
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_data_email(d):
    if not d: return "—"
    try:
        parts = str(d).split("-")
        if len(parts) == 3:
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except:
        pass
    return str(d)

async def enviar_email_relatorio(viagem, itens, totais, data_pagamento):
    """Envia email via SMTP direto do Gmail"""
    try:
        v = dict(viagem)
        t = totais
        
        # Credenciais SMTP (via variáveis de ambiente)
        EMAIL_REMETENTE = os.environ.get('SMTP_EMAIL', '')
        SENHA_APP = os.environ.get('SMTP_PASSWORD', '')
        
        # Montar corpo HTML do email
        linhas_despesas = ""
        if t.get("cafe_manha", 0) > 0:
            linhas_despesas += f"<tr><td>Cafe da Manha</td><td>{int(t['cafe_dias'])}x</td><td>R$ 30,00</td><td><b>{fmt_brl_email(t['cafe_manha'])}</b></td></tr>"
        if t.get("almoco", 0) > 0:
            linhas_despesas += f"<tr><td>Almoco</td><td>{int(t['almoco_dias'])}x</td><td>R$ 60,00</td><td><b>{fmt_brl_email(t['almoco'])}</b></td></tr>"
        if t.get("jantar", 0) > 0:
            linhas_despesas += f"<tr><td>Jantar</td><td>{int(t['jantar_dias'])}x</td><td>R$ 90,00</td><td><b>{fmt_brl_email(t['jantar'])}</b></td></tr>"
        if t.get("hospedagem", 0) > 0:
            linhas_despesas += f"<tr><td>Hospedagem</td><td>{int(t['hospedagem_noites'])} noite(s)</td><td>R$ 500,00/noite</td><td><b>{fmt_brl_email(t['hospedagem'])}</b></td></tr>"
        if t.get("km", 0) > 0:
            linhas_despesas += f"<tr><td>Quilometragem</td><td>{int(t['km_rodados'])} km</td><td>R$ 1,60/km</td><td><b>{fmt_brl_email(t['km'])}</b></td></tr>"
        if t.get("extras", 0) > 0:
            linhas_despesas += f"<tr><td>Extras</td><td>-</td><td>-</td><td><b>{fmt_brl_email(t['extras'])}</b></td></tr>"
        
        assunto = f"Prestacao de Contas {v['numero_prestacao']} - {v['destino']} | {fmt_brl_email(t['total'])}"
        
        corpo_html = f"""<html><body style='font-family:Arial,sans-serif;color:#1a1a1a;'>
<div style='max-width:600px;margin:0 auto;'>
<div style='background:#6B21A8;padding:20px;border-radius:8px 8px 0 0;'>
<h2 style='color:white;margin:0;'>Acaizinho O Original</h2>
<p style='color:#E9D5FF;margin:4px 0 0;'>Sistema de Prestacao de Contas</p>
</div>
<div style='background:#F9FAFB;padding:20px;border:1px solid #E5E7EB;'>
<h3 style='color:#6B21A8;'>Numero: {v['numero_prestacao']}</h3>
<table style='width:100%;border-collapse:collapse;margin-bottom:16px;'>
<tr><td style='padding:4px 8px;color:#6B7280;'>Solicitante</td><td style='padding:4px 8px;font-weight:bold;'>{v.get('solicitante','Diogo Machado')}</td></tr>
<tr><td style='padding:4px 8px;color:#6B7280;'>Destino</td><td style='padding:4px 8px;font-weight:bold;'>{v['destino']}</td></tr>
<tr><td style='padding:4px 8px;color:#6B7280;'>Motivo</td><td style='padding:4px 8px;'>{v['motivo']}</td></tr>
<tr><td style='padding:4px 8px;color:#6B7280;'>Periodo</td><td style='padding:4px 8px;'>{fmt_data_email(v['data_inicio'])} a {fmt_data_email(v.get('data_fim',''))}</td></tr>
</table>
<h4 style='color:#374151;border-bottom:2px solid #6B21A8;padding-bottom:8px;'>Despesas (Politica de Viagem - Diretoria)</h4>
<table style='width:100%;border-collapse:collapse;'>
<thead><tr style='background:#6B21A8;color:white;'><th style='padding:8px;text-align:left;'>Categoria</th><th style='padding:8px;'>Qtd</th><th style='padding:8px;'>Valor Unit.</th><th style='padding:8px;'>Total</th></tr></thead>
<tbody>{linhas_despesas}</tbody>
</table>
<div style='background:#6B21A8;color:white;padding:12px;margin-top:16px;border-radius:4px;text-align:right;'>
<span style='font-size:18px;font-weight:bold;'>TOTAL A REEMBOLSAR: {fmt_brl_email(t['total'])}</span>
</div>
<div style='margin-top:20px;background:#F3F4F6;padding:16px;border-radius:4px;border-left:4px solid #6B21A8;'>
<h4 style='margin:0 0 8px;color:#6B21A8;'>Dados para Pagamento via PIX</h4>
<p style='margin:4px 0;'><b>Chave PIX:</b> 063.291.156-56</p>
<p style='margin:4px 0;'><b>Titular:</b> Diogo Machado Santos</p>
<p style='margin:4px 0;'><b>Banco:</b> Nubank</p>
<p style='margin:4px 0;'><b>Data para Pagamento:</b> {fmt_data_email(data_pagamento)}</p>
</div>
</div>
<div style='background:#F3F4F6;padding:12px;border-radius:0 0 8px 8px;text-align:center;color:#9CA3AF;font-size:12px;'>
Gerado automaticamente pelo Bot Telegram - Acaizinho O Original
</div>
</div>
</body></html>"""
        
        # Montar mensagem
        msg = MIMEMultipart('alternative')
        msg['Subject'] = assunto
        msg['From'] = 'Acaizinho PC Bot <' + EMAIL_REMETENTE + '>'
        msg['To'] = 'hithiara.ferreira@acaifood.com'
        msg['Cc'] = 'diogo.machado@acaifood.com'
        msg.attach(MIMEText(corpo_html, 'html'))
        
        # Enviar via SMTP em thread separada (nao bloqueia o asyncio)
        def _send():
            server = smtplib.SMTP('smtp.gmail.com', 587, timeout=20)
            server.ehlo()
            server.starttls()
            server.login(EMAIL_REMETENTE, SENHA_APP)
            destinatarios = ['hithiara.ferreira@acaifood.com', 'diogo.machado@acaifood.com']
            server.sendmail(EMAIL_REMETENTE, destinatarios, msg.as_string())
            server.quit()
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)
        logger.info(f"Email enviado via SMTP para {v['numero_prestacao']}")
    except Exception as e:
        logger.error(f"Erro ao enviar email via SMTP: {e}")
        raise

# ==================== CALLBACK BUTTONS ====================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa botões inline"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "cancelar":
        await query.edit_message_text("❌ Operação cancelada.")
    
    elif data.startswith("confirmar_fechar_"):
        viagem_id = int(data.split("_")[-1])
        await query.edit_message_text("⏳ Processando fechamento...")
        # Criar fake update com query.message para reply_text funcionar
        class _FakeUpdateFechar:
            def __init__(self, msg):
                self.message = msg
                self.effective_chat = msg.chat
                self.effective_message = msg
        await executar_fechamento(_FakeUpdateFechar(query.message), context, viagem_id)
    
    elif data.startswith("ver_resumo_"):
        viagem_id = int(data.split("_")[-1])
        # Ao vir de callback, update.message é None — usar query.message
        class _FakeUpdate:
            def __init__(self, msg):
                self.message = msg
                self.effective_chat = msg.chat
                self.effective_message = msg
        await enviar_resumo(_FakeUpdate(query.message), viagem_id)
    
    elif data.startswith("fechar_e_nova_"):
        viagem_id = int(data.split("_")[-1])
        await query.edit_message_text("⏳ Fechando viagem atual...")
        class _FakeUpdateNova:
            def __init__(self, msg):
                self.message = msg
                self.effective_chat = msg.chat
                self.effective_message = msg
        await executar_fechamento(_FakeUpdateNova(query.message), context, viagem_id)
        # Abrir nova viagem
        texto_pendente = context.user_data.pop("nova_viagem_pendente", None)
        if texto_pendente:
            await abrir_viagem_direto(update, context, texto_pendente)

# ==================== MAIN ====================

def main():
    migrar_banco()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ajuda", cmd_ajuda))
    app.add_handler(CommandHandler("help", cmd_ajuda))
    app.add_handler(CommandHandler("nova", cmd_nova))
    app.add_handler(CommandHandler("resumo", cmd_resumo))
    app.add_handler(CommandHandler("fechar", cmd_fechar))
    app.add_handler(CommandHandler("historico", cmd_historico))
    
    # Mensagens
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    logger.info("🤖 Bot iniciado! Aguardando mensagens...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

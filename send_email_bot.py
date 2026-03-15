#!/usr/bin/env python3
"""Script de envio de email chamado pelo bot Telegram"""
import json
import subprocess
import sys
import os

def fmt_brl(val):
    val = float(val or 0)
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_data(d):
    if not d: return "—"
    try:
        parts = str(d).split("-")
        if len(parts) == 3:
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
    except:
        pass
    return str(d)

def main():
    try:
        with open("/tmp/email_relatorio.json") as f:
            dados = json.load(f)
    except:
        print("Erro: arquivo de dados não encontrado")
        sys.exit(1)
    
    v = dados["viagem"]
    t = dados["totais"]
    data_pagamento = dados["data_pagamento"]
    
    # Montar linhas HTML
    linhas = ""
    if t.get("cafe_manha", 0) > 0:
        linhas += f"<tr><td>☕ Café da Manhã</td><td>{int(t['cafe_dias'])}×</td><td>R$ 30,00</td><td>{fmt_brl(t['cafe_manha'])}</td></tr>"
    if t.get("almoco", 0) > 0:
        linhas += f"<tr><td>🍽️ Almoço</td><td>{int(t['almoco_dias'])}×</td><td>R$ 60,00</td><td>{fmt_brl(t['almoco'])}</td></tr>"
    if t.get("jantar", 0) > 0:
        linhas += f"<tr><td>🌙 Jantar</td><td>{int(t['jantar_dias'])}×</td><td>R$ 90,00</td><td>{fmt_brl(t['jantar'])}</td></tr>"
    if t.get("hospedagem", 0) > 0:
        linhas += f"<tr><td>🏨 Hospedagem</td><td>{int(t['hospedagem_noites'])} noite(s)</td><td>R$ 500,00</td><td>{fmt_brl(t['hospedagem'])}</td></tr>"
    if t.get("km", 0) > 0:
        linhas += f"<tr><td>🚗 Quilometragem</td><td>{int(t['km_rodados'])} km</td><td>R$ 1,60/km</td><td>{fmt_brl(t['km'])}</td></tr>"
    if t.get("extras", 0) > 0:
        linhas += f"<tr><td>📎 Extras</td><td>—</td><td>—</td><td>{fmt_brl(t['extras'])}</td></tr>"
    
    assunto = f"✅ Prestação de Contas {v['numero_prestacao']} — {v['destino']} | {fmt_brl(t['total'])}"
    
    conteudo = f"""PRESTAÇÃO DE CONTAS — AÇAIZINHO O ORIGINAL
==========================================

Número: {v['numero_prestacao']}
Solicitante: {v.get('solicitante', 'Diogo Machado')}
Cargo: {v.get('cargo', 'Diretoria / Executivo')}
Destino: {v['destino']}
Motivo: {v['motivo']}
Período: {fmt_data(v['data_inicio'])} → {fmt_data(v.get('data_fim', ''))}

DESPESAS DETALHADAS (Política de Viagem — Diretoria)
-----------------------------------------------------"""
    
    if t.get("cafe_manha", 0) > 0:
        conteudo += f"\nCafé da Manhã:   {int(t['cafe_dias'])}×  R$ 30,00/un   = {fmt_brl(t['cafe_manha'])}"
    if t.get("almoco", 0) > 0:
        conteudo += f"\nAlmoço:          {int(t['almoco_dias'])}×  R$ 60,00/un   = {fmt_brl(t['almoco'])}"
    if t.get("jantar", 0) > 0:
        conteudo += f"\nJantar:          {int(t['jantar_dias'])}×  R$ 90,00/un   = {fmt_brl(t['jantar'])}"
    if t.get("hospedagem", 0) > 0:
        conteudo += f"\nHospedagem:      {int(t['hospedagem_noites'])} noite(s)  R$ 500,00/noite = {fmt_brl(t['hospedagem'])}"
    if t.get("km", 0) > 0:
        conteudo += f"\nQuilometragem:   {int(t['km_rodados'])} km  R$ 1,60/km   = {fmt_brl(t['km'])}"
    if t.get("extras", 0) > 0:
        conteudo += f"\nExtras:          {fmt_brl(t['extras'])}"
    
    conteudo += f"""

TOTAL GERAL A REEMBOLSAR: {fmt_brl(t['total'])}

DADOS PARA PAGAMENTO VIA PIX
-----------------------------
Chave PIX: 063.291.156-56
Titular: Diogo Machado Santos
Banco: Nubank
Data para Pagamento: {fmt_data(data_pagamento)}

--
Este email foi gerado automaticamente pelo Sistema de Prestação de Contas — Açaizinho O Original.
Lançamentos realizados via Bot Telegram."""
    
    payload = json.dumps({
        "messages": [
            {
                "to": ["hithiara.ferreira@acaifood.com"],
                "cc": ["diogo.machado@acaifood.com"],
                "subject": assunto,
                "content": conteudo
            }
        ]
    })
    
    with open("/tmp/email_bot_payload.json", "w") as f:
        f.write(payload)
    
    result = subprocess.run(
        ["manus-mcp-cli", "tool", "call", "gmail_send_messages", "--server", "gmail", "--input", payload],
        capture_output=True, text=True, timeout=30
    )
    
    if result.returncode == 0:
        print("✅ Email enviado com sucesso!")
    else:
        print(f"❌ Erro ao enviar email: {result.stderr}")

if __name__ == "__main__":
    main()

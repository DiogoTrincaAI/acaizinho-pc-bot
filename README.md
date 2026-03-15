# 🍇 Açaizinho O Original — Sistema de Prestação de Contas

Sistema web + Bot Telegram para controle de despesas de viagem.

## Deploy no Railway

### Variáveis de Ambiente Necessárias

| Variável | Valor |
|---|---|
| `TELEGRAM_TOKEN` | Token do BotFather |
| `OPENAI_API_KEY` | Chave da OpenAI |
| `DB_PATH` | `/data/prestacoes.db` |

### Como fazer deploy

1. Crie conta em [railway.app](https://railway.app)
2. Novo projeto → Deploy from GitHub
3. Conecte este repositório
4. Configure as variáveis de ambiente acima
5. Adicione volume persistente em `/data`

## Funcionalidades

- ✅ Sistema web de prestação de contas
- ✅ Bot Telegram conversacional com IA
- ✅ Suporte a texto, áudio e fotos
- ✅ Envio automático de email
- ✅ Integração OpenAI (GPT-4o + Whisper)

## Bot Telegram

[@acaizinho_pc_viagem_bot](https://t.me/acaizinho_pc_viagem_bot)

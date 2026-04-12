# Polymarket Copy-Trade Bot v2 — Railway

## Deploy no Railway

### 1. Criar projeto
1. Vai a [railway.app](https://railway.app) → **New Project**
2. Escolhe **Deploy from GitHub repo** (faz push destes ficheiros para um repo)
   ou **Deploy from local** com o CLI: `railway up`

### 2. Variáveis de ambiente
No Railway → **Variables**, adiciona as que quiseres alterar:

| Variável         | Default | Descrição                            |
|------------------|---------|--------------------------------------|
| `BUDGET`         | 1000    | USDC simulados por carteira          |
| `INTERVAL`       | 30      | Segundos entre ciclos                |
| `TOP_WALLETS`    | 5       | Nº de carteiras top a seguir         |
| `MARKETS_TO_SCAN`| 15      | Mercados a analisar na Fase 1        |
| `MAX_TRADES`     | 500     | Trades máximos a analisar por wallet |
| `TELEGRAM_TOKEN` | —       | Token do teu bot Telegram (opcional) |
| `TELEGRAM_CHAT_ID`| —      | Chat ID do Telegram (opcional)       |

> `PORT` é injetado automaticamente pelo Railway — não precisas de o definir.

### 3. Ver o dashboard
Depois do deploy, o Railway dá-te um URL público (ex: `https://polymarket-bot.up.railway.app`).  
Abre esse URL no browser — o dashboard atualiza automaticamente.

### Endpoints disponíveis
| Path         | Descrição                        |
|--------------|----------------------------------|
| `/`          | Dashboard HTML                   |
| `/health`    | Health check (usado pelo Railway)|
| `/api/state` | Estado atual em JSON             |

### Estrutura
```
main.py          — bot + servidor HTTP numa só app
requirements.txt — dependências Python
Procfile         — comando de arranque
railway.toml     — configuração Railway
```

### Como funciona
O `main.py` corre **duas threads em paralelo**:
- **Thread Bot** — Fases 1, 2 e 3 (busca carteiras, rankeia, monitoriza)
- **Thread HTTP** — serve o dashboard na porta injetada pelo Railway

O dashboard é regenerado a cada **2 ciclos** (60 segundos) e o Railway
mantém o processo sempre ativo com restart automático em caso de falha.

# Growth Bot

Bot de crescimento no Instagram controlado 100% via Telegram.
Hospedado no Discloud — segue, visualiza stories e deixa de seguir automaticamente.

---

## Deploy no Discloud

### 1. Configurar variáveis de ambiente
No painel do Discloud, vá em **Variáveis de Ambiente** do app e adicione:

```
TELEGRAM_TOKEN=seu_token
TELEGRAM_OWNER_ID=seu_chat_id
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=sua_anon_key
SESSION_ENCRYPTION_KEY=sua_chave_fernet
```

### 2. Criar tabelas no Supabase
Abra o **SQL Editor** no painel do Supabase, copie o bloco `SCHEMA_SQL`
de `database/accounts.py` e execute. As 8 tabelas serão criadas.

### 3. Subir o projeto
Suba a pasta `growth-bot/` como um zip no Discloud.
O arquivo `discloud.config` já está configurado com 200 MB de RAM.

### 4. Primeiro uso (no Telegram)
```
/conta_add @seu_instagram sua_senha
/alvo_add https://instagram.com/pagina
/campanha_nova nome-da-campanha
/nicho_set seu nicho
/status
```

---

## Execução local (desenvolvimento)

```bash
cd growth-bot
pip install -r requirements.txt
# Preencha o .env com suas credenciais
python main.py
```

---

## Como o Discloud é usado

- `discloud.config` define RAM=200, AUTORESTART=true e entrypoint `main.py`
- Sessões do Instagram ficam em `/tmp` (efêmero) e têm backup criptografado no Supabase
- A cada restart, `main.py` restaura automaticamente as sessões do banco
- Variáveis sensíveis ficam nas **env vars do Discloud**, não no código

---

## Todos os comandos

### Contas
| Comando | Descrição |
|---|---|
| `/conta_add @u senha` | Conectar conta Instagram |
| `/conta_lista` | Ver contas e status |
| `/conta_pausar @u` | Pausar conta específica |
| `/conta_retomar @u` | Retomar conta pausada |
| `/conta_remover @u` | Desconectar e remover |
| `/conta_aquecer @u` | Reiniciar aquecimento progressivo |
| `/conta_fingerprint @u` | Randomizar dispositivo simulado |

### Páginas-alvo e campanhas
| Comando | Descrição |
|---|---|
| `/alvo_add URL` | Adicionar página por link |
| `/alvo_lista` | Ver alvos cadastrados |
| `/alvo_remover @pagina` | Remover alvo |
| `/campanha_nova nome` | Criar campanha |
| `/campanha_hist` | Histórico e comparativo |

### Nichos, filtros e listas
| Comando | Descrição |
|---|---|
| `/nicho_set nicho` | Definir nicho principal |
| `/score_set 60` | Score mínimo para seguir (0-100) |
| `/white_add @usuario` | Nunca deixar de seguir |
| `/black_add termo` | Nunca seguir (username ou palavra) |
| `/listas_ver` | Ver white e blacklist |

### Limites e comportamento
| Comando | Descrição |
|---|---|
| `/limite_set follows=50` | Follows por dia |
| `/limite_set unfollows=40` | Unfollows por dia |
| `/horario_set 8-22` | Janela de operação |
| `/delay_set 30-90` | Delay entre ações (segundos) |
| `/unfollow_prazo 5` | Dias para checar retorno |

### Fila de ações
| Comando | Descrição |
|---|---|
| `/fila_ver` | Ver ações pendentes/retry |
| `/fila_limpar` | Esvaziar fila |

### Segurança
| Comando | Descrição |
|---|---|
| `/risco_status` | Nível de risco por conta |
| `/alerta_set` | Configurar thresholds |
| `/sessao_backup` | Forçar backup no Supabase |
| `/sessao_restaurar @u` | Restaurar sessão do banco |

### Monitoramento
| Comando | Descrição |
|---|---|
| `/status` | Resumo em tempo real |
| `/relatorio` | Relatório semanal + gráfico PNG |
| `/pendentes` | Quem ainda não seguiu de volta |
| `/log` | Últimas 10 ações |
| `/modo_teste` | Simular sem executar nada |
| `/config_ver` | Ver configurações completas |

### Controle geral
| Comando | Descrição |
|---|---|
| `/pausar` | Parar todas as contas |
| `/retomar` | Retomar operação |

---

## Agendamento automático

| Job | Frequência |
|---|---|
| Seguir pessoas | A cada 2h (8h, 10h, 12h, 14h, 16h, 18h, 20h) |
| Unfollow automático | 1x/dia às 9h30 |
| Backup de sessões | A cada 6h |
| Avanço de aquecimento | 1x/dia às 23h59 |
| Detector de anomalias | A cada 30 minutos |

---

## Estrutura de arquivos

```
growth-bot/
├── discloud.config            Configuração do Discloud (RAM=200)
├── main.py                    Entrypoint + restauração de sessões
├── config.py                  Variáveis e constantes
├── requirements.txt
├── .env                       Local apenas (não sobe pro Discloud)
├── instagram/
│   ├── client.py              Login, sessão, fingerprint
│   ├── scraper.py             Extrai seguidores por link
│   ├── follower.py            Segue com rate limit e score
│   ├── unfollower.py          Unfollow com whitelist
│   ├── stories.py             Visualização de stories
│   ├── score.py               Score de perfil + filtros
│   └── risk_detector.py       Detector de risco, pause automático
├── database/
│   ├── accounts.py            CRUD contas + sessões criptografadas
│   └── operations.py          Seguidos, alvos, campanhas, logs
├── queue/
│   └── action_queue.py        Fila com retry e backoff exponencial
├── scheduler/
│   ├── jobs.py                Todos os jobs agendados
│   ├── warmup.py              Aquecimento progressivo
│   └── anomaly.py             Alertas automáticos
├── reports/
│   └── daily.py               Relatório texto + gráfico PNG
└── bot/handlers/
    ├── contas.py              /conta_*
    └── operacoes.py           Todos os outros comandos
```

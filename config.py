import os
from dotenv import load_dotenv

# load_dotenv funciona localmente; no Discloud as vars já estão no ambiente
load_dotenv()

# ─── Telegram ────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_OWNER_ID = int(os.getenv("TELEGRAM_OWNER_ID", "0"))

# ─── Supabase ────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ─── Criptografia de sessões ─────────────────────────────────
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY")

# ─── Limites padrão ──────────────────────────────────────────
DEFAULT_DAILY_FOLLOWS = 40
DEFAULT_DAILY_UNFOLLOWS = 40
DEFAULT_DELAY_MIN = 30
DEFAULT_DELAY_MAX = 90
DEFAULT_UNFOLLOW_AFTER_DAYS = 5
DEFAULT_SCORE_MIN = 50

# ─── Janela de operação padrão ───────────────────────────────
DEFAULT_HOUR_START = 8
DEFAULT_HOUR_END = 22

# ─── Aquecimento progressivo ─────────────────────────────────
WARMUP_SCHEDULE = [5, 10, 20, 30, 40]  # follows por dia (dias 1-5+)

# ─── Detector de risco ───────────────────────────────────────
RISK_ERROR_RATE_THRESHOLD = 0.15   # 15% de erros → pausa automática
RISK_MIN_ACTIONS_TO_EVAL = 10
ANOMALY_ZERO_ACTION_HOURS = 2      # horas sem ação dentro da janela → alerta

# ─── Fila / retry ────────────────────────────────────────────
QUEUE_MAX_RETRIES = 3
QUEUE_BACKOFF_BASE = 60            # segundos base para backoff exponencial

# ─── Pasta de sessões ────────────────────────────────────────
# /tmp é gravável tanto no Discloud quanto localmente.
# Backup criptografado das sessões fica no Supabase (persistente).
SESSIONS_DIR = os.path.join("/tmp", "growth-bot-sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

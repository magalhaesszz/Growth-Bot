import os
import tempfile
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

# Instagram — opcionais; sem proxy o bot usa o IP da máquina em execução.
INSTAGRAM_COUNTRY = os.getenv("INSTAGRAM_COUNTRY", "BR")
INSTAGRAM_COUNTRY_CODE = int(os.getenv("INSTAGRAM_COUNTRY_CODE", "55"))
INSTAGRAM_LOCALE = os.getenv("INSTAGRAM_LOCALE", "pt_BR")
INSTAGRAM_TIMEZONE_OFFSET = int(os.getenv("INSTAGRAM_TIMEZONE_OFFSET", "-10800"))
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "").strip()

# API de vídeo
VIDEO_API_URL = os.getenv("VIDEO_API_URL", "").rstrip("/")
VIDEO_API_SECRET = os.getenv("VIDEO_API_SECRET", "")

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
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "").strip() or os.path.join(
    tempfile.gettempdir(), "growth-bot-sessions"
)
os.makedirs(SESSIONS_DIR, exist_ok=True)


def validate_config() -> None:
    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "TELEGRAM_OWNER_ID": TELEGRAM_OWNER_ID,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
        "SESSION_ENCRYPTION_KEY": SESSION_ENCRYPTION_KEY,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Variáveis obrigatórias ausentes: " + ", ".join(missing)
        )
    if TELEGRAM_OWNER_ID <= 0:
        raise RuntimeError("TELEGRAM_OWNER_ID deve ser um ID numérico positivo")
    if not str(TELEGRAM_TOKEN).count(":") == 1:
        raise RuntimeError("TELEGRAM_TOKEN tem formato inválido")
    if not str(SUPABASE_URL).startswith("https://"):
        raise RuntimeError("SUPABASE_URL deve usar HTTPS")
    try:
        from cryptography.fernet import Fernet

        Fernet(SESSION_ENCRYPTION_KEY.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SESSION_ENCRYPTION_KEY não é uma chave Fernet válida") from exc
    if bool(VIDEO_API_URL) != bool(VIDEO_API_SECRET):
        raise RuntimeError(
            "VIDEO_API_URL e VIDEO_API_SECRET devem ser configurados juntos"
        )
    if VIDEO_API_URL and not VIDEO_API_URL.startswith("https://"):
        raise RuntimeError("VIDEO_API_URL deve usar HTTPS")

import os
import tempfile

from dotenv import load_dotenv

load_dotenv()

# ─── Telegram ────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_OWNER_ID = int(os.getenv("TELEGRAM_OWNER_ID", "0"))

# ─── Supabase ────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ─── Criptografia de sessoes ─────────────────────────────────
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY")

# ─── Video API ───────────────────────────────────────────────
VIDEO_API_URL = os.getenv("VIDEO_API_URL", "").rstrip("/")
VIDEO_API_SECRET = os.getenv("VIDEO_API_SECRET", "")
VIDEO_MAX_FILE_MB = max(1, int(os.getenv("VIDEO_MAX_FILE_MB", "45")))
VIDEO_MAX_BATCH_MB = max(VIDEO_MAX_FILE_MB, int(os.getenv("VIDEO_MAX_BATCH_MB", "120")))
VIDEO_SETTINGS_REMOTE = os.getenv("VIDEO_SETTINGS_REMOTE", "true").strip().lower() == "true"

# ─── Limites padrao ──────────────────────────────────────────
DEFAULT_DAILY_FOLLOWS = 40
DEFAULT_DAILY_UNFOLLOWS = 40
DEFAULT_DELAY_MIN = 30
DEFAULT_DELAY_MAX = 90
DEFAULT_UNFOLLOW_AFTER_DAYS = 5
DEFAULT_SCORE_MIN = 50

# ─── Janela de operacao padrao ───────────────────────────────
DEFAULT_HOUR_START = 8
DEFAULT_HOUR_END = 22

# ─── Aquecimento progressivo ─────────────────────────────────
WARMUP_SCHEDULE = [5, 10, 20, 30, 40]

# ─── Detector de risco ───────────────────────────────────────
RISK_ERROR_RATE_THRESHOLD = 0.15
RISK_MIN_ACTIONS_TO_EVAL = 10
ANOMALY_ZERO_ACTION_HOURS = 2

# A fila antiga continua disponivel para compatibilidade/inspecao. Acoes do
# Instagram nao sao retryadas cegamente: side-effects incertos podem duplicar.
QUEUE_MAX_RETRIES = 3
QUEUE_BACKOFF_BASE = 60

# ─── Proxy Instagram ─────────────────────────────────────────
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "").strip()
INSTAGRAM_USE_PROXY = os.getenv("INSTAGRAM_USE_PROXY", "false").strip().lower() == "true"
INSTAGRAM_COUNTRY = os.getenv("INSTAGRAM_COUNTRY", "BR")
INSTAGRAM_COUNTRY_CODE = int(os.getenv("INSTAGRAM_COUNTRY_CODE", "55"))
INSTAGRAM_LOCALE = os.getenv("INSTAGRAM_LOCALE", "pt_BR")
INSTAGRAM_TIMEZONE_OFFSET = int(os.getenv("INSTAGRAM_TIMEZONE_OFFSET", "-10800"))

# ─── Monitor continuo de Stories ─────────────────────────────
# Roda 24/7 e independe da janela de follow/unfollow. O tray detecta stories
# novos rapidamente; uma varredura em rodizio cobre qualquer perfil que o tray
# omitir. Os limites abaixo controlam somente leitura/seen de stories.
STORY_MONITOR_ENABLED = os.getenv("STORY_MONITOR_ENABLED", "true").strip().lower() == "true"
STORY_MONITOR_INTERVAL_SECONDS = max(
    30, min(900, int(os.getenv("STORY_MONITOR_INTERVAL_SECONDS", "60")))
)
STORY_MONITOR_FOLLOWING_REFRESH_SECONDS = max(
    STORY_MONITOR_INTERVAL_SECONDS,
    min(86400, int(os.getenv("STORY_MONITOR_FOLLOWING_REFRESH_SECONDS", "900"))),
)
STORY_MONITOR_FALLBACK_BATCH = max(
    1, min(100, int(os.getenv("STORY_MONITOR_FALLBACK_BATCH", "10")))
)
STORY_MONITOR_DELAY_MIN = max(
    0.0, min(10.0, float(os.getenv("STORY_MONITOR_DELAY_MIN", "0.5")))
)
STORY_MONITOR_DELAY_MAX = max(
    STORY_MONITOR_DELAY_MIN,
    min(15.0, float(os.getenv("STORY_MONITOR_DELAY_MAX", "1.2"))),
)

# ─── Pasta de sessoes ────────────────────────────────────────
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
        raise RuntimeError("Variáveis obrigatórias ausentes: " + ", ".join(missing))
    if TELEGRAM_OWNER_ID <= 0:
        raise RuntimeError("TELEGRAM_OWNER_ID deve ser um número positivo")
    if str(TELEGRAM_TOKEN).count(":") != 1:
        raise RuntimeError("TELEGRAM_TOKEN possui formato inválido")
    if not str(SUPABASE_URL).startswith("https://"):
        raise RuntimeError("SUPABASE_URL deve usar HTTPS")

    try:
        from cryptography.fernet import Fernet

        Fernet(str(SESSION_ENCRYPTION_KEY).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SESSION_ENCRYPTION_KEY não é uma chave Fernet válida") from exc

    if bool(VIDEO_API_URL) != bool(VIDEO_API_SECRET):
        raise RuntimeError("VIDEO_API_URL e VIDEO_API_SECRET devem ser configurados juntos")
    if VIDEO_API_URL and not VIDEO_API_URL.startswith("https://"):
        raise RuntimeError("VIDEO_API_URL deve usar HTTPS")
    if VIDEO_MAX_BATCH_MB < VIDEO_MAX_FILE_MB:
        raise RuntimeError("VIDEO_MAX_BATCH_MB deve ser >= VIDEO_MAX_FILE_MB")
    if INSTAGRAM_USE_PROXY and not INSTAGRAM_PROXY:
        raise RuntimeError("INSTAGRAM_USE_PROXY=true exige INSTAGRAM_PROXY configurada")

# ============================================================
#  FULLTENNIS SPI BOT — config.py
#  Toda la configuración centralizada. Solo editar este archivo.
# ============================================================

import os
from dotenv import load_dotenv

load_dotenv()

# ── APIs ─────────────────────────────────────────────────────
GOALSERVE_API_KEY   = os.getenv("GOALSERVE_API_KEY", "")
PINNODDS_API_KEY    = os.getenv("PINNODDS_API_KEY", "")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")

# ── GoalServe ─────────────────────────────────────────────────
GOALSERVE_BASE_URL      = "https://www.goalserve.com/getfeed"
GOALSERVE_POLL_INTERVAL = 15
GOALSERVE_POLL_OFFSET   = 7
GOALSERVE_SPORT         = "tennis"

# ── Pinnodds SSE ─────────────────────────────────────────────
PINNODDS_BASE_URL     = "https://pinnodds.com"
PINNODDS_SSE_LIVE     = f"{PINNODDS_BASE_URL}/odds-drop"
PINNODDS_SSE_PREMATCH = f"{PINNODDS_BASE_URL}/odds-drop-prematch"
PINNODDS_MIN_RISE_PCT = 5    # % mínimo de subida de cuota para disparar señal

# ── Pesos SPI — GoalServe (cancha) ───────────────────────────
SPI_WEIGHTS_GOALSERVE = {
    "walkover_noshown":      25,   # walkover / no show → alerta inmediata
    "match_suspended":       22,   # suspendido / interrupted / retired
    "score_frozen_10min":    10,   # score congelado 10+ min
    "score_frozen_5min":      5,   # score congelado 5–7 min
    "game_lost_from_4000":   12,   # pierde game después de ir 40-0 arriba
    "consecutive_breaks":    12,   # 3 veces llegando a 0-40 con saque propio
}

# ── Pesos SPI — Pinnodds (mercado) ───────────────────────────
# Para riesgo físico: cuota SUBE = mercado castiga al jugador
# cuota baja = mercado confía más → no es señal de riesgo físico
SPI_WEIGHTS_PINNACLE = {
    "odds_disappeared":      22,   # cuota desaparece del mercado → alerta inmediata
    "market_suspended":      18,   # mercado suspendido 2–15 min
    "odds_spike":            12,   # cuota sube 15%+ rápido
    "no_recovery_movement":   8,   # cuota sube 5–14% sin recuperar
    "odds_trend":             6,   # tendencia alcista en 3 movimientos seguidos
}

# ── Umbrales de alerta ────────────────────────────────────────
SPI_THRESHOLD_RED   = 80   # alerta roja (ambas fuentes requeridas)
SPI_THRESHOLD_AMBER = 55   # subido de 40 a 55 — más exigente

# ── Señales de alerta inmediata ───────────────────────────────
# Disparan Telegram sin importar SPI ni doble fuente
IMMEDIATE_ALERT_SIGNALS = [
    "walkover_noshown",   # GoalServe
    "odds_disappeared",   # Pinnacle
]

# ── Reducción porcentual por contexto ────────────────────────
SPI_REDUCTION_CHALLENGER = 0.30    # Challenger/ITF → −30%
SPI_REDUCTION_API_DELAY  = 0.20    # delay de API → −20%

# ── Filtros de bloqueo total (veto) ──────────────────────────
VETO_FLAGS = [
    "is_raining",
    "is_changeover",
    "is_medical_timeout",
    "is_tiebreak",
    "is_early_match",
]

# ── Cooldown entre alertas del mismo partido (por nivel) ─────
ALERT_COOLDOWN_SECONDS = {
    "immediate": 0,      # alerta inmediata — sin cooldown
    "red":       600,    # 10 min
    "amber":     300,    #  5 min
}

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = "fulltennis_spi.log"

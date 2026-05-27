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
GOALSERVE_POLL_INTERVAL = 15          # segundos entre cada fetch
GOALSERVE_POLL_OFFSET   = 7           # delay inicial al arrancar (evita
                                      # coincidir con otros bots en el mismo
                                      # endpoint — si tu otro bot arranca en 0,
                                      # este arranca en el segundo 7)
GOALSERVE_SPORT         = "tennis"

# ── Pinnodds SSE ─────────────────────────────────────────────
PINNODDS_BASE_URL       = "https://pinnodds.com"
PINNODDS_SSE_LIVE       = f"{PINNODDS_BASE_URL}/odds-drop"
PINNODDS_SSE_PREMATCH   = f"{PINNODDS_BASE_URL}/odds-drop-prematch"
PINNODDS_MIN_DROP_PCT   = 5           # % mínimo de caída para disparar señal

# ── Pesos SPI — GoalServe (cancha) ───────────────────────────
SPI_WEIGHTS_GOALSERVE = {
    "match_suspended":       22,   # partido suspendido / interrumpido
    "performance_drop":      18,   # bajón fuerte de rendimiento
    "consecutive_breaks":    15,   # pierde servicios seguidos (3+)
    "serve_speed_drop":      12,   # caída del saque
    "no_break_points":       10,   # no genera break points
    "score_frozen_10min":    10,   # score congelado 10+ min
    "score_frozen_5min":      5,   # score congelado 5–7 min
}

# ── Pesos SPI — Pinnodds (mercado) ───────────────────────────
SPI_WEIGHTS_PINNACLE = {
    "market_suspended":      30,   # ★ señal más pesada
    "odds_disappeared":      25,   # cuota desaparece del mercado
    "odds_spike":            20,   # cuota sube muy rápido
    "no_recovery_movement":  16,   # movimiento fuerte sin recuperación
    "market_slow_return":    14,   # mercado tarda en volver
}

# ── Umbrales de alerta ────────────────────────────────────────
SPI_THRESHOLD_RED    = 80    # alerta máxima (ambas fuentes requeridas)
SPI_THRESHOLD_AMBER  = 40    # vigilancia elevada

# ── Reducción porcentual por contexto ────────────────────────
SPI_REDUCTION_CHALLENGER = 0.30    # Challenger/ITF → −30%
SPI_REDUCTION_API_DELAY  = 0.20    # delay de API documentado → −20%

# ── Filtros de bloqueo total (veto) ──────────────────────────
# Claves que tracker.py puede setear en el estado del partido.
# Si cualquiera es True, la alerta se bloquea completamente.
VETO_FLAGS = [
    "is_raining",            # lluvia / condición climática confirmada
    "is_changeover",         # cambio de set en curso / recién terminado
    "is_medical_timeout",    # atención médica oficial en cancha
    "is_tiebreak",           # tie break activo
    "is_early_match",        # partido recién iniciado (< 3 juegos jugados)
]

# ── Cooldown entre alertas del mismo partido (por nivel) ─────
# Cuanto más seria la alerta, más tiempo antes de repetirla.
# Lógica: si ya mandaste alerta roja, la situación está monitoreada.
# Spam a los 5 min no agrega valor — solo ruido.
ALERT_COOLDOWN_SECONDS = {
    "red":    600,   # 10 min — alerta roja (alto riesgo físico)
    "amber":  300,   #  5 min — señal combinada
    "yellow": 180,   #  3 min — señal parcial
}

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")   # DEBUG | INFO | WARNING
LOG_FILE  = "fulltennis_spi.log"
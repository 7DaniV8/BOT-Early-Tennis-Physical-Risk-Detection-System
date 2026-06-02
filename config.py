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
    # Señales clásicas
    "walkover_noshown":      25,   # walkover / no show → alerta inmediata
    "match_suspended":       22,   # suspendido / interrupted / retired
    "score_frozen_10min":    10,   # score congelado 10+ min
    "score_frozen_5min":      5,   # score congelado 5–7 min
    "game_lost_from_4000":   12,   # pierde game después de ir 40-0 arriba
    "consecutive_breaks":    12,   # 3 veces llegando a 0-40 con saque propio

    # Nuevas señales físicas
    "double_break_same_set": 20,   # FUERTE: perdió el saque 2 veces en el mismo set
    "inset_collapse":        18,   # FUERTE: iba ganando el set y lo está perdiendo
    "slow_point_pace":        6,   # DÉBIL: ritmo más lento que su propio promedio
                                   # (solo como señal de apoyo, nunca disparador solo)
}

# ── Pesos SPI — Pinnodds (mercado) ───────────────────────────
SPI_WEIGHTS_PINNACLE = {
    "odds_disappeared":      22,   # cuota desaparece del mercado → alerta inmediata
    "market_suspended":      18,   # mercado suspendido 2–15 min
    "odds_spike":            12,   # cuota sube 15%+ rápido
    "no_recovery_movement":   8,   # cuota sube 5–14% sin recuperar
    "odds_trend":             6,   # tendencia alcista en 3 movimientos seguidos
}

# ── Señales fuertes de cancha (pueden disparar sin mercado si hay 2) ──
# Una señal fuerte sola → observación
# Dos señales fuertes → alerta amber sin necesidad de Pinnacle
STRONG_CANCHA_SIGNALS = {
    "double_break_same_set",
    "inset_collapse",
    "match_suspended",
    "score_frozen_10min",
    "walkover_noshown",
}

# ── Señales fuertes de mercado (pueden confirmar cualquier señal de cancha) ──
STRONG_MARKET_SIGNALS = {
    "odds_disappeared",
    "market_suspended",
    "odds_spike",
}

# ── Umbrales de alerta — ATP/WTA ─────────────────────────────
SPI_THRESHOLD_RED     = 75
SPI_THRESHOLD_AMBER   = 55   # antes 60 — doble fuente física ahora llega aquí
SPI_THRESHOLD_OBSERVE = 35   # antes 40

# ── Umbrales de alerta — Challenger/ITF ──────────────────────
SPI_THRESHOLD_RED_CHALLENGER     = 70
SPI_THRESHOLD_AMBER_CHALLENGER   = 52
SPI_THRESHOLD_OBSERVE_CHALLENGER = 32

# ── Señales de alerta inmediata ───────────────────────────────
IMMEDIATE_ALERT_SIGNALS = [
    "walkover_noshown",
    "odds_disappeared",
]

# ── Reducción porcentual por contexto ────────────────────────
SPI_REDUCTION_CHALLENGER = 0.30
SPI_REDUCTION_API_DELAY  = 0.20

# ── Vetos DUROS — bloqueo total, sin excepciones ─────────────
# Solo condiciones donde la señal es físicamente imposible de interpretar
VETO_FLAGS_HARD = [
    "is_raining",           # suspensión externa, no física
    "is_medical_timeout",   # ya está siendo atendido, la alerta llegaría tarde
]

# ── Vetos SUAVES — reducen SPI en lugar de bloquear ──────────
# Contextos donde la señal puede tener explicación táctica normal
# El valor es el % de reducción aplicado al SPI
VETO_FLAGS_SOFT = {
    "is_tiebreak":    0.20,   # tie break: patrones de juego diferentes
    "is_changeover":  0.10,   # changeover: pausa normal entre games
    "is_early_match": 0.15,   # primeros games: nerviosismo normal
}

# ── Señales que IGNORAN vetos suaves ─────────────────────────
# Estas señales son tan fuertes que el contexto no las invalida
VETO_SOFT_IMMUNE_SIGNALS = {
    "walkover_noshown",
    "match_suspended",
    "odds_disappeared",
    "double_break_same_set",  # 2 breaks en el mismo set es anómalo en cualquier contexto
}

# ── Reglas de combinación para alertas físicas ───────────────
# Define qué combinaciones de señales justifican una alerta

# Mínimo para enviar a Telegram:
#   Opción A: 1 señal fuerte de cancha + 1 señal de mercado (cualquiera)
#   Opción B: 2 señales fuertes de cancha (sin mercado → amber)
#   Opción C: señal inmediata (siempre)

# En Challenger/ITF: el mercado debe tener señal fuerte (no solo no_recovery)
REQUIRE_STRONG_MARKET_FOR_CHALLENGER = True

# ── Parámetros de detección física ───────────────────────────

# set_multiplier: amplificación de SPI por número de set
# Set 1: ×1.00 | Set 2: ×1.15 | Set 3: ×1.30 | Set 4: ×1.45 | Set 5: ×1.60
SET_MULTIPLIER_BASE = 0.15

# slow_point_pace: cuántas veces el promedio para activar
# Si avg=45s y actual>112s (45×2.5), activa
SLOW_PACE_MULTIPLIER = 2.5

# inset_collapse: ventaja mínima que tuvo + swing mínimo para considerar colapso
INSET_COLLAPSE_MIN_LEAD  = 2   # tuvo al menos 2 games de ventaja
INSET_COLLAPSE_MIN_SWING = 3   # esa ventaja se revirtió en 3+ games

# ── TTL de señales Pinnacle ───────────────────────────────────
# 600s = 10 min — permite que GoalServe confirme dentro de la ventana
PINNACLE_SIGNAL_TTL = 600   # antes 120 — este valor se aplica en pinnacle.py

# ── Cooldown entre alertas del mismo partido (por nivel) ─────
ALERT_COOLDOWN_SECONDS = {
    "immediate": 0,
    "red":       600,
    "amber":     300,
}

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = "fulltennis_spi.log"

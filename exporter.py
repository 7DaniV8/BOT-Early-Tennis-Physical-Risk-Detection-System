# ============================================================
#  FULLTENNIS SPI BOT — exporter.py
#  Cada vez que se envía una alerta, genera un CSV con el
#  detalle completo y lo envía como archivo a un canal
#  de Telegram separado (TELEGRAM_EXPORT_CHAT_ID).
# ============================================================

import csv
import io
import logging
import requests
from datetime import datetime, timezone
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_EXPORT_CHAT_ID,
    SPI_WEIGHTS_GOALSERVE,
    SPI_WEIGHTS_PINNACLE,
)

logger = logging.getLogger(__name__)


# ── Columnas del CSV ──────────────────────────────────────────
CSV_COLUMNS = [
    # Identificación
    "timestamp_utc",
    "match_id",
    "player_home",
    "player_away",
    "tournament",
    "status",
    "is_challenger",

    # Nivel de alerta
    "alert_level",
    "both_sources",

    # SPI
    "cancha_spi",
    "market_spi",
    "total_spi",
    "adjusted_spi",

    # Señales activas
    "cancha_signals",
    "market_signals",

    # Pesos individuales GoalServe
    "w_walkover_noshown",
    "w_match_suspended",
    "w_score_frozen_10min",
    "w_score_frozen_5min",
    "w_game_lost_from_4000",
    "w_consecutive_breaks",
    "w_double_break_same_set",
    "w_inset_collapse",
    "w_slow_point_pace",

    # Pesos individuales Pinnacle
    "w_odds_disappeared",
    "w_market_suspended",
    "w_odds_spike",
    "w_no_recovery_movement",
    "w_odds_trend",

    # Cuotas
    "home_odds",
    "away_odds",
    "risk_player",
    "opportunity_player",

    # Context flags
    "flag_is_tiebreak",
    "flag_is_changeover",
    "flag_is_medical_timeout",
    "flag_is_raining",
    "flag_is_early_match",

    # Reducciones aplicadas
    "reductions",
]


def _build_row(alert: dict) -> dict:
    """Construye el diccionario de una fila del CSV a partir del dict de alerta."""
    now            = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cancha_signals = alert.get("cancha_signals", [])
    market_signals = alert.get("market_signals", [])
    context_flags  = alert.get("context_flags", {})
    reductions     = alert.get("reductions", [])

    # Pesos individuales — 0 si la señal no estaba activa
    def gs_weight(signal: str) -> int:
        return SPI_WEIGHTS_GOALSERVE.get(signal, 0) if signal in cancha_signals else 0

    def pin_weight(signal: str) -> int:
        return SPI_WEIGHTS_PINNACLE.get(signal, 0) if signal in market_signals else 0

    return {
        # Identificación
        "timestamp_utc":           now,
        "match_id":                alert.get("match_id", ""),
        "player_home":             alert.get("player_home", ""),
        "player_away":             alert.get("player_away", ""),
        "tournament":              alert.get("tournament", ""),
        "status":                  alert.get("status", ""),
        "is_challenger":           int(alert.get("is_challenger", False)),

        # Nivel
        "alert_level":             alert.get("alert_level", ""),
        "both_sources":            int(alert.get("both_sources", False)),

        # SPI
        "cancha_spi":              alert.get("cancha_spi", 0),
        "market_spi":              alert.get("market_spi", 0),
        "total_spi":               alert.get("total_spi", 0),
        "adjusted_spi":            alert.get("adjusted_spi", 0),

        # Señales activas (pipe-separated para no romper el CSV)
        "cancha_signals":          "|".join(cancha_signals),
        "market_signals":          "|".join(market_signals),

        # Pesos individuales GoalServe
        "w_walkover_noshown":      gs_weight("walkover_noshown"),
        "w_match_suspended":       gs_weight("match_suspended"),
        "w_score_frozen_10min":    gs_weight("score_frozen_10min"),
        "w_score_frozen_5min":     gs_weight("score_frozen_5min"),
        "w_game_lost_from_4000":   gs_weight("game_lost_from_4000"),
        "w_consecutive_breaks":    gs_weight("consecutive_breaks"),
        "w_double_break_same_set": gs_weight("double_break_same_set"),
        "w_inset_collapse":        gs_weight("inset_collapse"),
        "w_slow_point_pace":       gs_weight("slow_point_pace"),

        # Pesos individuales Pinnacle
        "w_odds_disappeared":      pin_weight("odds_disappeared"),
        "w_market_suspended":      pin_weight("market_suspended"),
        "w_odds_spike":            pin_weight("odds_spike"),
        "w_no_recovery_movement":  pin_weight("no_recovery_movement"),
        "w_odds_trend":            pin_weight("odds_trend"),

        # Cuotas
        "home_odds":               alert.get("home_odds") or "",
        "away_odds":               alert.get("away_odds") or "",
        "risk_player":             alert.get("risk_player") or "",
        "opportunity_player":      alert.get("opportunity_player") or "",

        # Context flags
        "flag_is_tiebreak":        int(context_flags.get("is_tiebreak", False)),
        "flag_is_changeover":      int(context_flags.get("is_changeover", False)),
        "flag_is_medical_timeout": int(context_flags.get("is_medical_timeout", False)),
        "flag_is_raining":         int(context_flags.get("is_raining", False)),
        "flag_is_early_match":     int(context_flags.get("is_early_match", False)),

        # Reducciones
        "reductions":              "|".join(reductions),
    }


def _build_csv_bytes(row: dict) -> bytes:
    """Serializa una fila como CSV en memoria y retorna bytes UTF-8 con BOM."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=CSV_COLUMNS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerow(row)
    # BOM para que Excel lo abra correctamente sin configuración extra
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _filename(alert: dict) -> str:
    ts    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    home  = alert.get("player_home", "home").split()[-1].lower()
    away  = alert.get("player_away", "away").split()[-1].lower()
    level = alert.get("alert_level", "alert")
    return f"spi_{level}_{home}_vs_{away}_{ts}.csv"


def export_alert(alert: dict) -> bool:
    """
    Genera el CSV de la alerta y lo envía como documento
    al canal de exportación de Telegram.

    Retorna True si el envío fue exitoso.
    """
    if not TELEGRAM_EXPORT_CHAT_ID:
        logger.warning("[EXPORTER] TELEGRAM_EXPORT_CHAT_ID no configurado — export omitido")
        return False

    try:
        row      = _build_row(alert)
        csv_data = _build_csv_bytes(row)
        filename = _filename(alert)

        level   = alert.get("alert_level", "").upper()
        home    = alert.get("player_home", "")
        away    = alert.get("player_away", "")
        caption = (
            f"📊 Export SPI | {level}\n"
            f"{home} vs {away}\n"
            f"SPI={alert.get('adjusted_spi',0)} | "
            f"Señales: {', '.join(alert.get('cancha_signals',[]) + alert.get('market_signals',[]))}"
        )

        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_EXPORT_CHAT_ID,
                "caption": caption,
            },
            files={
                "document": (filename, csv_data, "text/csv"),
            },
            timeout=15,
        )
        resp.raise_for_status()
        logger.info(f"[EXPORTER] CSV enviado — {filename} → chat {TELEGRAM_EXPORT_CHAT_ID}")
        return True

    except requests.exceptions.HTTPError as e:
        logger.error(f"[EXPORTER] HTTP error al enviar CSV: {e.response.status_code} — {e}")
    except requests.exceptions.Timeout:
        logger.error("[EXPORTER] Timeout al enviar CSV a Telegram")
    except requests.exceptions.ConnectionError as e:
        logger.error(f"[EXPORTER] Error de conexión al enviar CSV: {e}")
    except Exception as e:
        logger.error(f"[EXPORTER] Error inesperado: {e}")

    return False

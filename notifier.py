# ============================================================
#  FULLTENNIS SPI BOT — notifier.py
#  Formatea y envía alertas a Telegram.
# ============================================================

import logging
import requests
from datetime import datetime, timezone
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    SPI_THRESHOLD_RED,
    SPI_THRESHOLD_AMBER,
)

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Cooldown: evitar spam del mismo partido
_last_alert: dict[str, datetime] = {}


def send_startup_message():
    """
    Envía mensaje de inicio al canal cuando el bot arranca.
    """
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    msg = "🚨 FullTennis — Sistema de Monitoreo Físico"
    try:
        resp = requests.post(
            TELEGRAM_API,
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       msg,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Mensaje de inicio enviado a Telegram")
    except Exception as e:
        logger.error(f"Error enviando mensaje de inicio: {e}")


def _format_signals(signals: list[str], source: str) -> str:
    """Convierte lista de claves de señales en texto legible."""
    labels = {
        # GoalServe
        "match_suspended":       "Partido suspendido / interrumpido",
        "performance_drop":      "Bajón fuerte de rendimiento",
        "consecutive_breaks":    "Pierde servicios seguidos (3+)",
        "serve_speed_drop":      "Caída del saque",
        "no_break_points":       "Sin break points generados",
        "score_frozen_10min":    "Score congelado 10+ min",
        "score_frozen_5min":     "Score congelado 5–7 min",
        # Pinnacle
        "market_suspended":      "Mercado suspendido ★",
        "odds_disappeared":      "Cuota desaparece del mercado",
        "odds_spike":            "Cuota sube muy rápido",
        "no_recovery_movement":  "Movimiento fuerte sin recuperación",
        "market_slow_return":    "Mercado tarda en volver",
    }
    lines = []
    for s in signals:
        label = labels.get(s, s)
        lines.append(f"  • {label}")
    return "\n".join(lines) if lines else "  —"


def _alert_level(spi: int, both_sources: bool) -> tuple[str, str]:
    """Retorna (emoji_nivel, texto_nivel)."""
    if spi >= SPI_THRESHOLD_RED and both_sources:
        return "🔴", "Alto riesgo físico / posible retiro"
    if spi >= SPI_THRESHOLD_RED and not both_sources:
        return "🟠", "Señal parcial alta — una fuente"
    if spi >= SPI_THRESHOLD_AMBER and both_sources:
        return "🟠", "Señal combinada activa"
    if spi >= SPI_THRESHOLD_AMBER and not both_sources:
        return "🟡", "Señal parcial"
    return "🟢", "Vigilancia normal"


def build_message(data: dict) -> str:
    """
    Construye el mensaje Telegram con el formato limpio de FullTennis.

    Formato:
    🚨 FullTennis — Sistema de Monitoreo Físico
    🎾 L. Darderi (1.35) | S. Ofner (3.16)
    🎯 Riesgo detectado en: L. DARDERI
    📈 Oportunidad en: S. OFNER
    ⚠️ Revisar antes de entrar.
    """
    spi          = data["adjusted_spi"]
    both         = data["both_sources"]
    now_str      = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Jugadores y cuotas
    home       = data["player_home"]
    away       = data["player_away"]
    home_odds  = data.get("home_odds")
    away_odds  = data.get("away_odds")
    risk       = data.get("risk_player")
    opp        = data.get("opportunity_player")

    # Formato corto de nombre: "Luciano Darderi" → "L. Darderi"
    def short_name(name: str) -> str:
        parts = name.strip().split()
        if len(parts) >= 2:
            return f"{parts[0][0]}. {' '.join(parts[1:])}"
        return name

    def fmt_odds(odds) -> str:
        return f"{odds:.2f}" if odds else "—"

    home_short = short_name(home)
    away_short = short_name(away)
    risk_short = short_name(risk) if risk else short_name(home)
    opp_short  = short_name(opp)  if opp  else short_name(away)

    # Línea de jugadores con cuotas
    players_line = f"🎾 {home_short} ({fmt_odds(home_odds)}) | {away_short} ({fmt_odds(away_odds)})"

    # Encabezado según nivel
    _, nivel = _alert_level(spi, both)

    msg  = f"🚨 *FullTennis — Sistema de Monitoreo Físico*\n"
    msg += f"{players_line}\n"
    msg += f"🎯 Riesgo detectado en: *{risk_short.upper()}*\n"
    msg += f"📈 Oportunidad en: *{opp_short.upper()}*\n"
    msg += f"⚠️ Revisar antes de entrar.\n"

    # Bloque secundario con detalles (colapsado visualmente)
    msg += f"─────────────────────\n"
    msg += f"📊 SPI: *{spi}*"
    if data["total_spi"] != spi:
        msg += f" _(bruto: {data['total_spi']})_"
    msg += f" | {nivel}\n"
    msg += f"🏆 {data['tournament']}\n"
    msg += f"🕐 {now_str}"

    # Fuente única → advertencia
    if not both and spi >= SPI_THRESHOLD_RED:
        msg += f"\n⚡ _Solo una fuente activa — confirmar antes de actuar_"

    # Reducciones aplicadas
    if data.get("reductions"):
        msg += f"\n⚙️ _{', '.join(data['reductions'])}_"

    return msg


def send_alert(data: dict, cooldown_seconds: int = 300) -> bool:
    """
    Envía la alerta a Telegram si no está en cooldown.
    Retorna True si se envió, False si fue suprimida.
    """
    match_id = data["match_id"]
    now      = datetime.now(timezone.utc)

    # Cooldown check
    last = _last_alert.get(match_id)
    if last:
        elapsed = (now - last).total_seconds()
        if elapsed < cooldown_seconds:
            logger.debug(f"Cooldown activo para {match_id} ({elapsed:.0f}s < {cooldown_seconds}s)")
            return False

    message = build_message(data)

    try:
        resp = requests.post(
            TELEGRAM_API,
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        resp.raise_for_status()
        _last_alert[match_id] = now
        logger.info(f"Alerta enviada: {match_id} SPI={data['adjusted_spi']}")
        return True

    except requests.exceptions.HTTPError as e:
        logger.error(f"Telegram HTTP error: {e} — {resp.text}")
    except Exception as e:
        logger.error(f"Error enviando alerta Telegram: {e}")

    return False


def send_veto_log(match_id: str, player_home: str, player_away: str,
                  spi: int, active_vetos: list[str]):
    """
    Envía un mensaje silencioso de log cuando una alerta fue bloqueada por filtro.
    Solo se loguea localmente, no se envía a Telegram para no generar ruido.
    """
    veto_str = ", ".join(active_vetos)
    logger.info(
        f"ALERTA VETADA — {player_home} vs {player_away} "
        f"(SPI={spi}) — filtros: {veto_str}"
    )
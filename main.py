# ============================================================
#  FULLTENNIS SPI BOT — main.py
#  Orquesta todo. Loop principal GoalServe + SSE Pinnacle.
#  Manejo de errores granular + logs descriptivos.
# ============================================================

import logging
import time
import sys
import traceback
from datetime import datetime, timezone
from config import (
    GOALSERVE_POLL_INTERVAL,
    GOALSERVE_POLL_OFFSET,
    SPI_THRESHOLD_RED,
    SPI_THRESHOLD_AMBER,
    SPI_REDUCTION_CHALLENGER,
    SPI_REDUCTION_API_DELAY,
    VETO_FLAGS,
    ALERT_COOLDOWN_SECONDS,
    LOG_LEVEL,
    LOG_FILE,
)
import tracker
import pinnacle
import notifier

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ── Contadores de ciclo ───────────────────────────────────────
_cycle_count        = 0
_total_alerts_sent  = 0
_consecutive_errors = 0
MAX_CONSECUTIVE_ERRORS = 10   # si falla 10 ciclos seguidos → reinicio suave


# ── Matcher por apellido GoalServe ↔ Pinnacle ─────────────────

def _extract_surnames(full_name: str) -> set[str]:
    name = full_name.lower().strip()
    if len(name) > 2 and name[1] in ".":
        name = name[3:].strip()
    parts = name.replace("-", " ").split()
    return {p for p in parts if len(p) > 2}


def _find_goalserve_match(pinnacle_home: str, pinnacle_away: str,
                           goalserve_matches: list[dict]) -> dict | None:
    pin_surnames_h = _extract_surnames(pinnacle_home)
    pin_surnames_a = _extract_surnames(pinnacle_away)
    for gm in goalserve_matches:
        gs_surnames_h = _extract_surnames(gm.get("player_home", ""))
        gs_surnames_a = _extract_surnames(gm.get("player_away", ""))
        if bool(pin_surnames_h & gs_surnames_h) and bool(pin_surnames_a & gs_surnames_a):
            return gm
    return None


# ── SPI Engine ────────────────────────────────────────────────

def _resolve_alert_level(spi: int, both_sources: bool) -> str:
    if spi >= SPI_THRESHOLD_RED and both_sources:
        return "red"
    if spi >= SPI_THRESHOLD_RED and not both_sources:
        return "amber"
    if spi >= SPI_THRESHOLD_AMBER and both_sources:
        return "amber"
    return "yellow"


def _apply_reductions(spi: int, is_challenger: bool, api_delay: bool = False) -> tuple[int, list[str]]:
    reductions = []
    adjusted   = float(spi)
    if is_challenger:
        adjusted *= (1 - SPI_REDUCTION_CHALLENGER)
        reductions.append(f"Challenger/ITF −{int(SPI_REDUCTION_CHALLENGER*100)}%")
    if api_delay:
        adjusted *= (1 - SPI_REDUCTION_API_DELAY)
        reductions.append(f"Delay de API −{int(SPI_REDUCTION_API_DELAY*100)}%")
    return round(adjusted), reductions


def _check_vetos(context_flags: dict) -> list[str]:
    return [flag for flag in VETO_FLAGS if context_flags.get(flag, False)]


def evaluate_match(match_data: dict, all_goalserve_matches: list[dict]) -> dict | None:
    """Evalúa un partido GoalServe cruzando con Pinnacle por apellido."""
    match_id       = match_data["match_id"]
    player_home    = match_data["player_home"]
    player_away    = match_data["player_away"]
    cancha_spi     = match_data["cancha_spi"]
    cancha_signals = match_data["cancha_signals"]
    context_flags  = match_data["context_flags"]
    is_challenger  = match_data["is_challenger"]

    try:
        pin_state = pinnacle.find_market_state_by_name(player_home, player_away)
    except Exception as e:
        logger.error(f"[EVALUATE] Error buscando estado Pinnacle para {player_home} vs {player_away}: {e}")
        pin_state = pinnacle._empty_state()

    market_spi     = pin_state["market_spi"]
    market_signals = pin_state["signals"]
    total_spi      = cancha_spi + market_spi
    both_sources   = cancha_spi > 0 and market_spi > 0

    if total_spi == 0:
        return None

    # Verificar vetos
    active_vetos = _check_vetos(context_flags)
    if active_vetos:
        logger.info(
            f"[VETO] {player_home} vs {player_away} | "
            f"SPI={total_spi} bloqueado por: {', '.join(active_vetos)}"
        )
        notifier.send_veto_log(match_id, player_home, player_away, total_spi, active_vetos)
        return None

    adjusted_spi, reductions = _apply_reductions(total_spi, is_challenger)

    if adjusted_spi < SPI_THRESHOLD_AMBER:
        return None

    alert_level = _resolve_alert_level(adjusted_spi, both_sources)

    # Log detallado de por qué sonó la alerta
    logger.info(
        f"[ALERTA {alert_level.upper()}] {player_home} vs {player_away} | "
        f"SPI bruto={total_spi} ajustado={adjusted_spi} | "
        f"Cancha={cancha_spi} {cancha_signals} | "
        f"Mercado={market_spi} {market_signals} | "
        f"Ambas fuentes={both_sources} | "
        f"Reducciones={reductions} | "
        f"Torneo={match_data['tournament']}"
    )

    return {
        "match_id":           match_id,
        "player_home":        player_home,
        "player_away":        player_away,
        "tournament":         match_data["tournament"],
        "status":             match_data["status"],
        "total_spi":          total_spi,
        "adjusted_spi":       adjusted_spi,
        "alert_level":        alert_level,
        "cancha_spi":         cancha_spi,
        "cancha_signals":     cancha_signals,
        "market_spi":         market_spi,
        "market_signals":     market_signals,
        "both_sources":       both_sources,
        "reductions":         reductions,
        "context_flags":      context_flags,
        "is_challenger":      is_challenger,
        "home_odds":          pin_state.get("home_odds"),
        "away_odds":          pin_state.get("away_odds"),
        "risk_player":        pin_state.get("risk_player"),
        "opportunity_player": pin_state.get("opportunity_player"),
    }


def evaluate_pinnacle_only(event_id: str, market_state: dict) -> dict | None:
    """Evalúa partidos solo en Pinnacle. Máximo nivel: amber."""
    market_spi     = market_state["market_spi"]
    market_signals = market_state["signals"]
    home           = market_state.get("home", "")
    away           = market_state.get("away", "")
    league         = market_state.get("league", "")

    if market_spi < SPI_THRESHOLD_AMBER:
        return None

    is_challenger = any(k in league.lower() for k in ["challenger", "itf", "125k", "future"])
    adjusted_spi, reductions = _apply_reductions(market_spi, is_challenger)

    if adjusted_spi < SPI_THRESHOLD_AMBER:
        return None

    alert_level = "amber" if adjusted_spi >= SPI_THRESHOLD_RED else "yellow"

    logger.info(
        f"[ALERTA PINNACLE-ONLY {alert_level.upper()}] {home} vs {away} | "
        f"SPI bruto={market_spi} ajustado={adjusted_spi} | "
        f"Señales={market_signals} | "
        f"Liga={league} | Reducciones={reductions}"
    )

    return {
        "match_id":           event_id,
        "player_home":        home,
        "player_away":        away,
        "tournament":         league,
        "status":             "in_progress",
        "total_spi":          market_spi,
        "adjusted_spi":       adjusted_spi,
        "alert_level":        alert_level,
        "cancha_spi":         0,
        "cancha_signals":     [],
        "market_spi":         market_spi,
        "market_signals":     market_signals,
        "both_sources":       False,
        "reductions":         reductions,
        "context_flags":      {},
        "is_challenger":      is_challenger,
        "home_odds":          market_state.get("home_odds"),
        "away_odds":          market_state.get("away_odds"),
        "risk_player":        market_state.get("risk_player"),
        "opportunity_player": market_state.get("opportunity_player"),
    }


# ── Validación de config ──────────────────────────────────────

def _validate_config():
    from config import (
        GOALSERVE_API_KEY, PINNODDS_API_KEY,
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    )
    missing = []
    if not GOALSERVE_API_KEY:  missing.append("GOALSERVE_API_KEY")
    if not PINNODDS_API_KEY:   missing.append("PINNODDS_API_KEY")
    if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:   missing.append("TELEGRAM_CHAT_ID")
    if missing:
        logger.critical(f"[CONFIG] Variables faltantes: {', '.join(missing)} — el bot no puede iniciar")
        sys.exit(1)


# ── Un ciclo completo de evaluación ──────────────────────────

def _run_cycle() -> tuple[int, int]:
    """
    Ejecuta un ciclo completo de evaluación.
    Retorna (partidos_evaluados, alertas_enviadas).
    Lanza excepción si algo falla — el caller decide cómo manejarla.
    """
    alerts_sent = 0

    # ── GoalServe ────────────────────────────────────────────
    try:
        matches = tracker.process_matches()
    except Exception as e:
        logger.error(f"[GOALSERVE] Fallo al obtener partidos: {e}")
        matches = []

    active_ids = [m["match_id"] for m in matches]

    # Path 1: partidos cubiertos por GoalServe + cruce con Pinnacle
    for match_data in matches:
        try:
            alert = evaluate_match(match_data, matches)
        except Exception as e:
            logger.error(
                f"[EVALUATE] Error evaluando {match_data.get('player_home','?')} vs "
                f"{match_data.get('player_away','?')}: {e}"
            )
            continue

        if alert:
            try:
                level    = alert["alert_level"]
                cooldown = ALERT_COOLDOWN_SECONDS.get(level, 300)
                sent     = notifier.send_alert(alert, cooldown_seconds=cooldown)
                if sent:
                    alerts_sent += 1
            except Exception as e:
                logger.error(
                    f"[NOTIFIER] Error enviando alerta para "
                    f"{alert.get('player_home','?')} vs {alert.get('player_away','?')}: {e}"
                )

    # ── Pinnacle-only: partidos que GoalServe no cubre ────────
    try:
        pinnacle_tennis = pinnacle.get_all_tennis_states()
    except Exception as e:
        logger.error(f"[PINNACLE] Error obteniendo estados de mercado: {e}")
        pinnacle_tennis = {}

    gs_names = set()
    for m in matches:
        gs_names.update(_extract_surnames(m["player_home"]))
        gs_names.update(_extract_surnames(m["player_away"]))

    for event_id, market_state in pinnacle_tennis.items():
        home_surnames = _extract_surnames(market_state.get("home", ""))
        away_surnames = _extract_surnames(market_state.get("away", ""))
        if not (home_surnames & gs_names) and not (away_surnames & gs_names):
            try:
                alert = evaluate_pinnacle_only(event_id, market_state)
            except Exception as e:
                logger.error(f"[EVALUATE-PIN] Error evaluando event_id={event_id}: {e}")
                continue

            if alert:
                try:
                    level    = alert["alert_level"]
                    cooldown = ALERT_COOLDOWN_SECONDS.get(level, 300)
                    sent     = notifier.send_alert(alert, cooldown_seconds=cooldown)
                    if sent:
                        alerts_sent += 1
                except Exception as e:
                    logger.error(f"[NOTIFIER] Error enviando alerta Pinnacle-only {event_id}: {e}")

    # Limpiar partidos terminados
    try:
        tracker.cleanup_finished_matches(active_ids)
    except Exception as e:
        logger.warning(f"[CLEANUP] Error limpiando partidos terminados: {e}")

    return len(matches) + len(pinnacle_tennis), alerts_sent


# ── Loop principal ────────────────────────────────────────────

def main():
    global _cycle_count, _total_alerts_sent, _consecutive_errors

    logger.info("=" * 52)
    logger.info("  FULLTENNIS SPI BOT — iniciando")
    logger.info("=" * 52)

    _validate_config()

    # Mensaje de inicio a Telegram
    try:
        notifier.send_startup_message()
    except Exception as e:
        logger.warning(f"[STARTUP] No se pudo enviar mensaje de inicio a Telegram: {e}")

    # Pinnacle SSE en hilo daemon
    try:
        pinnacle.start_stream()
        logger.info("[PINNACLE] Stream SSE iniciado correctamente")
    except Exception as e:
        logger.critical(f"[PINNACLE] No se pudo iniciar el stream SSE: {e}")
        sys.exit(1)

    # Offset inicial
    logger.info(f"[GOALSERVE] Polling cada {GOALSERVE_POLL_INTERVAL}s — offset inicial {GOALSERVE_POLL_OFFSET}s")
    time.sleep(GOALSERVE_POLL_OFFSET)

    while True:
        cycle_start  = time.time()
        _cycle_count += 1

        try:
            evaluated, alerts = _run_cycle()
            _total_alerts_sent  += alerts
            _consecutive_errors  = 0   # reset al tener un ciclo exitoso

            logger.info(
                f"[CICLO #{_cycle_count}] "
                f"Evaluados={evaluated} | "
                f"Alertas este ciclo={alerts} | "
                f"Total alertas={_total_alerts_sent}"
            )

        except Exception as e:
            _consecutive_errors += 1
            logger.error(
                f"[CICLO #{_cycle_count}] Error inesperado "
                f"(consecutivos={_consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}\n"
                f"{traceback.format_exc()}"
            )

            if _consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.critical(
                    f"[CICLO] {MAX_CONSECUTIVE_ERRORS} errores consecutivos — "
                    f"reiniciando proceso en 60s"
                )
                try:
                    notifier.send_startup_message()   # re-notifica en Telegram
                except Exception:
                    pass
                time.sleep(60)
                _consecutive_errors = 0

        elapsed    = time.time() - cycle_start
        sleep_time = max(0, GOALSERVE_POLL_INTERVAL - elapsed)
        logger.debug(
            f"[CICLO #{_cycle_count}] "
            f"Duración={elapsed:.1f}s | "
            f"Próximo en {sleep_time:.1f}s"
        )
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
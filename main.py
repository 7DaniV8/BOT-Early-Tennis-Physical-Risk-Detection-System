# ============================================================
#  FULLTENNIS SPI BOT — main.py
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

_cycle_count        = 0
_total_alerts_sent  = 0
_consecutive_errors = 0
MAX_CONSECUTIVE_ERRORS = 10


# ── Helpers de señales ────────────────────────────────────────

def _signal_label(signal: str) -> str:
    labels = {
        "match_suspended":      "Partido suspendido / interrumpido",
        "performance_drop":     "Bajón fuerte de rendimiento",
        "consecutive_breaks":   "Pierde servicios seguidos (3+)",
        "serve_speed_drop":     "Caída del saque",
        "no_break_points":      "Sin break points generados",
        "score_frozen_10min":   "Score congelado 10+ min",
        "score_frozen_5min":    "Score congelado 5–7 min",
        "market_suspended":     "Mercado suspendido",
        "odds_disappeared":     "Cuota desaparece del mercado",
        "odds_spike":           "Cuota subió fuerte y rápido",
        "no_recovery_movement": "Movimiento fuerte sin recuperación",
        "market_slow_return":   "Mercado tardó en retomar",
    }
    return labels.get(signal, signal)


def _signal_weight(signal: str) -> int:
    from config import SPI_WEIGHTS_GOALSERVE, SPI_WEIGHTS_PINNACLE
    return SPI_WEIGHTS_GOALSERVE.get(signal, SPI_WEIGHTS_PINNACLE.get(signal, 0))


def _log_alert(player_home: str, player_away: str, tournament: str,
               status: str, cancha_spi: int, cancha_signals: list,
               market_spi: int, market_signals: list, total_spi: int,
               adjusted_spi: int, alert_level: str, both_sources: bool,
               reductions: list, veto: str = None, sent: bool = None):
    """Log limpio y legible para cada partido con señal."""
    lines = [f"\n→ {player_home} vs {player_away} | {tournament} | {status}"]

    for sig in cancha_signals:
        pts = _signal_weight(sig)
        lines.append(f"     GS  +{pts:<2} → {_signal_label(sig)}")

    for sig in market_signals:
        pts = _signal_weight(sig)
        lines.append(f"     PIN +{pts:<2} → 💰 {_signal_label(sig)}")

    if veto:
        lines.append(f"     ⛔ VETADO — {veto}")
        logger.info("\n".join(lines))
        return

    if reductions:
        lines.append(f"     ⚙️  Ajuste: {', '.join(reductions)}")

    spi_str = f"Total SPI={total_spi}"
    if adjusted_spi != total_spi:
        spi_str += f" → ajustado={adjusted_spi}"
    fuentes = "✅ ambas fuentes" if both_sources else "⚠️  fuente única"
    lines.append(f"     {spi_str} | nivel={alert_level.upper()} | {fuentes}")

    if sent is True:
        lines.append(f"     [✅ Telegram] Alerta enviada")
    elif sent is False:
        lines.append(f"     [⏸ Telegram] En cooldown — no enviada")

    logger.info("\n".join(lines))


# ── Matcher por apellido ──────────────────────────────────────

def _extract_surnames(full_name: str) -> set[str]:
    name = full_name.lower().strip()
    if len(name) > 2 and name[1] in ".":
        name = name[3:].strip()
    parts = name.replace("-", " ").split()
    return {p for p in parts if len(p) > 2}


# ── SPI Engine ────────────────────────────────────────────────

def _resolve_alert_level(spi: int, both_sources: bool) -> str:
    if spi >= SPI_THRESHOLD_RED and both_sources:
        return "red"
    if spi >= SPI_THRESHOLD_RED and not both_sources:
        return "amber"
    if spi >= SPI_THRESHOLD_AMBER and both_sources:
        return "amber"
    if spi >= SPI_THRESHOLD_AMBER and not both_sources:
        return "amber"   # antes era yellow — ahora mínimo es amber
    return None   # por debajo del umbral → no alerta


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
    tournament     = match_data["tournament"]
    status         = match_data["status"]

    try:
        pin_state = pinnacle.find_market_state_by_name(player_home, player_away)
    except Exception as e:
        logger.error(f"[EVALUATE] Error buscando Pinnacle para {player_home} vs {player_away}: {e}")
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
        _log_alert(player_home, player_away, tournament, status,
                   cancha_spi, cancha_signals, market_spi, market_signals,
                   total_spi, total_spi, "vetado", both_sources, [],
                   veto=", ".join(active_vetos))
        notifier.send_veto_log(match_id, player_home, player_away, total_spi, active_vetos)
        return None

    adjusted_spi, reductions = _apply_reductions(total_spi, is_challenger)

    if adjusted_spi < SPI_THRESHOLD_AMBER:
        return None

    alert_level = _resolve_alert_level(adjusted_spi, both_sources)
    if alert_level is None:
        return None

    return {
        "match_id":           match_id,
        "player_home":        player_home,
        "player_away":        player_away,
        "tournament":         tournament,
        "status":             status,
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
    total_games    = market_state.get("total_games", 0)

    if market_spi < SPI_THRESHOLD_AMBER:
        return None

    # Filtro: partido recién iniciado
    if total_games < 3:
        _log_alert(home, away, league, "in_progress",
                   0, [], market_spi, market_signals,
                   market_spi, market_spi, "vetado", False, [],
                   veto=f"partido recién iniciado (games={total_games})")
        return None

    is_challenger = any(k in league.lower() for k in ["challenger", "itf", "125k", "future"])
    adjusted_spi, reductions = _apply_reductions(market_spi, is_challenger)

    if adjusted_spi < SPI_THRESHOLD_AMBER:
        return None

    alert_level = "amber"   # fuente única → siempre amber, nunca rojo

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
        logger.critical(f"[CONFIG] Variables faltantes: {', '.join(missing)}")
        sys.exit(1)


# ── Un ciclo completo ─────────────────────────────────────────

def _run_cycle() -> tuple[int, int]:
    alerts_sent = 0

    try:
        matches = tracker.process_matches()
    except Exception as e:
        logger.error(f"[GOALSERVE] Fallo al obtener partidos: {e}")
        matches = []

    active_ids = [m["match_id"] for m in matches]

    # Path 1: GoalServe + cruce Pinnacle
    for match_data in matches:
        try:
            alert = evaluate_match(match_data, matches)
        except Exception as e:
            logger.error(f"[EVALUATE] {match_data.get('player_home','?')} vs {match_data.get('player_away','?')}: {e}")
            continue

        if alert:
            try:
                level    = alert["alert_level"]
                cooldown = ALERT_COOLDOWN_SECONDS.get(level, 300)
                sent     = notifier.send_alert(alert, cooldown_seconds=cooldown)
                _log_alert(
                    alert["player_home"], alert["player_away"],
                    alert["tournament"], alert["status"],
                    alert["cancha_spi"], alert["cancha_signals"],
                    alert["market_spi"], alert["market_signals"],
                    alert["total_spi"], alert["adjusted_spi"],
                    alert["alert_level"], alert["both_sources"],
                    alert["reductions"], sent=sent
                )
                if sent:
                    alerts_sent += 1
            except Exception as e:
                logger.error(f"[NOTIFIER] {alert.get('player_home','?')} vs {alert.get('player_away','?')}: {e}")

    # Path 2: Pinnacle-only
    try:
        pinnacle_tennis = pinnacle.get_all_tennis_states()
    except Exception as e:
        logger.error(f"[PINNACLE] Error obteniendo estados: {e}")
        pinnacle_tennis = {}

    gs_names = set()
    for m in matches:
        gs_names.update(_extract_surnames(m["player_home"]))
        gs_names.update(_extract_surnames(m["player_away"]))

    for event_id, market_state in pinnacle_tennis.items():
        home_s = _extract_surnames(market_state.get("home", ""))
        away_s = _extract_surnames(market_state.get("away", ""))
        if not (home_s & gs_names) and not (away_s & gs_names):
            try:
                alert = evaluate_pinnacle_only(event_id, market_state)
            except Exception as e:
                logger.error(f"[EVALUATE-PIN] event_id={event_id}: {e}")
                continue

            if alert:
                try:
                    level    = alert["alert_level"]
                    cooldown = ALERT_COOLDOWN_SECONDS.get(level, 300)
                    sent     = notifier.send_alert(alert, cooldown_seconds=cooldown)
                    _log_alert(
                        alert["player_home"], alert["player_away"],
                        alert["tournament"], alert["status"],
                        0, [],
                        alert["market_spi"], alert["market_signals"],
                        alert["total_spi"], alert["adjusted_spi"],
                        alert["alert_level"], False,
                        alert["reductions"], sent=sent
                    )
                    if sent:
                        alerts_sent += 1
                except Exception as e:
                    logger.error(f"[NOTIFIER-PIN] event_id={event_id}: {e}")

    try:
        tracker.cleanup_finished_matches(active_ids)
    except Exception as e:
        logger.warning(f"[CLEANUP] {e}")

    return len(matches) + len(pinnacle_tennis), alerts_sent


# ── Loop principal ────────────────────────────────────────────

def main():
    global _cycle_count, _total_alerts_sent, _consecutive_errors

    logger.info("=" * 52)
    logger.info("  FULLTENNIS SPI BOT — iniciando")
    logger.info("=" * 52)

    _validate_config()

    try:
        notifier.send_startup_message()
    except Exception as e:
        logger.warning(f"[STARTUP] No se pudo enviar mensaje de inicio: {e}")

    try:
        pinnacle.start_stream()
        logger.info("[PINNACLE] Stream SSE iniciado")
    except Exception as e:
        logger.critical(f"[PINNACLE] No se pudo iniciar el stream SSE: {e}")
        sys.exit(1)

    logger.info(f"[GOALSERVE] Polling cada {GOALSERVE_POLL_INTERVAL}s — offset {GOALSERVE_POLL_OFFSET}s")
    time.sleep(GOALSERVE_POLL_OFFSET)

    while True:
        cycle_start  = time.time()
        _cycle_count += 1

        try:
            evaluated, alerts = _run_cycle()
            _total_alerts_sent  += alerts
            _consecutive_errors  = 0

            logger.info(
                f"[CICLO #{_cycle_count}] "
                f"Evaluados={evaluated} | "
                f"Alertas={alerts} | "
                f"Total={_total_alerts_sent}"
            )

        except Exception as e:
            _consecutive_errors += 1
            logger.error(
                f"[CICLO #{_cycle_count}] Error inesperado "
                f"(consecutivos={_consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}\n"
                f"{traceback.format_exc()}"
            )
            if _consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.critical(f"[CICLO] {MAX_CONSECUTIVE_ERRORS} errores seguidos — reiniciando en 60s")
                try:
                    notifier.send_startup_message()
                except Exception:
                    pass
                time.sleep(60)
                _consecutive_errors = 0

        elapsed    = time.time() - cycle_start
        sleep_time = max(0, GOALSERVE_POLL_INTERVAL - elapsed)
        logger.debug(f"[CICLO #{_cycle_count}] Duración={elapsed:.1f}s | Próximo en {sleep_time:.1f}s")
        time.sleep(sleep_time)


if __name__ == "__main__":
    main()

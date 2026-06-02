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
    SPI_THRESHOLD_OBSERVE,
    SPI_THRESHOLD_RED_CHALLENGER,
    SPI_THRESHOLD_AMBER_CHALLENGER,
    SPI_THRESHOLD_OBSERVE_CHALLENGER,
    SPI_REDUCTION_CHALLENGER,
    SPI_REDUCTION_API_DELAY,
    VETO_FLAGS_HARD,
    VETO_FLAGS_SOFT,
    VETO_SOFT_IMMUNE_SIGNALS,
    ALERT_COOLDOWN_SECONDS,
    IMMEDIATE_ALERT_SIGNALS,
    STRONG_CANCHA_SIGNALS,
    STRONG_MARKET_SIGNALS,
    REQUIRE_STRONG_MARKET_FOR_CHALLENGER,
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
_last_gs_matches    = []
MAX_CONSECUTIVE_ERRORS = 10


# ── Helpers de señales ────────────────────────────────────────

def _signal_label(signal: str) -> str:
    labels = {
        # GoalServe — clásicas
        "walkover_noshown":      "🚨 Walkover / No Show",
        "match_suspended":       "Partido suspendido / interrumpido",
        "consecutive_breaks":    "3 veces a 0-40 con saque propio",
        "game_lost_from_4000":   "Perdió game desde 40-0 arriba",
        "score_frozen_10min":    "Score congelado 10+ min",
        "score_frozen_5min":     "Score congelado 5–7 min",
        # GoalServe — nuevas señales físicas
        "double_break_same_set": "🎾 Double break en el mismo set",
        "inset_collapse":        "📉 Colapso dentro del set",
        "slow_point_pace":       "🐢 Ritmo lento vs su propio promedio",
        # Pinnacle
        "odds_disappeared":      "🚨 Cuota desaparece del mercado",
        "market_suspended":      "Mercado suspendido (2–15 min)",
        "odds_spike":            "Cuota sube 15%+ rápido",
        "no_recovery_movement":  "Cuota sube 5–14% sin recuperar",
        "odds_trend":            "Tendencia alcista en 3 movimientos",
    }
    return labels.get(signal, signal)


def _signal_weight(signal: str) -> int:
    from config import SPI_WEIGHTS_GOALSERVE, SPI_WEIGHTS_PINNACLE
    return SPI_WEIGHTS_GOALSERVE.get(signal, SPI_WEIGHTS_PINNACLE.get(signal, 0))


def _log_alert(player_home, player_away, tournament, status,
               cancha_spi, cancha_signals, market_spi, market_signals,
               total_spi, adjusted_spi, alert_level, both_sources,
               reductions, veto=None, sent=None):

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

    if alert_level == "immediate":
        nivel_str = "nivel=🚨 ALERTA INMEDIATA"
        fuentes   = "✅ señal crítica"
    elif alert_level == "red":
        nivel_str = "nivel=ALERTA ROJA"
        fuentes   = "✅ doble fuente"
    elif alert_level == "amber":
        nivel_str = "nivel=AMBER"
        fuentes   = "✅ doble fuente" if both_sources else "⚠️  cancha doble señal"
    elif alert_level == "observe":
        nivel_str = "nivel=OBSERVE"
        fuentes   = "✅ doble fuente" if both_sources else "⚠️  fuente única"
    else:
        nivel_str = f"nivel={alert_level.upper()}"
        fuentes   = "✅ doble fuente" if both_sources else "⚠️  fuente única"

    lines.append(f"     {spi_str} | {nivel_str} | {fuentes}")

    if sent is True:
        lines.append(f"     [📲 Telegram] Enviada")
    elif sent is False:
        lines.append(f"     [⏸ Telegram] En cooldown — no enviada")
    elif sent is None:
        lines.append(f"     [👁️  OBSERVE] Solo log — no enviada a Telegram")

    logger.info("\n".join(lines))


# ── Matcher por apellido ──────────────────────────────────────

def _extract_surnames(full_name: str) -> set[str]:
    name = full_name.lower().strip()
    if len(name) > 2 and name[1] in ".":
        name = name[3:].strip()
    parts = name.replace("-", " ").split()
    return {p for p in parts if len(p) > 2}


# ── Lógica de combinación de señales físicas ─────────────────

def _resolve_physical_alert(
    cancha_signals: list,
    market_signals: list,
    cancha_spi: int,
    market_spi: int,
    is_challenger: bool,
) -> tuple[bool, bool, str | None]:
    """
    Decide si la combinación de señales justifica una alerta.

    Retorna: (can_send, both_sources, block_reason)

    Reglas:
      A) Señal inmediata → siempre pasa (gestionado antes de llegar aquí)
      B) 1 señal fuerte de cancha + 1 señal de mercado (cualquiera ATP/WTA)
         En Challenger/ITF: el mercado debe ser señal fuerte
      C) 2 señales fuertes de cancha → amber sin mercado
      D) Todo lo demás → observe o bloquear
    """
    all_cancha  = set(cancha_signals)
    all_market  = set(market_signals)
    strong_c    = all_cancha & STRONG_CANCHA_SIGNALS
    strong_m    = all_market & STRONG_MARKET_SIGNALS
    has_market  = market_spi > 0

    # Regla B: señal fuerte de cancha + confirmación de mercado
    if strong_c and has_market:
        if is_challenger and REQUIRE_STRONG_MARKET_FOR_CHALLENGER:
            if strong_m:
                return True, True, None
            else:
                return False, False, "challenger_requires_strong_market"
        else:
            return True, True, None

    # Regla C: 2 señales fuertes de cancha sin mercado → amber
    if len(strong_c) >= 2:
        return True, False, None

    # Regla D: señal débil sola o única señal fuerte sin mercado → observe
    if cancha_spi > 0 or market_spi > 0:
        return False, False, "insufficient_combination"

    return False, False, "no_signal"


# ── SPI Engine ────────────────────────────────────────────────

def _resolve_alert_level(spi: int, both_sources: bool,
                          is_challenger: bool = False) -> str | None:
    if is_challenger:
        t_red     = SPI_THRESHOLD_RED_CHALLENGER
        t_amber   = SPI_THRESHOLD_AMBER_CHALLENGER
        t_observe = SPI_THRESHOLD_OBSERVE_CHALLENGER
    else:
        t_red     = SPI_THRESHOLD_RED
        t_amber   = SPI_THRESHOLD_AMBER
        t_observe = SPI_THRESHOLD_OBSERVE

    if spi >= t_red and both_sources:
        return "red"
    if spi >= t_red and not both_sources:
        return "amber"
    if spi >= t_amber and both_sources:
        return "amber"
    if spi >= t_amber and not both_sources:
        return "observe"
    if spi >= t_observe:
        return "observe"
    return None


def _apply_reductions(spi: int, is_challenger: bool,
                       context_flags: dict, all_signals: set) -> tuple[int, list[str]]:
    reductions = []
    adjusted   = float(spi)

    if is_challenger:
        adjusted *= (1 - SPI_REDUCTION_CHALLENGER)
        reductions.append(f"Challenger/ITF −{int(SPI_REDUCTION_CHALLENGER*100)}%")

    # Vetos suaves — solo si las señales no son inmunes
    immune = all_signals & VETO_SOFT_IMMUNE_SIGNALS
    if not immune:
        for flag, pct in VETO_FLAGS_SOFT.items():
            if context_flags.get(flag, False):
                adjusted *= (1 - pct)
                reductions.append(f"{flag} −{int(pct*100)}%")

    return round(adjusted), reductions


def _check_hard_vetos(context_flags: dict) -> list[str]:
    return [flag for flag in VETO_FLAGS_HARD if context_flags.get(flag, False)]


def evaluate_match(match_data: dict, all_goalserve_matches: list[dict]) -> dict | None:
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

    if total_spi == 0:
        return None

    # ── Alerta inmediata ──────────────────────────────────────
    all_signals  = set(cancha_signals) | set(market_signals)
    is_immediate = any(s in IMMEDIATE_ALERT_SIGNALS for s in all_signals)
    if is_immediate:
        logger.info(f"  🚨 ALERTA INMEDIATA [{player_home} vs {player_away}]")
        return {
            "match_id":           match_id,
            "player_home":        player_home,
            "player_away":        player_away,
            "tournament":         tournament,
            "status":             status,
            "total_spi":          total_spi,
            "adjusted_spi":       total_spi,
            "alert_level":        "immediate",
            "cancha_spi":         cancha_spi,
            "cancha_signals":     cancha_signals,
            "market_spi":         market_spi,
            "market_signals":     market_signals,
            "both_sources":       cancha_spi > 0 and market_spi > 0,
            "reductions":         [],
            "context_flags":      context_flags,
            "is_challenger":      is_challenger,
            "home_odds":          pin_state.get("home_odds"),
            "away_odds":          pin_state.get("away_odds"),
            "risk_player":        pin_state.get("risk_player"),
            "opportunity_player": pin_state.get("opportunity_player"),
        }

    # ── Vetos duros ───────────────────────────────────────────
    active_hard_vetos = _check_hard_vetos(context_flags)
    if active_hard_vetos:
        _log_alert(player_home, player_away, tournament, status,
                   cancha_spi, cancha_signals, market_spi, market_signals,
                   total_spi, total_spi, "vetado", False, [],
                   veto=", ".join(active_hard_vetos))
        notifier.send_veto_log(match_id, player_home, player_away, total_spi, active_hard_vetos)
        return None

    # ── Lógica de combinación física ──────────────────────────
    can_send, both_sources, block_reason = _resolve_physical_alert(
        cancha_signals, market_signals, cancha_spi, market_spi, is_challenger
    )

    if not can_send:
        if cancha_spi > 0 or market_spi > 0:
            logger.debug(
                f"[EVALUATE] Bloqueado ({block_reason}) — "
                f"cancha={cancha_spi} market={market_spi} "
                f"[{player_home} vs {player_away}]"
            )
        return None

    # ── Reducciones (challenger + vetos suaves) ───────────────
    adjusted_spi, reductions = _apply_reductions(
        total_spi, is_challenger, context_flags, all_signals
    )

    if adjusted_spi < SPI_THRESHOLD_OBSERVE:
        return None

    alert_level = _resolve_alert_level(adjusted_spi, both_sources, is_challenger)
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
    market_spi     = market_state["market_spi"]
    market_signals = market_state["signals"]
    home           = market_state.get("home", "")
    away           = market_state.get("away", "")
    league         = market_state.get("league", "")

    if market_spi < SPI_THRESHOLD_AMBER:
        return None

    # Pinnacle-only: solo enviar si tiene señal fuerte de mercado
    strong_m = set(market_signals) & STRONG_MARKET_SIGNALS
    if not strong_m:
        logger.debug(f"[EVALUATE-PIN] Bloqueado — sin señal fuerte de mercado [{home} vs {away}]")
        return None

    home_s = _extract_surnames(home)
    away_s = _extract_surnames(away)
    for gs in _last_gs_matches:
        gs_h = _extract_surnames(gs.get("player_home", ""))
        gs_a = _extract_surnames(gs.get("player_away", ""))
        if (home_s & gs_h) and (away_s & gs_a):
            raw_match   = gs.get("raw_match", {})
            total_games = tracker.count_total_games(raw_match.get("sets", {}))
            if 0 < total_games < 3:
                _log_alert(home, away, league, "in_progress",
                           0, [], market_spi, market_signals,
                           market_spi, market_spi, "vetado", False, [],
                           veto=f"partido recién iniciado (games={total_games})")
                return None
            break

    is_challenger = any(k in league.lower() for k in ["challenger", "itf", "125k", "future"])
    adjusted_spi, reductions = _apply_reductions(market_spi, is_challenger, {}, set(market_signals))

    if adjusted_spi < SPI_THRESHOLD_AMBER:
        return None

    return {
        "match_id":           event_id,
        "player_home":        home,
        "player_away":        away,
        "tournament":         league,
        "status":             "in_progress",
        "total_spi":          market_spi,
        "adjusted_spi":       adjusted_spi,
        "alert_level":        "amber",
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
    global _last_gs_matches

    try:
        matches = tracker.process_matches()
        _last_gs_matches = matches
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
                level        = alert["alert_level"]
                both_sources = alert["both_sources"]
                cooldown     = ALERT_COOLDOWN_SECONDS.get(level, 300)

                # observe → solo log
                if level == "observe":
                    _log_alert(
                        alert["player_home"], alert["player_away"],
                        alert["tournament"], alert["status"],
                        alert["cancha_spi"], alert["cancha_signals"],
                        alert["market_spi"], alert["market_signals"],
                        alert["total_spi"], alert["adjusted_spi"],
                        "observe", both_sources, alert["reductions"], sent=None
                    )
                    continue

                sent = notifier.send_alert(alert, cooldown_seconds=cooldown)
                _log_alert(
                    alert["player_home"], alert["player_away"],
                    alert["tournament"], alert["status"],
                    alert["cancha_spi"], alert["cancha_signals"],
                    alert["market_spi"], alert["market_signals"],
                    alert["total_spi"], alert["adjusted_spi"],
                    alert["alert_level"], both_sources,
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

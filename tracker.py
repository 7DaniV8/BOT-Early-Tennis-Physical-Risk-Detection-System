# ============================================================
#  FULLTENNIS SPI BOT — tracker.py
#  Polling GoalServe cada 15s. Mantiene estado de cada partido
#  y calcula el SPI de cancha. Logs descriptivos + try/catch granular.
# ============================================================

import xml.etree.ElementTree as ET
import logging
import requests
from datetime import datetime, timezone
from config import (
    GOALSERVE_API_KEY,
    GOALSERVE_BASE_URL,
    GOALSERVE_SPORT,
    GOALSERVE_POLL_INTERVAL,
    SPI_WEIGHTS_GOALSERVE,
    SPI_REDUCTION_CHALLENGER,
    SPI_REDUCTION_API_DELAY,
    VETO_FLAGS,
)

logger = logging.getLogger(__name__)

# ── Estado en memoria ─────────────────────────────────────────
_match_states: dict[str, dict] = {}

# Contadores de diagnóstico
_fetch_errors   = 0
_fetch_ok       = 0


def _fetch_live_matches() -> list[dict]:
    """
    Llama a GoalServe y parsea el XML.
    Retorna lista vacía si falla, con log detallado del error.
    """
    global _fetch_errors, _fetch_ok
    url = f"{GOALSERVE_BASE_URL}/{GOALSERVE_API_KEY}/tennis/live"

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        _fetch_errors += 1
        logger.warning(f"[GOALSERVE] Timeout al conectar (errores acumulados: {_fetch_errors})")
        return []
    except requests.exceptions.HTTPError as e:
        _fetch_errors += 1
        logger.error(f"[GOALSERVE] HTTP error {e.response.status_code}: {e} (errores acumulados: {_fetch_errors})")
        return []
    except requests.exceptions.ConnectionError as e:
        _fetch_errors += 1
        logger.error(f"[GOALSERVE] Error de conexión: {e} (errores acumulados: {_fetch_errors})")
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        _fetch_errors += 1
        logger.error(f"[GOALSERVE] XML inválido: {e} — primeros 200 bytes: {resp.content[:200]}")
        return []

    result = []
    tournaments_found = 0

    for tournament in root.findall("tournament"):
        t_name = tournament.get("name", "").strip()
        t_id   = tournament.get("id", "")
        tournaments_found += 1

        for matches_node in tournament.findall("matches"):
            for match in matches_node.findall("match"):
                try:
                    players = match.findall("player")
                    if len(players) < 2:
                        logger.debug(f"[GOALSERVE] Partido sin 2 jugadores en torneo {t_name} — omitido")
                        continue

                    home = players[0]
                    away = players[1]

                    sets = {}
                    for i in range(1, 6):
                        h = home.get(f"set{i}", "0") or "0"
                        a = away.get(f"set{i}", "0") or "0"
                        if h != "0" or a != "0":
                            sets[f"set{i}"] = {"home": h, "away": a}

                    result.append({
                        "id":               match.get("id", ""),
                        "status":           match.get("status", ""),
                        "court":            match.get("court", ""),
                        "date":             match.get("date", ""),
                        "time":             match.get("time", ""),
                        "player_home":      home.get("name", ""),
                        "player_away":      away.get("name", ""),
                        "home_serve":       home.get("serve", "False") == "True",
                        "away_serve":       away.get("serve", "False") == "True",
                        "home_game_score":  home.get("game_score", "0"),
                        "away_game_score":  away.get("game_score", "0"),
                        "home_sets_won":    int(home.get("sets_won", "0") or 0),
                        "away_sets_won":    int(away.get("sets_won", "0") or 0),
                        "home_winner":      home.get("winner", "False") == "True",
                        "away_winner":      away.get("winner", "False") == "True",
                        "sets":             sets,
                        "_tournament_name": t_name,
                        "_tournament_id":   t_id,
                    })
                except Exception as e:
                    logger.warning(f"[GOALSERVE] Error parseando partido en {t_name}: {e}")
                    continue

    _fetch_ok += 1
    logger.debug(
        f"[GOALSERVE] Fetch OK #{_fetch_ok} — "
        f"torneos={tournaments_found} partidos={len(result)} "
        f"(errores previos: {_fetch_errors})"
    )
    return result


def _is_challenger_itf(tournament_name: str) -> bool:
    keywords = ["challenger", "itf", "125k", "future"]
    return any(k in tournament_name.lower() for k in keywords)


def count_total_games(sets: dict) -> int:
    """Función pública para usar desde main.py."""
    return _count_total_games(sets)


def _count_total_games(sets: dict) -> int:
    total = 0
    for key, val in sets.items():
        if isinstance(val, dict):
            try:
                total += int(val.get("home", 0)) + int(val.get("away", 0))
            except (ValueError, TypeError):
                pass
    return total


def _current_set_number(sets: dict) -> str:
    return str(len(sets)) if sets else "0"


def _build_context_flags(match: dict, prev: dict | None) -> dict:
    status = match.get("status", "").lower()
    sets   = match.get("sets", {})

    is_tiebreak   = "tiebreak" in status or "tie break" in status
    current_set   = _current_set_number(sets)
    prev_set      = (prev or {}).get("_current_set", "0")
    is_changeover = (current_set != prev_set) and prev is not None
    is_medical    = "medical" in status or "mto" in status
    is_raining    = any(k in status for k in ["rain", "weather", "suspended"])
    is_early      = _count_total_games(sets) < 3

    flags = {
        "is_tiebreak":        is_tiebreak,
        "is_changeover":      is_changeover,
        "is_medical_timeout": is_medical,
        "is_raining":         is_raining,
        "is_early_match":     is_early,
    }

    # Log solo si hay algún flag activo
    active = [k for k, v in flags.items() if v]
    if active:
        logger.debug(f"  ⚑  Flags activos: {', '.join(active)} [{match.get('player_home','?')} vs {match.get('player_away','?')}]")

    return flags


def _detect_score_freeze(match_id: str, current_score: str) -> int:
    state = _match_states.get(match_id, {})
    now   = datetime.now(timezone.utc)

    last_change_time = state.get("_last_score_change_time")
    last_score       = state.get("_last_score")

    if last_score != current_score:
        _match_states.setdefault(match_id, {})
        _match_states[match_id]["_last_score"]             = current_score
        _match_states[match_id]["_last_score_change_time"] = now
        return 0

    if last_change_time is None:
        return 0

    frozen_seconds = (now - last_change_time).total_seconds()

    if frozen_seconds >= 600:
        logger.debug(f"  GS  +10 → Score congelado {frozen_seconds:.0f}s (10+ min)")
        return SPI_WEIGHTS_GOALSERVE["score_frozen_10min"]
    if frozen_seconds >= 300:
        logger.debug(f"  GS  +5  → Score congelado {frozen_seconds:.0f}s (5–7 min)")
        return SPI_WEIGHTS_GOALSERVE["score_frozen_5min"]
    return 0


def _calculate_cancha_spi(match: dict, match_id: str) -> tuple[int, list[str]]:
    signals = []
    score   = 0
    status  = match.get("status", "").lower()
    home    = match.get("player_home", "?")
    away    = match.get("player_away", "?")

    # Partido suspendido / retirado
    if any(k in status for k in ["suspended", "interrupted", "walkover", "retired", "retire"]):
        signals.append("match_suspended")
        score += SPI_WEIGHTS_GOALSERVE["match_suspended"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['match_suspended']:<2} → Partido suspendido (status='{status}') [{home} vs {away}]")

    # Bajón de rendimiento por pérdida de sets
    home_sets = match.get("home_sets_won", 0)
    away_sets = match.get("away_sets_won", 0)
    prev      = _match_states.get(match_id, {})
    prev_home = prev.get("_home_sets_won", home_sets)
    if prev_home > home_sets and home_sets == 0:
        signals.append("performance_drop")
        score += SPI_WEIGHTS_GOALSERVE["performance_drop"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['performance_drop']:<2} → Bajón de rendimiento (sets: {prev_home}→{home_sets}) [{home} vs {away}]")

    # Breaks consecutivos
    home_serve   = match.get("home_serve", False)
    home_g_score = match.get("home_game_score", "0")
    away_g_score = match.get("away_game_score", "0")
    serving_losing = (
        (home_serve and home_g_score == "00" and away_g_score == "40") or
        (not home_serve and away_g_score == "00" and home_g_score == "40")
    )
    consec_breaks = prev.get("_consecutive_serving_losses", 0)
    consec_breaks = consec_breaks + 1 if serving_losing else 0
    _match_states.setdefault(match_id, {})
    _match_states[match_id]["_consecutive_serving_losses"] = consec_breaks

    if consec_breaks >= 3:
        signals.append("consecutive_breaks")
        score += SPI_WEIGHTS_GOALSERVE["consecutive_breaks"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['consecutive_breaks']:<2} → Pierde servicios seguidos ({consec_breaks}) [{home} vs {away}]")

    # Score congelado
    current_score = (
        f"{match.get('home_sets_won',0)}-{match.get('away_sets_won',0)}"
        f"/{match.get('home_game_score','0')}-{match.get('away_game_score','0')}"
    )
    freeze_pts = _detect_score_freeze(match_id, current_score)
    if freeze_pts > 0:
        tag = "score_frozen_10min" if freeze_pts == 10 else "score_frozen_5min"
        min_label = "10+ min" if freeze_pts == 10 else "5–7 min"
        signals.append(tag)
        score += freeze_pts
        logger.info(f"  GS  +{freeze_pts:<2} → Score congelado {min_label} [{home} vs {away}]")

    # Guardar estado
    _match_states[match_id]["_home_sets_won"] = home_sets
    _match_states[match_id]["_away_sets_won"] = away_sets

    return score, signals


def process_matches() -> list[dict]:
    """Fetch GoalServe + calcular SPI de cancha para cada partido."""
    matches = _fetch_live_matches()
    results = []

    for match in matches:
        match_id = str(match.get("id", ""))
        if not match_id:
            logger.debug("[GOALSERVE] Partido sin ID — omitido")
            continue

        try:
            prev_state    = _match_states.get(match_id)
            context_flags = _build_context_flags(match, prev_state)
            cancha_spi, cancha_signals = _calculate_cancha_spi(match, match_id)
            tournament_name = match.get("_tournament_name", "")
            is_challenger   = _is_challenger_itf(tournament_name)

            _match_states.setdefault(match_id, {})
            _match_states[match_id].update({
                **context_flags,
                "_current_set": _current_set_number(match.get("sets", {})),
            })

            results.append({
                "match_id":       match_id,
                "player_home":    match.get("player_home", "Home"),
                "player_away":    match.get("player_away", "Away"),
                "tournament":     tournament_name,
                "status":         match.get("status", ""),
                "cancha_spi":     cancha_spi,
                "cancha_signals": cancha_signals,
                "context_flags":  context_flags,
                "is_challenger":  is_challenger,
                "raw_match":      match,
            })
        except Exception as e:
            logger.error(f"[TRACKER] Error procesando match_id={match_id}: {e}")
            continue

    logger.info(f"[GOALSERVE] Procesados {len(results)}/{len(matches)} partidos en vivo")
    return results


def cleanup_finished_matches(active_ids: list[str]):
    removed = 0
    for mid in list(_match_states.keys()):
        if mid not in active_ids:
            del _match_states[mid]
            removed += 1
    if removed:
        logger.debug(f"[CLEANUP] {removed} partidos eliminados del estado en memoria")

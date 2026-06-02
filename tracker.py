# ============================================================
#  FULLTENNIS SPI BOT — tracker.py
#  Polling GoalServe cada 15s. Mantiene estado de cada partido
#  y calcula el SPI de cancha. Logs descriptivos + try/catch granular.
#
#  NUEVAS SEÑALES FÍSICAS:
#    · double_break_same_set  — 2 breaks de saque en el mismo set
#    · inset_collapse         — iba X-Y arriba y ahora va perdiendo ese set
#    · slow_point_pace        — ritmo del marcador más lento que su propio promedio
#    · set_multiplier         — amplifica SPI en sets avanzados (3, 4, 5)
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
    VETO_FLAGS_HARD,
    VETO_FLAGS_SOFT,
    SET_MULTIPLIER_BASE,
    SLOW_PACE_MULTIPLIER,
    INSET_COLLAPSE_MIN_LEAD,
    INSET_COLLAPSE_MIN_SWING,
)

logger = logging.getLogger(__name__)

# ── Estado en memoria ─────────────────────────────────────────
_match_states: dict[str, dict] = {}

# Contadores de diagnóstico
_fetch_errors = 0
_fetch_ok     = 0


def _fetch_live_matches() -> list[dict]:
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


def _current_set_number(sets: dict) -> int:
    return len(sets) if sets else 0


def _build_context_flags(match: dict, prev: dict | None) -> dict:
    status = match.get("status", "").lower()
    sets   = match.get("sets", {})

    is_tiebreak   = "tiebreak" in status or "tie break" in status
    current_set   = _current_set_number(sets)
    prev_set      = (prev or {}).get("_current_set", 0)
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
        # Actualizar ritmo promedio de puntos
        _update_avg_point_pace(match_id, last_change_time, now)
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


# ── Ritmo promedio de puntos ──────────────────────────────────

def _update_avg_point_pace(match_id: str, last_time, now):
    """
    Cada vez que el score cambia, registramos cuánto tardó ese punto.
    Mantenemos una media móvil de los últimos 10 puntos.
    """
    if last_time is None:
        return
    duration = (now - last_time).total_seconds()
    # Ignorar duraciones anómalas (>5 min entre puntos = changeover, MTO, etc.)
    if duration > 300 or duration < 5:
        return

    state = _match_states.setdefault(match_id, {})
    history = state.get("_point_pace_history", [])
    history.append(duration)
    if len(history) > 10:
        history = history[-10:]
    state["_point_pace_history"] = history
    state["_avg_point_pace"]     = sum(history) / len(history)


def _detect_slow_point_pace(match_id: str, seconds_since_last_change: float) -> bool:
    """
    Señal DÉBIL: el tiempo transcurrido desde el último cambio de score
    es más del doble del promedio histórico del partido.
    Solo activa si tenemos al menos 5 puntos de referencia.
    No activa si el score lleva más de 5 min sin cambiar (eso ya es score_freeze).
    """
    state   = _match_states.get(match_id, {})
    history = state.get("_point_pace_history", [])
    avg     = state.get("_avg_point_pace", 0)

    if len(history) < 5 or avg == 0:
        return False
    if seconds_since_last_change > 300:
        return False  # ya lo cubre score_freeze

    return seconds_since_last_change > avg * SLOW_PACE_MULTIPLIER


# ── Double break en el mismo set ─────────────────────────────

def _detect_double_break_same_set(match_id: str, match: dict) -> bool:
    """
    Detecta si el jugador que sirve perdió el saque 2 veces en el set actual.
    Lógica: comparamos games ganados por el servidor en el set actual
    vs el número de games que debería tener si no hubiera sido quebrado.

    Aproximación: tracking de breaks confirmados por set.
    Un break se confirma cuando el jugador que NO servía gana un game.
    """
    state       = _match_states.setdefault(match_id, {})
    sets        = match.get("sets", {})
    current_set = _current_set_number(sets)

    if current_set == 0:
        return False

    set_key     = f"set{current_set}"
    current_set_data = sets.get(set_key, {})

    try:
        home_games = int(current_set_data.get("home", 0))
        away_games = int(current_set_data.get("away", 0))
    except (ValueError, TypeError):
        return False

    home_serve = match.get("home_serve", False)

    # Breaks del set actual: si home sirve, cada game de away es un break potencial
    # Usamos la diferencia respecto al ciclo anterior para detectar breaks nuevos
    prev_set_key  = state.get("_last_set_key")
    prev_home     = state.get("_last_set_home_games", 0)
    prev_away     = state.get("_last_set_away_games", 0)
    breaks_in_set = state.get("_breaks_in_current_set", 0)

    # Reset si cambiamos de set
    if prev_set_key != set_key:
        state["_breaks_in_current_set"] = 0
        state["_last_set_key"]          = set_key
        state["_last_set_home_games"]   = home_games
        state["_last_set_away_games"]   = away_games
        return False

    # Detectar si acaba de caer un break
    if home_serve:
        # Si away ganó un game nuevo, es un break
        if away_games > prev_away:
            breaks_in_set += 1
            state["_breaks_in_current_set"] = breaks_in_set
            logger.debug(f"  GS  Break detectado (home sirve, away ganó game) — breaks_set={breaks_in_set} [{match.get('player_home','?')}]")
    else:
        # Si home ganó un game nuevo, es un break
        if home_games > prev_home:
            breaks_in_set += 1
            state["_breaks_in_current_set"] = breaks_in_set
            logger.debug(f"  GS  Break detectado (away sirve, home ganó game) — breaks_set={breaks_in_set} [{match.get('player_away','?')}]")

    state["_last_set_home_games"] = home_games
    state["_last_set_away_games"] = away_games

    return breaks_in_set >= 2


# ── Colapso dentro del set ────────────────────────────────────

def _detect_inset_collapse(match_id: str, match: dict) -> bool:
    """
    Detecta si el jugador que estaba ganando el set actual perdió
    la ventaja de forma significativa.

    Condición:
      - Tuvo una ventaja de >= INSET_COLLAPSE_MIN_LEAD games
      - Esa ventaja cayó en >= INSET_COLLAPSE_MIN_SWING games
      - Ahora va perdiendo o igualado

    Ejemplo con defaults (lead=2, swing=3):
      Iba 3-0 arriba → ahora 3-4: lead_max=3, swing=4 → colapso
      Iba 4-1 arriba → ahora 4-5: lead_max=3, swing=3 → colapso
      Iba 2-0 arriba → ahora 2-3: lead_max=2, swing=3 → colapso
    """
    state    = _match_states.setdefault(match_id, {})
    sets     = match.get("sets", {})
    current_set = _current_set_number(sets)

    if current_set == 0:
        return False

    set_key          = f"set{current_set}"
    current_set_data = sets.get(set_key, {})

    try:
        home_games = int(current_set_data.get("home", 0))
        away_games = int(current_set_data.get("away", 0))
    except (ValueError, TypeError):
        return False

    # Reset al cambiar de set
    prev_set_key = state.get("_collapse_set_key")
    if prev_set_key != set_key:
        state["_collapse_set_key"]      = set_key
        state["_collapse_peak_home"]    = home_games
        state["_collapse_peak_away"]    = away_games
        state["_collapse_peak_diff"]    = home_games - away_games
        return False

    # Actualizar picos de ventaja
    current_diff = home_games - away_games
    peak_diff    = state.get("_collapse_peak_diff", 0)

    if current_diff > peak_diff:
        state["_collapse_peak_diff"]  = current_diff
        state["_collapse_peak_home"]  = home_games
        state["_collapse_peak_away"]  = away_games

    if current_diff < -peak_diff:
        # away también podría estar colapsando
        state["_collapse_peak_diff"]  = current_diff
        state["_collapse_peak_home"]  = home_games
        state["_collapse_peak_away"]  = away_games

    peak_diff = state.get("_collapse_peak_diff", 0)

    # Evaluar colapso de HOME (iba ganando, ahora va perdiendo)
    if peak_diff >= INSET_COLLAPSE_MIN_LEAD:
        swing = peak_diff - current_diff
        if swing >= INSET_COLLAPSE_MIN_SWING and current_diff <= 0:
            logger.debug(
                f"  GS  Colapso HOME — peak_diff={peak_diff} swing={swing} actual={current_diff} "
                f"[{match.get('player_home','?')} vs {match.get('player_away','?')}]"
            )
            return True

    # Evaluar colapso de AWAY (iba ganando, ahora va perdiendo)
    if peak_diff <= -INSET_COLLAPSE_MIN_LEAD:
        swing = abs(peak_diff) - abs(current_diff) if current_diff >= 0 else abs(peak_diff) + current_diff
        # Simplificado: si la ventaja de away se revirtió
        away_peak_diff = -peak_diff
        away_swing     = away_peak_diff - (-current_diff) if current_diff >= 0 else away_peak_diff + current_diff
        if away_swing >= INSET_COLLAPSE_MIN_SWING and current_diff >= 0:
            logger.debug(
                f"  GS  Colapso AWAY — peak_diff={peak_diff} actual={current_diff} "
                f"[{match.get('player_home','?')} vs {match.get('player_away','?')}]"
            )
            return True

    return False


# ── Multiplicador por número de set ──────────────────────────

def _get_set_multiplier(sets: dict) -> float:
    """
    Set 1: x1.00
    Set 2: x1.15
    Set 3: x1.30
    Set 4: x1.45
    Set 5: x1.60

    Un problema físico en el tercer set o más es más significativo
    porque el cuerpo ya acumula fatiga.
    """
    n = _current_set_number(sets)
    if n <= 1:
        return 1.00
    return round(1.0 + (n - 1) * SET_MULTIPLIER_BASE, 2)


# ── Cálculo SPI de cancha ─────────────────────────────────────

def _calculate_cancha_spi(match: dict, match_id: str) -> tuple[int, list[str]]:
    signals = []
    score   = 0
    status  = match.get("status", "").lower()
    home    = match.get("player_home", "?")
    away    = match.get("player_away", "?")
    prev    = _match_states.get(match_id, {})
    sets    = match.get("sets", {})

    # ── Walkover / no show ────────────────────────────────────
    if any(k in status for k in ["walkover", "no_show", "noshow"]):
        signals.append("walkover_noshown")
        score += SPI_WEIGHTS_GOALSERVE["walkover_noshown"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['walkover_noshown']:<2} → 🚨 Walkover / No Show (status='{status}') [{home} vs {away}]")

    # ── Partido suspendido / retired ──────────────────────────
    elif any(k in status for k in ["suspended", "interrupted", "retired", "retire"]):
        signals.append("match_suspended")
        score += SPI_WEIGHTS_GOALSERVE["match_suspended"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['match_suspended']:<2} → Partido suspendido (status='{status}') [{home} vs {away}]")

    # ── Score congelado ───────────────────────────────────────
    is_tiebreak = "tiebreak" in status or "tie break" in status
    now = datetime.now(timezone.utc)
    last_change_time = prev.get("_last_score_change_time")
    seconds_since_change = (now - last_change_time).total_seconds() if last_change_time else 0

    if not is_tiebreak:
        current_score = (
            f"{match.get('home_sets_won',0)}-{match.get('away_sets_won',0)}"
            f"/{match.get('home_game_score','0')}-{match.get('away_game_score','0')}"
        )
        freeze_pts = _detect_score_freeze(match_id, current_score)
        if freeze_pts > 0:
            tag = "score_frozen_10min" if freeze_pts == SPI_WEIGHTS_GOALSERVE["score_frozen_10min"] else "score_frozen_5min"
            min_label = "10+ min" if tag == "score_frozen_10min" else "5–7 min"
            signals.append(tag)
            score += freeze_pts
            logger.info(f"  GS  +{freeze_pts:<2} → Score congelado {min_label} [{home} vs {away}]")
    else:
        logger.debug(f"  GS  Score freeze ignorado — tie break activo [{home} vs {away}]")

    # ── Game lost from 40-0 ───────────────────────────────────
    home_serve      = match.get("home_serve", False)
    home_g_score    = match.get("home_game_score", "0")
    away_g_score    = match.get("away_game_score", "0")
    prev_home_score = prev.get("_prev_home_game_score", "0")
    prev_away_score = prev.get("_prev_away_game_score", "0")

    lost_from_4000 = (
        (home_serve and prev_home_score == "40" and prev_away_score == "00" and home_g_score == "0") or
        (not home_serve and prev_away_score == "40" and prev_home_score == "00" and away_g_score == "0")
    )
    if lost_from_4000:
        signals.append("game_lost_from_4000")
        score += SPI_WEIGHTS_GOALSERVE["game_lost_from_4000"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['game_lost_from_4000']:<2} → Perdió game desde 40-0 arriba [{home} vs {away}]")

    # ── Consecutive breaks (señal clásica) ────────────────────
    serving_at_0_40 = (
        (home_serve and home_g_score == "00" and away_g_score == "40") or
        (not home_serve and away_g_score == "00" and home_g_score == "40")
    )
    consec_breaks = prev.get("_consecutive_serving_losses", 0)
    consec_breaks = consec_breaks + 1 if serving_at_0_40 else 0
    _match_states.setdefault(match_id, {})
    _match_states[match_id]["_consecutive_serving_losses"] = consec_breaks

    if consec_breaks >= 3:
        signals.append("consecutive_breaks")
        score += SPI_WEIGHTS_GOALSERVE["consecutive_breaks"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['consecutive_breaks']:<2} → 3 veces a 0-40 con saque propio [{home} vs {away}]")

    # ── NUEVA: Double break en el mismo set ───────────────────
    if _detect_double_break_same_set(match_id, match):
        signals.append("double_break_same_set")
        score += SPI_WEIGHTS_GOALSERVE["double_break_same_set"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['double_break_same_set']:<2} → 🎾 Double break en mismo set [{home} vs {away}]")

    # ── NUEVA: Colapso dentro del set ─────────────────────────
    if _detect_inset_collapse(match_id, match):
        signals.append("inset_collapse")
        score += SPI_WEIGHTS_GOALSERVE["inset_collapse"]
        logger.info(f"  GS  +{SPI_WEIGHTS_GOALSERVE['inset_collapse']:<2} → 📉 Colapso dentro del set [{home} vs {away}]")

    # ── NUEVA: Ritmo lento (señal débil) ─────────────────────
    # Solo si el score no está congelado (eso ya lo cubre freeze)
    # y solo si tenemos historial suficiente
    if not is_tiebreak and seconds_since_change > 0 and "score_frozen_5min" not in signals and "score_frozen_10min" not in signals:
        if _detect_slow_point_pace(match_id, seconds_since_change):
            signals.append("slow_point_pace")
            score += SPI_WEIGHTS_GOALSERVE["slow_point_pace"]
            logger.debug(f"  GS  +{SPI_WEIGHTS_GOALSERVE['slow_point_pace']:<2} → 🐢 Ritmo lento vs promedio propio [{home} vs {away}]")

    # ── Multiplicador por set ─────────────────────────────────
    if score > 0:
        multiplier = _get_set_multiplier(sets)
        if multiplier > 1.0:
            raw_score = score
            score     = round(score * multiplier)
            logger.debug(
                f"  GS  ×{multiplier} set_multiplier (set {_current_set_number(sets)}) "
                f"{raw_score}→{score} [{home} vs {away}]"
            )

    # ── Guardar estado ────────────────────────────────────────
    _match_states[match_id].update({
        "_home_sets_won":        match.get("home_sets_won", 0),
        "_away_sets_won":        match.get("away_sets_won", 0),
        "_prev_home_game_score": home_g_score,
        "_prev_away_game_score": away_g_score,
    })

    return score, signals


def process_matches() -> list[dict]:
    matches = _fetch_live_matches()
    results = []

    for match in matches:
        match_id = str(match.get("id", ""))
        if not match_id:
            logger.debug("[GOALSERVE] Partido sin ID — omitido")
            continue

        try:
            tournament_name = match.get("_tournament_name", "")
            is_challenger   = _is_challenger_itf(tournament_name)

            _match_states.setdefault(match_id, {
                "_consecutive_serving_losses": 0,
                "_prev_home_game_score":       "0",
                "_prev_away_game_score":       "0",
                "_home_sets_won":              0,
                "_away_sets_won":              0,
                "_missing_cycles":             0,
                "_point_pace_history":         [],
                "_avg_point_pace":             0,
                "_breaks_in_current_set":      0,
                "_last_set_key":               None,
                "_collapse_set_key":           None,
                "_collapse_peak_diff":         0,
            })

            _match_states[match_id]["_missing_cycles"] = 0

            prev_state    = _match_states.get(match_id)
            context_flags = _build_context_flags(match, prev_state)
            cancha_spi, cancha_signals = _calculate_cancha_spi(match, match_id)

            logger.info(
                f"[GS DEBUG] {match.get('player_home','?')} vs {match.get('player_away','?')} | "
                f"SPI={cancha_spi} | signals={cancha_signals} | "
                f"set={_current_set_number(match.get('sets',{}))} | "
                f"flags={[f for f,v in context_flags.items() if v]}"
            )

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
            _match_states[mid]["_missing_cycles"] = \
                _match_states[mid].get("_missing_cycles", 0) + 1
            if _match_states[mid]["_missing_cycles"] >= 20:
                del _match_states[mid]
                removed += 1
        else:
            _match_states[mid]["_missing_cycles"] = 0
    if removed:
        logger.debug(f"[CLEANUP] {removed} partidos eliminados tras faltar varios ciclos")

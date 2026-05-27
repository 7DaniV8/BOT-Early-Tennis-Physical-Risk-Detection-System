# ============================================================
#  FULLTENNIS SPI BOT — pinnacle.py
#  SSE stream de pinnodds.com. Logs descriptivos + try/catch granular.
# ============================================================

import json
import logging
import threading
import time
import requests
from datetime import datetime, timezone
from config import (
    PINNODDS_API_KEY,
    PINNODDS_SSE_LIVE,
    PINNODDS_MIN_DROP_PCT,
    SPI_WEIGHTS_PINNACLE,
)

logger = logging.getLogger(__name__)

_market_states: dict[str, dict] = {}
_state_lock    = threading.Lock()
SIGNAL_TTL     = 120

# Contadores de diagnóstico SSE
_events_received  = 0
_tennis_events    = 0
_reconnect_count  = 0


def _parse_event(raw_data: str):
    try:
        if raw_data.startswith("data:"):
            raw_data = raw_data[5:].strip()
        return json.loads(raw_data)
    except json.JSONDecodeError as e:
        logger.warning(f"[PINNACLE] JSON inválido en evento SSE: {e} — data='{raw_data[:100]}'")
        return None


def _process_market_event(event: dict):
    """
    Procesa un evento individual del SSE.
    Solo tenis. Guarda cuotas actuales y determina jugador en riesgo.
    """
    global _events_received, _tennis_events
    _events_received += 1

    sport = event.get("sport", "").lower()
    if sport != "tennis":
        return

    _tennis_events += 1

    event_id   = str(event.get("id", ""))
    home       = event.get("home", "")
    away       = event.get("away", "")
    league     = event.get("league", "")
    from_price = float(event.get("from_price", 0) or 0)
    to_price   = float(event.get("to_price", 0) or 0)
    outcome    = event.get("outcome", "")
    sect       = event.get("sect", "").lower()
    interval   = int(event.get("interval", 0) or 0)
    home_score = event.get("home_score")   # games/sets jugados
    away_score = event.get("away_score")

    if not event_id or from_price == 0 or to_price == 0:
        logger.debug(f"[PINNACLE] Evento tenis incompleto — id={event_id} from={from_price} to={to_price}")
        return

    drop_pct = abs((from_price - to_price) / from_price * 100)

    signals = []
    score   = 0

    if interval >= 120:
        signals.append("market_suspended")
        score += SPI_WEIGHTS_PINNACLE["market_suspended"]
        logger.info(f"  PIN +{SPI_WEIGHTS_PINNACLE['market_suspended']:<2} → 💰 Mercado suspendido (interval={interval}min) [{home} vs {away}]")
    elif interval >= 60:
        signals.append("market_slow_return")
        score += SPI_WEIGHTS_PINNACLE["market_slow_return"]
        logger.info(f"  PIN +{SPI_WEIGHTS_PINNACLE['market_slow_return']:<2} → 💰 Mercado tardó en retomar (interval={interval}min) [{home} vs {away}]")

    if drop_pct >= 15 and interval <= 30:
        signals.append("odds_spike")
        score += SPI_WEIGHTS_PINNACLE["odds_spike"]
        logger.info(f"  PIN +{SPI_WEIGHTS_PINNACLE['odds_spike']:<2} → 💰 Cuota subió fuerte y rápido (drop={drop_pct:.1f}% en {interval}min) [{home} vs {away}]")
    elif drop_pct >= PINNODDS_MIN_DROP_PCT:
        signals.append("no_recovery_movement")
        score += SPI_WEIGHTS_PINNACLE["no_recovery_movement"]
        logger.debug(f"  PIN +{SPI_WEIGHTS_PINNACLE['no_recovery_movement']:<2} → 💰 Movimiento sin recuperación (drop={drop_pct:.1f}%) [{home} vs {away}]")

    if not signals:
        return

    now = datetime.now(timezone.utc)

    # Determinar jugador en riesgo desde Moneyline
    risk_player        = None
    opportunity_player = None
    risk_odds          = None

    if sect == "moneyline":
        if "home" in outcome.lower():
            risk_player        = home
            opportunity_player = away
            risk_odds          = round(to_price, 2)
        elif "away" in outcome.lower():
            risk_player        = away
            opportunity_player = home
            risk_odds          = round(to_price, 2)

    with _state_lock:
        current = _market_states.get(event_id, {})
        existing_signals = set(current.get("signals", []))
        existing_signals.update(signals)

        current_home_odds = current.get("home_odds")
        current_away_odds = current.get("away_odds")

        if sect == "moneyline":
            if "home" in outcome.lower():
                current_home_odds = round(to_price, 2)
            elif "away" in outcome.lower():
                current_away_odds = round(to_price, 2)

        final_risk        = risk_player        or current.get("risk_player")
        final_opportunity = opportunity_player or current.get("opportunity_player")
        final_risk_odds   = risk_odds          or current.get("risk_odds")

        # Calcular total de games jugados
        try:
            total_games = (int(home_score or 0)) + (int(away_score or 0))
        except (TypeError, ValueError):
            total_games = current.get("total_games", 0)

        _market_states[event_id] = {
            "signals":            list(existing_signals),
            "market_spi":         max(current.get("market_spi", 0), score),
            "last_updated":       now,
            "home":               home,
            "away":               away,
            "league":             league,
            "home_odds":          current_home_odds,
            "away_odds":          current_away_odds,
            "risk_player":        final_risk,
            "opportunity_player": final_opportunity,
            "risk_odds":          final_risk_odds,
            "total_games":        max(current.get("total_games", 0), total_games),
        }


def _stream_worker(url: str, label: str):
    """Worker SSE con reconexión automática y backoff exponencial."""
    global _reconnect_count
    headers = {
        "x-portal-apikey": PINNODDS_API_KEY,
        "Accept":          "text/event-stream",
    }
    reconnect_wait = 5

    while True:
        try:
            logger.info(f"[PINNACLE-SSE] Conectando a {url} (reconexiones: {_reconnect_count})")
            with requests.get(url, headers=headers, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                _reconnect_count += 1
                reconnect_wait = 5   # reset backoff

                buffer = ""
                for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
                    if not chunk:
                        continue
                    buffer += chunk

                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        lines      = [l.strip() for l in event_str.splitlines() if l.strip()]
                        event_data = None
                        event_type = None

                        for line in lines:
                            if line.startswith("data:"):
                                event_data = _parse_event(line)
                            elif line.startswith("event:"):
                                event_type = line[6:].strip()

                        if event_data is None:
                            continue

                        if isinstance(event_data, dict) and event_data.get("type") == "connected":
                            logger.info(f"[PINNACLE-SSE] Conectado OK — session_id={event_data.get('id','')}")
                            continue

                        items = event_data if isinstance(event_data, list) else [event_data]
                        for item in items:
                            if isinstance(item, dict):
                                try:
                                    _process_market_event(item)
                                except Exception as e:
                                    logger.error(f"[PINNACLE-SSE] Error procesando evento: {e} — item={str(item)[:200]}")

        except requests.exceptions.HTTPError as e:
            logger.error(f"[PINNACLE-SSE] HTTP {e.response.status_code if e.response else '?'}: {e}")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"[PINNACLE-SSE] Conexión perdida: {e}")
        except requests.exceptions.Timeout:
            logger.warning(f"[PINNACLE-SSE] Timeout de lectura — reconectando")
        except Exception as e:
            logger.error(f"[PINNACLE-SSE] Error inesperado: {e}")

        logger.info(f"[PINNACLE-SSE] Reconectando en {reconnect_wait}s... (total reconexiones: {_reconnect_count})")
        time.sleep(reconnect_wait)
        reconnect_wait = min(reconnect_wait * 2, 60)


def start_stream():
    t = threading.Thread(
        target=_stream_worker,
        args=(PINNODDS_SSE_LIVE, "live"),
        daemon=True,
        name="pinnacle-sse-live",
    )
    t.start()
    logger.info("[PINNACLE-SSE] Hilo live iniciado")
    return t


# ── Helpers de estado ─────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "market_spi":         0,
        "signals":            [],
        "last_updated":       None,
        "home":               "",
        "away":               "",
        "league":             "",
        "home_odds":          None,
        "away_odds":          None,
        "risk_player":        None,
        "opportunity_player": None,
        "risk_odds":          None,
        "total_games":        0,
    }


def _export_state(state: dict) -> dict:
    return {
        "market_spi":         state["market_spi"],
        "signals":            state["signals"],
        "last_updated":       state["last_updated"],
        "home":               state.get("home", ""),
        "away":               state.get("away", ""),
        "league":             state.get("league", ""),
        "home_odds":          state.get("home_odds"),
        "away_odds":          state.get("away_odds"),
        "risk_player":        state.get("risk_player"),
        "opportunity_player": state.get("opportunity_player"),
        "risk_odds":          state.get("risk_odds"),
        "total_games":        state.get("total_games", 0),
    }


def _extract_surnames(name: str) -> set[str]:
    name = name.lower().strip()
    if len(name) > 2 and name[1] in ".":
        name = name[3:].strip()
    parts = name.replace("-", " ").split()
    return {p for p in parts if len(p) > 2}


def get_market_state(event_id: str) -> dict:
    with _state_lock:
        state = _market_states.get(str(event_id))
        if not state:
            return _empty_state()
        age = (datetime.now(timezone.utc) - state["last_updated"]).total_seconds()
        if age > SIGNAL_TTL:
            return _empty_state()
        return _export_state(state)


def find_market_state_by_name(home: str, away: str) -> dict:
    home_surnames = _extract_surnames(home)
    away_surnames = _extract_surnames(away)
    best = _empty_state()

    now = datetime.now(timezone.utc)
    with _state_lock:
        for event_id, state in _market_states.items():
            age = (now - state["last_updated"]).total_seconds()
            if age > SIGNAL_TTL:
                continue
            pin_home_s = _extract_surnames(state.get("home", ""))
            pin_away_s = _extract_surnames(state.get("away", ""))
            if (home_surnames & pin_home_s) and (away_surnames & pin_away_s):
                if state["market_spi"] > best["market_spi"]:
                    best = _export_state(state)
    return best


def get_all_tennis_states() -> dict[str, dict]:
    now = datetime.now(timezone.utc)
    result = {}
    with _state_lock:
        for event_id, state in _market_states.items():
            age = (now - state["last_updated"]).total_seconds()
            if age <= SIGNAL_TTL:
                result[event_id] = _export_state(state)
    return result


def clear_event(event_id: str):
    with _state_lock:
        _market_states.pop(str(event_id), None)
        logger.debug(f"[PINNACLE] Estado eliminado para event_id={event_id}")

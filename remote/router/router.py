"""Reset-aware router for the CLIProxyAPI hub.

CLIProxyAPI selects a credential by its integer priority. A larger value wins for each
new session. Seats with the same priority share new sessions in turn. This program sets
the priority from the quota signals that the proxy records per account. The pool then
spends first the weekly quota that expires first, and it keeps new sessions away from a
seat whose 5-hour window is almost full.

The quota signals are the rate-limit headers of the upstream service. A reader exists for
Claude and for Codex accounts. A seat of a different type has no quota view. The router
then ranks it by its weight and its cooldown only.

The program reads and writes through the management API only. It never reads a token.
It also serves the pool state on a read-only HTTP endpoint for the client statusline.
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INTERVAL_SECONDS = int(os.environ.get("ROUTER_INTERVAL_SECONDS", "60"))
# From this 5-hour use, a seat takes a new session only when no cooler seat is open.
FIVE_HOUR_WARM = float(os.environ.get("ROUTER_5H_WARM", "0.80"))
# From these limits, a seat cannot take a new session safely.
FIVE_HOUR_LIMIT = float(os.environ.get("ROUTER_5H_LIMIT", "0.95"))
SEVEN_DAY_LIMIT = float(os.environ.get("ROUTER_7D_LIMIT", "0.97"))
# Seats whose score is within this ratio of the best score share one priority. A burst
# of new sessions then spreads over them and does not fill one 5-hour window.
SHARE_RATIO = float(os.environ.get("ROUTER_SHARE_RATIO", "0.85"))
# How much the 5-hour use lowers the score of an open seat. 0 means no effect: a seat
# keeps its rank until it becomes warm. 0.5 halves the score of a seat at 100 percent.
FIVE_HOUR_WEIGHT = float(os.environ.get("ROUTER_5H_WEIGHT", "0.5"))
# The spend guard blocks a seat that can bill extra usage, from this share of a window.
GUARD_ENABLED = os.environ.get("ROUTER_SPEND_GUARD", "1") != "0"
GUARD_LIMIT = float(os.environ.get("ROUTER_SPEND_GUARD_LIMIT", "0.97"))
KEY_DIR = Path(os.environ.get("ROUTER_KEY_DIR", "/keys"))
STATE_DIR = Path(os.environ.get("ROUTER_STATE_DIR", "/state"))
STATUS_PORT = int(os.environ.get("ROUTER_STATUS_PORT", "8320"))

WEEK_HOURS = 168.0
PRIORITY_STEP = 10
TIER_TOP = {"open": 100, "warm": 40}
PRIORITY_FIVE_HOUR_FULL = 5
# The lowest priority of a seat with quota. Each full seat is below it.
PRIORITY_FLOOR = PRIORITY_FIVE_HOUR_FULL + 1
PRIORITY_WEEK_FULL = 0
# The proxy stores the header names of the upstream. The router compares them in lower case.
SIGNAL_PREFIX = "anthropic-ratelimit-unified-"
# A Codex window longer than this is the weekly window. A shorter one is the 5-hour window.
DAY_MINUTES = 1440.0


def parse_weights(text: str) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Reads 'pool=seat:0.5,seat:0.25 pool=seat:0.1' into the seat weights of each pool.

    Returns the weights and one message for each item that it cannot read.
    """
    weights: dict[str, dict[str, float]] = {}
    errors: list[str] = []
    for item in text.split():
        pool, _, pairs = item.partition("=")
        if not pool or not pairs:
            errors.append(f"'{item}' is not 'pool=seat:weight'")
            continue
        for pair in pairs.split(","):
            label, _, value = pair.rpartition(":")
            weight = as_float(value)
            if not label or weight is None or not 0.0 < weight < float("inf"):
                errors.append(f"'{pair}' in pool '{pool}' is not 'seat:weight' with a weight above 0")
                continue
            weights.setdefault(pool, {})[label] = weight
    return weights, errors


def as_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


# The weight multiplies the priority of a seat that has quota. A seat with no entry has
# the weight 1.
WEIGHTS, WEIGHT_ERRORS = parse_weights(os.environ.get("ROUTER_SEAT_WEIGHTS", ""))
# The account types that the router ranks. Empty means each type.
SEAT_TYPES = frozenset(os.environ.get("ROUTER_SEAT_TYPES", "").replace(",", " ").split())

# The management list does not always return the priority field. This record of the
# last value that the router sent stops a repeat of the same write in each cycle.
_sent: dict[tuple[str, str], int] = {}
# The newest state of each pool, for the status endpoint.
_latest: dict[str, dict] = {}
# The weight entries that name no seat, and that the log already has.
_reported: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class Seat:
    """The quota view of one account at one moment."""

    name: str
    label: str
    current_priority: int | None
    unavailable: bool
    week_used: float | None
    week_hours_left: float | None
    five_hour_used: float | None
    five_hour_hours_left: float | None = None
    week_reset_epoch: int | None = None
    five_hour_reset_epoch: int | None = None
    # True when the account can bill extra usage after its included quota.
    overage_open: bool = False
    # The account type that the proxy gives, for example "claude" or "codex".
    kind: str = ""
    # Multiplies the priority of the seat while it has quota.
    weight: float = 1.0


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def call(base_url: str, key: str, method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{base_url}/v0/management{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        text = response.read().decode()
    return json.loads(text) if text.strip() else {}


def parse_time(value: object) -> float | None:
    """Returns the epoch seconds of an ISO time, or None when the value is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def lowered(signals: dict) -> dict:
    return {str(name).lower(): value for name, value in signals.items()}


def settle(used: float | None, reset: float | None, now: float) -> tuple[float | None, float | None, int | None]:
    """Returns (utilization, hours until reset, reset time) for one rate-limit window.

    A reset time in the past means that the window ended. The seat is then empty,
    and its next window starts with the next request.
    """
    if reset is None:
        return used, None, None
    if reset <= now:
        return 0.0, None, None
    return used, (reset - now) / 3600.0, int(reset)


def window(signals: dict, name: str, now: float) -> tuple[float | None, float | None]:
    """Returns (utilization, hours until reset) for one Claude rate-limit window."""
    return claude_window(lowered(signals), name, now)[:2]


def claude_window(signals: dict, name: str, now: float) -> tuple[float | None, float | None, int | None]:
    used = as_float(signals.get(f"{SIGNAL_PREFIX}{name}-utilization"))
    return settle(used, as_float(signals.get(f"{SIGNAL_PREFIX}{name}-reset")), now)


def claude_quota(signals: dict, now: float, observed: float | None) -> dict:
    """Reads the unified rate-limit headers of a Claude account."""
    week_used, week_hours_left, week_reset = claude_window(signals, "7d", now)
    five_hour_used, five_hour_hours_left, five_hour_reset = claude_window(signals, "5h", now)
    return {
        "week_used": week_used,
        "week_hours_left": week_hours_left,
        "week_reset_epoch": week_reset,
        "five_hour_used": five_hour_used,
        "five_hour_hours_left": five_hour_hours_left,
        "five_hour_reset_epoch": five_hour_reset,
        "overage_open": str(signals.get(f"{SIGNAL_PREFIX}overage-status", "rejected")).lower() != "rejected",
    }


def codex_quota(signals: dict, now: float, observed: float | None) -> dict:
    """Reads the primary and secondary rate-limit headers of a Codex account.

    The window length tells which window a header set describes. A plan can have only
    one of the two windows, so the slot name alone does not tell.
    """
    quota: dict = {}
    for slot in ("primary", "secondary"):
        percent = as_float(signals.get(f"x-codex-{slot}-used-percent"))
        if percent is None:
            continue
        minutes = as_float(signals.get(f"x-codex-{slot}-window-minutes"))
        reset = as_float(signals.get(f"x-codex-{slot}-reset-at"))
        after = as_float(signals.get(f"x-codex-{slot}-reset-after-seconds"))
        if reset is None and after is not None and observed is not None:
            reset = observed + after
        weekly = minutes > DAY_MINUTES if minutes is not None else slot == "secondary"
        used, hours_left, reset_epoch = settle(percent / 100.0, reset, now)
        name = "week" if weekly else "five_hour"
        quota.update({f"{name}_used": used, f"{name}_hours_left": hours_left, f"{name}_reset_epoch": reset_epoch})
    return quota


# One reader per account type. Each reader returns the quota fields of a seat.
QUOTA_READERS = {"claude": claude_quota, "codex": codex_quota}


def read_seat(entry: dict, now: float) -> Seat:
    quota = entry.get("quota") or {}
    kind = str(entry.get("type") or entry.get("provider") or "").lower()
    reader = QUOTA_READERS.get(kind)
    fields = reader(lowered(quota.get("signals") or {}), now, parse_time(quota.get("observed_at"))) if reader else {}
    priority = entry.get("priority")
    # The proxy marks a seat unavailable for a cooldown and gives the retry time. A retry
    # time in the past means that the cooldown ended, so the flag no longer counts.
    retry_at = parse_time(entry.get("next_retry_after"))
    blocked = bool(entry.get("unavailable")) and (retry_at is None or retry_at > now)
    # A client matches the reset times with its own rate-limit data to find its seat.
    return Seat(
        name=entry["name"],
        label=str(entry.get("email") or entry["name"]).split("@")[0],
        current_priority=priority if isinstance(priority, int) else None,
        unavailable=blocked,
        week_used=fields.get("week_used"),
        week_hours_left=fields.get("week_hours_left"),
        five_hour_used=fields.get("five_hour_used"),
        five_hour_hours_left=fields.get("five_hour_hours_left"),
        week_reset_epoch=fields.get("week_reset_epoch"),
        five_hour_reset_epoch=fields.get("five_hour_reset_epoch"),
        overage_open=bool(fields.get("overage_open", False)),
        kind=kind,
    )


def with_weight(seat: Seat, pool: str) -> Seat:
    """Returns the seat with the weight that the settings give it in this pool."""
    return replace(seat, weight=WEIGHTS.get(pool, {}).get(seat.label, 1.0))


def state_of(seat: Seat) -> str:
    if seat.week_used is not None and seat.week_used >= SEVEN_DAY_LIMIT:
        return "week_full"
    five_hour_used = seat.five_hour_used or 0.0
    if seat.unavailable or five_hour_used >= FIVE_HOUR_LIMIT:
        return "five_hour_full"
    if five_hour_used >= FIVE_HOUR_WARM:
        return "warm"
    return "open"


def score(seat: Seat) -> float:
    """Larger means that new sessions go to this seat first.

    The base is the weekly quota that expires per hour if nobody uses the seat. Room in
    the 5-hour window scales it, so that between two equal seats the cooler one wins.
    A seat with no signal yet gets the largest value. One request then goes to it, and
    the proxy records its signals.
    """
    if seat.week_used is None and seat.week_hours_left is None:
        return float("inf")
    remaining = max(0.0, 1.0 - (seat.week_used or 0.0))
    hours_left = seat.week_hours_left if seat.week_hours_left is not None else WEEK_HOURS
    urgency = remaining / max(hours_left, 1.0)
    return urgency * (1.0 - FIVE_HOUR_WEIGHT * min(seat.five_hour_used or 0.0, 1.0))


def rank_tier(seats: list[Seat], top: int) -> dict[str, int]:
    """Gives priorities inside one tier. Seats with a similar score share a priority."""
    priorities: dict[str, int] = {}
    ranked = sorted(seats, key=lambda seat: (-score(seat), seat.name))
    priority = top
    group_best: float | None = None
    for seat in ranked:
        value = score(seat)
        if group_best is None:
            group_best = value
        elif not value >= group_best * SHARE_RATIO:
            priority -= PRIORITY_STEP
            group_best = value
        priorities[seat.name] = max(priority, PRIORITY_FLOOR)
    return priorities


def plan(seats: list[Seat]) -> dict[str, int]:
    """Returns the priority for each seat name.

    Seats of one type and one weight rank against each other. The weight then multiplies
    the priority. A seat with quota never goes below a full seat.
    """
    priorities: dict[str, int] = {}
    groups: dict[tuple[str, float, str], list[Seat]] = {}
    for seat in seats:
        state = state_of(seat)
        if state == "week_full":
            priorities[seat.name] = PRIORITY_WEEK_FULL
        elif state == "five_hour_full":
            priorities[seat.name] = PRIORITY_FIVE_HOUR_FULL
        else:
            groups.setdefault((seat.kind, seat.weight, state), []).append(seat)
    for (_, weight, state), members in groups.items():
        for name, priority in rank_tier(members, TIER_TOP[state]).items():
            priorities[name] = max(PRIORITY_FLOOR, round(priority * weight))
    return priorities


def guard_until(seat: Seat) -> int | None:
    """Returns the time until which the spend guard must block a seat, or None.

    An account with extra usage bills each request after its included quota ends. The
    guard takes such a seat out of the pool shortly before that point. A seat with no
    extra usage needs no guard, because the upstream rejects the request at no cost.
    """
    if not GUARD_ENABLED or not seat.overage_open:
        return None
    if seat.week_used is not None and seat.week_used >= GUARD_LIMIT and seat.week_reset_epoch:
        return seat.week_reset_epoch
    if seat.five_hour_used is not None and seat.five_hour_used >= GUARD_LIMIT and seat.five_hour_reset_epoch:
        return seat.five_hour_reset_epoch
    return None


def load_guard(pool: str) -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in json.loads((STATE_DIR / f"{pool}.guard.json").read_text()).items()}
    except (OSError, ValueError):
        return {}


def save_guard(pool: str, record: dict[str, int]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_DIR / f"{pool}.guard.json.tmp"
    temp.write_text(json.dumps(record))
    temp.replace(STATE_DIR / f"{pool}.guard.json")


def apply_guard(pool: str, base_url: str, key: str, entries: list[dict], now: float) -> dict[str, int]:
    """Disables a seat that is about to bill extra usage and enables it after its reset.

    The record holds only the seats that the guard disabled. A seat that a person
    disabled is not in the record, so the guard never enables it.
    """
    record = load_guard(pool)
    updated = dict(record)
    for entry in entries:
        name = entry["name"]
        if name in record:
            if now >= record[name]:
                call(base_url, key, "PATCH", "/auth-files/status", {"name": name, "disabled": False})
                del updated[name]
                log(f"{pool}: spend guard enabled {name} again after its reset")
            continue
        if entry.get("disabled"):
            continue
        until = guard_until(read_seat(entry, now))
        if until is not None:
            call(base_url, key, "PATCH", "/auth-files/status", {"name": name, "disabled": True})
            updated[name] = until
            log(f"{pool}: spend guard disabled {name}. Extra usage is open and the quota is at the guard limit.")
    if updated != record:
        save_guard(pool, updated)
    return updated


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def describe(seat: Seat, priority: int) -> dict:
    return {
        "seat": seat.label,
        "priority": priority,
        "state": state_of(seat),
        "week_used": seat.week_used,
        "week_hours_left": rounded(seat.week_hours_left),
        "five_hour_used": seat.five_hour_used,
        "five_hour_hours_left": rounded(seat.five_hour_hours_left),
        "week_reset_epoch": seat.week_reset_epoch,
        "five_hour_reset_epoch": seat.five_hour_reset_epoch,
        "unavailable": seat.unavailable,
        "type": seat.kind,
        "weight": seat.weight,
    }


def route_pool(pool: str, base_url: str) -> None:
    key = (KEY_DIR / f"{pool}.mgmt").read_text().strip()
    listing = call(base_url, key, "GET", "/auth-files")
    now = time.time()
    entries = [
        entry
        for entry in listing.get("files", [])
        if not SEAT_TYPES or str(entry.get("type") or "").lower() in SEAT_TYPES
    ]
    guarded = apply_guard(pool, base_url, key, entries, now)
    seats = [
        with_weight(read_seat(entry, now), pool)
        for entry in entries
        if not entry.get("disabled") and entry["name"] not in guarded
    ]
    for label in sorted(set(WEIGHTS.get(pool, {})) - {seat.label for seat in seats}):
        if (pool, label) not in _reported:
            _reported.add((pool, label))
            log(f"{pool}: the weight for '{label}' matches no active seat. Check the name in ROUTER_SEAT_WEIGHTS.")
    priorities = plan(seats)
    changes = []
    for seat in seats:
        wanted = priorities[seat.name]
        known = seat.current_priority if seat.current_priority is not None else _sent.get((pool, seat.name))
        if known != wanted:
            call(base_url, key, "PATCH", "/auth-files/fields", {"name": seat.name, "priority": wanted})
            _sent[(pool, seat.name)] = wanted
            changes.append(f"{seat.label}={wanted}")
    rows = sorted(
        (describe(seat, priorities[seat.name]) for seat in seats),
        key=lambda row: (-row["priority"], row["seat"]),
    )
    state = {
        "pool": pool,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "updated_epoch": int(now),
        "next_seat": rows[0]["seat"] if rows else None,
        "extra_usage_open": sorted(seat.label for seat in seats if seat.overage_open),
        "spend_guard_blocked": sorted(name for name in guarded),
        "seats": rows,
    }
    _latest[pool] = state
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = STATE_DIR / f"{pool}.json.tmp"
    temp.write_text(json.dumps(state, indent=2))
    temp.replace(STATE_DIR / f"{pool}.json")
    if changes:
        log(f"{pool}: priority changed: {', '.join(changes)}")


class StatusHandler(BaseHTTPRequestHandler):
    """Serves GET /status/<pool>. The caller sends the access key of that pool."""

    def log_message(self, format: str, *args: object) -> None:
        return

    def reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "status" or not parts[1].isalnum():
            self.reply(404, {"error": "not found"})
            return
        pool = parts[1]
        key_file = KEY_DIR / f"{pool}.access"
        if not key_file.is_file():
            self.reply(404, {"error": "unknown pool"})
            return
        sent = self.headers.get("X-Api-Key") or self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(sent.strip().encode(), key_file.read_text().strip().encode()):
            self.reply(401, {"error": "the access key is missing or wrong"})
            return
        state = _latest.get(pool)
        if state is None:
            self.reply(503, {"error": "the router has no state for this pool yet"})
            return
        self.reply(200, state)


def serve_status() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", STATUS_PORT), StatusHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"status endpoint on port {STATUS_PORT}.")


def main() -> int:
    pools = dict(item.split("=", 1) for item in os.environ.get("ROUTER_POOLS", "").split())
    if not pools:
        log("ROUTER_POOLS is empty. Set it to 'pool=url pool=url'.")
        return 2
    log(f"router started. Pools: {', '.join(pools)}. Interval: {INTERVAL_SECONDS} s.")
    for message in WEIGHT_ERRORS:
        log(f"ROUTER_SEAT_WEIGHTS: {message}. The router ignores this item.")
    for pool, weights in WEIGHTS.items():
        log(f"{pool}: seat weights: {', '.join(f'{label}={weight:g}' for label, weight in sorted(weights.items()))}")
    once = "--once" in sys.argv
    if not once:
        serve_status()
    while True:
        for pool, base_url in pools.items():
            try:
                route_pool(pool, base_url)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
                log(f"{pool}: cycle failed: {error}")
        if once:
            return 0
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())

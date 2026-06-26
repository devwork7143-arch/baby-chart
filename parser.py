"""Feeding-log free-text parser shared by the batch importer and the live bot.

Inputs: a message body (free text) and the message's wall-clock timestamp.
Output: a list of feeding event dicts with absolute start/end datetimes.

Also contains the domain-level state-mutation helpers (merge_new, apply_undo,
ingest_feeding, ingest_sleep, etc.) that have no Discord dependency — bot.py
imports them from here and wraps them with reactions and replies.
"""
import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger("bot")

TIME_RE = re.compile(r"(\d{1,2})\s*[:;.]\s*(\d{2})")
BARE3_RE = re.compile(r"(?<!\d)(\d{3,4})(?!\d)")
BARE_HOUR_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")
SIDE_RE = re.compile(r"(?<![a-z])([lr])(?![a-z])", re.I)
DONE_RE = re.compile(r"\b(done|finished|stop|end|ended)\b", re.I)
LEFT_RE = re.compile(r"\bleft\b", re.I)
RIGHT_RE = re.compile(r"\bright\b", re.I)
# `o`/`O` adjacent to a digit, treated as a typo for 0 (e.g. `1o:3o` → `10:30`)
O_AS_ZERO_RE = re.compile(r"(?<=\d)[oO]|[oO](?=\d)")

# Continuation-time matching (bare time with optional leading dash/keyword)
CONT_KW_RE = re.compile(r"^(?:done|finished|finish|stop|stopped|end|ended|ending)\s+", re.I)
CONT_TIME_RE = re.compile(r"^(\d{1,2})\s*[:;.]\s*(\d{2})$")
CONT_BARE_RE = re.compile(r"^(\d{3,4})$")

# Sleep grammar: first line starts with SS or SF
SLEEP_RE = re.compile(r"^\s*(SS|SF)(?=$|\s|\d|[:;.])", re.I)

# Diaper grammar: first line starts with `d` or `diaper` (then a variant word)
DIAPER_RE = re.compile(r"^\s*(d|diaper)(?=$|\s)", re.I)
DIAPER_VARIANTS = {"wet": "wet", "pee": "wet",
                   "dirty": "dirty", "poop": "dirty",
                   "both": "both"}

# Bottle grammar: first line starts with `B` or `bottle`, then optional oz + time(s)
BOTTLE_RE = re.compile(r"^\s*(b|bottle)(?=$|\s|\d)", re.I)
# Ounce amount: explicit "N oz" / "N ounce(s)", or a bare 1-2 digit (opt. decimal)
OZ_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:oz|ounces?)\b", re.I)
OZ_BARE_RE = re.compile(r"(?<![\d:;.])(\d{1,2}(?:\.\d+)?)(?![:;.\d])")

# Named constants for magic numbers
MAX_SESSION_MIN = 120               # sessions longer than this are treated as unfinished
MAX_STITCH_GAP = timedelta(hours=2) # max gap for stitch to close an open session
STITCH_WINDOW_H = 3                 # hours around new events to re-stitch
SLEEP_MAX_H = 16                    # a computed sleep longer than this is a forgotten SF


def normalize(s):
    """Pre-pass typo fixes: o→0 around digits, left/right → L/R."""
    s = O_AS_ZERO_RE.sub("0", s)
    s = LEFT_RE.sub("L", s)
    s = RIGHT_RE.sub("R", s)
    return s


def parse_times(s):
    """All (hour, minute) candidates in order, accepting H:MM and bare HMM/HHMM."""
    out, spans = [], []
    for m in TIME_RE.finditer(s):
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            out.append((h, mn, m.start()))
            spans.append(m.span())
    def covered(pos):
        return any(a <= pos < b for a, b in spans)
    for m in BARE3_RE.finditer(s):
        if covered(m.start()):
            continue
        v = m.group(1)
        h, mn = (int(v[0]), int(v[1:])) if len(v) == 3 else (int(v[:2]), int(v[2:]))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            out.append((h, mn, m.start()))
    out.sort(key=lambda t: t[2])
    return [(h, mn) for h, mn, _ in out]


def resolve_dt(h, mn, msg_dt):
    """Snap bare H:MM to the candidate closest to msg_dt.

    Prefer past within 24h, but allow up to 30 min future to cover the common
    case where the message is sent moments before the time it reports (e.g.
    "ending at 10:15" typed at 10:14:30).
    """
    candidates = []
    for d_offset in (-1, 0):
        d = msg_dt.date() + timedelta(days=d_offset)
        if h < 12:
            hrs = (h, (h + 12) % 24)  # AM/PM ambiguous, e.g. 6 → {06, 18}
        elif h == 12:
            hrs = (12, 0)             # 12:xx is noon OR midnight (12 AM == 00:xx)
        else:
            hrs = (h,)                # 13–23 are unambiguous 24h times
        for hr in hrs:
            candidates.append(datetime(d.year, d.month, d.day, hr, mn))
    grace = timedelta(minutes=30)
    near = [c for c in candidates if (msg_dt - c) <= timedelta(hours=24) and (c - msg_dt) <= grace]
    if near:
        return max(near)
    return min(candidates, key=lambda c: abs((c - msg_dt).total_seconds()))


def _parse_sleep_time(rest, msg_dt):
    """Resolve the time trailing an SS/SF token, or fall back to msg_dt."""
    times = parse_times(normalize(rest))
    if not times:
        return msg_dt
    h, mn = times[0]
    return resolve_dt(h, mn, msg_dt)


# Trailing word that turns an SS/SF/diaper command into an undo
UNDO_WORDS = ("undo", "oops")


def parse_sleep_message(body, msg_dt):
    """If the first line starts with SS/SF, return a sleep command dict, else None.

    {"kind": "start"|"finish"|"undo_start"|"undo_finish", "time": datetime, "raw": ...}
    """
    first = body.splitlines()[0] if body else ""
    m = SLEEP_RE.match(first)
    if not m:
        return None
    base = "start" if m.group(1).upper() == "SS" else "finish"
    rest = first[m.end():]
    if rest.strip().lower() in UNDO_WORDS:
        return {"kind": f"undo_{base}", "raw": first.strip()}
    return {"kind": base, "time": _parse_sleep_time(rest, msg_dt), "raw": first.strip()}


def parse_diaper_message(body, msg_dt):
    """If the first line is a diaper command, return a command dict, else None.

    {"kind": "log", "variant": "wet"|"dirty"|"both", "time": datetime, "raw": ...}
    {"kind": "undo", "raw": ...}   # `d undo` / `d oops`
    {"kind": "bad",  "raw": ...}   # starts with d/diaper but no recognized variant
    """
    first = body.splitlines()[0] if body else ""
    m = DIAPER_RE.match(first)
    if not m:
        return None
    rest = first[m.end():].strip()
    if rest.lower() in UNDO_WORDS:
        return {"kind": "undo", "raw": first.strip()}
    parts = rest.split(None, 1)
    word = parts[0].lower() if parts else ""
    if word not in DIAPER_VARIANTS:
        return {"kind": "bad", "raw": first.strip()}
    time_rest = parts[1] if len(parts) > 1 else ""
    return {"kind": "log", "variant": DIAPER_VARIANTS[word],
            "time": _parse_sleep_time(time_rest, msg_dt), "raw": first.strip()}


_BOTTLE_DONE_RE = re.compile(r"^(done|finished|finish|stop|stopped|end|ended|ending)\b", re.I)


def parse_bottle_message(body, msg_dt):
    """If the first line is a bottle command, return a command dict, else None.

    {"kind": "log", "oz": float|None, "start": dt|None, "end": dt|None,
     "time": dt|None, "now": msg_dt, "raw": ...}   # ingest decides open vs close
    {"kind": "done", "time": dt|None, "now": msg_dt, "raw": ...}   # B done [time]
    {"kind": "undo", "raw": ...}                                    # B undo / B oops

    oz is an explicit `N oz`/`N ounce(s)` or a bare 1-2 digit number (opt. decimal)
    that is not part of a time. Bottle times must be explicit (have a separator or
    be a 3-4 digit run) — a bare 1-2 digit number is always read as oz, not an hour.
    """
    first = body.splitlines()[0] if body else ""
    m = BOTTLE_RE.match(first)
    if not m:
        return None
    rest = first[m.end():].strip()
    low = rest.lower()
    if low in UNDO_WORDS:
        return {"kind": "undo", "raw": first.strip()}
    dm = _BOTTLE_DONE_RE.match(rest)
    if dm:
        times = parse_times(normalize(rest[dm.end():]))
        t = resolve_dt(times[0][0], times[0][1], msg_dt) if times else None
        return {"kind": "done", "time": t, "now": msg_dt, "raw": first.strip()}

    oz = None
    work = rest
    mu = OZ_UNIT_RE.search(work)
    if mu:
        oz = float(mu.group(1))
        work = work[:mu.start()] + " " + work[mu.end():]
    norm = normalize(work)
    times = parse_times(norm)
    if oz is None:
        mb = OZ_BARE_RE.search(norm)
        if mb:
            oz = float(mb.group(1))
    start = end = time = None
    if len(times) >= 2:
        start = resolve_dt(times[0][0], times[0][1], msg_dt)
        end = resolve_dt(times[1][0], times[1][1], msg_dt)
    elif len(times) == 1:
        time = resolve_dt(times[0][0], times[0][1], msg_dt)
    if oz is not None and oz == int(oz):
        oz = int(oz)
    return {"kind": "log", "oz": oz, "start": start, "end": end, "time": time,
            "now": msg_dt, "raw": first.strip()}


def parse_line(line, msg_dt):
    s = normalize(line.strip())
    if not s:
        return []
    sides = [m.group(1).upper() for m in SIDE_RE.finditer(s)]
    times = parse_times(s)
    side = sides[0] if sides else None

    # Only short-circuit to "end-only" when the line genuinely *is* an end report:
    # one time + a stop/done/finished keyword. Otherwise fall through to range pairing.
    if DONE_RE.search(s) and len(times) == 1:
        eh, em = times[0]
        return [{"side": side, "start": None, "end": resolve_dt(eh, em, msg_dt), "raw": s}]

    if len(times) >= 2:
        events = []
        i = 0
        while i + 1 < len(times):
            sh, sm = times[i]
            eh, em = times[i + 1]
            ev_side = side
            if len(sides) > 1 and (i // 2) < len(sides):
                ev_side = sides[i // 2]
            events.append({
                "side": ev_side,
                "start": resolve_dt(sh, sm, msg_dt),
                "end": resolve_dt(eh, em, msg_dt),
                "raw": s,
            })
            i += 2
        if i < len(times):
            sh, sm = times[i]
            events.append({"side": side, "start": resolve_dt(sh, sm, msg_dt), "end": None, "raw": s})
        return events

    if len(times) == 1:
        sh, sm = times[0]
        return [{"side": side, "start": resolve_dt(sh, sm, msg_dt), "end": None, "raw": s}]

    if side:
        # Fallback for "L 10" — treat a bare 1-2 digit hour as HH:00 if we have a side.
        for m in BARE_HOUR_RE.finditer(s):
            h = int(m.group(1))
            if 0 <= h <= 23:
                return [{"side": side, "start": resolve_dt(h, 0, msg_dt), "end": None, "raw": s}]
        return [{"side": side, "start": None, "end": None, "raw": s}]

    return []


def is_feeding_message(body):
    for line in body.splitlines():
        s = normalize(line)
        if SIDE_RE.search(s):
            return True
        if TIME_RE.search(s) and DONE_RE.search(s):
            return True
    return False


def parse_message(msg_dt, source, body):
    """Parse one message body into raw event dicts. Stamps source/message_ts."""
    out = []
    for line in body.splitlines():
        for ev in parse_line(line, msg_dt):
            ev["source"] = source
            ev["message_ts"] = msg_dt
            out.append(ev)
    return out


def stitch(events):
    """Close open sessions using a later same-side end-only event within MAX_STITCH_GAP.

    The end-only event that closes a session is removed from the output so
    we don't keep a dangling orphan alongside the now-closed session.
    """
    consumed = set()
    for i, ev in enumerate(events):
        if ev.get("start") and not ev.get("end"):
            for j in range(i + 1, len(events)):
                if j in consumed:
                    continue
                nx = events[j]
                if not nx.get("start") and nx.get("end") and (nx["end"] - ev["start"]) < MAX_STITCH_GAP:
                    if nx.get("side") in (None, ev["side"]):
                        ev["end"] = nx["end"]
                        consumed.add(j)
                        break
                if nx.get("start") and (nx["start"] - ev["start"]) < MAX_STITCH_GAP:
                    break
    return [ev for i, ev in enumerate(events) if i not in consumed]


def finalize(events):
    """Convert raw events to the on-disk shape: iso strings, duration_min, dedup."""
    final = []
    for ev in events:
        start, end = ev.get("start"), ev.get("end")
        if not start and not end:
            continue
        dur = None
        if start and end:
            delta = (end - start).total_seconds() / 60.0
            if delta < 0:
                end = end + timedelta(days=1)
                delta = (end - start).total_seconds() / 60.0
            if 0 <= delta <= MAX_SESSION_MIN:
                dur = round(delta, 1)
            else:
                end = None
        final.append({
            "start": start.isoformat(timespec="minutes") if start else None,
            "end": end.isoformat(timespec="minutes") if end else None,
            "side": ev.get("side"),
            "duration_min": dur,
            "oz": ev.get("oz"),
            "source": ev["source"],
            "raw": ev["raw"],
        })
    seen, deduped = set(), []
    for ev in final:
        key = (ev["start"], ev["end"], ev["side"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    deduped.sort(key=lambda e: e["start"] or e["end"] or "")
    return deduped


# ─── State management helpers (no Discord dependency) ──────────────────────────


def match_continuation_time(body, msg_dt):
    """If body is only a time (with optional leading `-`/end-keyword), return its datetime.

    Examples that match: `10:15`, `1015`, `- 10:15`, `-1o15`, `done 10:15`, `finished 1015`.
    """
    s = normalize(body.strip())
    # Strip hyphen, en-dash, em-dash, AND U+2212 minus sign (iOS/macOS autoreplace).
    s = re.sub(r"^[-–—−]+\s*", "", s)
    s = CONT_KW_RE.sub("", s)
    s = s.strip()
    m = CONT_TIME_RE.match(s)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
    else:
        m = CONT_BARE_RE.match(s)
        if not m:
            return None
        v = m.group(1)
        h, mn = (int(v[0]), int(v[1:])) if len(v) == 3 else (int(v[:2]), int(v[2:]))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return resolve_dt(h, mn, msg_dt)


def find_open_session(events, end_dt, max_duration_h=2, side=None):
    """Most recent open session that could plausibly have ended at end_dt.

    Compares against `end_dt` (the reported end), not msg_dt (when the message
    arrived). Author-agnostic. If side is provided, only matches that side.
    """
    best = None
    best_start = None
    for e in events:
        if not e.get("start") or e.get("end"):
            continue
        if side is not None and e.get("side") != side:
            continue
        start_dt = datetime.fromisoformat(e["start"])
        delta = (end_dt - start_dt).total_seconds()
        if 0 <= delta <= max_duration_h * 3600:
            if best_start is None or start_dt > best_start:
                best = e
                best_start = start_dt
    return best


def find_open_sleep(sleeps):
    """Most recent sleep with a start and no end (by start time). None if none open.

    Unbounded in time — overnight sleeps run 8–12 h, so no 2h window like feedings.
    """
    opens = [s for s in sleeps if s.get("start") and not s.get("end")]
    if not opens:
        return None
    return max(opens, key=lambda s: s["start"])


def find_user_last_event(events, source=None, window_h=12, side=None):
    """Most recent event matching the given filters within window_h hours of now.

    `source` filters by author (None = any author). `side` filters by L/R (None = any).
    Used by apply_undo for both author-specific and side-specific undo.
    """
    cutoff = datetime.now() - timedelta(hours=window_h)
    best = None
    best_anchor = None
    for e in events:
        if source is not None and e.get("source") != source:
            continue
        if side is not None and e.get("side") != side:
            continue
        anchor_str = e.get("start") or e.get("end")
        if not anchor_str:
            continue
        anchor = datetime.fromisoformat(anchor_str)
        if anchor < cutoff:
            continue
        if best_anchor is None or anchor > best_anchor:
            best = e
            best_anchor = anchor
    return best


def _existing_to_raw(e):
    anchor = e.get("start") or e.get("end")
    return {
        "start": datetime.fromisoformat(e["start"]) if e["start"] else None,
        "end": datetime.fromisoformat(e["end"]) if e["end"] else None,
        "side": e["side"], "oz": e.get("oz"), "raw": e["raw"], "source": e["source"],
        "message_ts": datetime.fromisoformat(anchor) if anchor else None,
    }


def merge_new(existing, new_raw):
    """Re-stitch + finalize a sliding window around each new event's anchor.

    Events older than ~3h before / younger than ~3h after every new event are
    passed through unchanged. If `new_raw` is empty, runs the full pipeline over
    everything (backfill/cleanup path).

    Bottle events (side "B") are always passed through untouched — they manage
    their own open/close in ingest_bottle and must never enter stitch (a bottle's
    start would otherwise block an L/R open session from being closed by a later
    continuation, and re-stitch could mangle its oz).
    """
    if not new_raw:
        combined = [_existing_to_raw(e) for e in existing
                    if (e.get("start") or e.get("end")) and e.get("side") != "B"]
        combined.sort(key=lambda e: e.get("start") or e.get("end") or e["message_ts"])
        bottles = [e for e in existing if e.get("side") == "B"]
        out = finalize(stitch(combined)) + bottles
        out.sort(key=lambda e: e.get("start") or e.get("end") or "")
        return out

    anchors = [(e.get("start") or e.get("end") or e.get("message_ts")) for e in new_raw]
    win_start = min(anchors) - timedelta(hours=STITCH_WINDOW_H)
    win_end = max(anchors) + timedelta(hours=STITCH_WINDOW_H)

    frozen = []
    active_raw = []
    for e in existing:
        if e.get("side") == "B":  # bottles never enter stitch
            frozen.append(e)
            continue
        anchor_str = e.get("start") or e.get("end")
        if not anchor_str:
            frozen.append(e)
            continue
        anchor_dt = datetime.fromisoformat(anchor_str)
        if win_start <= anchor_dt <= win_end:
            active_raw.append(_existing_to_raw(e))
        else:
            frozen.append(e)

    combined_active = active_raw + new_raw
    combined_active.sort(key=lambda e: e.get("start") or e.get("end") or e["message_ts"])
    active_finalized = finalize(stitch(combined_active))

    all_events = frozen + active_finalized
    all_events.sort(key=lambda e: e.get("start") or e.get("end") or "")
    return all_events


def apply_undo(d, source, side=None):
    """Remove a recent event from `d`. Mutates in place; returns removed event or None.

    When `side` is None: filters by `source` (global undo — removes the author's last event).
    When `side` is set: ignores `source`, filters by `side` (side-specific undo).
    Shared by the live handler and startup backfill.
    """
    if side is not None:
        target = find_user_last_event(d["events"], source=None, side=side)
    else:
        target = find_user_last_event(d["events"], source=source)
    if not target:
        return None
    d["events"].remove(target)
    return target


def apply_fix_end(d, source):
    """Flip `source`'s most recent open start into an end-only event and re-stitch.

    Mutates `d` in place. Returns (target, end_iso), or None if there's no recent
    open start. side=None on the synth-end so stitch can close a prior open session
    of any side. Shared by the live `fix_end` handler and backfill.
    """
    target = find_user_last_event(d["events"], source=source)
    if not target or not target.get("start") or target.get("end"):
        return None
    end_iso = target["start"]
    d["events"].remove(target)
    synth_end = {
        "start": None,
        "end": datetime.fromisoformat(end_iso),
        "side": None,
        "raw": target.get("raw"),
        "source": target.get("source"),
        "message_ts": datetime.fromisoformat(end_iso),
    }
    d["events"] = merge_new(d["events"], [synth_end])
    return target, end_iso


def ingest_feeding(d, body, msg_dt, source):
    """Parse one feeding message (continuation-or-parse) and merge it into `d`.

    Mutates `d` in place (events or unparsed). Returns a dict:
    {parsed, raw_events, closed_session, cont_end, added}. Shared by the live
    message handler and startup backfill so both take exactly the same path.
    """
    cont_end = match_continuation_time(body, msg_dt)
    # A bare continuation time closes an open L/R feed. Bottles are excluded from
    # the L/R lookup (they bypass stitch); they get their own close below.
    open_feeds = [e for e in d["events"] if e.get("side") != "B"]
    closed_session = find_open_session(open_feeds, cont_end) if cont_end else None

    # If no L/R feed is open, a continuation time closes an open bottle instead
    # (closed directly, since bottles never enter the stitch pipeline).
    if cont_end and not closed_session:
        open_bottle = find_open_session(d["events"], cont_end, side="B")
        if open_bottle:
            _close_bottle(open_bottle, cont_end, None)
            log.info("continuation: closing open bottle started %s at %s",
                     open_bottle["start"], cont_end)
            return {"parsed": True, "raw_events": [], "closed_session": open_bottle,
                    "cont_end": cont_end, "added": 0}

    if closed_session and cont_end:
        raw_events = [{
            "side": closed_session["side"], "start": None, "end": cont_end,
            "raw": body, "source": source, "message_ts": msg_dt,
        }]
        log.info("continuation: closing open session from %s started %s",
                 closed_session.get("source"), closed_session["start"])
    else:
        raw_events = parse_message(msg_dt, source, body)
        # Drop sideless events that have a start — they'd create "?" sessions.
        # Sideless end-only events (start=None) are kept: stitch will use them to
        # close an existing L/R session (retaining its side) or drop them as orphans.
        raw_events = [e for e in raw_events
                      if e.get("side") is not None or e.get("start") is None]
        if cont_end and not closed_session:
            log.info("continuation time parsed but no open session within 2h; treating as start")
        # If parse gave a single start-only event with a known side, check whether
        # it should close an open session for that side (extends bare-L/R toggle to
        # messages with explicit times, e.g. "R 555" closing an open R at 17:39).
        if (len(raw_events) == 1
                and raw_events[0].get("side")
                and raw_events[0].get("start")
                and raw_events[0].get("end") is None):
            ev = raw_events[0]
            open_sess = find_open_session(d["events"], ev["start"], side=ev["side"])
            if open_sess:
                closed_session = open_sess
                cont_end = ev["start"]
                raw_events = [{
                    "side": open_sess["side"], "start": None, "end": ev["start"],
                    "raw": body, "source": source, "message_ts": msg_dt,
                }]
                log.info("side+time toggle: closing open %s session started %s at %s",
                         open_sess["side"], open_sess["start"], ev["start"])

    if not raw_events:
        d.setdefault("unparsed", []).append(
            {"message_ts": msg_dt.isoformat(), "body": body, "source": source}
        )
        return {"parsed": False, "raw_events": [], "closed_session": None,
                "cont_end": cont_end, "added": 0}

    before = len(d["events"])
    d["events"] = merge_new(d["events"], raw_events)
    after = len(d["events"])
    return {"parsed": True, "raw_events": raw_events, "closed_session": closed_session,
            "cont_end": cont_end, "added": after - before}


def ingest_sleep(d, cmd, source):
    """Apply a parsed SS/SF command to `d["sleeps"]`. Mutates in place.

    Returns a result dict whose "action" is one of:
    start | finish | finish_noop | finish_too_long | undo_start | undo_finish |
    undo_start_noop | undo_finish_noop | start_dup.
    Shared by the live handler and startup backfill.
    """
    d.setdefault("sleeps", [])
    kind = cmd["kind"]

    if kind == "undo_start":
        if not d["sleeps"]:
            return {"action": "undo_start_noop", "record": None}
        target = max(d["sleeps"], key=lambda s: s.get("start", ""))
        d["sleeps"].remove(target)
        return {"action": "undo_start", "record": target}

    if kind == "undo_finish":
        finished = [s for s in d["sleeps"] if s.get("end")]
        if not finished:
            return {"action": "undo_finish_noop", "record": None}
        target = max(finished, key=lambda s: s["end"])
        cleared_end = target["end"]
        target["end"] = None
        target["duration_min"] = None
        return {"action": "undo_finish", "record": target, "cleared_end": cleared_end}

    if kind == "start":
        start_iso = cmd["time"].isoformat(timespec="minutes")
        for s in d["sleeps"]:
            if s.get("start") == start_iso and s.get("source") == source:
                return {"action": "start_dup", "record": s, "prior_open": None}
        prior_open = find_open_sleep(d["sleeps"])
        rec = {
            "start": cmd["time"].isoformat(timespec="minutes"),
            "end": None, "duration_min": None,
            "source": source, "raw": cmd["raw"],
        }
        d["sleeps"].append(rec)
        return {"action": "start", "record": rec, "prior_open": prior_open}

    target = find_open_sleep(d["sleeps"])
    if not target:
        return {"action": "finish_noop", "record": None}
    start_dt = datetime.fromisoformat(target["start"])
    end_dt = cmd["time"]
    if end_dt < start_dt:  # crossed midnight → roll the finish forward a day
        end_dt = end_dt + timedelta(days=1)
    dur = (end_dt - start_dt).total_seconds() / 60.0
    if dur > SLEEP_MAX_H * 60:
        return {"action": "finish_too_long", "record": target,
                "attempted_end": end_dt, "dur_min": dur}
    target["end"] = end_dt.isoformat(timespec="minutes")
    target["duration_min"] = int(round(dur))
    return {"action": "finish", "record": target}


def ingest_diaper(d, cmd, source):
    """Apply a parsed diaper command to `d["diapers"]`. Mutates in place.

    Returns a result dict whose "action" is one of: log | log_dup | undo |
    undo_noop. Shared by the live handler and startup backfill. Diapers are
    instantaneous events (no open/close), so this is much simpler than
    `ingest_sleep`.
    """
    d.setdefault("diapers", [])
    kind = cmd["kind"]

    if kind == "undo":
        if not d["diapers"]:
            return {"action": "undo_noop", "record": None}
        target = max(d["diapers"], key=lambda x: x.get("time", ""))
        d["diapers"].remove(target)
        return {"action": "undo", "record": target}

    time_iso = cmd["time"].isoformat(timespec="minutes")
    variant = cmd["variant"]
    for x in d["diapers"]:  # idempotent backfill replays
        if (x.get("time") == time_iso and x.get("variant") == variant
                and x.get("source") == source):
            return {"action": "log_dup", "record": x}
    rec = {"time": time_iso, "variant": variant, "source": source, "raw": cmd["raw"]}
    d["diapers"].append(rec)
    return {"action": "log", "record": rec}


def _bottle_event(start, end, oz, source, raw):
    """Build a finalized bottle event dict (side B), capping/rolling like finalize."""
    dur = None
    if start and end:
        delta = (end - start).total_seconds() / 60.0
        if delta < 0:
            end = end + timedelta(days=1)
            delta = (end - start).total_seconds() / 60.0
        if 0 <= delta <= MAX_SESSION_MIN:
            dur = round(delta, 1)
        else:
            end = None
    return {
        "start": start.isoformat(timespec="minutes") if start else None,
        "end": end.isoformat(timespec="minutes") if end else None,
        "side": "B", "duration_min": dur, "oz": oz, "source": source, "raw": raw,
    }


def _append_bottle(d, rec):
    d["events"].append(rec)
    d["events"].sort(key=lambda e: e.get("start") or e.get("end") or "")


def _find_bottle_at(events, iso, source):
    """A `source`'s bottle event anchored (start or end) at `iso`, or None.

    Used as the backfill dup-guard so replaying the same `B …` message is a no-op.
    """
    for e in events:
        if (e.get("side") == "B" and e.get("source") == source
                and (e.get("start") == iso or e.get("end") == iso)):
            return e
    return None


def ingest_bottle(d, cmd, source):
    """Apply a parsed bottle command to `d["events"]` (side "B"). Mutates in place.

    Bottles are feedings stored in the events array, but manage their own
    open/close here (no reliance on stitch). Returns a result dict whose
    "action" is one of: log | open | closed | undo | log_dup | undo_noop |
    closed_noop. Shared by the live handler and startup backfill.
    """
    kind = cmd["kind"]

    if kind == "undo":
        target = find_user_last_event(d["events"], source=None, side="B")
        if not target:
            return {"action": "undo_noop", "record": None}
        d["events"].remove(target)
        return {"action": "undo", "record": target}

    now = cmd.get("now")

    if kind == "done":
        when = cmd.get("time") or now
        open_b = find_open_session(d["events"], when, side="B")
        if not open_b:
            return {"action": "closed_noop", "record": None}
        return {"action": "closed", "record": _close_bottle(open_b, when, None)}

    # kind == "log"
    oz, start, end, time = cmd["oz"], cmd["start"], cmd["end"], cmd["time"]

    if start and end:  # explicit range → a complete bottle
        dup = _find_bottle_at(d["events"], start.isoformat(timespec="minutes"), source)
        if dup:  # idempotent backfill replay
            return {"action": "log_dup", "record": dup}
        rec = _bottle_event(start, end, oz, source, cmd["raw"])
        _append_bottle(d, rec)
        return {"action": "log", "record": rec}

    if time:  # single time → close an open bottle, else open one
        open_b = find_open_session(d["events"], time, side="B")
        if open_b and datetime.fromisoformat(open_b["start"]) <= time:
            return {"action": "closed", "record": _close_bottle(open_b, time, oz)}
        dup = _find_bottle_at(d["events"], time.isoformat(timespec="minutes"), source)
        if dup:  # replay of a bottle already opened/closed at this time
            return {"action": "log_dup", "record": dup}
        rec = _bottle_event(time, None, oz, source, cmd["raw"])
        _append_bottle(d, rec)
        return {"action": "open", "record": rec}

    # no times: bare `B`, or oz-only (`B 4`)
    # Guard first: a B event already anchored at `now` is a replay of this very
    # message — return it untouched rather than duplicating it or (for a bare-B
    # open) spuriously closing the bottle we opened on the prior pass.
    dup = _find_bottle_at(d["events"], now.isoformat(timespec="minutes"), source)
    if dup:
        return {"action": "log_dup", "record": dup}
    open_b = find_open_session(d["events"], now, side="B")
    if open_b:
        return {"action": "closed", "record": _close_bottle(open_b, now, oz)}
    if oz is not None:  # instant bottle now with an amount
        rec = _bottle_event(now, now, oz, source, cmd["raw"])
        _append_bottle(d, rec)
        return {"action": "log", "record": rec}
    rec = _bottle_event(now, None, oz, source, cmd["raw"])  # bare B → open now
    _append_bottle(d, rec)
    return {"action": "open", "record": rec}


def _close_bottle(open_b, end_dt, oz):
    """Close an open bottle event in place: set end/duration, attach oz if given."""
    start_dt = datetime.fromisoformat(open_b["start"])
    if end_dt < start_dt:
        end_dt = end_dt + timedelta(days=1)
    dur = (end_dt - start_dt).total_seconds() / 60.0
    open_b["end"] = end_dt.isoformat(timespec="minutes")
    open_b["duration_min"] = round(dur, 1) if 0 <= dur <= MAX_SESSION_MIN else None
    if oz is not None:
        open_b["oz"] = oz
    return open_b

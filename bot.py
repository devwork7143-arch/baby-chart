"""Discord feeding bot + local chart server.

Listens to one Discord channel; every text message goes through the same
free-text parser used for the historical Signal backfill. Parsed events are
appended to feedings.json. The same process serves the interactive chart at
http://127.0.0.1:8000/ for the rich view at home.
"""
import asyncio
import difflib
import io
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import discord
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from parser import (
    apply_fix_end, apply_undo, find_open_session, ingest_bottle, ingest_diaper,
    ingest_feeding, ingest_sleep, merge_new, parse_bottle_message,
    parse_diaper_message, parse_sleep_message,
)
from render import (
    BOTTLE_CHARTS, CHARTS, DIAPER_CHARTS, NAP_TOPIC_PREFIX, SLEEP_CHARTS,
    TOPIC_PREFIX, build_bottle_data, build_chart_data, build_diaper_data,
    build_html, build_png, build_sleep_data, build_text_stats,
    compute_last_completed_line, format_events_brief, render_bottle_chart,
    render_chart, render_diaper_chart, render_sleep_chart,
)
from nap import predict_next_nap

SIDE_UNDO_RE = re.compile(r"^([LRlr])\s+(undo|oops)$", re.I)
SIDE_TOGGLE_RE = re.compile(r"^([LRlr])(?:\s+(done|stop|finish|finished|end|ended|ending|stopped))?$", re.I)
WAKE_RE = re.compile(r"^wake\s+(\d+|off|auto(?:\s+off)?)$", re.I)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("bot")
# discord.py is chatty at INFO; quiet it down.
logging.getLogger("discord").setLevel(logging.WARNING)

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["FEEDING_CHANNEL_ID"])

BABY_NAME = os.environ.get("BABY_NAME", "Baby")


def _parse_dob(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        log.warning("BABY_DOB=%r is not YYYY-MM-DD; nap prediction disabled", s)
        return None


BABY_DOB = _parse_dob(os.environ.get("BABY_DOB", ""))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

ROOT = Path(__file__).parent
_data_dir = Path(os.environ.get("DATA_DIR", str(ROOT)))
_data_dir.mkdir(parents=True, exist_ok=True)  # cloud volume / DATA_DIR may not exist yet
FEEDINGS_PATH = _data_dir / "feedings.json"

TRIGGERS_CHART = ("chart", "today", "week", "graph")
TRIGGERS_STATS = ("stats", "summary")
TRIGGERS_HELP = ("help", "commands")
TRIGGERS_LIST = ("listcharts", "list_charts")
TRIGGERS_UNDO = ("undo", "oops")                       # exact-match only — destructive
TRIGGERS_FIX_END = ("fix_end", "fixend", "fix end")    # exact-match only — destructive
# ALL_TRIGGERS is what the *fuzzy* matcher considers. Destructive commands are
# left out on purpose so a stray "fixed it" can't accidentally trigger them.
ALL_TRIGGERS = TRIGGERS_CHART + TRIGGERS_STATS + TRIGGERS_HELP + TRIGGERS_LIST


def match_chart_key(body):
    """Return ("feeding"|"sleep"|"diaper", key) for an exact (normalized) chart-name match."""
    s = body.strip().lower()
    if not s:
        return None
    s = re.sub(r"\s+", "_", s)
    if s in CHARTS:
        return ("feeding", s)
    if s in SLEEP_CHARTS:
        return ("sleep", s)
    if s in DIAPER_CHARTS:
        return ("diaper", s)
    if s in BOTTLE_CHARTS:
        return ("bottle", s)
    return None

HELP_TEXT = (
    f"**{BABY_NAME} feeding bot** — every message in this channel is a feeding entry.\n"
    "\n"
    "**Log a feed** (case-insensitive, typos OK):\n"
    "```\n"
    "L 10:00 - 10:10        # side + start–end\n"
    "right 9:27 - 9:45      # `left`/`right` words also work\n"
    "L 1o:3o - 1o:4o        # `o` next to digits → 0\n"
    "R 802 - 810            # bare 3/4-digit times\n"
    "L 10                   # bare hour → 10:00 (open session)\n"
    "L done 10:12           # end only (done/finished/stop/end/ended)\n"
    "```\n"
    "\n"
    "**Log a bottle** (a feed; oz optional, shows as its own color):\n"
    "```\n"
    "B 4 10:00 - 10:15      # 4 oz bottle, start–end\n"
    "B 10:00                # open a bottle (close later)\n"
    "B 4                    # close the open bottle now, 4 oz (or instant 4 oz)\n"
    "B done                 # close the open bottle (no oz)\n"
    "B undo                 # remove the most recent bottle\n"
    "```\n"
    "\n"
    "**Log sleep** (separate from feeds, no L/R):\n"
    "```\n"
    "SS                     # sleep start now\n"
    "SS 22:30               # sleep start at 22:30\n"
    "SF                     # finish the open sleep now\n"
    "SF 6:15                # finish at 6:15 (rolls past midnight automatically)\n"
    "SS undo                # remove the most recent sleep start\n"
    "SF undo                # re-open the most recently finished sleep\n"
    "```\n"
    "\n"
    "**Log a diaper** (separate from feeds):\n"
    "```\n"
    "d wet                  # pee (`pee` also works)\n"
    "d dirty                # poop (`poop` also works)\n"
    "d both                 # both\n"
    "d wet 14:30            # back-time an entry\n"
    "d undo                 # remove the most recent diaper\n"
    "```\n"
    "\n"
    "**Split across messages** — works for ~2 h:\n"
    "```\n"
    "L 10:00                # open session\n"
    "- 10:15                # closes the most recent open same-author session\n"
    "```\n"
    "\n"
    "**Other commands** (fuzzy-matched, so `chrt`/`stat`/`hlp` all work):\n"
    "• `chart` / `today` / `week` / `graph` — multi-panel PNG summary\n"
    "• `listcharts` — list every individual chart you can request by name\n"
    "• any chart name (e.g. `timeline`, `gap_night`, `heatmap`) — that chart as PNG\n"
    "• sleep charts: `sleep_timeline`, `sleep_clock`, `sleep_daily`\n"
    "• diaper chart: `diaper_daily`\n"
    "• bottle charts: `bottle_oz`, `bottle_avg`, `bottle_count`, `bottle_clock`\n"
    "• `stats` / `summary` — text summary\n"
    "• `undo` / `oops` / `^` — remove your most recent event\n"
    "• `fix_end` (or `fixend`) — convert your most recent open start into an end "
    "and re-stitch (use when a `- 10:15` continuation got logged as a start)\n"
    "• `wake 90` / `wake 0` (or `wake off`) — set, or disable, a max wake time; "
    f"bot posts once when {BABY_NAME} has been awake that long with no SS logged\n"
    "• `wake auto` / `wake auto off` — auto-set the wake time from each sleep's "
    "predicted next-nap target (needs `BABY_DOB`)\n"
    "• `help` / `commands` — this message\n"
    "\n"
    "Reactions: 🍼 = feed logged · 😴 = sleep logged · 🚼 = diaper logged · 🤔 = couldn't parse · ⚠️ = problem\n"
    f"Live interactive chart: http://{HOST}:{PORT}/ (on the host machine)"
)


def chunk_message(text, limit=2000):
    """Split text into ≤limit-char pieces for Discord's 2000-char message cap.

    Splits on blank lines first (keeps code fences / sections intact), then on
    single newlines if a section is still too long. A single line longer than
    limit is hard-sliced as a last resort.
    """
    chunks = []
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            chunks.append(buf)
            buf = ""

    for para in text.split("\n\n"):
        piece = para + "\n\n"
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        flush()
        if len(piece) <= limit:
            buf = piece
            continue
        # section itself too long: fall back to line-by-line packing
        for line in para.split("\n"):
            ln = line + "\n"
            if len(buf) + len(ln) > limit:
                flush()
                while len(ln) > limit:           # single oversized line
                    chunks.append(ln[:limit])
                    ln = ln[limit:]
            buf += ln
        flush()
    flush()
    return [c.rstrip("\n") for c in chunks] or [""]


def match_trigger(body):
    """Return the canonical trigger word if body is a typo-tolerant match.

    Only fires for short, digit-free messages so feeding entries can't trip it.
    """
    s = re.sub(r"[^a-z]", "", body.lower())
    if not s or len(s) > 12:
        return None
    if any(c.isdigit() for c in body):
        return None
    matches = difflib.get_close_matches(s, ALL_TRIGGERS, n=1, cutoff=0.75)
    return matches[0] if matches else None

state_lock = asyncio.Lock()


def load_feedings():
    if FEEDINGS_PATH.exists():
        d = json.loads(FEEDINGS_PATH.read_text())
        d.setdefault("sleeps", [])
        d.setdefault("diapers", [])
        d.setdefault("wake_limit_min", None)
        d.setdefault("wake_reminder_sent", False)
        d.setdefault("wake_auto", False)
        return d
    return {"generated_at": datetime.now().isoformat(timespec="microseconds"),
            "events": [], "unparsed": [], "sleeps": [], "diapers": []}


def save_feedings(data):
    data["generated_at"] = datetime.now().isoformat(timespec="microseconds")
    FEEDINGS_PATH.write_text(json.dumps(data, indent=2))


# ─── Small bot-layer helpers ───────────────────────────────────────────────────


def hhmm(iso_str):
    return iso_str[11:] if iso_str else "?"


def make_raw_event(side, start, end, body, source, msg_dt, oz=None):
    return {"side": side, "start": start, "end": end, "oz": oz,
            "raw": body, "source": source, "message_ts": msg_dt}


def format_closed_reply(side, closed_session, cont_end):
    s = hhmm(closed_session["start"])
    t = cont_end.isoformat(timespec="minutes")[11:]
    dur_min = int((cont_end - datetime.fromisoformat(closed_session["start"])).total_seconds() // 60)
    return f"closed: {side} {s}–{t} ({dur_min} min)"


def compute_double_open_warning(events, source_label, msg_dt):
    opens = sorted(
        (e for e in events if e.get("start") and not e.get("end")),
        key=lambda e: e["start"],
    )
    if len(opens) < 2:
        return None
    latest = opens[-1]
    latest_dt = datetime.fromisoformat(latest["start"])
    if not (latest.get("source") == source_label
            and abs((latest_dt - msg_dt).total_seconds()) < 120):
        return None
    prior = opens[-2]
    prior_dt = datetime.fromisoformat(prior["start"])
    if (latest_dt - prior_dt) > timedelta(hours=2):
        return None
    p_side = prior["side"] or "?"
    p_start = hhmm(prior["start"])
    new_start = hhmm(latest["start"])
    dur = int((latest_dt - prior_dt).total_seconds() // 60)
    who = "" if prior.get("source") == source_label else f" ({prior['source']})"
    return (
        f"\n⚠️ {p_side} {p_start}{who} is still open. "
        f"send `fix_end` to close it as {p_side} {p_start}–{new_start} ({dur} min)"
    )


def apply_side_toggle(d, side, msg_dt, source, body, is_done=False):
    open_sess = find_open_session(d["events"], msg_dt, side=side)
    if open_sess:
        d["events"] = merge_new(d["events"], [make_raw_event(side, None, msg_dt, body, source, msg_dt)])
        return {"action": "closed", "open_sess": open_sess}
    if not is_done:
        d["events"] = merge_new(d["events"], [make_raw_event(side, msg_dt, None, body, source, msg_dt)])
        return {"action": "started"}
    return {"action": "noop"}


# ─── Channel topic ─────────────────────────────────────────────────────────────


def topic_snapshot(d):
    """In-lock snapshot of the state fields update_channel_topic reads."""
    return {
        "events": list(d["events"]),
        "sleeps": list(d.get("sleeps", [])),
        "wake_limit_min": d.get("wake_limit_min"),
        "wake_auto": d.get("wake_auto"),
    }


def compute_nap_target_line(snap):
    """Topic line for the next nap target, or None to omit.

    Mode order (auto first, since auto also leaves wake_limit_min armed):
      • wake auto on              → predicted (predict_next_nap)
      • manual `wake N` set       → fixed (last_sleep_end + N min)
      • wake off but BABY_DOB set → predicted
      • else / no finished sleep / baby asleep → None
    """
    sleeps = snap.get("sleeps", [])
    if any(s["end"] is None for s in sleeps):
        return None  # currently asleep — no next-nap target yet
    finished = [s for s in sleeps if s["end"]]
    if not finished:
        return None
    last = max(finished, key=lambda s: s["end"])
    sleep_end = datetime.fromisoformat(last["end"])
    if snap.get("wake_auto"):
        target = predict_next_nap(BABY_DOB, sleep_end, last["duration_min"])
    elif snap.get("wake_limit_min") is not None:
        target = sleep_end + timedelta(minutes=snap["wake_limit_min"])
    elif BABY_DOB is not None:
        target = predict_next_nap(BABY_DOB, sleep_end, last["duration_min"])
    else:
        target = None
    if target is None:
        return None
    return f"{NAP_TOPIC_PREFIX} {target.isoformat(timespec='minutes')[11:]}"


async def update_channel_topic(channel, snap):
    """Update the bot-managed topic lines (feed, then nap). Silent if no permission."""
    feed_line = compute_last_completed_line(snap["events"])
    nap_line = compute_nap_target_line(snap)
    managed = [feed_line] + ([nap_line] if nap_line else [])
    current = channel.topic or ""
    others = [ln for ln in (current.split("\n") if current else [])
              if not ln.startswith(TOPIC_PREFIX) and not ln.startswith(NAP_TOPIC_PREFIX)]
    new_topic = "\n".join(managed + others)[:1024]  # Discord topic max length
    if new_topic == current:
        return  # no change → skip API call (topic edits are rate-limited)
    try:
        await channel.edit(topic=new_topic)
        log.info("topic updated: %s", " | ".join(managed))
    except discord.Forbidden:
        log.warning("topic not updated: missing 'Manage Channels' permission")
    except discord.HTTPException as exc:
        log.warning("topic update failed: %s", exc)


# ─── Discord client ────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

_backfilled = False


async def backfill_missed(channel):
    """Replay messages sent while the bot was offline, then refresh the topic."""
    global _backfilled
    _backfilled = True
    startup = datetime.now().astimezone()
    gen = load_feedings().get("generated_at")
    after_dt = datetime.fromisoformat(gen).astimezone()

    applied = 0
    async with state_lock:
        d = load_feedings()
        dirty = False
        async for msg in channel.history(after=after_dt, before=startup,
                                          oldest_first=True, limit=1000):
            if msg.author == client.user:
                continue
            body = (msg.content or "").strip()
            if not body:
                continue

            # Skip pure view commands — re-rendering old charts would be noise.
            if match_chart_key(body):
                continue
            trig = match_trigger(body)
            bn = body.lower()

            if bn in TRIGGERS_UNDO or body == "^":
                if apply_undo(d, msg.author.name):
                    await msg.add_reaction("↩️")
                    applied += 1
                    dirty = True
                continue
            if bn in TRIGGERS_FIX_END:
                if apply_fix_end(d, msg.author.name):
                    await msg.add_reaction("🔁")
                    applied += 1
                    dirty = True
                continue
            if trig in (TRIGGERS_CHART + TRIGGERS_STATS + TRIGGERS_HELP
                        + TRIGGERS_LIST) or body == "?":
                continue

            msg_dt = msg.created_at.astimezone().replace(tzinfo=None)

            side_undo_match = SIDE_UNDO_RE.match(body.strip())
            if side_undo_match:
                side = side_undo_match.group(1).upper()
                if apply_undo(d, msg.author.name, side=side):
                    await msg.add_reaction("↩️")
                    applied += 1
                    dirty = True
                else:
                    await msg.add_reaction("🤔")
                continue

            side_match = SIDE_TOGGLE_RE.match(body.strip())
            if side_match:
                side = side_match.group(1).upper()
                is_done = side_match.group(2) is not None
                res = apply_side_toggle(d, side, msg_dt, msg.author.name, body, is_done)
                if res["action"] == "noop":
                    await msg.add_reaction("🤔")
                else:
                    await msg.add_reaction("🍼")
                    applied += 1
                    dirty = True
                continue

            sleep_cmd = parse_sleep_message(body, msg_dt)
            if sleep_cmd:
                sres = ingest_sleep(d, sleep_cmd, msg.author.name)
                dirty = True
                if sres["action"] in ("start", "finish", "undo_start", "undo_finish"):
                    d["wake_reminder_sent"] = False
                if sres["action"] in ("start", "finish"):
                    await msg.add_reaction("😴")
                    applied += 1
                elif sres["action"] in ("undo_start", "undo_finish"):
                    await msg.add_reaction("↩️")
                    applied += 1
                elif sres["action"] == "finish_too_long":
                    await msg.add_reaction("⚠️")
                elif sres["action"] == "start_dup":
                    pass  # replay of an already-logged start — no-op, no reaction
                else:  # *_noop
                    await msg.add_reaction("🤔")
                continue

            diaper_cmd = parse_diaper_message(body, msg_dt)
            if diaper_cmd and diaper_cmd["kind"] != "bad":
                dres = ingest_diaper(d, diaper_cmd, msg.author.name)
                dirty = True
                if dres["action"] == "log":
                    await msg.add_reaction("🚼")
                    applied += 1
                elif dres["action"] == "undo":
                    await msg.add_reaction("↩️")
                    applied += 1
                elif dres["action"] == "log_dup":
                    pass  # replay of an already-logged diaper — no-op, no reaction
                else:  # undo_noop
                    await msg.add_reaction("🤔")
                continue

            bottle_cmd = parse_bottle_message(body, msg_dt)
            if bottle_cmd:
                bres = ingest_bottle(d, bottle_cmd, msg.author.name)
                dirty = True
                if bres["action"] in ("log", "open", "closed"):
                    await msg.add_reaction("🍼")
                    applied += 1
                elif bres["action"] == "undo":
                    await msg.add_reaction("↩️")
                    applied += 1
                elif bres["action"] == "log_dup":
                    pass  # replay of an already-logged bottle — no-op, no reaction
                else:  # undo_noop / closed_noop
                    await msg.add_reaction("🤔")
                continue

            res = ingest_feeding(d, body, msg_dt, msg.author.name)
            if res["parsed"] and (res["added"] > 0 or res["closed_session"]):
                dirty = True
                await msg.add_reaction("🍼")
                applied += 1
            elif res["parsed"]:
                await msg.add_reaction("🤔")
            else:
                await msg.add_reaction("🤔")

        if dirty:
            save_feedings(d)
        topic_snap = topic_snapshot(d)

    log.info("backfill: applied %d missed message(s)", applied)
    if applied:
        await channel.send(f"⏪ caught up {applied} missed message(s) while I was offline")
    await update_channel_topic(channel, topic_snap)


async def _check_wake_reminder():
    d = load_feedings()
    if not d.get("wake_limit_min") or d.get("wake_reminder_sent"):
        return
    sleeps = d.get("sleeps", [])
    if any(s["end"] is None for s in sleeps):
        return  # baby is currently asleep
    finished = [s for s in sleeps if s["end"] is not None]
    if not finished:
        return
    last_end_str = max(finished, key=lambda s: s["end"])["end"]
    last_end = datetime.fromisoformat(last_end_str)
    elapsed_min = (datetime.now() - last_end).total_seconds() / 60
    if elapsed_min < d["wake_limit_min"]:
        return
    async with state_lock:
        d = load_feedings()
        if not d.get("wake_limit_min") or d.get("wake_reminder_sent"):
            return
        if any(s["end"] is None for s in d.get("sleeps", [])):
            return
        d["wake_reminder_sent"] = True
        save_feedings(d)
    ch = client.get_channel(CHANNEL_ID)
    if ch:
        h, m = divmod(int(elapsed_min), 60)
        elapsed_str = f"{h}h {m}min" if h else f"{m}min"
        await ch.send(f"⏰ {BABY_NAME} has been awake for {elapsed_str} — send `SS` to start a sleep log.")
        log.info("Wake reminder sent (elapsed %.0f min)", elapsed_min)


async def wake_reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(60)
        try:
            await _check_wake_reminder()
        except Exception:
            log.exception("wake reminder check failed")


@client.event
async def on_ready():
    log.info("Connected as %s; watching channel %d", client.user, CHANNEL_ID)
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        return
    if not _backfilled:
        asyncio.create_task(wake_reminder_loop())
        try:
            await backfill_missed(ch)
            return
        except Exception:
            log.exception("backfill failed; continuing without catch-up")
    await update_channel_topic(ch, topic_snapshot(load_feedings()))


@client.event
async def on_message(msg: discord.Message):
    if msg.author == client.user:
        return
    if msg.channel.id != CHANNEL_ID:
        return

    body = (msg.content or "").strip()
    if not body:
        return

    chart_match = match_chart_key(body)
    if chart_match:
        kind, chart_key = chart_match
        try:
            raw = load_feedings()
            if kind == "sleep":
                data = build_sleep_data(raw["sleeps"], raw["generated_at"])
                png = await asyncio.to_thread(render_sleep_chart, chart_key, data)
                title = SLEEP_CHARTS[chart_key][0]
            elif kind == "diaper":
                data = build_diaper_data(raw["diapers"], raw["generated_at"])
                png = await asyncio.to_thread(render_diaper_chart, chart_key, data)
                title = DIAPER_CHARTS[chart_key][0]
            elif kind == "bottle":
                data = build_bottle_data(raw["events"], raw["generated_at"])
                png = await asyncio.to_thread(render_bottle_chart, chart_key, data)
                title = BOTTLE_CHARTS[chart_key][0]
            else:
                data = build_chart_data(raw["events"], raw["generated_at"])
                png = await asyncio.to_thread(render_chart, chart_key, data)
                title = CHARTS[chart_key][0]
            file = discord.File(io.BytesIO(png), filename=f"{chart_key}.png")
            await msg.channel.send(content=f"**{title}**", file=file)
        except Exception:
            log.exception("per-chart render failed for %s", chart_key)
            await msg.add_reaction("⚠️")
        return

    trigger = match_trigger(body)
    body_norm = body.strip().lower()

    if body_norm in TRIGGERS_UNDO or body.strip() == "^":
        async with state_lock:
            d = load_feedings()
            target = apply_undo(d, msg.author.name)
            if not target:
                await msg.channel.send("nothing to undo (no recent event from you)")
                return
            save_feedings(d)
            topic_snap = topic_snapshot(d)
        side = target.get("side") or "?"
        s = hhmm(target.get("start"))
        t = hhmm(target.get("end"))
        await msg.add_reaction("↩️")
        await msg.reply(f"undid: {side} {s}–{t} (raw: {target.get('raw')!r})",
                        mention_author=False, silent=True)
        log.info("Undo: removed event raw=%r source=%s", target.get("raw"), msg.author.name)
        asyncio.create_task(update_channel_topic(msg.channel, topic_snap))
        return

    if body_norm in TRIGGERS_FIX_END:
        async with state_lock:
            d = load_feedings()
            result = apply_fix_end(d, msg.author.name)
            if not result:
                await msg.channel.send("nothing to fix (no recent open start from you)")
                return
            target, end_iso = result
            save_feedings(d)
            topic_snap = topic_snapshot(d)
        await msg.add_reaction("🔁")
        await msg.reply(f"flipped your last entry to an end at {hhmm(end_iso)}; re-stitched",
                        mention_author=False, silent=True)
        log.info("FixEnd: flipped event raw=%r source=%s", target.get("raw"), msg.author.name)
        asyncio.create_task(update_channel_topic(msg.channel, topic_snap))
        return

    if trigger in TRIGGERS_LIST:
        lines = ["**Available charts** — send any name to get just that chart:"]
        for k, (title, _) in CHARTS.items():
            lines.append(f"• `{k}` — {title}")
        lines.append("**Sleep charts:**")
        for k, (title, _) in SLEEP_CHARTS.items():
            lines.append(f"• `{k}` — {title}")
        lines.append("**Diaper charts:**")
        for k, (title, _) in DIAPER_CHARTS.items():
            lines.append(f"• `{k}` — {title}")
        lines.append("**Bottle charts:**")
        for k, (title, _) in BOTTLE_CHARTS.items():
            lines.append(f"• `{k}` — {title}")
        lines.append("\nAlso: `chart` for the multi-panel summary, `stats` for a text recap.")
        await msg.channel.send("\n".join(lines))
        return

    if trigger in TRIGGERS_CHART:
        try:
            raw = load_feedings()
            data = build_chart_data(raw["events"], raw["generated_at"])
            sleep = build_sleep_data(raw["sleeps"], raw["generated_at"])
            diaper = build_diaper_data(raw["diapers"], raw["generated_at"])
            bottle = build_bottle_data(raw["events"], raw["generated_at"])
            png = await asyncio.to_thread(build_png, data, 14, sleep, diaper, bottle)
            file = discord.File(io.BytesIO(png), filename="feedings.png")
            await msg.channel.send(content=build_text_stats(data, bottle), file=file)
        except Exception:
            log.exception("chart render failed")
            await msg.add_reaction("⚠️")
        return

    if trigger in TRIGGERS_STATS:
        raw = load_feedings()
        data = build_chart_data(raw["events"], raw["generated_at"])
        bottle = build_bottle_data(raw["events"], raw["generated_at"])
        await msg.channel.send(build_text_stats(data, bottle))
        return

    if trigger in TRIGGERS_HELP or body.strip() == "?":
        for chunk in chunk_message(HELP_TEXT):
            await msg.channel.send(chunk)
        return

    wake_match = WAKE_RE.match(body.strip())
    if wake_match:
        arg = re.sub(r"\s+", " ", wake_match.group(1).lower())  # "auto  off" → "auto off"
        if arg == "auto" and BABY_DOB is None:
            await msg.reply("wake auto needs BABY_DOB set — not enabled",
                            mention_author=False, silent=True)
            return
        async with state_lock:
            d = load_feedings()
            if arg == "auto":
                d["wake_auto"] = True
                d["wake_limit_min"] = None  # clear any stale manual limit; arms on next SF
                d["wake_reminder_sent"] = False
            elif arg == "auto off":
                d["wake_auto"] = False
            elif arg == "off" or arg == "0":
                d["wake_limit_min"] = None
                d["wake_reminder_sent"] = False
                d["wake_auto"] = False  # manual clears auto
            else:
                d["wake_limit_min"] = int(arg)
                d["wake_reminder_sent"] = False
                d["wake_auto"] = False  # manual clears auto
            save_feedings(d)
            topic_snap = topic_snapshot(d)
        if arg == "auto":
            await msg.reply("wake auto on — arms at each sleep's predicted next-nap target",
                            mention_author=False, silent=True)
        elif arg == "auto off":
            await msg.reply("wake auto off", mention_author=False, silent=True)
        elif arg == "off" or arg == "0":
            await msg.reply("wake limit cleared", mention_author=False, silent=True)
        else:
            await msg.reply(f"wake limit set to {int(arg)} min", mention_author=False, silent=True)
        asyncio.create_task(update_channel_topic(msg.channel, topic_snap))
        return

    # discord.py gives tz-aware UTC. Convert to system-local then drop tz so
    # comparisons line up with the local-time naive datetimes stored in feedings.json.
    msg_dt = msg.created_at.astimezone().replace(tzinfo=None)
    source_label = msg.author.name

    # Sleep grammar (SS/SF) is intercepted before feeding parsing.
    sleep_cmd = parse_sleep_message(body, msg_dt)
    if sleep_cmd:
        nap_target = None
        async with state_lock:
            d = load_feedings()
            res = ingest_sleep(d, sleep_cmd, source_label)
            if res["action"] in ("start", "finish", "undo_start", "undo_finish"):
                d["wake_reminder_sent"] = False
            if res["action"] == "finish":
                r = res["record"]
                nap_target = predict_next_nap(BABY_DOB, datetime.fromisoformat(r["end"]),
                                              r["duration_min"])
                if d.get("wake_auto") and nap_target:  # arm the wake reminder
                    d["wake_limit_min"] = round(
                        (nap_target - datetime.fromisoformat(r["end"])).total_seconds() / 60)
            save_feedings(d)
            topic_snap = topic_snapshot(d)
        action = res["action"]
        if action == "start":
            await msg.add_reaction("😴")
            t = sleep_cmd["time"].isoformat(timespec="minutes")[11:]
            reply = f"sleep start: {t}"
            prior = res["prior_open"]
            if prior:
                p_start = hhmm(prior["start"])
                who = "" if prior.get("source") == source_label else f" ({prior['source']})"
                reply += (f"\n⚠️ a sleep started {p_start}{who} is still open "
                          f"(SF closes the most recent — `SS undo`, then `SF the sleep started at {p_start}`, then re-send this)")
            await msg.reply(reply, mention_author=False, silent=True)
        elif action == "finish":
            r = res["record"]
            await msg.add_reaction("😴")
            reply = f"sleep: {hhmm(r['start'])}–{hhmm(r['end'])} ({r['duration_min']} min)"
            if nap_target:
                reply += f"\n🍼 Next nap target: {nap_target.isoformat(timespec='minutes')[11:]}"
                if d.get("wake_auto"):
                    reply += " (wake auto armed)"
            await msg.reply(reply, mention_author=False, silent=True)
        elif action == "undo_start":
            r = res["record"]
            await msg.add_reaction("↩️")
            tail = f"–{hhmm(r['end'])}" if r.get("end") else " (was open)"
            await msg.reply(f"removed sleep start: {hhmm(r['start'])}{tail}",
                            mention_author=False, silent=True)
        elif action == "undo_finish":
            r = res["record"]
            await msg.add_reaction("↩️")
            await msg.reply(f"re-opened sleep started {hhmm(r['start'])} "
                            f"(cleared finish {hhmm(res['cleared_end'])})",
                            mention_author=False, silent=True)
        elif action == "start_dup":
            await msg.add_reaction("😴")
            await msg.reply(f"sleep start already logged: {hhmm(res['record']['start'])}",
                            mention_author=False, silent=True)
        elif action == "undo_start_noop":
            await msg.add_reaction("🤔")
            await msg.reply("no sleep to undo", mention_author=False, silent=True)
        elif action == "undo_finish_noop":
            await msg.add_reaction("🤔")
            await msg.reply("no finished sleep to re-open", mention_author=False, silent=True)
        elif action == "finish_noop":
            await msg.add_reaction("🤔")
            await msg.reply("no open sleep to finish", mention_author=False, silent=True)
        else:  # finish_too_long
            await msg.add_reaction("⚠️")
            await msg.reply("that sleep would be >16h — left open; check the start time",
                            mention_author=False, silent=True)
        log.info("Sleep %s by %s", action, source_label)
        asyncio.create_task(update_channel_topic(msg.channel, topic_snap))
        return

    # Diaper grammar (d wet / d dirty / d both) — intercepted before feeding parsing.
    diaper_cmd = parse_diaper_message(body, msg_dt)
    if diaper_cmd:
        if diaper_cmd["kind"] == "bad":
            await msg.add_reaction("🤔")
            await msg.reply("try: `d wet` · `d dirty` · `d both` (optionally `d wet 14:30`)",
                            mention_author=False, silent=True)
            return
        async with state_lock:
            d = load_feedings()
            res = ingest_diaper(d, diaper_cmd, source_label)
            save_feedings(d)
        action = res["action"]
        if action == "log":
            r = res["record"]
            await msg.add_reaction("🚼")
            await msg.reply(f"diaper: {r['variant']} ({hhmm(r['time'])})",
                            mention_author=False, silent=True)
        elif action == "log_dup":
            await msg.add_reaction("🚼")
        elif action == "undo":
            r = res["record"]
            await msg.add_reaction("↩️")
            await msg.reply(f"removed diaper: {r['variant']} ({hhmm(r['time'])})",
                            mention_author=False, silent=True)
        else:  # undo_noop
            await msg.add_reaction("🤔")
            await msg.reply("no diaper to undo", mention_author=False, silent=True)
        log.info("Diaper %s by %s", action, source_label)
        return

    # Bottle grammar (B / bottle + oz) — intercepted before the L/R side handlers.
    bottle_cmd = parse_bottle_message(body, msg_dt)
    if bottle_cmd:
        async with state_lock:
            d = load_feedings()
            res = ingest_bottle(d, bottle_cmd, source_label)
            save_feedings(d)
        action = res["action"]
        r = res.get("record")

        def _bottle_tail(rec):
            bits = []
            if rec.get("oz") is not None:
                bits.append(f"{rec['oz']} oz")
            if rec.get("duration_min"):
                bits.append(f"{rec['duration_min']} min")
            return f" ({', '.join(bits)})" if bits else ""

        if action in ("log", "closed"):
            await msg.add_reaction("🍼")
            verb = "logged" if action == "log" else "closed"
            span = f"{hhmm(r['start'])}–{hhmm(r['end'])}" if r.get("end") else hhmm(r["start"])
            await msg.reply(f"{verb}: B {span}{_bottle_tail(r)}",
                            mention_author=False, silent=True)
        elif action == "open":
            await msg.add_reaction("🍼")
            await msg.reply(f"bottle open: B {hhmm(r['start'])}{_bottle_tail(r)}",
                            mention_author=False, silent=True)
        elif action == "log_dup":
            await msg.add_reaction("🍼")
        elif action == "undo":
            await msg.add_reaction("↩️")
            span = f"{hhmm(r['start'])}–{hhmm(r['end'])}" if r.get("end") else hhmm(r["start"])
            await msg.reply(f"removed bottle: B {span}{_bottle_tail(r)}",
                            mention_author=False, silent=True)
        else:  # undo_noop / closed_noop
            await msg.add_reaction("🤔")
            await msg.reply("no open bottle / nothing to undo", mention_author=False, silent=True)
        log.info("Bottle %s by %s", action, source_label)
        return

    # Side-specific undo: L undo / R oops
    side_undo_match = SIDE_UNDO_RE.match(body.strip())
    if side_undo_match:
        side = side_undo_match.group(1).upper()
        async with state_lock:
            d = load_feedings()
            target = apply_undo(d, source_label, side=side)
            if not target:
                await msg.add_reaction("🤔")
                await msg.reply(f"no recent {side} event to undo", mention_author=False, silent=True)
                log.info("SideUndo %s by %s: noop", side, source_label)
                return
            save_feedings(d)
            topic_snap = topic_snapshot(d)
        s = hhmm(target.get("start"))
        t = hhmm(target.get("end"))
        await msg.add_reaction("↩️")
        await msg.reply(f"removed: {side} {s}–{t}" if target.get("end") else f"removed: {side} {s} (was open)",
                        mention_author=False, silent=True)
        log.info("SideUndo %s by %s: removed raw=%r", side, source_label, target.get("raw"))
        asyncio.create_task(update_channel_topic(msg.channel, topic_snap))
        return

    # Bare L/R toggle + L/R done: start a session, or close an open one for that side
    side_match = SIDE_TOGGLE_RE.match(body.strip())
    if side_match:
        side = side_match.group(1).upper()
        is_done = side_match.group(2) is not None
        async with state_lock:
            d = load_feedings()
            res = apply_side_toggle(d, side, msg_dt, source_label, body, is_done)
            if res["action"] == "noop":
                await msg.add_reaction("🤔")
                await msg.reply(f"no open {side} session to close", mention_author=False, silent=True)
                log.info("SideToggle %s done by %s: noop", side, source_label)
                return
            save_feedings(d)
            topic_snap = topic_snapshot(d)
            warning = (compute_double_open_warning(d["events"], source_label, msg_dt)
                       if res["action"] == "started" else None)

        await msg.add_reaction("🍼")
        if res["action"] == "closed":
            reply = format_closed_reply(side, res["open_sess"], msg_dt)
        else:  # started
            t = msg_dt.isoformat(timespec="minutes")[11:]
            reply = f"logged: {side} {t}"
            if warning:
                reply += warning
        await msg.reply(reply, mention_author=False, silent=True)
        log.info("SideToggle %s %s by %s", side, res["action"], source_label)
        asyncio.create_task(update_channel_topic(msg.channel, topic_snap))
        return

    # All state inspection + mutation happens inside the lock.
    async with state_lock:
        d = load_feedings()
        res = ingest_feeding(d, body, msg_dt, source_label)

        if not res["parsed"]:
            save_feedings(d)
            await msg.add_reaction("🤔")
            log.info("Unparsed message logged (%d chars)", len(body))
            return

        raw_events = res["raw_events"]
        closed_session = res["closed_session"]
        cont_end = res["cont_end"]
        added = res["added"]
        save_feedings(d)
        topic_snap = topic_snapshot(d)

        # Guard: if parse succeeded but nothing was actually added (all events were
        # finalized away as null-null), treat as unparsed.
        # Exception: if a session was closed via the side+time toggle, added is 0
        # because stitch updates the existing event in-place rather than adding one.
        if added == 0 and not closed_session:
            await msg.add_reaction("🤔")
            log.info("Parsed but added 0 events (all finalized away)")
            return

        warning = compute_double_open_warning(d["events"], source_label, msg_dt)

    await msg.add_reaction("🍼")
    log.info("Logged %d new event(s)", added)
    if closed_session:
        side = closed_session["side"] or "?"
        reply = format_closed_reply(side, closed_session, cont_end)
    else:
        new_stored = d["events"][-added:] if added > 0 else []
        reply = f"logged: {format_events_brief(new_stored)}"
    if warning:
        reply += warning
    await msg.reply(reply, mention_author=False, silent=True)
    asyncio.create_task(update_channel_topic(msg.channel, topic_snap))


# ─── Local chart server ────────────────────────────────────────────────────────

api = FastAPI()


@api.get("/", response_class=HTMLResponse)
async def index():
    return build_html(data=None)


@api.get("/data.json")
async def data_json():
    raw = load_feedings()
    data = build_chart_data(raw["events"], raw["generated_at"])
    data["sleep"] = build_sleep_data(raw["sleeps"], raw["generated_at"])
    data["diaper"] = build_diaper_data(raw["diapers"], raw["generated_at"])
    data["bottle"] = build_bottle_data(raw["events"], raw["generated_at"])
    return JSONResponse(data)


@api.get("/health")
async def health():
    raw = load_feedings()
    return {"events": len(raw["events"]), "generated_at": raw["generated_at"]}


async def main():
    config = uvicorn.Config(api, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await asyncio.gather(client.start(TOKEN), server.serve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

The baby's breastfeeding log → live chart. One Python process runs:

1. A **Discord bot** that listens to a dedicated channel. Every text message is
   parsed by the same free-text feeding parser, appended to `feedings.json`,
   and confirmed with 🍼 (or 🤔 if nothing parses).
2. A **local FastAPI server** on `127.0.0.1:8000` serving the interactive
   Chart.js dashboard for the rich view at home.
3. On the `chart` (or `today`/`week`/`graph`) trigger in the channel, the bot
   renders a matplotlib PNG summary and posts it back — useful from a phone.

## Files

- `parser.py` — free-text parser: `parse_message`, `stitch`, `finalize`,
  `is_feeding_message`, plus `parse_sleep_message` (SS/SF grammar) +
  `ingest_diaper`, `parse_diaper_message` (`d`/`diaper` grammar), and
  `parse_bottle_message` + `ingest_bottle` (`B`/`bottle` grammar, side `B`).
  Shared by every entry path.
- `render.py` —
  - `build_chart_data(events, generated_at)` shapes events into the chart-data
    dict consumed by both renderers (now also emits `daily_B` bottle minutes).
  - `build_sleep_data(sleeps, generated_at)` shapes the separate `sleeps` array
    into the sleep-chart data dict (independent of feeding stats).
  - `build_diaper_data(diapers, generated_at)` shapes the separate `diapers`
    array into per-day wet/dirty/both counts (independent of feeding stats).
  - `build_bottle_data(events, generated_at)` filters side-`B` events into
    per-day oz / bottle-count / avg-oz arrays + an oz-by-hour clock heatmap.
  - `build_html(data=None)` returns the interactive Chart.js page; `data=None`
    means live mode (browser polls `/data.json`).
  - `build_png(data, window_days=14, sleep=None, diaper=None, bottle=None)`
    returns PNG bytes for Discord (the `diaper`/`bottle` dicts add the
    "Diapers today"/"Bottles today" text lines).
  - `CHARTS` / `SLEEP_CHARTS` / `DIAPER_CHARTS` / `BOTTLE_CHARTS` registries +
    `render_chart` / `render_sleep_chart` / `render_diaper_chart` /
    `render_bottle_chart` render one named chart to PNG.
  - `python3 render.py` writes a static `chart.html` for offline viewing.
- `bot.py` — discord.py client + uvicorn server in one asyncio loop.
- `feedings.json` — the only durable state. Schema:
  `{generated_at, events: [{start, end, side, duration_min, oz, source, raw}], unparsed: [], sleeps: [{start, end, duration_min, source, raw}], diapers: [{time, variant, source, raw}]}`.
  `side` is `L`/`R`/`B`(bottle)/`null`; `oz` is set only on bottle events.
- `.env` — `DISCORD_TOKEN`, `FEEDING_CHANNEL_ID` (gitignored).
- `.env.example` — template to copy.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # then fill in token + channel ID
.venv/bin/python bot.py
```

The bot connects to Discord and the chart server is up at
`http://127.0.0.1:8000/`. Use Ctrl-C to stop both.

## Discord setup (one-time)

1. https://discord.com/developers/applications → New Application → Bot tab →
   Reset Token, copy the token.
2. Same page, toggle **Message Content Intent** ON under "Privileged Gateway
   Intents".
3. OAuth2 → URL Generator → scopes: `bot`; permissions: View Channels, Send
   Messages, Read Message History, Add Reactions, Attach Files. Open the URL
   and invite the bot to a server you own.
4. In Discord, Settings → Advanced → enable Developer Mode. Right-click your
   feedings channel → Copy Channel ID.
5. Put both values in `.env`.

## Channel grammar

| Message body              | Bot behavior                                   |
|---------------------------|------------------------------------------------|
| `L 10:00 - 10:10`         | parse → append → 🍼 + reply `logged: L 10:00–10:10` |
| `R 9:27 - 9:45`           | same                                           |
| `L 10` (then later) `- 10:15` | first message logs `L 10:00` open; second closes it → 🍼 + reply `closed: L 10:00–10:15` |
| Multi-line feeding entries| each line parsed separately, all logged in one go |
| `B 4 10:00 - 10:15`       | bottle feed (side `B`), 4 oz, start–end → 🍼 + reply `logged: B 10:00–10:15 (4 oz, 15 min)` |
| `B 10:00` then `B 4`      | open a bottle, then close it with the oz amount → 🍼 + reply `closed: B 10:00–10:15 (4 oz, …)` |
| `B done` / `B undo`       | close the open bottle (no oz) / remove the most recent bottle |
| `SS` / `SS 22:30`         | sleep start (now, or at the given time) → 😴 + reply `sleep start: 22:30` |
| `SF` / `SF 6:15`          | finish the most recent open sleep (now, or at the given time) → 😴 + reply `sleep: 22:30–06:15 (N min)` (+ `🍼 Next nap target: HH:MM` when `BABY_DOB` is set) |
| `SS undo` / `SS oops`     | remove the most recent sleep (by start) → ↩️ + reply showing what was removed |
| `SF undo` / `SF oops`     | re-open the most recently finished sleep (clear its end) → ↩️ + reply |
| `wake 90` / `wake 0` / `wake off` | set, or clear, a max wake time; bot posts one ⏰ when awake that long with no `SS` |
| `wake auto` / `wake auto off` | auto-set the wake time from each sleep's predicted next-nap target on every live `SF` (replies a warning + stays off if `BABY_DOB` unset); any manual `wake N`/`off` clears auto |
| `d wet` / `d dirty` / `d both` | log a diaper (synonyms `pee`/`poop`; optional trailing time `d wet 14:30`) → 🚼 + reply `diaper: wet (14:30)` |
| `d undo` / `d oops`       | remove the most recent diaper → ↩️ + reply showing what was removed |
| `d` (no/unknown variant)  | 🤔 + usage hint (`d wet` / `d dirty` / `d both`) |
| `chart` / `today` / `graph` / `week` | replies with a summary PNG (matplotlib)  |
| `stats` / `summary`       | replies with text summary only                 |
| `help` / `commands` / `?` | replies with the trigger list + parser cheat sheet |
| `listcharts`              | enumerate per-chart commands                   |
| `<chart-name>`            | reply with that specific chart PNG (feeding: `timeline`, `gap_night`, `heatmap`; sleep: `sleep_timeline`, `sleep_clock`, `sleep_daily`; diaper: `diaper_daily`; bottle: `bottle_oz`, `bottle_avg`, `bottle_count`, `bottle_clock`) |
| `L` / `R`                 | start that side now; if an open session already exists for that side (within 2h), close it instead |
| `L done` / `R done` / `L stop` / etc. | explicitly close an open session for that side right now (no time needed) |
| `L undo` / `R undo` / `L oops` / `R oops` | remove the most recent L or R event (last 12 h), regardless of overall last event |
| `undo` / `oops` / `^`     | remove your most recent event (last 12 h)      |
| `fix_end` / `fixend` / `fix end` | flip your most recent open start into an end-only event and re-stitch (use when a `- 10:15` got logged as a start) |
| anything else             | 🤔; raw body stashed under `unparsed`         |

The destructive `undo` and `fix_end` triggers are matched **exactly** (no
fuzzy), so a stray `fixed it` in conversation can't accidentally mutate state.

`fix_end` synthesizes its end-only event with `side=None` so it can close a
prior open session regardless of side — this is what handles the cross-author
L→R handoff case (parent_a opens L 10:00, parent_b's R 10:30 was meant as the end).

**Second-open warning**: after every write, the bot scans the saved state for
≥2 open sessions. If your just-logged event is one of them and a prior open
session exists within 2 h, the reply is suffixed with a one-line warning
showing what `fix_end` would produce. The bot doesn't auto-close — silent
mutation was rejected in favor of a passive warning the user can act on.

## Sleep tracking

Sleeps are tracked **completely separately** from feedings and never touch any
feeding statistic (L:R, gaps, clusters, daily minutes). A message whose **first
line** starts with `SS` opens a sleep; one starting with `SF` finishes the most
recent currently-open sleep. An optional time follows (`SS 22:30`, `SF 6:15`);
with no time, the message timestamp is used.

- **Storage**: a top-level `sleeps` array in `feedings.json`, parallel to
  `events`: `{start, end, duration_min, source, raw}` (no `side`). `end` /
  `duration_min` are `null` while a sleep is open. `load_feedings` does
  `d.setdefault("sleeps", [])` so files predating this feature upgrade on load.
- **Why a parallel pipeline** (`parser.py:ingest_sleep` / `find_open_sleep`,
  `parser.py:parse_sleep_message`): sleeps deliberately do **not** use the
  feeding `stitch` / `finalize` / 120-min cap / 2 h window. `finalize` nulls any
  session > 120 min and `find_open_session` only looks back 2 h — overnight
  sleeps run 8–12 h, so both would corrupt them. `duration_min` is stored
  uncapped.
- **Interception order**: `parse_sleep_message` runs **before** feeding parsing
  in both `on_message` and `backfill_missed` — otherwise `SS 22:30` would be
  mis-parsed as a sideless feeding start. The open-sleep lookup + close runs
  inside `state_lock` (same race fix as feeding continuations).
- **Midnight roll**: when a finish time is earlier than the open start (e.g.
  `SS 22:30` → `SF 06:15`), the finish rolls forward one day before computing
  duration.
- **16 h ceiling** (`SLEEP_MAX_H`): a computed sleep longer than 16 h is treated
  as a forgotten `SF` — the sleep is left **open**, the reply is ⚠️, and nothing
  is stored. Re-send `SF` with the right time.
- **"Still asleep" warning**: logging `SS` while a prior sleep is still open
  appends a passive one-line warning to the reply (mirrors the feeding
  second-open warning). The bot never auto-closes.
- **Undo** (`parser.py:ingest_sleep`, handled before start/finish): `SS undo`
  removes the most recent sleep by start time (open or already closed); `SF undo`
  re-opens the most recently finished sleep (clears `end`/`duration_min`) so a
  wrong `SF` can be re-sent. The trailing word is matched **exactly** (`undo` /
  `oops`), like the feeding `undo`, so casual text can't trip it. Author-agnostic
  (shared baby, like `find_open_sleep`). There is no `fix_end` for sleeps.
- **Only the first line** is inspected for SS/SF — a sleep message does not also
  log feeding lines below it. (Known limitation.)
- **Charts** (`render.py:SLEEP_CHARTS`): `sleep_timeline` (time-of-day slept by
  date, overnight split at midnight), `sleep_clock` (minutes-asleep by
  hour-of-day × date, 24 h heatmap), `sleep_daily` (hours asleep per day, by
  start date). Reachable as Discord chart-name commands and shown in the "Sleep"
  section of the web dashboard (`/data.json` carries them under a `sleep` key).
- Backfill replays SS/SF in chronological order through the same `ingest_sleep`
  core (😴 reaction, no per-message reply), so an offline `SS`…`SF` pair closes
  correctly on restart.
- **Next-nap prediction (SweetSpot)** (`nap.py:predict_next_nap`): on a
  successful **live** `SF`, the reply gains a `🍼 Next nap target: HH:MM` line.
  The target is `sleep_end + wake_window`, where the wake window is the average
  of the age bracket in `nap.py:WAKE_WINDOWS` (edit there to retune); a nap
  shorter than `SHORT_NAP_MIN` (45) shaves `SHORT_NAP_PENALTY` (30) min off to
  fight overtiredness. Age is anchored at the sleep's end. Gated entirely in
  `bot.py`'s live `finish` branch — `nap.py` is pure (no env/Discord) and the
  parser core is untouched, so **backfill never emits the line**. The line is
  **omitted** when the `BABY_DOB` env var is unset/invalid or the baby is ≥ the
  top bracket (6 mo). `BABY_DOB` (`YYYY-MM-DD`) is read once in `bot.py` next to
  `BABY_NAME`; missing/malformed values disable the feature (logged once).
- **Wake reminder** (`bot.py:_check_wake_reminder` / `WAKE_RE`): `wake 90` posts
  one ⏰ if the baby's been awake 90 min with no `SS`; `wake 0` (aliased to
  `wake off`) clears it — handy if you'd rather rely on the next-nap prediction.
- **`wake auto`** (`wake_auto` state key, `setdefault` in `load_feedings`): a mode
  that, on every **live** `SF` finish, sets `wake_limit_min` to the predicted wake
  window — `round(predict_next_nap target − sleep_end)` in minutes — so the
  existing reminder loop fires right at the predicted next-nap target. Gated to the
  live `finish` branch (computed inside the same `state_lock` write), so **backfill
  never auto-arms** — same gating as the `Next nap target` reply line. `wake auto`
  with `BABY_DOB` unset/invalid replies a warning and **stays off** (it could never
  predict). Any explicit `wake N` / `wake off` / `wake 0` **clears** auto so the two
  never silently fight. The toggle itself does not arm immediately — it clears any
  stale `wake_limit_min` and takes effect on the next `SF`. The `wake` command,
  like `SS`/`SF`, refreshes the channel-topic nap line (below).
- **Channel-topic nap line** (`render.py:NAP_TOPIC_PREFIX`,
  `bot.py:compute_nap_target_line`): the bot-managed channel topic carries a second
  line `💤 next nap: HH:MM` directly **below** the `🍼 last:` feed line. It reflects
  whichever mode is active: a manual `wake N` → `last_sleep_end + N min` (fixed); or
  (auto on, or wake off with `BABY_DOB` set) → the `predict_next_nap` target. The
  line is **omitted** when there's no `BABY_DOB` and no fixed limit, no finished
  sleep, or the baby is currently asleep. `update_channel_topic` now takes a
  `topic_snapshot(d)` dict (events + sleeps + wake config) instead of just `events`,
  and manages both lines (insert / update / **remove**) idempotently.

Trigger words are fuzzy-matched via `difflib` with cutoff 0.75, so `chrt`,
`stat`, `todya`, etc. work. The fuzzy match only fires on short, digit-free
messages (`bot.py:match_trigger`), so a real feeding entry containing letters
never trips it.

Continuation handling (`bot.py:match_continuation_time` + `find_open_session`):
a message that is *only* a time (optionally prefixed with `-`, en-dash,
em-dash, U+2212 minus, or a `done`/`stop`/`finished`/`ended`/`finish`/`stopped`
keyword) is treated as the end of the most recent open session whose start
is within 2 h *of the reported end time* — **any author**. Critically, the
2 h window is measured against `cont_end` (the time being reported), not
`msg_dt` (when the message arrived), so a late-typed end like `- 16:22`
sent at 18:33 still matches an `R 16:15` open session. If no matching open
session exists, the message falls through to normal parsing (becomes an
orphan start).

The whole continuation-lookup + write happens inside `state_lock` so two
messages arriving in quick succession can't race (start, end, both read
pre-write state, neither stitches).

**Timezones**: `bot.py:on_message` converts `msg.created_at` (tz-aware UTC
from discord.py) to **system-local naive** before any downstream use
(`.astimezone().replace(tzinfo=None)`). All stored datetimes in
`feedings.json` are local-naive, and `resolve_dt` / `find_open_session` /
`find_user_last_event` all assume local-naive — so the conversion at the
boundary is what keeps the arithmetic correct. If you ever move the host to
a different timezone, existing entries will read with the new offset; switch
the host TZ deliberately or migrate the data.

## Diaper tracking

Diapers are tracked **completely separately** from feedings and sleeps and never
touch any feeding/sleep statistic. They are **instantaneous** events (no
open/close), so the pipeline is simpler than sleep.

- **Grammar** (`parser.py:parse_diaper_message` / `DIAPER_RE`): a message whose
  **first line** starts with `d` or `diaper` (the `(?=$|\s)` lookahead means a
  bare `d` or `d …` matches but words like `done` don't). The next word is the
  variant — `wet`/`pee` → `wet`, `dirty`/`poop` → `dirty`, `both` → `both`
  (`DIAPER_VARIANTS`). An optional trailing time (`d wet 14:30`) is resolved by
  the shared `_parse_sleep_time`; with no time the message timestamp is used.
  Returns `{"kind": "log"|"undo"|"bad", ...}` — `bad` means `d`/`diaper` with no
  recognized variant (bot replies 🤔 + hint rather than falling through to
  feeding parse).
- **Storage**: a top-level `diapers` array in `feedings.json`, parallel to
  `events`/`sleeps`: `{time, variant, source, raw}`. `load_feedings` does
  `d.setdefault("diapers", [])` so older files upgrade on load.
- **Ingest** (`parser.py:ingest_diaper`): `log` appends a record (with a
  `(time, variant, source)` dup-guard so backfill replays are idempotent →
  `log_dup`); `undo` removes the most-recent diaper by `time` (→ `undo_noop` if
  none). Shared by the live handler and backfill, like `ingest_sleep`.
- **Interception order**: `parse_diaper_message` runs **after** sleep and
  **before** feeding parsing in both `on_message` and `backfill_missed`. The
  write runs inside `state_lock` (same race fix as feedings/sleeps).
- **Reaction**: 🚼 for every successful log (and `log_dup`, no reply); ↩️ for
  undo; 🤔 for `bad`/`undo_noop`.
- **Charts** (`render.py:DIAPER_CHARTS`): `diaper_daily` — a **stacked** bar of
  wet/dirty/both counts per day (`build_diaper_data` → per-day count arrays).
  Reachable as a Discord chart-name command and shown in the "Diapers" section
  of the web dashboard (`/data.json` carries it under a `diaper` key).
- **Today PNG**: `build_png` adds a `Diapers today: x wet, y dirty, z both`
  line to the text panel, indexing today's date into the `build_diaper_data`
  per-day arrays.
- Backfill replays `d …` messages through the same `ingest_diaper` core (🚼
  reaction, no per-message reply); the dup-guard prevents double-counting.

## Bottle feedings

Bottles are **feedings**, not a separate data type: they live in the `events`
array with `side="B"` and an optional `oz` amount, so they appear in every feed
graph (as a 4th color) and also get their own bottle charts. They count as feeds
in sessions/day and gap math but are **excluded from the L:R ratio** (`lr_split`
only buckets L/R/?).

- **Grammar** (`parser.py:parse_bottle_message` / `BOTTLE_RE`): first line starts
  with `B` or `bottle`. `oz` is an explicit `N oz`/`N ounce(s)` **or** a bare 1-2
  digit number (opt. decimal) that isn't part of a time — so a bottle **time must
  be explicit** (`10:00` or a 3-4 digit run); a bare 1-2 digit is always oz, not
  an hour. oz is optional. Forms: `B 4 10:00 - 10:15` (range), `B 4 10:00` / `B
  10:00` (open), `B 4` / `B 4 10:15` (close the open bottle, attach oz), bare `B`
  (open now), `B done [time]` (close, no oz), `B undo`. Returns
  `{"kind": "log"|"done"|"undo", ...}`; `parse_times` is reused for times and
  `oz` units are stripped **before** normalize (so `4oz` isn't mangled by the
  `o`→`0` rule).
- **Ingest** (`parser.py:ingest_bottle`): manages its own open/close (no reliance
  on `stitch`), using `find_open_session(..., side="B")` (side-generic) to find an
  open bottle within 2 h. A single time / `B oz` / `done` closes an open bottle
  (and attaches oz on close); otherwise it opens or logs an instant bottle. Undo
  removes the most recent `B` event via `find_user_last_event(side="B")`.
  Dup-guards (matching start/source) keep backfill replays idempotent → `log_dup`.
  Result `action` ∈ `log | open | closed | undo | log_dup | undo_noop |
  closed_noop`; the live handler reacts 🍼 (↩️ undo, 🤔 noop) and replies with oz.
- **Bottles bypass stitch (critical)**: `merge_new` treats side-`B` events as
  **frozen** — they're passed through untouched and never enter `stitch`/`finalize`.
  Two reasons: (1) a bottle's start sitting between an open L/R feed and its
  continuation would otherwise make `stitch` give up on closing that feed; (2) it
  keeps the bottle's `oz` intact. As belt-and-suspenders, `oz` is also carried
  through `finalize` and `_existing_to_raw`, so any bottle that ever does pass
  through the pipeline still keeps its oz.
- **Continuation close**: a bare continuation time (`- 10:30`) closes an open
  L/R feed first; if **no L/R feed is open**, it closes an open bottle instead
  (`ingest_feeding` closes it directly via `_close_bottle`, since bottles bypass
  stitch). It does not overwrite the bottle's oz.
- **Charts** (`render.py:BOTTLE_CHARTS`): `bottle_oz` (oz/day), `bottle_avg`
  (avg oz/bottle/day), `bottle_count` (bottles/day), `bottle_clock` (oz by
  hour-of-day heatmap). `build_chart_data` adds `daily_B` (bottle minutes) for
  the 4th stacked segment + B color in the feed charts/timeline/today strip;
  `build_bottle_data` powers the bottle charts + the "Bottles today" PNG line +
  the web "Bottles" dashboard section (`/data.json` `bottle` key).
- Backfill replays `B …` messages through the same `ingest_bottle` core (🍼
  reaction, no per-message reply); the dup-guard prevents double-counting.

## Feeding-log grammar (parser handles all of these)

```
L 6:13 - 6:16          # side + start-end (canonical)
8:37 L breast          # time first
L 6:33                 # start only (later end-only message closes it)
L 10                   # bare hour after side → 10:00 start
left 10:00 - 10:10     # "left"/"right" word forms → L/R
L 10 : 30 - 10 : 40    # whitespace around the colon is fine
L 1o:3o                # 'o'/'O' adjacent to digits is treated as 0
L 2;15 - 2:25          # `;` and `.` as time separators
L done 10:12           # end only — keywords: done/finished/stop/end/ended/ending/finish/stopped
12:11 R, 12:25 stop. 12:32 L. ...   # multiple ranges in one message → multiple events
R 1:34 - 1:42                       # missing-colon typos: `143` → 1:43, `802` → 8:02
R 802 - 810                         # bare 3/4-digit times
```

Heuristics (all in `parser.py`):
- **`normalize(s)`**: pre-pass typo fixes — `o`/`O` adjacent to a digit
  becomes `0`; whole words `left`/`right` (any case) → `L`/`R`.
- **AM/PM resolution (`resolve_dt`)**: bare `H:MM` snaps to the candidate
  (today, today ± 12 h, or yesterday) closest to but not more than 30 min
  past the message's send time, preferring the latest such candidate. The
  30-min future grace covers users typing the time the instant before it
  ticks over. An hour of `12` is treated as ambiguous between **noon and
  midnight** (`12 AM == 00:xx`), so `12:10` typed just after midnight resolves
  to `00:10`, not noon — important for late-night feeds/sleeps.
- **Multi-event lines**: when a line contains ≥ 2 times, the parser pairs
  them left-to-right as `(start, end)` ranges. Side tokens are distributed
  across the ranges (first side → first range, etc.). The `done`/`stop`
  short-circuit only fires when there's exactly one time; otherwise it falls
  through to range pairing (so `12:11 R, 12:25 stop. 12:32 L. 12:44 resume L`
  parses as three events, not one end-only event).
- **Stitching (`stitch`)**: an open session (start, no end) is closed by a
  later same-side end-only event within 2 h. The end-only event that closes a
  session is removed from the output (no orphan duplicates).
- **Sanity filter (`finalize`)**: sessions longer than 120 min have their
  `end` nulled out; sessions that cross midnight roll forward a day.
- **Feeding clusters (chart math, `build_chart_data`)**: consecutive sessions
  whose end-to-start spacing is ≤ 30 min are treated as one feeding for the
  inter-feed gap statistics (`avg_gap_day`, `avg_gap_night`, `gap_buckets`,
  `longest_gap_h`). This means an L → R side-switch within 30 min doesn't
  register as a "gap between feeds". The cluster definition does NOT collapse
  sessions in the stored data — each L/R is still its own event in
  `feedings.json`, the timeline strip, and the daily-totals charts.
- **Day/night split**: a gap is classified by the wall-clock hour of the next
  feeding's start — 08:00–19:59 → day, 20:00–07:59 → night.
- **Side may be missing**: bottle/pump entries (`12:22 pump end`,
  `Started pumping 6:36`) are kept with `side: null`.

If `feedings.json` shows surprising entries, the `raw` field on each event
preserves the original message line.

## Known parser limitations

Things the bot deliberately doesn't try to handle (low frequency, ambiguous,
or would need user-facing edit/delete UX):

- `L 8:58 -9` (bare 1-digit end of range) — only the start is captured.
- `^False start` corrections — no edit/delete mechanism exists.
- `R 2:46 - X` / `R x- 8:40` (`X` as unknown-time marker) — the `X` is
  silently dropped; one end of the range is lost.
- Side-only continuation across separate lines (`R 9:27 - 9:45\nL` meaning
  "L started immediately after") — the trailing `L` is dropped. Send
  `L 9:45` instead.
- `Will end 6:51` (predictive phrasing) — treated as an actual end at 6:51
  via the 30-min future grace; further-future predictions land wrong.
- `resume`/`pause` keywords have no special meaning. Split-message
  stitching covers most pause-then-resume cases anyway.

## State management

- All writes go through `state_lock` (asyncio.Lock) — single-process write
  serialization is sufficient since this is one uvicorn worker + one
  discord.py client.
- Each new message re-finalizes a **sliding window** around the new event's
  anchor (`merge_new` in `bot.py`, controlled by `STITCH_WINDOW_H = 3`). Events
  inside the window are converted back to raw form and run through
  `stitch` + `finalize` together with the new event; events outside are
  passed through untouched. The window is wider than `stitch`'s own 2h reach,
  so anything that could affect or be affected by the new event is included.
  Per-message cost is O(events-in-window²) rather than O(total-events²).
- If `merge_new` is called with an empty `new_raw`, it falls back to the full
  pipeline over every event — used for backfill/cleanup operations that need
  the whole history re-considered.
- Continuation-time messages (a bare time with no side) synthesize an
  end-only raw event tagged with the open session's side, so the same
  `stitch()` pass that handles in-message stitching also handles
  cross-message stitching. The end-only event is consumed by `stitch` and
  doesn't appear in the saved output.
- The live message handler and the startup backfill share the same mutation
  cores (`apply_undo`, `apply_fix_end`, `ingest_feeding` in `bot.py`), each of
  which mutates an in-memory `d` and returns what happened; the live handler
  wraps them with reactions/replies/warnings, backfill wraps them in a loop.
  This is the "shared by every entry path" rule applied to the state writers,
  not just the parser.
- The browser polls `/data.json` every 30 s; no push channel.
- `source` on each event is the Discord username at message time (e.g.
  `parent_a`, `parent_b`); some deployments also carry historical imported
  events under other source labels. Open-session lookup for continuation matches
  on `source`, so each author closes their own sessions.

## Startup backfill

Discord's gateway does **not** replay messages sent while the bot is
disconnected, so a crash/restart would silently lose any feedings logged during
the outage. On the **first** `on_ready` (guarded by `_backfilled` so it runs
once per process, not on every reconnect), `backfill_missed` scans channel
history and replays everything it missed:

- **Window** is `(generated_at, startup)`. `generated_at` (read from
  `feedings.json`) is the watermark of the last state-changing save; `startup`
  is `datetime.now()` captured at `on_ready` entry. The `before=startup` upper
  bound makes backfill **race-free** against live `on_message`: downtime
  messages never arrive via the gateway, so every live message has
  `created_at >= startup` and is excluded from the fetch — no double-processing.
- History is fetched `oldest_first=True` (so continuation stitching and
  `undo`/`fix_end` ordering are correct) with `limit=1000`, and the whole pass
  runs inside `state_lock` with a single load + single save.
- State-changing messages are **replayed in order** through the same shared
  helpers the live path uses — feeds and continuation times (`ingest_feeding`),
  plus `undo` (`apply_undo`) and `fix_end` (`apply_fix_end`). Each gets the same
  reaction it would live (🍼 / 🤔 / ↩️ / 🔁) but **no** per-message reply.
- Pure **view** commands (`chart` / `stats` / `help` / `listcharts` /
  chart-name / `?`) are **skipped** — re-rendering old charts would be noise.
- If anything was applied, the bot posts one `⏪ caught up N missed message(s)`
  line; then it refreshes the channel topic. A history-fetch failure (e.g.
  missing **Read Message History** permission) is caught and logged, and the bot
  falls back to just a topic update.

Limitations: recovery is bounded by `limit=1000` and the `generated_at`
watermark (a future-dated `generated_at` fetches nothing). `apply_undo` /
`apply_fix_end` use `find_user_last_event`'s 12 h recency window (measured from
real now), so a downtime longer than ~12 h can make a missed `undo` / `fix_end`
a no-op.

## Backup

`feedings.json` is the only thing worth backing up. One-liner:

```bash
cp feedings.json feedings.json.$(date +%F).bak
```

## Privacy & data invariants

- Data lives only in `feedings.json` on this machine.
- FastAPI binds to `127.0.0.1`, not all interfaces.
- `.env` (Discord token) is gitignored.
- Logs record event counts and message lengths — not bodies, not Discord IDs.

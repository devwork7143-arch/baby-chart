"""Build chart data + render the baby's feeding chart HTML.

Used in two ways:
- `python3 render.py`           → reads feedings.json, writes a static chart.html.
- `from render import build_chart_data, build_html`  → used by main.py for live serving.
"""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent


def _parse_ts(s):
    return datetime.fromisoformat(s) if s else None


def minutes_since_midnight(dt):
    return dt.hour * 60 + dt.minute


def _parse_and_sort(records, start_key="start", end_key="end"):
    # Strips the original ISO string keys so callers can't accidentally use them as datetimes.
    result = []
    for r in records:
        st = _parse_ts(r.get(start_key))
        en = _parse_ts(r.get(end_key))
        if not (st or en):
            continue
        rec = {k: v for k, v in r.items() if k not in (start_key, end_key)}
        rec["_start"] = st
        rec["_end"] = en
        result.append(rec)
    result.sort(key=lambda x: x["_start"] or x["_end"])
    return result


def build_chart_data(events_raw, generated_at):
    """Shape on-disk events into the JSON blob the chart template consumes."""
    events = _parse_and_sort(events_raw)

    def when(e):
        return e["_start"] or e["_end"]

    timeline = []
    for e in events:
        s, t = e["_start"], e["_end"]
        if not s:
            continue
        start_min = minutes_since_midnight(s)
        end_min = minutes_since_midnight(t) if t and t.date() == s.date() else start_min + (e["duration_min"] or 1)
        timeline.append({
            "date": s.date().isoformat(),
            "start": start_min,
            "end": end_min,
            "side": e["side"] or "?",
            "duration": e["duration_min"],
            "oz": e.get("oz"),
        })

    daily_L, daily_R, daily_U = defaultdict(float), defaultdict(float), defaultdict(float)
    daily_B = defaultdict(float)
    daily_count = Counter()
    durations_by_day = defaultdict(list)
    for e in events:
        d = when(e).date().isoformat()
        daily_count[d] += 1
        dur = e["duration_min"] or 0
        target = {"L": daily_L, "R": daily_R, "B": daily_B}.get(e["side"], daily_U)
        target[d] += dur
        if e["duration_min"]:
            durations_by_day[d].append(e["duration_min"])

    days = sorted(daily_count.keys())
    avg_dur = [round(sum(durations_by_day[d]) / len(durations_by_day[d]), 1) if durations_by_day[d] else None for d in days]

    # Gaps are computed between feeding *clusters*: consecutive sessions whose
    # end-to-start spacing is ≤ 30 min are treated as one feeding (so an L
    # immediately followed by an R doesn't register as a gap between feeds).
    SAME_CLUSTER = timedelta(minutes=30)
    ordered = sorted(events, key=when)
    gaps_h = []
    gaps_day_by_day = defaultdict(list)
    gaps_night_by_day = defaultdict(list)
    cluster_end = None
    for e in ordered:
        s, t = e["_start"], e["_end"]
        if cluster_end is None:
            cluster_end = t or s
            continue
        if not s:
            if t and t > cluster_end:
                cluster_end = t
            continue
        gap = s - cluster_end
        if gap <= SAME_CLUSTER:
            if t and t > cluster_end:
                cluster_end = t
            continue
        gap_h = gap.total_seconds() / 3600.0
        if 0 < gap_h < 12:
            gaps_h.append(gap_h)
            d = s.date().isoformat()
            if 8 <= s.hour < 20:
                gaps_day_by_day[d].append(gap_h)
            else:
                gaps_night_by_day[d].append(gap_h)
        cluster_end = t or s
    gap_buckets = [0, 0, 0, 0, 0]
    for g in gaps_h:
        gap_buckets[min(int(g), 4)] += 1
    avg_gap_day = [round(sum(gaps_day_by_day[d]) / len(gaps_day_by_day[d]), 2) if gaps_day_by_day[d] else None for d in days]
    avg_gap_night = [round(sum(gaps_night_by_day[d]) / len(gaps_night_by_day[d]), 2) if gaps_night_by_day[d] else None for d in days]

    def lr_split(evs):
        c = Counter(e["side"] or "?" for e in evs)
        return [c["L"], c["R"], c["?"]]

    if events:
        cutoff7 = max(when(e) for e in events) - timedelta(days=RECENT_WINDOW_DAYS)
        last7 = [e for e in events if when(e) >= cutoff7]
    else:
        last7 = []

    heatmap = Counter()
    for e in events:
        w = when(e)
        heatmap[(w.date().isoformat(), w.hour)] += 1

    total_min = round(sum(e["duration_min"] or 0 for e in events))
    n_L = sum(1 for e in events if e["side"] == "L")
    n_R = sum(1 for e in events if e["side"] == "R")

    return {
        "timeline": timeline,
        "days": days,
        "daily_L": [round(daily_L[d], 1) for d in days],
        "daily_R": [round(daily_R[d], 1) for d in days],
        "daily_U": [round(daily_U[d], 1) for d in days],
        "daily_B": [round(daily_B[d], 1) for d in days],
        "daily_count": [daily_count[d] for d in days],
        "avg_dur": avg_dur,
        "avg_gap_day": avg_gap_day,
        "avg_gap_night": avg_gap_night,
        "gap_buckets": gap_buckets,
        "lr_all": lr_split(events),
        "lr_7": lr_split(last7),
        "heatmap": [{"date": k[0], "hour": k[1], "n": v} for k, v in heatmap.items()],
        "summary": {
            "total_sessions": len(events),
            "total_min": total_min,
            "total_hours": round(total_min / 60, 1),
            "longest_session": max((e["duration_min"] or 0 for e in events), default=0),
            "longest_gap_h": round(max(gaps_h, default=0), 1),
            "lr_ratio": f"{n_L}:{n_R}",
            "generated_at": generated_at,
            "days_tracked": len(days),
        },
    }


def build_sleep_data(sleeps, generated_at):
    """Shape the separate `sleeps` array into a JSON blob for the sleep charts.

    Completely independent of build_chart_data — sleeps never touch feeding
    stats. Overnight sleeps are split at midnight for the timeline and
    attributed minute-by-minute to their hour-of-day for the clock heatmap.
    """
    parsed = _parse_and_sort(sleeps)

    # Timeline: one segment per sleep, split at each midnight so a 22:30–06:15
    # sleep draws on both its start date (22:30→24:00) and the next (00:00→06:15).
    timeline = []
    for s in parsed:
        st, en = s["_start"], s["_end"]
        if not en:
            timeline.append({"date": st.date().isoformat(),
                             "start": minutes_since_midnight(st), "end": 1440, "open": True})
            continue
        cur = st
        while cur.date() < en.date():
            timeline.append({"date": cur.date().isoformat(),
                             "start": minutes_since_midnight(cur), "end": 1440, "open": False})
            cur = datetime(cur.year, cur.month, cur.day) + timedelta(days=1)
        timeline.append({"date": cur.date().isoformat(),
                         "start": minutes_since_midnight(cur),
                         "end": minutes_since_midnight(en), "open": False})

    # Clock heatmap: minutes asleep per (date, hour-of-day) cell.
    hour_grid = Counter()
    for s in parsed:
        st, en = s["_start"], s["_end"]
        if not en:
            continue
        cur = st
        while cur < en:
            nxt = min(en, datetime(cur.year, cur.month, cur.day, cur.hour) + timedelta(hours=1))
            hour_grid[(cur.date().isoformat(), cur.hour)] += (nxt - cur).total_seconds() / 60.0
            cur = nxt

    # Daily hours: total hours asleep attributed to the sleep's START date.
    daily = defaultdict(float)
    for s in parsed:
        if s["duration_min"]:
            daily[s["_start"].date().isoformat()] += s["duration_min"] / 60.0
    sdays = sorted(daily.keys())

    total_min = sum(s["duration_min"] or 0 for s in parsed)
    return {
        "timeline": timeline,
        "heatmap": [{"date": k[0], "hour": k[1], "min": round(v, 1)} for k, v in hour_grid.items()],
        "days": sdays,
        "daily_hours": [round(daily[d], 2) for d in sdays],
        "summary": {
            "total_sleeps": len(parsed),
            "total_hours": round(total_min / 60.0, 1),
            "avg_per_day": round(sum(daily.values()) / len(daily), 1) if daily else 0,
            "longest_sleep_h": round(max((s["duration_min"] or 0 for s in parsed), default=0) / 60.0, 1),
            "generated_at": generated_at,
        },
    }


def build_diaper_data(diapers, generated_at):
    """Shape the separate `diapers` array into a JSON blob for the diaper chart.

    Independent of feeding/sleep stats. Diapers are instantaneous events bucketed
    by their calendar date into per-variant counts.
    """
    wet = defaultdict(int)
    dirty = defaultdict(int)
    both = defaultdict(int)
    bucket = {"wet": wet, "dirty": dirty, "both": both}
    for x in diapers:
        t = x.get("time")
        v = x.get("variant")
        if not t or v not in bucket:
            continue
        bucket[v][t[:10]] += 1
    days = sorted(set(wet) | set(dirty) | set(both))
    return {
        "days": days,
        "daily_wet": [wet[d] for d in days],
        "daily_dirty": [dirty[d] for d in days],
        "daily_both": [both[d] for d in days],
        "summary": {
            "total": sum(wet.values()) + sum(dirty.values()) + sum(both.values()),
            "total_wet": sum(wet.values()),
            "total_dirty": sum(dirty.values()),
            "total_both": sum(both.values()),
            "generated_at": generated_at,
        },
    }


def build_bottle_data(events, generated_at):
    """Shape bottle feeds (side "B") from the events array into the bottle charts blob.

    Bottles live in the feeding events array; this filters them out and aggregates
    oz/counts by day plus an oz-by-hour-of-day clock heatmap. Independent of the
    L/R feeding stats.
    """
    bottles = [e for e in events if e.get("side") == "B"]
    oz_by_day = defaultdict(float)
    count_by_day = defaultdict(int)
    clock = defaultdict(float)  # (date, hour) -> oz
    for e in bottles:
        anchor = e.get("start") or e.get("end")
        if not anchor:
            continue
        day = anchor[:10]
        count_by_day[day] += 1
        oz = e.get("oz") or 0
        oz_by_day[day] += oz
        clock[(day, int(anchor[11:13]))] += oz
    days = sorted(count_by_day)
    total_oz = sum(oz_by_day.values())
    return {
        "days": days,
        "daily_oz": [round(oz_by_day[d], 1) for d in days],
        "daily_bottles": [count_by_day[d] for d in days],
        "daily_avg_oz": [round(oz_by_day[d] / count_by_day[d], 1) if count_by_day[d] else 0
                         for d in days],
        "clock": [{"date": k[0], "hour": k[1], "oz": round(v, 1)} for k, v in clock.items()],
        "summary": {
            "total_bottles": len(bottles),
            "total_oz": round(total_oz, 1),
            "avg_oz": round(total_oz / len(bottles), 1) if bottles else 0,
            "generated_at": generated_at,
        },
    }


C = {"L": "#e91e8c", "R": "#3a8dde", "?": "#aaaaaa", "B": "#e8a13a"}
C_DAY = "#f0a000"
C_NIGHT = "#5a5cc8"
C_SLEEP = "#6a4ca8"
C_SLEEP_OPEN = "#c9bae8"
C_WET = "#3a8dde"
C_DIRTY = "#a5682a"
C_BOTH = "#6a9a3a"
RECENT_WINDOW_DAYS = 7      # cutoff for the "last 7 days" L:R chart


def _rolling(arr, k):
    out = []
    for i, _ in enumerate(arr):
        window = [x for x in arr[max(0, i - k + 1):i + 1] if x is not None]
        out.append(round(sum(window) / len(window), 2) if window else None)
    return out


def _chart_timeline(data, ax):
    days = data["days"]
    idx = {d: i for i, d in enumerate(days)}
    for t in data["timeline"]:
        if t["date"] not in idx:
            continue
        x = idx[t["date"]]
        ax.plot([x, x], [t["start"], t["end"]], color=C.get(t["side"], C["?"]),
                linewidth=3, solid_capstyle="butt")
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylim(0, 1440)
    ax.invert_yaxis()
    ax.set_yticks(range(0, 1441, 120))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(0, 25, 2)], fontsize=8)
    ax.set_ylabel("time of day")
    ax.set_title("Session timeline", loc="left")
    ax.grid(axis="y", color="#eee", linewidth=0.5)
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=C[s], label=s) for s in ("L", "R", "?", "B")], loc="upper right", fontsize=9)


def _chart_daily(data, ax):
    days = data["days"]
    x = range(len(days))
    dL, dR, dU = data["daily_L"], data["daily_R"], data["daily_U"]
    dB = data.get("daily_B", [0] * len(days))
    ax.bar(x, dL, color=C["L"], label="L")
    ax.bar(x, dR, bottom=dL, color=C["R"], label="R")
    ax.bar(x, dU, bottom=[a + b for a, b in zip(dL, dR)], color=C["?"], label="?")
    ax.bar(x, dB, bottom=[a + b + c for a, b, c in zip(dL, dR, dU)], color=C["B"], label="B")
    ax.set_xticks(list(x))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("minutes")
    ax.set_title("Minutes per day (L + R + ?)", loc="left")
    ax.legend(loc="upper left", fontsize=9)


def _chart_count(data, ax):
    days = data["days"]
    ax.plot(range(len(days)), data["daily_count"], color="#444", marker="o", markersize=3)
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("sessions")
    ax.set_ylim(bottom=0)
    ax.set_title("Sessions per day", loc="left")


def _line_with_rolling(data, ax, series, label, color, ylabel, title):
    days = data["days"]
    x = list(range(len(days)))
    ax.plot(x, series, color=color, marker="o", markersize=3, label=label)
    ax.plot(x, _rolling(series, 3), color="#c33", linestyle="--", label="3-day mean")
    ax.set_xticks(x)
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel(ylabel)
    ax.set_ylim(bottom=0)
    ax.set_title(title, loc="left")
    ax.legend(loc="upper left", fontsize=9)


def _chart_avg_dur(data, ax):
    _line_with_rolling(data, ax, data["avg_dur"], "avg min", "#888", "minutes",
                       "Avg session duration")


def _chart_gap_day(data, ax):
    _line_with_rolling(data, ax, data["avg_gap_day"], "avg h", C_DAY, "hours",
                       "Avg gap · day (08:00–20:00)")


def _chart_gap_night(data, ax):
    _line_with_rolling(data, ax, data["avg_gap_night"], "avg h", C_NIGHT, "hours",
                       "Avg gap · night (20:00–08:00)")


def _chart_gaps(data, ax):
    labels = ["0-1h", "1-2h", "2-3h", "3-4h", "4h+"]
    ax.bar(labels, data["gap_buckets"], color="#6a9")
    ax.set_ylabel("count")
    ax.set_title("Gap between sessions (excludes overnight > 12h)", loc="left")


def _chart_lr_all(data, ax):
    _donut(ax, data["lr_all"], "L vs R · all time")


def _chart_lr_7(data, ax):
    _donut(ax, data["lr_7"], "L vs R · last 7 days")


def _donut(ax, vals, title):
    labels = ["L", "R", "?"]
    colors = [C["L"], C["R"], C["?"]]
    paired = [(l, v, c) for l, v, c in zip(labels, vals, colors) if v > 0]
    if not paired:
        ax.text(0.5, 0.5, "no data", ha="center", va="center")
        ax.axis("off")
        ax.set_title(title, loc="left")
        return
    lbl, vs, cs = zip(*paired)
    ax.pie(vs, labels=lbl, colors=cs, wedgeprops={"width": 0.4}, startangle=90,
           autopct="%1.0f%%", textprops={"fontsize": 10})
    ax.set_title(title, loc="left")


def _chart_heatmap(data, ax):
    import numpy as np
    days = data["days"]
    if not days:
        ax.text(0.5, 0.5, "no data", ha="center", va="center")
        ax.axis("off")
        return
    grid = np.zeros((24, len(days)))
    didx = {d: i for i, d in enumerate(days)}
    for p in data["heatmap"]:
        if p["date"] in didx:
            grid[p["hour"], didx[p["date"]]] = p["n"]
    im = ax.imshow(grid, aspect="auto", origin="upper", cmap="Blues")
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(0, 24, 3))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(0, 24, 3)], fontsize=8)
    ax.set_ylabel("hour of day")
    ax.set_title("Sessions per hour-of-day · by date", loc="left")
    from matplotlib import colorbar
    cbar = ax.figure.colorbar(im, ax=ax, shrink=0.7, pad=0.02)
    cbar.ax.tick_params(labelsize=8)


def _chart_sleep_timeline(data, ax):
    days = data["days"]
    if not days:
        ax.text(0.5, 0.5, "no sleep data", ha="center", va="center")
        ax.axis("off")
        return
    idx = {d: i for i, d in enumerate(days)}
    for t in data["timeline"]:
        if t["date"] not in idx:
            continue
        x = idx[t["date"]]
        color = C_SLEEP_OPEN if t.get("open") else C_SLEEP
        ax.plot([x, x], [t["start"], t["end"]], color=color, linewidth=6, solid_capstyle="butt")
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylim(0, 1440)
    ax.invert_yaxis()
    ax.set_yticks(range(0, 1441, 120))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(0, 25, 2)], fontsize=8)
    ax.set_ylabel("time of day")
    ax.set_title("Sleep timeline (overnight split at midnight)", loc="left")
    ax.grid(axis="y", color="#eee", linewidth=0.5)


def _chart_sleep_heatmap(data, ax):
    import numpy as np
    days = data["days"]
    if not days:
        ax.text(0.5, 0.5, "no sleep data", ha="center", va="center")
        ax.axis("off")
        return
    grid = np.zeros((24, len(days)))
    didx = {d: i for i, d in enumerate(days)}
    for p in data["heatmap"]:
        if p["date"] in didx:
            grid[p["hour"], didx[p["date"]]] = p["min"]
    im = ax.imshow(grid, aspect="auto", origin="upper", cmap="Purples", vmin=0, vmax=60)
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(0, 24, 3))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(0, 24, 3)], fontsize=8)
    ax.set_ylabel("hour of day")
    ax.set_title("Minutes asleep · by hour-of-day & date", loc="left")
    cbar = ax.figure.colorbar(im, ax=ax, shrink=0.7, pad=0.02)
    cbar.ax.tick_params(labelsize=8)
    cbar.set_label("min/hour", fontsize=8)


def _chart_sleep_daily(data, ax):
    days = data["days"]
    if not days:
        ax.text(0.5, 0.5, "no sleep data", ha="center", va="center")
        ax.axis("off")
        return
    x = range(len(days))
    ax.bar(x, data["daily_hours"], color=C_SLEEP)
    ax.set_xticks(list(x))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("hours")
    ax.set_ylim(bottom=0)
    ax.set_title("Hours asleep per day (by start date)", loc="left")


def _chart_diaper_daily(data, ax):
    days = data["days"]
    if not days:
        ax.text(0.5, 0.5, "no diaper data", ha="center", va="center")
        ax.axis("off")
        return
    x = list(range(len(days)))
    wet, dirty, both = data["daily_wet"], data["daily_dirty"], data["daily_both"]
    ax.bar(x, wet, color=C_WET, label="wet")
    bottom = list(wet)
    ax.bar(x, dirty, bottom=bottom, color=C_DIRTY, label="dirty")
    bottom = [a + b for a, b in zip(bottom, dirty)]
    ax.bar(x, both, bottom=bottom, color=C_BOTH, label="both")
    ax.set_xticks(x)
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("diapers")
    ax.set_ylim(bottom=0)
    ax.legend(fontsize=8)
    ax.set_title("Diapers per day (wet/dirty/both)", loc="left")


def _bottle_bar(data, ax, key, ylabel, title):
    days = data["days"]
    if not days:
        ax.text(0.5, 0.5, "no bottle data", ha="center", va="center")
        ax.axis("off")
        return
    x = list(range(len(days)))
    ax.bar(x, data[key], color=C["B"])
    ax.set_xticks(x)
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_ylabel(ylabel)
    ax.set_ylim(bottom=0)
    ax.set_title(title, loc="left")


def _chart_bottle_oz(data, ax):
    _bottle_bar(data, ax, "daily_oz", "oz", "Bottle oz per day")


def _chart_bottle_avg(data, ax):
    _bottle_bar(data, ax, "daily_avg_oz", "oz", "Avg oz per bottle (per day)")


def _chart_bottle_count(data, ax):
    _bottle_bar(data, ax, "daily_bottles", "bottles", "Bottles per day")


def _chart_bottle_clock(data, ax):
    import numpy as np
    days = data["days"]
    if not days:
        ax.text(0.5, 0.5, "no bottle data", ha="center", va="center")
        ax.axis("off")
        return
    grid = np.zeros((24, len(days)))
    didx = {d: i for i, d in enumerate(days)}
    for p in data["clock"]:
        if p["date"] in didx:
            grid[p["hour"], didx[p["date"]]] = p["oz"]
    im = ax.imshow(grid, aspect="auto", origin="upper", cmap="Oranges", vmin=0)
    ax.set_xticks(range(len(days)))
    ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(0, 24, 3))
    ax.set_yticklabels([f"{h:02d}:00" for h in range(0, 24, 3)], fontsize=8)
    ax.set_ylabel("hour of day")
    ax.set_title("Bottle oz · by hour-of-day & date", loc="left")
    cbar = ax.figure.colorbar(im, ax=ax, shrink=0.7, pad=0.02)
    cbar.ax.tick_params(labelsize=8)
    cbar.set_label("oz", fontsize=8)


CHARTS = {
    "timeline":  ("Session timeline (time of day by date)", _chart_timeline),
    "daily":     ("Minutes per day (L + R + ?)",             _chart_daily),
    "count":     ("Sessions per day",                        _chart_count),
    "avg_dur":   ("Avg session duration (min)",              _chart_avg_dur),
    "gaps":      ("Gap between sessions (histogram)",        _chart_gaps),
    "gap_day":   ("Avg gap · day (08:00–20:00)",             _chart_gap_day),
    "gap_night": ("Avg gap · night (20:00–08:00)",           _chart_gap_night),
    "lr_all":    ("L vs R · all time",                       _chart_lr_all),
    "lr_7":      ("L vs R · last 7 days",                    _chart_lr_7),
    "heatmap":   ("Hour-of-day heatmap",                     _chart_heatmap),
}


SLEEP_CHARTS = {
    "sleep_timeline": ("Sleep timeline (time of day by date)", _chart_sleep_timeline),
    "sleep_clock":    ("Sleep by hour-of-day (24h heatmap)",   _chart_sleep_heatmap),
    "sleep_daily":    ("Hours asleep per day",                 _chart_sleep_daily),
}


DIAPER_CHARTS = {
    "diaper_daily": ("Diapers per day (wet/dirty/both)", _chart_diaper_daily),
}


BOTTLE_CHARTS = {
    "bottle_oz":    ("Bottle oz per day",            _chart_bottle_oz),
    "bottle_avg":   ("Avg oz per bottle (per day)",  _chart_bottle_avg),
    "bottle_count": ("Bottles per day",              _chart_bottle_count),
    "bottle_clock": ("Bottle oz by hour-of-day",     _chart_bottle_clock),
}


def _render(registry, key, data):
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _title, fn = registry[key]
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.patch.set_facecolor("white")
    try:
        fn(data, ax)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
        return buf.getvalue()
    finally:
        plt.close(fig)


def render_chart(key, data):
    """Render one registered feeding chart to PNG bytes. Raises KeyError on unknown key."""
    return _render(CHARTS, key, data)


def render_sleep_chart(key, data):
    """Render one registered sleep chart to PNG bytes. Raises KeyError on unknown key."""
    return _render(SLEEP_CHARTS, key, data)


def render_diaper_chart(key, data):
    """Render one registered diaper chart to PNG bytes. Raises KeyError on unknown key."""
    return _render(DIAPER_CHARTS, key, data)


def render_bottle_chart(key, data):
    """Render one registered bottle chart to PNG bytes. Raises KeyError on unknown key."""
    return _render(BOTTLE_CHARTS, key, data)


def build_png(data, window_days=14, sleep=None, diaper=None, bottle=None):
    """Render a phone-friendly summary PNG. Returns PNG bytes.

    Three stacked panels: last-N-days minutes (stacked L/R/?), today's
    timeline strip (feeds + sleep lanes), and a text summary box.

    `sleep` is the dict from build_sleep_data(); its midnight-split timeline
    lets the today strip show every sleep overlapping the calendar day,
    including the partial slice of an overnight sleep. `diaper` is the dict
    from build_diaper_data(); it adds a "Diapers today" line to the text box.
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import date as _date

    fig, axes = plt.subplots(
        3, 1, figsize=(10, 9),
        gridspec_kw={"height_ratios": [3, 2, 1.2]},
    )
    fig.patch.set_facecolor("white")

    days = data["days"][-window_days:]
    dL = data["daily_L"][-window_days:]
    dR = data["daily_R"][-window_days:]
    dU = data["daily_U"][-window_days:]
    dB = data.get("daily_B", [0] * len(data["days"]))[-window_days:]
    ax = axes[0]
    if days:
        x = range(len(days))
        ax.bar(x, dL, color=C["L"], label="L")
        ax.bar(x, dR, bottom=dL, color=C["R"], label="R")
        ax.bar(x, dU, bottom=[a + b for a, b in zip(dL, dR)], color=C["?"], label="?")
        ax.bar(x, dB, bottom=[a + b + c for a, b, c in zip(dL, dR, dU)], color=C["B"], label="B")
        ax.set_xticks(list(x))
        ax.set_xticklabels([d[5:] for d in days], rotation=45, ha="right", fontsize=8)
        ax.legend(loc="upper left", fontsize=9)
    ax.set_ylabel("minutes")
    ax.set_title(f"Daily totals · last {len(days)} days", fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)

    today = _date.today().isoformat()
    today_sessions = [t for t in data["timeline"] if t["date"] == today]
    # Sleep segments are already split at midnight by build_sleep_data, so a
    # segment with date == today is exactly the part of that sleep inside the
    # calendar day (handles overnight sleeps spilling in from yesterday or
    # running past midnight into tomorrow, plus still-open sleeps).
    today_sleeps = [t for t in (sleep or {}).get("timeline", []) if t["date"] == today]
    today_sleep_min = round(sum(t["end"] - t["start"] for t in today_sleeps))
    FEED_Y, SLEEP_Y = 0.5, -0.5
    ax = axes[1]
    for t in today_sessions:
        ax.barh(FEED_Y, (t["end"] - t["start"]) / 60.0, left=t["start"] / 60.0,
                color=C.get(t["side"], C["?"]), height=0.7)
    for t in today_sleeps:
        ax.barh(SLEEP_Y, (t["end"] - t["start"]) / 60.0, left=t["start"] / 60.0,
                color=C_SLEEP_OPEN if t.get("open") else C_SLEEP, height=0.7)
    ax.set_xlim(0, 24)
    ax.set_ylim(-1, 1)
    ax.set_yticks([FEED_Y, SLEEP_Y])
    ax.set_yticklabels(["feed", "sleep"], fontsize=9)
    ax.set_xticks(range(0, 25, 3))
    ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 3)], fontsize=8)
    ax.set_xlabel("hour of day")
    ax.set_title(f"Today · {today} · {len(today_sessions)} feed(s) · "
                 f"{round(today_sleep_min / 60.0, 1)} h asleep", fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    for h in range(0, 25, 3):
        ax.axvline(h, color="#eee", linewidth=1, zorder=0)

    s = data["summary"]
    today_min = round(sum(dL[-1:] + dR[-1:] + dU[-1:]) if days and days[-1] == today else 0, 1)
    last_gap = s["longest_gap_h"]
    ax = axes[2]
    ax.axis("off")
    dd = diaper or {}
    if today in dd.get("days", []):
        di = dd["days"].index(today)
        dw, ddi, db = dd["daily_wet"][di], dd["daily_dirty"][di], dd["daily_both"][di]
    else:
        dw = ddi = db = 0
    bd = bottle or {}
    if today in bd.get("days", []):
        bi = bd["days"].index(today)
        b_n, b_oz = bd["daily_bottles"][bi], bd["daily_oz"][bi]
    else:
        b_n = b_oz = 0
    lines = [
        f"Today: {len(today_sessions)} sessions · {today_min} min · sleep {round(today_sleep_min / 60.0, 1)} h",
    ]
    if dw or ddi or db:
        lines.append(f"Diapers today: {dw} wet, {ddi} dirty, {db} both")
    if b_n:
        lines.append(f"Bottles today: {b_n} ({b_oz} oz)")
    lines += [
        f"All time: {s['total_sessions']} sessions · {s['total_hours']} h · {s['days_tracked']} days tracked",
        f"L : R = {s['lr_ratio']}    Longest session: {s['longest_session']} min    Longest gap (excl. overnight): {last_gap} h",
    ]
    for i, line in enumerate(lines):
        ax.text(0.0, 0.92 - 0.22 * i, line, fontsize=11, family="monospace")

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def build_html(data=None):
    """Return the chart HTML. If `data` is given, embed it; else fetch from /data.json."""
    if data is None:
        injected = "await (await fetch('/data.json', {cache:'no-store'})).json()"
        live = True
    else:
        injected = json.dumps(data)
        live = False
    # Read BABY_NAME at call time, not import time: bot.py imports render before
    # it runs load_dotenv(), so a module-level read would miss values from .env.
    baby_name = os.environ.get("BABY_NAME", "Baby")
    return (HTML_TEMPLATE
            .replace("__DATA_SRC__", injected)
            .replace("__LIVE__", "true" if live else "false")
            .replace("__BABY_NAME__", baby_name))


TOPIC_PREFIX = "🍼 last:"
NAP_TOPIC_PREFIX = "💤 next nap:"


def format_events_brief(events):
    parts = []
    for e in events:
        side = e["side"] or "?"
        s = e["start"][11:] if e["start"] else "?"
        t = e["end"][11:] if e["end"] else "?"
        parts.append(f"{side} {s}–{t}")
    return ", ".join(parts)


def compute_last_completed_line(events):
    completed = [e for e in events if e.get("start") and e.get("end")]
    if not completed:
        return f"{TOPIC_PREFIX} none yet"
    e = max(completed, key=lambda x: x["end"])
    side = e.get("side") or "?"
    s = e["start"][11:]
    t = e["end"][11:]
    dur = e.get("duration_min")
    dur_str = f" ({int(dur)} min)" if dur else ""
    return f"{TOPIC_PREFIX} {side} {s}–{t}{dur_str}"


def build_text_stats(data, bottle=None):
    s = data["summary"]
    today = datetime.now().date().isoformat()
    today_sessions = [t for t in data["timeline"] if t["date"] == today]
    # Use daily accumulators (same source as build_png) so open sessions contribute 0
    # rather than the phantom 1-min sentinel set on timeline entries without a real end.
    try:
        idx = data["days"].index(today)
        today_min = round(data["daily_L"][idx] + data["daily_R"][idx] + data["daily_U"][idx], 1)
    except ValueError:
        today_min = 0
    bottle_line = ""
    if bottle and bottle.get("days"):
        bsum = bottle["summary"]
        today_oz = bottle["daily_oz"][bottle["days"].index(today)] if today in bottle["days"] else 0
        bottle_line = (f"\nBottles · {today_oz} oz today · "
                       f"{bsum['total_oz']} oz all time ({bsum['total_bottles']} bottles)")
    return (
        f"**Today** · {len(today_sessions)} sessions · {today_min} min\n"
        f"**All time** · {s['total_sessions']} sessions · {s['total_hours']} h · "
        f"{s['days_tracked']} days\n"
        f"L : R = {s['lr_ratio']} · longest session {s['longest_session']} min · "
        f"longest gap {s['longest_gap_h']} h"
        f"{bottle_line}"
    )


def main():
    raw = json.loads((ROOT / "feedings.json").read_text())
    data = build_chart_data(raw["events"], raw["generated_at"])
    data["sleep"] = build_sleep_data(raw.get("sleeps", []), raw["generated_at"])
    data["diaper"] = build_diaper_data(raw.get("diapers", []), raw["generated_at"])
    data["bottle"] = build_bottle_data(raw["events"], raw["generated_at"])
    (ROOT / "chart.html").write_text(build_html(data))
    print("wrote chart.html")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__BABY_NAME__ tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #222; }
  h1 { margin: 0 0 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 24px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 16px 0 32px; }
  .card { background: #f6f6f8; border-radius: 8px; padding: 12px 14px; }
  .card .v { font-size: 22px; font-weight: 600; }
  .card .k { font-size: 11px; color: #777; text-transform: uppercase; letter-spacing: 0.05em; }
  .chart-box { background: white; border: 1px solid #eee; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
  .chart-box h3 { margin: 0 0 12px; font-size: 14px; color: #444; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  canvas { max-width: 100%; }
  .tall { position: relative; height: 380px; }
  .med  { position: relative; height: 260px; }
  .live { display: inline-block; padding: 2px 8px; border-radius: 10px; background: #2a7; color: white; font-size: 11px; margin-left: 8px; }
  @media (max-width: 700px) { .row { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<h1>__BABY_NAME__ tracker<span id="live-badge"></span></h1>
<div class="sub">Generated <span id="gen"></span> · L = left, R = right, B = bottle, ? = unrecorded (pump)</div>

<div class="cards" id="cards"></div>

<div class="chart-box">
  <h3>Session timeline (time of day by date)</h3>
  <div class="tall"><canvas id="timeline"></canvas></div>
</div>

<div class="row">
  <div class="chart-box"><h3>Minutes per day (L + R + ?)</h3><canvas id="daily"></canvas></div>
  <div class="chart-box"><h3>Sessions per day</h3><canvas id="count"></canvas></div>
</div>

<div class="row">
  <div class="chart-box"><h3>Avg session duration (min)</h3><canvas id="avg"></canvas></div>
  <div class="chart-box"><h3>Gap between sessions (hours)</h3><canvas id="gaps"></canvas></div>
</div>

<div class="row">
  <div class="chart-box"><h3>Avg gap · day (08:00–20:00, hours)</h3><canvas id="avgGapDay"></canvas></div>
  <div class="chart-box"><h3>Avg gap · night (20:00–08:00, hours)</h3><canvas id="avgGapNight"></canvas></div>
</div>

<div class="row">
  <div class="chart-box"><h3>L vs R · all time</h3><canvas id="lrAll"></canvas></div>
  <div class="chart-box"><h3>L vs R · last 7 days</h3><canvas id="lr7"></canvas></div>
</div>

<div class="chart-box">
  <h3>Hour-of-day heatmap</h3>
  <div class="med"><canvas id="heatmap"></canvas></div>
</div>

<div id="sleep-section" style="display:none">
<h2 style="margin-top:36px">Sleep <span style="font-size:13px;color:#888;font-weight:normal">· overnight sleeps split at midnight</span></h2>
<div class="cards" id="sleep-cards"></div>
<div class="chart-box">
  <h3>Sleep timeline (time of day by date)</h3>
  <div class="tall"><canvas id="sleepTimeline"></canvas></div>
</div>
<div class="row">
  <div class="chart-box"><h3>Hours asleep per day</h3><canvas id="sleepDaily"></canvas></div>
  <div class="chart-box"><h3>Sleep by hour-of-day (minutes)</h3><div class="med"><canvas id="sleepClock"></canvas></div></div>
</div>
</div>

<div id="diaper-section" style="display:none">
<h2 style="margin-top:36px">Diapers</h2>
<div class="cards" id="diaper-cards"></div>
<div class="chart-box"><h3>Diapers per day (wet/dirty/both)</h3><canvas id="diaperDaily"></canvas></div>
</div>

<div id="bottle-section" style="display:none">
<h2 style="margin-top:36px">Bottles</h2>
<div class="cards" id="bottle-cards"></div>
<div class="row">
  <div class="chart-box"><h3>Oz per day</h3><canvas id="bottleOz"></canvas></div>
  <div class="chart-box"><h3>Avg oz per bottle</h3><canvas id="bottleAvg"></canvas></div>
</div>
<div class="row">
  <div class="chart-box"><h3>Bottles per day</h3><canvas id="bottleCount"></canvas></div>
  <div class="chart-box"><h3>Oz by hour-of-day</h3><div class="med"><canvas id="bottleClock"></canvas></div></div>
</div>
</div>

<script>
const LIVE = __LIVE__;
const C = {L: '#e91e8c', R: '#3a8dde', '?': '#aaa', B: '#e8a13a'};
let charts = [];

function destroyAll() { charts.forEach(c => c.destroy()); charts = []; }

function rolling(arr, k) {
  const out = [];
  for (let i = 0; i < arr.length; i++) {
    const w = arr.slice(Math.max(0, i-k+1), i+1).filter(x => x != null);
    out.push(w.length ? Math.round(w.reduce((a,b)=>a+b,0)/w.length*10)/10 : null);
  }
  return out;
}

function render(D) {
  destroyAll();
  const s = D.summary;
  document.getElementById('gen').textContent = s.generated_at;
  if (LIVE) document.getElementById('live-badge').innerHTML = '<span class="live">LIVE</span>';
  const cards = [
    ['Sessions', s.total_sessions],
    ['Total hours', s.total_hours],
    ['Days tracked', s.days_tracked],
    ['Longest session', s.longest_session + ' min'],
    ['Longest gap', s.longest_gap_h + ' h'],
    ['L : R', s.lr_ratio],
  ];
  document.getElementById('cards').innerHTML = cards.map(([k,v]) =>
    `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');

  charts.push(new Chart(document.getElementById('timeline'), {
    type: 'bar',
    data: {datasets: ['L','R','?','B'].map(side => ({
      label: side,
      data: D.timeline.filter(t => t.side === side).map(t => ({x: t.date, y: [t.start, t.end]})),
      backgroundColor: C[side], borderColor: C[side], borderWidth: 0, barThickness: 4,
    }))},
    options: {
      maintainAspectRatio: false,
      scales: {
        x: {type: 'category', labels: D.days, title: {display:true, text:'Date'}},
        y: {min: 0, max: 1440, title: {display:true, text:'Time of day'},
            ticks: {stepSize: 120, callback: v => `${String(Math.floor(v/60)).padStart(2,'0')}:${String(v%60).padStart(2,'0')}`}},
      },
      plugins: {legend: {position:'top'}, tooltip: {callbacks: {
        label: ctx => {
          const t = ctx.raw.y;
          const fmt = m => `${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
          return `${ctx.dataset.label}: ${fmt(t[0])}–${fmt(t[1])} (${t[1]-t[0]} min)`;
        }
      }}},
    },
  }));

  charts.push(new Chart(document.getElementById('daily'), {
    type: 'bar',
    data: {labels: D.days, datasets: [
      {label:'L', data: D.daily_L, backgroundColor: C.L, stack:'a'},
      {label:'R', data: D.daily_R, backgroundColor: C.R, stack:'a'},
      {label:'?', data: D.daily_U, backgroundColor: C['?'], stack:'a'},
      {label:'B', data: D.daily_B, backgroundColor: C.B, stack:'a'},
    ]},
    options: {scales: {x:{stacked:true}, y:{stacked:true, title:{display:true,text:'minutes'}}}},
  }));

  charts.push(new Chart(document.getElementById('count'), {
    type: 'line',
    data: {labels: D.days, datasets:[{label:'sessions', data: D.daily_count, borderColor:'#444', tension:0.3, fill:false}]},
    options: {scales: {y: {beginAtZero:true}}},
  }));

  charts.push(new Chart(document.getElementById('avg'), {
    type: 'line',
    data: {labels: D.days, datasets: [
      {label:'avg min', data: D.avg_dur, borderColor:'#888', tension:0.3, fill:false, spanGaps:true},
      {label:'3-day mean', data: rolling(D.avg_dur, 3), borderColor:'#c33', borderDash:[4,4], tension:0.3, fill:false, spanGaps:true},
    ]},
  }));

  function gapLine(canvasId, series, color) {
    charts.push(new Chart(document.getElementById(canvasId), {
      type: 'line',
      data: {labels: D.days, datasets: [
        {label:'avg h', data: series, borderColor: color, tension:0.3, fill:false, spanGaps:true},
        {label:'3-day mean', data: rolling(series, 3), borderColor:'#c33', borderDash:[4,4], tension:0.3, fill:false, spanGaps:true},
      ]},
      options: {scales: {y: {beginAtZero:true, title: {display:true, text:'hours'}}}},
    }));
  }
  gapLine('avgGapDay', D.avg_gap_day, '#f0a000');
  gapLine('avgGapNight', D.avg_gap_night, '#5a5cc8');

  charts.push(new Chart(document.getElementById('gaps'), {
    type: 'bar',
    data: {labels: ['0-1h','1-2h','2-3h','3-4h','4h+'], datasets:[
      {label:'count', data: D.gap_buckets, backgroundColor:'#6a9'},
    ]},
  }));

  function donut(canvasId, vals) {
    charts.push(new Chart(document.getElementById(canvasId), {
      type: 'doughnut',
      data: {labels:['L','R','?'], datasets:[{data: vals, backgroundColor:[C.L, C.R, C['?']]}]},
    }));
  }
  donut('lrAll', D.lr_all);
  donut('lr7', D.lr_7);

  const maxN = Math.max(1, ...D.heatmap.map(p => p.n));
  charts.push(new Chart(document.getElementById('heatmap'), {
    type: 'bubble',
    data: {datasets: [{
      data: D.heatmap.map(p => ({x: p.date, y: p.hour, r: 4 + 10*p.n/maxN, n: p.n})),
      backgroundColor: 'rgba(58,141,222,0.5)',
    }]},
    options: {
      maintainAspectRatio: false,
      scales: {
        x: {type:'category', labels: D.days, title:{display:true, text:'Date'}},
        y: {min:-0.5, max:23.5, reverse:true, ticks:{stepSize:3, callback:v=>`${v}:00`}, title:{display:true, text:'Hour of day'}},
      },
      plugins: {legend:{display:false}, tooltip:{callbacks:{label: ctx => `${ctx.raw.x} ${ctx.raw.y}:00 — ${ctx.raw.n} session(s)`}}},
    },
  }));

  renderSleep(D.sleep);
  renderDiaper(D.diaper);
  renderBottle(D.bottle);
}

const C_SLEEP = '#6a4ca8';
const C_SLEEP_OPEN = '#c9bae8';
const C_WET = '#3a8dde';
const C_DIRTY = '#a5682a';
const C_BOTH = '#6a9a3a';
const fmtHM = m => `${String(Math.floor(m/60)).padStart(2,'0')}:${String(Math.round(m%60)).padStart(2,'0')}`;

function renderSleep(S) {
  const section = document.getElementById('sleep-section');
  if (!S || !S.days || !S.days.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  const s = S.summary;
  const cards = [
    ['Sleeps', s.total_sleeps],
    ['Total hours', s.total_hours],
    ['Avg per day', s.avg_per_day + ' h'],
    ['Longest', s.longest_sleep_h + ' h'],
  ];
  document.getElementById('sleep-cards').innerHTML = cards.map(([k,v]) =>
    `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');

  charts.push(new Chart(document.getElementById('sleepTimeline'), {
    type: 'bar',
    data: {datasets: [
      {label:'asleep', data: S.timeline.filter(t=>!t.open).map(t=>({x:t.date, y:[t.start, t.end]})),
       backgroundColor: C_SLEEP, barThickness: 8},
      {label:'open', data: S.timeline.filter(t=>t.open).map(t=>({x:t.date, y:[t.start, t.end]})),
       backgroundColor: C_SLEEP_OPEN, barThickness: 8},
    ]},
    options: {
      maintainAspectRatio: false,
      scales: {
        x: {type:'category', labels: S.days, title:{display:true, text:'Date'}},
        y: {min:0, max:1440, title:{display:true, text:'Time of day'},
            ticks:{stepSize:120, callback: v => fmtHM(v)}},
      },
      plugins: {legend:{position:'top'}, tooltip:{callbacks:{label: ctx => {
        const t = ctx.raw.y; return `${ctx.dataset.label}: ${fmtHM(t[0])}–${fmtHM(t[1])}`;
      }}}},
    },
  }));

  charts.push(new Chart(document.getElementById('sleepDaily'), {
    type: 'bar',
    data: {labels: S.days, datasets:[{label:'hours', data: S.daily_hours, backgroundColor: C_SLEEP}]},
    options: {scales: {y: {beginAtZero:true, title:{display:true, text:'hours'}}}},
  }));

  charts.push(new Chart(document.getElementById('sleepClock'), {
    type: 'bubble',
    data: {datasets: [{
      data: S.heatmap.map(p => ({x: p.date, y: p.hour, r: 3 + 9*p.min/60, min: p.min})),
      backgroundColor: 'rgba(106,76,168,0.5)',
    }]},
    options: {
      maintainAspectRatio: false,
      scales: {
        x: {type:'category', labels: S.days, title:{display:true, text:'Date'}},
        y: {min:-0.5, max:23.5, reverse:true, ticks:{stepSize:3, callback:v=>`${v}:00`}, title:{display:true, text:'Hour of day'}},
      },
      plugins: {legend:{display:false}, tooltip:{callbacks:{label: ctx => `${ctx.raw.x} ${ctx.raw.y}:00 — ${Math.round(ctx.raw.min)} min asleep`}}},
    },
  }));
}

function renderDiaper(S) {
  const section = document.getElementById('diaper-section');
  if (!S || !S.days || !S.days.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  const s = S.summary;
  const cards = [
    ['Total', s.total],
    ['Wet', s.total_wet],
    ['Dirty', s.total_dirty],
    ['Both', s.total_both],
  ];
  document.getElementById('diaper-cards').innerHTML = cards.map(([k,v]) =>
    `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');

  charts.push(new Chart(document.getElementById('diaperDaily'), {
    type: 'bar',
    data: {labels: S.days, datasets: [
      {label:'wet', data: S.daily_wet, backgroundColor: C_WET},
      {label:'dirty', data: S.daily_dirty, backgroundColor: C_DIRTY},
      {label:'both', data: S.daily_both, backgroundColor: C_BOTH},
    ]},
    options: {
      scales: {x: {stacked:true}, y: {stacked:true, beginAtZero:true, title:{display:true, text:'diapers'}}},
      plugins: {legend:{position:'top'}},
    },
  }));
}

function renderBottle(S) {
  const section = document.getElementById('bottle-section');
  if (!S || !S.days || !S.days.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  const s = S.summary;
  const cards = [
    ['Bottles', s.total_bottles],
    ['Total oz', s.total_oz],
    ['Avg oz', s.avg_oz],
  ];
  document.getElementById('bottle-cards').innerHTML = cards.map(([k,v]) =>
    `<div class="card"><div class="v">${v}</div><div class="k">${k}</div></div>`).join('');

  const bar = (id, data, label) => charts.push(new Chart(document.getElementById(id), {
    type: 'bar',
    data: {labels: S.days, datasets: [{label, data, backgroundColor: C.B}]},
    options: {plugins: {legend: {display:false}}, scales: {y: {beginAtZero:true, title:{display:true, text:label}}}},
  }));
  bar('bottleOz', S.daily_oz, 'oz');
  bar('bottleAvg', S.daily_avg_oz, 'avg oz');
  bar('bottleCount', S.daily_bottles, 'bottles');

  charts.push(new Chart(document.getElementById('bottleClock'), {
    type: 'bubble',
    data: {datasets: [{
      data: S.clock.map(p => ({x: p.date, y: p.hour, r: 3 + p.oz, oz: p.oz})),
      backgroundColor: 'rgba(232,161,58,0.55)',
    }]},
    options: {
      maintainAspectRatio: false,
      scales: {
        x: {type:'category', labels: S.days, title:{display:true, text:'Date'}},
        y: {min:-0.5, max:23.5, reverse:true, ticks:{stepSize:3, callback:v=>`${v}:00`}, title:{display:true, text:'Hour of day'}},
      },
      plugins: {legend:{display:false}, tooltip:{callbacks:{label: ctx => `${ctx.raw.x} ${ctx.raw.y}:00 — ${ctx.raw.oz} oz`}}},
    },
  }));
}

let lastGen = null;
async function load() {
  const D = await (async () => (__DATA_SRC__))();
  if (LIVE && D.summary && D.summary.generated_at === lastGen) return;
  lastGen = D.summary ? D.summary.generated_at : null;
  render(D);
}
load();
if (LIVE) setInterval(load, 120000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # Honor BABY_NAME (and any other vars) from .env for the static export path,
    # since this entrypoint doesn't go through bot.py's load_dotenv().
    from dotenv import load_dotenv
    load_dotenv()
    main()

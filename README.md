# Baby Chart Bot

A Discord bot that turns a baby's daily log — breast & bottle feeds, sleep and
naps, and diapers — into live charts. Type plain-text entries in a channel
(`L 10:00 - 10:10`, `SS 22:30`, `chart`); the bot parses, stores, and renders
them. One Python process runs the Discord bot and a local web dashboard.

---

## Deploy

Four ways to run it — pick one. All of them need a Discord bot token and a channel
ID first (see [Discord Bot Setup](#discord-bot-setup)). **Railway** is the easiest,
most hands-off deploy (~$5/month); **Sparked Host** is cheaper (~$1–2/month) and fully
point-and-click, with a bit more setup.

### A. Railway (one-click)

This bot is small, but it runs **always-on**, so it needs Railway's **Hobby
plan ($5/month**, which includes $5 of usage — plenty for this bot). The good
news: signing up through a referral link gives your new account **$20 in
credit**, which covers **~4 months** of Hobby hosting.

1. **Create your Railway account** through this referral link to get the **$20
   credit**: **[railway.com (referral)](https://railway.com?referralCode=79B_T4)**.
2. On the [Hobby plan](https://railway.com/pricing) ($5/mo), the $20 credit is
   applied to your bill automatically — so the first few months are effectively
   free. A card is required on file for any usage beyond the credit.
3. Click **Deploy on Railway** below, fill in `DISCORD_TOKEN` /
   `FEEDING_CHANNEL_ID` (and optionally `BABY_NAME` / `BABY_DOB`), and deploy.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hL6R1x?utm_medium=integration&utm_source=template&utm_campaign=baby-chart)

> Railway doesn't require a GitHub account — you can sign up with email. Its
> GitHub check only governs the **free trial** (a brand-new account may land on a
> limited trial with restricted networking). Adding a payment method / starting on
> the **Hobby plan** skips that entirely, so deploy on Hobby rather than evaluating
> on the free trial. If you'd rather spend less than $5/month and don't mind a more
> involved setup, use **Sparked Host** below.

<details>
<summary>Prefer to deploy from your own fork instead of the button?</summary>

1. **Fork** this repo.
2. On Railway, **New Project → Deploy from GitHub repo** and pick your fork. It
   builds from the included `Dockerfile`.
3. Set env vars (Variables tab): `DISCORD_TOKEN`, `FEEDING_CHANNEL_ID`, and
   optionally `BABY_NAME` / `BABY_DOB`. (`HOST`/`PORT` are handled automatically.)
4. **Add a persistent volume** (service → Settings → Volumes) mounted at **`/data`**
   — `feedings.json`, your only durable state, lives there. Without it your log
   resets on every redeploy.
5. **Invite the bot** to your server and start logging.

</details>

### B. Sparked Host (panel)

A cheaper, fully point-and-click alternative — and a good fallback if Railway gives
you trouble. It's a web control panel (no terminal required), and the flat price is
lower: **~$1–2/month**. Because this bot imports `matplotlib`
and `numpy` (memory-heavy), order the **Advanced** plan (**$2/mo, 1 GB RAM**) for
headroom — the **Basic** plan ($1/mo, 512 MB) works but is tight.

1. Sign up via **[Sparked Host (referral)](https://billing.sparkedhost.com/aff.php?aff=3312)**
   and order a **Discord Bot Hosting** plan (**Advanced — 1 GB** recommended).
2. In the panel, open the **File Manager** and upload the bot's files —
   `bot.py`, `parser.py`, `render.py`, `nap.py`, and `requirements.txt` (a
   zip-upload or the panel's "pull from Git" option both work). **Don't** upload
   `.env` — secrets go in the panel's variables instead (step 5).
3. **Startup → Python Packages**: paste the dependencies, space-separated —
   `discord.py fastapi uvicorn matplotlib numpy python-dotenv`. (Panels install
   from this field; some also read `requirements.txt` automatically.)
4. **Startup**: set the bot file / startup command to run **`bot.py`** (e.g.
   `python3 bot.py`, per Sparked Host's Python-bot guide).
5. **Variables**: add `DISCORD_TOKEN` and `FEEDING_CHANNEL_ID` (and optionally
   `BABY_NAME` / `BABY_DOB`), plus **`TZ=America/New_York`** for your timezone
   (this must be a host/panel variable — see [Environment Variables](#environment-variables)).
   Leave **`HOST=127.0.0.1`** so the unauthenticated dashboard stays private — the
   Discord `chart`/PNG commands give you the full view from your phone anyway. Only
   set `HOST=0.0.0.0` + `PORT=<panel-allocated port>` if you deliberately want the
   public web dashboard.
6. **Persistence**: `feedings.json` is written in the panel's working directory,
   which survives restarts — there's no volume to configure (just don't wipe the
   File Manager). Back it up by downloading it from the File Manager — see
   [Data & Backups](#data--backups).
7. Click **Start** in the console, then [invite the bot](#discord-bot-setup) to your
   server and start logging.

**Heads-up:** the "chart at home" web dashboard is awkward on a shared panel host
(one allocated port, no auth), so the Discord chart commands are the intended
experience there. And if the bot fails to start, double-check the **Python Packages**
field — panels don't always auto-install from `requirements.txt`.

### C. Docker (self-hosted)

Build the image, then run it with **docker compose** (recommended) or a raw
`docker run`. Both persist state to `./data/feedings.json` on the host.

```bash
docker build -t baby-chart .
```

**docker compose** — edit `TZ` in `docker-compose.yml`, put your token + channel
ID in `.env` (`cp .env.example .env`), then:

```bash
docker compose up -d
```

**Or raw `docker run`:**

```bash
docker run -d --name baby-chart --restart unless-stopped \
  --env-file .env \
  -e TZ=America/New_York \
  -e HOST=0.0.0.0 \
  -p 8000:8000 \
  -v "$PWD/data:/data" \
  baby-chart
```

The dashboard comes up at `http://localhost:8000/`. Set your timezone via the `TZ`
env var here (**not** in `.env` — see [Environment Variables](#environment-variables)).
The `-v` mount keeps `feedings.json` in `./data/` on the host.

### D. Local (no Docker)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env       # then fill in token + channel ID
.venv/bin/python bot.py
```

The bot connects to Discord and the chart server comes up at
`http://127.0.0.1:8000/`. Ctrl-C stops both. Locally none of the optional vars are
needed — the bot binds `127.0.0.1:8000` and stores `feedings.json` next to the code.

---

## Discord Bot Setup

One-time, from the [Discord Developer Portal](https://discord.com/developers/applications):

1. **New Application → Bot tab → Reset Token**, and copy the token →
   `DISCORD_TOKEN`.
2. On the same Bot page, toggle **Message Content Intent** **ON** (under
   "Privileged Gateway Intents"). The bot reads every message, so this is
   required.
3. **OAuth2 → URL Generator** → scopes: `bot`; permissions: **View Channels,
   Send Messages, Read Message History, Add Reactions, Attach Files** (and
   optionally **Manage Channels** if you want the bot to keep the channel topic
   updated). Open the generated URL and invite the bot to a server you own.
4. In Discord, **Settings → Advanced → enable Developer Mode**. Right-click the
   channel you want the bot to watch → **Copy Channel ID** → `FEEDING_CHANNEL_ID`.
5. Put both values in your environment (`.env` locally, or the Railway
   Variables tab).

---

## Environment Variables

| Variable             | Required | Default       | Description                                                        |
|----------------------|----------|---------------|--------------------------------------------------------------------|
| `DISCORD_TOKEN`      | ✅       | —             | Bot token from the Discord Developer Portal.                       |
| `FEEDING_CHANNEL_ID` | ✅       | —             | ID of the channel the bot listens to.                              |
| `BABY_NAME`          | —        | `Baby`        | Shown in the help text, web chart title, and wake reminders.       |
| `BABY_DOB`           | —        | —             | Birth date (`YYYY-MM-DD`). Enables the next-nap prediction on `SF`; omit to disable. |
| `HOST`               | —        | `127.0.0.1`   | Bind address for the web dashboard. Use `0.0.0.0` for cloud.       |
| `PORT`               | —        | `8000`        | Dashboard port. Railway injects this automatically.                |
| `DATA_DIR`           | —        | code dir      | Directory holding `feedings.json`. Set to a volume path in cloud.  |

These go in `.env` (local) or the Railway Variables tab (cloud).

**Timezone:** all timestamps are stored in the **process's local time**. To
control it, set `TZ` in the **host environment** — the Railway Variables tab,
`docker run -e TZ=America/New_York`, or your shell before launching — *not* in
`.env`. The `.env` file is loaded after the process has already resolved its
timezone, so a `TZ` placed there has no effect. Defaults to the system timezone.

---

## Channel Grammar

Every message in the channel is parsed. The parser is case-insensitive and
tolerant of common typos.

**Feeds**

| Message                | Meaning                                               |
|------------------------|-------------------------------------------------------|
| `L 10:00 - 10:10`      | Left side, start–end (R for right).                   |
| `right 9:27 - 9:45`    | `left`/`right` words work too.                        |
| `L 10`                 | Bare hour → 10:00 start (open session).               |
| `- 10:15`              | Closes the most recent open session (split messages). |
| `L done 10:12`         | End only (`done`/`finished`/`stop`/`end`/`ended`).    |
| `L` / `R`              | Start that side now (or close it if open).            |
| `L done` / `R stop`    | Close an open session for that side, now.             |
| `undo` / `oops` / `^`  | Remove your most recent event (last 12 h).            |
| `L undo` / `R undo`    | Remove the most recent L or R event.                  |
| `fix_end`              | Convert your most recent open start into an end.      |

**Bottles** (a feed; side `B`, optional oz — shown as its own color in feed graphs)

| Message                | Meaning                                               |
|------------------------|-------------------------------------------------------|
| `B 4 10:00 - 10:15`    | 4 oz bottle, start–end (oz optional).                 |
| `B 10:00`              | Open a bottle (close later).                          |
| `B 4`                  | Close the open bottle now with 4 oz (or instant 4 oz).|
| `B 4 10:15`            | Close the open bottle at 10:15 with 4 oz.             |
| `B done`               | Close the open bottle (no oz).                        |
| `B undo`               | Remove the most recent bottle.                        |

Bottle times must be explicit (`10:00`); a bare 1-2 digit number is always read
as ounces. `bottle` works as a synonym for `B`.

**Sleep** (tracked separately from feeds, no L/R)

| Message       | Meaning                                          |
|---------------|--------------------------------------------------|
| `SS` / `SS 22:30` | Sleep start (now, or at the given time).     |
| `SF` / `SF 6:15`  | Finish the open sleep (rolls past midnight). With `BABY_DOB` set, the reply adds a `🍼 Next nap target` (age-based wake window). |
| `SS undo`     | Remove the most recent sleep.                    |
| `SF undo`     | Re-open the most recently finished sleep.        |
| `wake 90`     | Remind once if awake 90 min with no `SS` logged. |
| `wake 0` / `wake off` | Disable the wake reminder (e.g. rely on the next-nap prediction). |

**Diapers** (tracked separately, instantaneous)

| Message       | Meaning                                          |
|---------------|--------------------------------------------------|
| `d wet`       | Log a wet diaper (pee; `pee` also works).        |
| `d dirty`     | Log a dirty diaper (poop; `poop` also works).    |
| `d both`      | Log a both diaper.                               |
| `d wet 14:30` | Back-time a diaper (optional trailing time).     |
| `d undo`      | Remove the most recent diaper.                   |

**Charts & info**

| Message                          | Meaning                                  |
|----------------------------------|------------------------------------------|
| `chart` / `today` / `week` / `graph` | Multi-panel PNG summary.             |
| `listcharts`                     | List individual charts you can request.  |
| `timeline` / `heatmap` / `gap_night` / `sleep_timeline` / `sleep_clock` / `sleep_daily` / `diaper_daily` / `bottle_oz` / `bottle_avg` / `bottle_count` / `bottle_clock` | A specific chart as PNG. |
| `stats` / `summary`              | Text summary.                            |
| `help` / `commands` / `?`        | Full command list + parser cheat sheet.  |

Reactions confirm each entry: 🍼 feed · 😴 sleep · 🚼 diaper · 🤔 couldn't parse · ⚠️ problem.

---

## Data & Backups

`feedings.json` is the **only** durable state — back it up and you've backed up
everything.

```bash
cp feedings.json feedings.json.$(date +%F).bak
```

Where the file lives depends on how you run it: next to the code locally, in
`./data/` with the Docker runbook (the host side of the `/data` bind-mount), in the
bot's working directory on Sparked Host (download it from the panel's File Manager),
and on the `/data` volume on Railway (download it via the Railway CLI or a volume
browser) — keep an off-host copy either way.

The web dashboard binds to `HOST` only and there is no auth — keep it on
`127.0.0.1` locally, and be aware that `0.0.0.0` in the cloud makes the chart
reachable at your service URL.

---

## License

Licensed under the MIT License — see [LICENSE](LICENSE).

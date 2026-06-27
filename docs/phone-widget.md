# Log from your phone's home screen (no Discord)

Put **L / R / SS / SF / undo** buttons on your Android or iPhone home screen. One
tap logs a feed or sleep without opening Discord, finding the channel, and typing.

## How it works

Every message in the feeding channel is parsed by the bot — and the bot reads
messages from *any* author, not just people. So a **Discord webhook** that posts
the literal text `L`, `R`, `SS`, `SF`, or `undo` into the channel is logged
exactly as if you'd typed it. The widget is just a button that fires one HTTP
`POST` to that webhook.

```
[ tap L ] ──HTTP POST {"content":"L"}──▶ Discord webhook ──▶ #feedings channel ──▶ bot parses ──▶ 🍼
```

Nothing in the bot changes. This works no matter where the bot runs (home,
Railway, Sparked Host) because everything routes through Discord — there's no
local server to reach.

## One-time setup

### 1. Create the webhook

1. In Discord, open the feeding channel → **Edit Channel** (gear) →
   **Integrations** → **Webhooks** → **New Webhook**.
2. Name it `widget` (the name doesn't matter), optionally give it an avatar.
3. **Copy Webhook URL.**

> **Treat the webhook URL like a password.** Anyone who has it can post to your
> channel. Don't commit it or share it. You can delete/regenerate it any time from
> the same screen.

The remaining steps are per-platform. Each button is one shortcut that POSTs this
JSON body to the webhook (swap the `content` value per button — `L`, `R`, `SS`,
`SF`, `undo`):

```json
{"content": "L", "username": "yourname"}
```

> **Use the same `username` on all five buttons.** That string becomes the event's
> `source`, and the bot's `undo` only removes the most recent event **logged under
> the same name**. Matching names makes the widget's `undo` reliably remove the
> last thing the widget logged. (Pick any name — e.g. your first name.)

### 2a. On Android — HTTP Shortcuts

1. **Install** **HTTP Request Shortcuts** by Waboodoo (free, open-source) —
   F-Droid or Play Store, package `ch.rmy.android.http_shortcuts`. (The project's
   site calls it "HTTP Shortcuts," but the store listing is "HTTP Request
   Shortcuts.")
2. **Create five shortcuts.** Tap **+** → **Regular shortcut**, one per button:

   | Field        | Value                                              |
   |--------------|----------------------------------------------------|
   | Name         | `L` (then `R`, `SS`, `SF`, `undo`)                 |
   | Method       | `POST`                                             |
   | URL          | your webhook URL                                   |
   | Request body | **JSON** / `Content-Type: application/json`        |
   | Body content | `{"content": "L", "username": "yourname"}`         |

   *Optional confirmation:* enable "Show toast on success" so you get an on-phone
   "OK" without opening Discord.
3. **Put them on the home screen.** Long-press the home screen → **Widgets** →
   **HTTP Shortcuts** → drop each shortcut as a 1×1 icon, or use the app's grid
   widget to group all five.

### 2b. On iPhone — Shortcuts (built-in)

1. **Open the Shortcuts app** (preinstalled; reinstall from the App Store if you
   removed it).
2. **Create five shortcuts.** Tap **+**, add the action **"Get Contents of
   URL"**, then:
   - Set **URL** to your webhook URL.
   - Tap **Show More** → **Method** = `POST`.
   - **Request Body** = `JSON`. Add two fields: `content` = `L` (Text), and
     `username` = `yourname` (Text). (JSON body sets `Content-Type` for you.)
   - Name the shortcut `L`. Repeat for `R`, `SS`, `SF`, `undo`.
   - *Optional:* the default banner already confirms it ran; you can add a
     **Show Notification** action if you want a clearer "OK".
3. **Put them on the home screen** — either:
   - **As icons:** open a shortcut → Share → **Add to Home Screen** (one icon
     each). Tapping briefly flashes the Shortcuts app open.
   - **As a button grid (closer to a widget):** put the five shortcuts in a
     **Folder**, then long-press the home screen → **+** → **Shortcuts** widget →
     pick a size and point it at that folder. Tapping a tile runs it in place with
     just a banner.

A handy layout either way:

```
[ L ] [ R ]
[ SS ][ SF ]
[  undo  ]
```

## Quick reference — what each button does

| Button | Posts  | Effect                                                        |
|--------|--------|--------------------------------------------------------------|
| L      | `L`    | Start a left feed; tap again to close it.                    |
| R      | `R`    | Start a right feed; tap again to close it.                   |
| SS     | `SS`   | Sleep start (now).                                           |
| SF     | `SF`   | Finish the open sleep (now).                                 |
| undo   | `undo` | Remove the most recent event the widget logged.             |

Open/close pairing for feeds and sleep is **author-agnostic** — a widget `L` will
close a left feed someone opened by typing in Discord, and an `SF` finishes a
sleep started by `SS` from anywhere. Only `undo` is scoped to the widget's own
`username`.

## Caveats

- **`undo` is per-name.** It won't undo a feed that someone *typed* in Discord
  under their own Discord name — only events the widget logged. To undo a typed
  feed, type `undo` (or `L undo`) in Discord.
- **No parsed confirmation on the phone.** The widget can show the webhook's HTTP
  `200`, but the bot's parse result (`logged: …` / 🍼) arrives a moment later in
  the channel, not back to the app.
- **Rate limit:** Discord caps a webhook at ~30 messages/min — irrelevant at
  baby-logging volume.
- Want timed entries (`L 10:00 - 10:10`) or bottles/diapers? Add more shortcuts
  with different `content` (e.g. `{"content": "d wet"}`, `{"content": "B"}`). The
  full grammar is in the [README](../README.md#channel-grammar).

## Test it before touching the phone

You can prove the whole chain from any terminal with `curl` — no app needed:

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"content":"L","username":"widget-test"}' "<WEBHOOK_URL>"
```

You should see the message land in the channel and the bot react 🍼 with a
`logged: L HH:MM` reply. POST `L` again to close it, then try `SS`/`SF` and
`undo`.

# Emporia Pro EV Charger — stock watcher

Polls two retailers' Shopify product JSON and pushes an alert the moment the
**hardwired + J1772** Emporia Pro variant flips from out-of-stock to in-stock.
J1772 only — *not* NACS.

- Reads structured Shopify variant data (`<product>.json`), never scrapes HTML.
- Alerts **only** on a false → true (out → in stock) transition.
- A network/parse error on one source is logged and skipped — never treated as
  "out of stock," and never crashes the other source.
- macOS (Apple Silicon), Python 3.11+, deps: stdlib + `requests` only.

## Files

| File | Purpose |
|------|---------|
| `emporia_watcher.py` | the watcher (poll / `--resolve` / `--test`) |
| `config.example.toml` | config template — copy to `config.toml` |
| `com.user.emporiawatcher.plist` | launchd job (macOS-native scheduling) |
| `state.json` | created at runtime; last-known availability (gitignored) |

## Setup

```bash
cd emporia_watcher
python3 -m pip install requests          # the only non-stdlib dependency
cp config.example.toml config.toml       # config.toml is gitignored (holds secrets)
```

### Step 1 — resolve the variant IDs (do this first)

Don't trust the handles/variant guesses. Print every variant and pick the
**J1772 + Hardwired** row:

```bash
python3 emporia_watcher.py --resolve
```

Example output (ids illustrative):

```
=== emporia  (https://shop.emporiaenergy.com/products/emporia-pro-ev-charger.json) ===
  product: Emporia Pro Level 2 EV Charger  (handle: emporia-pro-ev-charger)
              id  available  title / options
  --------------  ---------  ----------------------------------------
  43512345678901      False  J1772 / NEMA 14-50
  43512345678902      False  J1772 / Hardwired        <-- this one
  43512345678903       True  NACS / Hardwired
```

> Note: if a handle 404s, open the store, find the correct product, and update
> `json_url` / `product_url` / `handle` in `config.toml`.

Copy the J1772 + Hardwired `id` into `config.toml` as that source's
`variant_id`. A source whose `variant_id` is still `0` is skipped with a notice.

### Step 2 — set up alerts

Default channel is **ntfy** (no account). Pick an unguessable topic in
`config.toml` (`notify.ntfy.topic_url`), then subscribe to that same topic in
the [ntfy mobile app](https://ntfy.sh/). Pushover and SMTP email are wired as
stubs — set `channel = "pushover"` or `"smtp"` and fill that section in.

### Step 3 — verify the alert path end-to-end

```bash
python3 emporia_watcher.py --test
```

This forces a fake out → in-stock transition through the real
`handle_transition()` → `notify()` path. You should receive an alert. It does
**not** touch `state.json`.

### Step 4 — run a real poll manually

```bash
python3 emporia_watcher.py
```

## Scheduling (macOS launchd — preferred)

1. Edit `com.user.emporiawatcher.plist`: replace every `CHANGE_ME` path and
   confirm the `python3` path (`which python3`).
2. Install and start it:

```bash
cp com.user.emporiawatcher.plist ~/Library/LaunchAgents/
launchctl load   ~/Library/LaunchAgents/com.user.emporiawatcher.plist   # start
launchctl list | grep emporiawatcher                                    # confirm loaded
```

To stop / reload after editing:

```bash
launchctl unload ~/Library/LaunchAgents/com.user.emporiawatcher.plist   # stop
launchctl unload ~/Library/LaunchAgents/com.user.emporiawatcher.plist && \
launchctl load   ~/Library/LaunchAgents/com.user.emporiawatcher.plist   # reload
```

`StartInterval` is `1200` seconds (20 min); keep it in sync with
`poll_interval_minutes`. Logs go to `watcher.log` / `watcher.err.log`.

### Cron fallback

launchd is preferred (survives reboot/login, no open terminal). If you'd
rather use cron, every 20 minutes:

```cron
*/20 * * * * cd /Users/CHANGE_ME/emporia_watcher && /opt/homebrew/bin/python3 emporia_watcher.py >> watcher.log 2>&1
```

## How it decides "in stock"

For each source it fetches `<product>.json`, finds the variant whose `id`
matches `variant_id`, and reads its `available` boolean. It compares to the
last value in `state.json` and alerts only when it sees `False → True`. First-
ever observation (no prior state) never alerts, even if already in stock — only
genuine flips do.

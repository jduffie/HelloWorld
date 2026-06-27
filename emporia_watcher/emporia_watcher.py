#!/usr/bin/env python3
"""
Emporia Pro EV Charger stock watcher.

Polls Shopify product JSON at two retailers and alerts the moment the
hardwired + J1772 variant flips from out-of-stock to in-stock.

Design notes
------------
- Reads STRUCTURED variant data from Shopify (<product>.json), never HTML.
- Targets a single variant per source, identified by numeric variant id
  (resolved once via `--resolve` and stored in config, never inline here).
- Alerts ONLY on a false -> true (out -> in stock) transition.
- A network/parse error logs and SKIPS that source for the run. An error is
  never treated as "out of stock" and never crashes the other source.
- State (last-known availability per source) lives in a small JSON file.
- Alerting is pluggable behind one notify() function: ntfy (default),
  Pushover (stub), SMTP email (stub).

Requires Python 3.11+ (stdlib `tomllib`) and the `requests` package.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    sys.stderr.write("ERROR: Python 3.11+ is required (stdlib tomllib missing).\n")
    sys.exit(2)

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    sys.stderr.write("ERROR: the 'requests' package is required: pip3 install requests\n")
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Logging (stdlib, line-oriented, launchd-friendly: everything to stdout/err) #
# --------------------------------------------------------------------------- #
def log(msg: str, *, err: bool = False) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stream = sys.stderr if err else sys.stdout
    stream.write(f"[{stamp}] {msg}\n")
    stream.flush()


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    if not path.exists():
        log(f"Config not found: {path}. Copy config.example.toml to config.toml.", err=True)
        sys.exit(2)
    with path.open("rb") as fh:
        return tomllib.load(fh)


def http_cfg(config: dict) -> dict:
    http = config.get("http", {})
    return {
        "user_agent": http.get(
            "user_agent",
            "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X 14_0) "
            "EmporiaStockWatcher/1.0",
        ),
        "timeout": float(http.get("timeout_seconds", 15)),
        "retries": int(http.get("retries", 3)),
        "backoff_base": float(http.get("backoff_base_seconds", 2)),
    }


# --------------------------------------------------------------------------- #
# State                                                                        #
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    if not path.exists():
        return {"sources": {}, "last_heartbeat": None}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log(f"State file unreadable ({exc}); starting fresh.", err=True)
        return {"sources": {}, "last_heartbeat": None}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)  # atomic on POSIX


# --------------------------------------------------------------------------- #
# Shopify fetch + variant extraction                                           #
# --------------------------------------------------------------------------- #
class SourceError(Exception):
    """Network or parse failure for a single source. Caller logs and skips."""


def fetch_product(json_url: str, hc: dict) -> dict:
    """Fetch and parse a Shopify product JSON with light retry + backoff.

    Raises SourceError on persistent failure (caller skips the source).
    """
    headers = {"User-Agent": hc["user_agent"], "Accept": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(1, hc["retries"] + 1):
        try:
            resp = requests.get(json_url, headers=headers, timeout=hc["timeout"])
            resp.raise_for_status()
            data = resp.json()
            product = data.get("product", data)  # /<handle>.json wraps in "product"
            if "variants" not in product:
                raise SourceError("response has no 'variants' array")
            return product
        except (requests.RequestException, ValueError, SourceError) as exc:
            last_exc = exc
            if attempt < hc["retries"]:
                delay = hc["backoff_base"] * (2 ** (attempt - 1))
                log(f"  fetch attempt {attempt} failed ({exc}); retrying in {delay:.0f}s")
                time.sleep(delay)
    raise SourceError(f"giving up after {hc['retries']} attempts: {last_exc}")


def find_variant(product: dict, variant_id: int) -> dict:
    for v in product.get("variants", []):
        if int(v.get("id", -1)) == int(variant_id):
            return v
    raise SourceError(
        f"variant id {variant_id} not found "
        f"(have: {[v.get('id') for v in product.get('variants', [])]})"
    )


def variant_label(v: dict) -> str:
    opts = [v.get(k) for k in ("option1", "option2", "option3") if v.get(k)]
    title = v.get("title") or " / ".join(str(o) for o in opts)
    return title or f"variant {v.get('id')}"


# --------------------------------------------------------------------------- #
# Alerting (pluggable behind notify())                                         #
# --------------------------------------------------------------------------- #
def notify(config: dict, title: str, message: str, url: str | None = None) -> None:
    """Single dispatch point for all alerts. Channel selected in config."""
    channel = config.get("notify", {}).get("channel", "ntfy").lower()
    body = message if not url else f"{message}\n{url}"
    try:
        if channel == "ntfy":
            _notify_ntfy(config, title, body, url)
        elif channel == "pushover":
            _notify_pushover(config, title, body, url)
        elif channel == "smtp":
            _notify_smtp(config, title, body)
        else:
            log(f"Unknown notify channel '{channel}'; alert dropped.", err=True)
            return
        log(f"ALERT sent via {channel}: {title}")
    except Exception as exc:  # never let an alert failure crash the watcher
        log(f"Alert via {channel} FAILED ({exc}). Message was: {title} | {body}", err=True)


def _notify_ntfy(config: dict, title: str, body: str, url: str | None) -> None:
    cfg = config.get("notify", {}).get("ntfy", {})
    topic_url = cfg.get("topic_url")
    if not topic_url:
        raise RuntimeError("notify.ntfy.topic_url not set in config")
    # HTTP headers are latin-1 only, so strip any emoji/non-latin-1 from the
    # Title header (it would otherwise raise and drop the alert). The full title
    # is prepended to the UTF-8 body so nothing is lost in the notification.
    safe_title = title.encode("latin-1", "ignore").decode("latin-1").strip() or "Emporia watcher"
    headers = {"Title": safe_title, "Priority": "high", "Tags": "rotating_light"}
    if url:
        headers["Click"] = url
    full_body = f"{title}\n{body}" if safe_title != title else body
    resp = requests.post(topic_url, data=full_body.encode("utf-8"), headers=headers, timeout=15)
    resp.raise_for_status()


def _notify_pushover(config: dict, title: str, body: str, url: str | None) -> None:
    # --- STUB: fill in token/user in config and this works as-is. ---
    cfg = config.get("notify", {}).get("pushover", {})
    token, user = cfg.get("token"), cfg.get("user")
    if not token or not user:
        raise RuntimeError("notify.pushover token/user not set in config")
    payload = {"token": token, "user": user, "title": title, "message": body, "priority": 1}
    if url:
        payload["url"] = url
    resp = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=15)
    resp.raise_for_status()


def _notify_smtp(config: dict, title: str, body: str) -> None:
    # --- STUB: fill in SMTP creds in config and this works as-is. ---
    cfg = config.get("notify", {}).get("smtp", {})
    required = ("host", "port", "username", "password", "from_addr", "to_addr")
    if not all(cfg.get(k) for k in required):
        raise RuntimeError(f"notify.smtp missing one of {required}")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg["from_addr"]
    msg["To"] = cfg["to_addr"]
    msg.set_content(body)
    with smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=20) as srv:
        srv.starttls()
        srv.login(cfg["username"], cfg["password"])
        srv.send_message(msg)


# --------------------------------------------------------------------------- #
# Core polling                                                                  #
# --------------------------------------------------------------------------- #
def source_variant_ids(source: dict) -> list[int]:
    """Watched variant ids for a source.

    Accepts `variant_ids = [..]` (preferred — watch the hardwired variant for
    each connector you'll accept) or a single legacy `variant_id`.
    """
    ids = source.get("variant_ids")
    if ids is None:
        single = source.get("variant_id")
        ids = [single] if single else []
    return [int(i) for i in ids if i]


def check_source(source: dict, hc: dict) -> list[tuple[int, bool, str]]:
    """Return [(variant_id, available, label), ...] for every watched variant.

    Fetches the product once. A network/parse failure raises SourceError (caller
    skips the whole source). A single missing variant id is logged and skipped
    so one bad id never blinds the other watched variants.
    """
    product = fetch_product(source["json_url"], hc)
    results: list[tuple[int, bool, str]] = []
    for vid in source_variant_ids(source):
        try:
            variant = find_variant(product, vid)
        except SourceError as exc:
            log(f"  {source.get('name', '?')}: {exc}", err=True)
            continue
        results.append((vid, bool(variant.get("available")), variant_label(variant)))
    return results


def handle_transition(config: dict, state: dict, name: str, source: dict,
                      readings: list[tuple[int, bool, str]]) -> None:
    """Compare each watched variant to last-known state; alert only on false -> true.

    State per source is keyed by variant id so each watched variant (e.g. the
    NACS-hardwired and J1772-hardwired SKUs) transitions independently.
    """
    src_state = state["sources"].setdefault(name, {})
    for vid, available, label in readings:
        key = str(vid)
        prev = src_state.get(key, {}).get("available")
        src_state[key] = {
            "available": available,
            "label": label,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        status = "IN STOCK" if available else "out of stock"
        log(f"  {name}: {label} -> {status} (was {prev})")

        if available and prev is False:
            notify(
                config,
                title=f"🔌 IN STOCK: Emporia Pro ({name})",
                message=f"{label} just flipped to IN STOCK at {name}. Buy now:",
                url=source["product_url"],
            )
        elif available and prev is None:
            # First ever observation already in stock: inform, but it's not a flip.
            log(f"  {name}: {label} already in stock on first run (no flip alert).")


def maybe_heartbeat(config: dict, state: dict) -> None:
    if not config.get("heartbeat", False):
        return
    hb_hour = int(config.get("heartbeat_hour", 9))
    today = date.today().isoformat()
    now = datetime.now()
    if state.get("last_heartbeat") == today or now.hour < hb_hour:
        return
    parts = []
    for n, variants in state["sources"].items():
        any_in = any(v.get("available") for v in variants.values())
        parts.append(f"{n}: {'IN STOCK' if any_in else 'OOS'}")
    summary = ", ".join(parts) or "no sources checked yet"
    notify(config, title="💓 Emporia watcher alive", message=f"Still watching. {summary}.")
    state["last_heartbeat"] = today


def run_once(config: dict, state_path: Path) -> None:
    hc = http_cfg(config)
    state = load_state(state_path)
    sources = config.get("sources", [])
    if not sources:
        log("No [[sources]] configured.", err=True)
        return

    for source in sources:
        name = source.get("name", source.get("json_url", "?"))
        if not source_variant_ids(source):
            log(f"  {name}: variant_ids not set — run --resolve first. SKIPPING.", err=True)
            continue
        try:
            readings = check_source(source, hc)
        except SourceError as exc:
            # Error -> log and SKIP. Never treat as out-of-stock.
            log(f"  {name}: ERROR, skipping this run: {exc}", err=True)
            continue
        handle_transition(config, state, name, source, readings)

    maybe_heartbeat(config, state)
    save_state(state_path, state)


# --------------------------------------------------------------------------- #
# --resolve : the variant-verification step (run this first)                   #
# --------------------------------------------------------------------------- #
def resolve(config: dict) -> None:
    hc = http_cfg(config)
    for source in config.get("sources", []):
        name = source.get("name", "?")
        print(f"\n=== {name}  ({source.get('json_url')}) ===")
        try:
            product = fetch_product(source["json_url"], hc)
        except SourceError as exc:
            print(f"  ERROR: {exc}")
            print("  If this 404s, open the store and find the right handle.")
            continue
        print(f"  product: {product.get('title')}  (handle: {product.get('handle')})")
        print(f"  {'id':>14}  {'available':>9}  title / options")
        print(f"  {'-'*14}  {'-'*9}  {'-'*40}")
        watched = set(source_variant_ids(source))
        for v in product.get("variants", []):
            mark = "<-- configured" if int(v.get("id", -1)) in watched else ""
            print(f"  {v.get('id'):>14}  {str(bool(v.get('available'))):>9}  "
                  f"{variant_label(v)} {mark}")
    print("\nPick the HARDWIRED row(s) you'll accept (any connector — NACS and/or "
          "J1772) and list their ids in config.toml as that source's variant_ids.\n")


# --------------------------------------------------------------------------- #
# --test : force a fake out -> in transition end-to-end                        #
# --------------------------------------------------------------------------- #
def self_test(config: dict) -> None:
    log("--test: simulating an out-of-stock -> in-stock transition end-to-end.")
    sources = config.get("sources", [])
    source = sources[0] if sources else {
        "name": "test",
        "product_url": "https://shop.emporiaenergy.com/products/emporia-pro-ev-charger",
    }
    name = source.get("name", "test")
    # Fabricate prior state = out of stock, current reading = in stock, and run
    # the REAL transition logic so notify() fires through the configured channel.
    fake_vid = (source_variant_ids(source) or [1])[0]
    fake_state = {"sources": {name: {str(fake_vid): {"available": False}}},
                  "last_heartbeat": None}
    handle_transition(
        config, fake_state, name, source,
        [(fake_vid, True, "Hardwired (TEST)")],
    )
    log("--test complete. If you did not receive an alert, check notify config above.")


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emporia Pro EV charger stock watcher.")
    here = Path(__file__).resolve().parent
    parser.add_argument("-c", "--config", type=Path, default=here / "config.toml",
                        help="path to config.toml")
    parser.add_argument("--state", type=Path, default=here / "state.json",
                        help="path to state file")
    parser.add_argument("--resolve", action="store_true",
                        help="fetch each source and print all variants (id/title/available)")
    parser.add_argument("--test", action="store_true",
                        help="force a fake in-stock transition end-to-end (verifies alerts)")
    args = parser.parse_args(argv)

    config = load_config(args.config)

    if args.resolve:
        resolve(config)
    elif args.test:
        self_test(config)
    else:
        run_once(config, args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

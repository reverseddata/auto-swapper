"""Command line entry point: `mm2sniper <command>` (or `python -m mm2sniper`)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings, load_settings
from .logging_setup import setup_logging

log = logging.getLogger("mm2sniper")


def _settings(args) -> Settings:
    try:
        return load_settings(args.config)
    except ConfigError as e:
        print("Configuration problems:", file=sys.stderr)
        for p in e.problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(2)


def cmd_run(args):
    s = _settings(args)
    setup_logging(s.log_level, s.log_dir, s.log_json)
    from .service import run_service
    try:
        asyncio.run(run_service(s))
    except KeyboardInterrupt:
        pass


def cmd_check(args):
    s = _settings(args)
    print(json.dumps(s.redacted(), indent=2))
    print("configuration OK")


def _fmt_listing(l) -> str:
    state = "AVAILABLE" if l.available else "unavailable"
    return f"{l.name:<28} {l.side:<8} {state:<11} qty={l.quantity:<4} price={l.price} {l.unit}"


def cmd_balance(args):
    """Read-only: the BloxSwaps balance and the Roblox account the session belongs to."""
    s = _settings(args)
    setup_logging("WARNING")
    from .http import AuthError, HttpClient, MarketplaceError
    from .service import build_adapter

    async def go():
        http = HttpClient(default_timeout=s.request_timeout_seconds)
        adapter = build_adapter(s, http)
        try:
            if not adapter.capabilities.claim:
                print(f"no session: {adapter.capabilities.claim_unavailable_reason}")
                return 1
            account = await adapter._read_account()
            balance = await adapter.get_balance()
        except AuthError:
            print("session rejected: paste a fresh BLOXSWAPS_TOKEN into .env")
            return 1
        except MarketplaceError as e:
            print(f"could not read balance: {e}")
            return 1
        finally:
            await http.close()
        print(f"account: {account}\nbalance: {balance} BloxSwaps value")
        return 0
    raise SystemExit(asyncio.run(go()))


def cmd_probe(args):
    """One live read of BloxSwaps. Shows watched items. Never claims or notifies."""
    s = _settings(args)
    setup_logging("WARNING")
    from .http import HttpClient
    from .service import build_adapter

    async def go():
        http = HttpClient(default_timeout=s.request_timeout_seconds)
        adapter = build_adapter(s, http)
        try:
            listings = await adapter.get_listings()
        finally:
            await http.close()
        print(f"BloxSwaps reachable: {len({l.slug for l in listings})} items, "
              f"{sum(l.available for l in listings if l.side == 'withdraw')} in bot stock")
        print(f"claiming: {'supported' if adapter.capabilities.claim else 'NOT available'}"
              + ("" if adapter.capabilities.claim
                 else f" ({adapter.capabilities.claim_unavailable_reason})"))
        print("\nwatched items:")
        for w in s.watch:
            hits = [l for l in listings if w.matches(l.name, l.slug, l.side)]
            if not hits:
                print(f"  {w.item:<28} (no BloxSwaps item matches this name)")
            for l in hits:
                print("  " + _fmt_listing(l))
    asyncio.run(go())


def cmd_dry_run(args):
    """Evaluate two saved /items payloads (before -> after) offline and print
    what would be claimed. No network, no Discord, no claims, no secrets needed."""
    import os
    os.environ["MODE"] = "monitor"   # skip claim-mode secret checks; nothing is sent
    s = _settings(args)
    s.mode = args.as_mode
    setup_logging("WARNING")
    from .marketplaces.bloxswaps import normalize_rows
    from .service import is_trigger
    from .store import Store

    payloads = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.fixture]
    store = Store(":memory:")
    held = None
    print(f"mode={s.mode}, watching {len(s.watch)} item(s)\n")
    for i, payload in enumerate(payloads, 1):
        listings, bad = normalize_rows(payload)
        print(f"sweep {i}: {len({l.slug for l in listings})} items "
              f"({bad} malformed rows skipped)")
        for l in listings:
            change = store.observe(l)
            if not is_trigger(s, l, change, first_sweep=i == 1):
                continue
            w = s.watch_for(l.name, l.slug, l.side)
            if w is None:
                continue
            ok = w.price_ok(l.side, l.price) and (
                l.side != "withdraw" or s.max_claim_price is None or l.price <= s.max_claim_price)
            if not ok:
                verdict = "skip (price limit)"
            elif s.mode == "monitor":
                verdict = "WOULD NOTIFY"
            elif held:
                verdict = f"would wait (polling paused for {held})"
            else:
                verdict, held = "WOULD CLAIM, then pause polling", l.name
            print(f"  [{change.value:<13}] {_fmt_listing(l)}  -> {verdict}")
    store.close()


def cmd_status(args):
    s = _settings(args)
    if not s.status_file.exists():
        print("no status file yet (has the service run?)")
        raise SystemExit(1)
    print(s.status_file.read_text(encoding="utf-8"))


def cmd_resume(args):
    s = _settings(args)
    from .models import ClaimStatus
    from .store import Store
    store = Store(s.db_path)
    hold = store.get_hold()
    if hold is None:
        print("no active hold; polling is not paused")
    else:
        if store.claim(hold.idem_key) is not None:
            store.update_claim(hold.idem_key, ClaimStatus.COMPLETED,
                               detail="resumed by operator")
        store.clear_hold()
        print(f"hold for {hold.name} cleared; the running service resumes polling within "
              f"{s.claim_status_interval_seconds:g}s")
    store.close()


def cmd_claims(args):
    s = _settings(args)
    from .store import Store
    store = Store(s.db_path)
    rows = store.recent_claims(args.limit)
    if not rows:
        print("no claims recorded")
    for r in rows:
        print(f"{r['created_at'][:19]}  {r['status']:<9} {r['side']:<8} {r['name']:<26} "
              f"{r['price']:>8}  {r['detail'] or ''}")
    store.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="mm2sniper", description="BloxSwaps restock claimer")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="config file (default: ./config.toml or CONFIG_FILE)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the watcher").set_defaults(fn=cmd_run)
    sub.add_parser("check-config", help="validate configuration").set_defaults(fn=cmd_check)
    sub.add_parser("probe", help="one live read, no claims").set_defaults(fn=cmd_probe)
    sub.add_parser("balance", help="read-only: BloxSwaps balance and account"
                   ).set_defaults(fn=cmd_balance)
    d = sub.add_parser("dry-run", help="evaluate saved payloads offline")
    d.add_argument("fixture", nargs="+", help="one or more saved /items JSON files, in order")
    d.add_argument("--as-mode", choices=("monitor", "claim"), default="claim",
                   help="which mode's decisions to show (default: claim)")
    d.set_defaults(fn=cmd_dry_run)
    sub.add_parser("status", help="print the last status snapshot").set_defaults(fn=cmd_status)
    sub.add_parser("resume", help="clear the hold after you finished a trade"
                   ).set_defaults(fn=cmd_resume)
    c = sub.add_parser("claims", help="list recent claims")
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(fn=cmd_claims)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()

# auto-swapper (mm2sniper)

Watches [BloxSwaps](https://bloxswaps.com) for Murder Mystery 2 items. When a watched
item becomes available, it either pings Discord (`monitor` mode) or claims it on
your BloxSwaps account and pings Discord with the join link (`claim` mode).

* **withdraw**: the bots restocked an item (stock went 0 → 1+). Claim it.
* **deposit**: a deposit slot opened for an item (slots went 0 → 1+). Claim the slot.

After a claim, polling pauses until the trade finishes on BloxSwaps (or a timeout),
so it never holds two claims at once. **You still do the in-game trade yourself.**
This bot never touches Roblox.

> Use it on your own account only. Automating a site may break its terms of service.
> You are responsible for what the bot claims. Start in `monitor` mode.

## Quick start (local)

```bash
python -m venv .venv
.venv/bin/pip install .            # Windows: .venv\Scripts\pip install .
cp .env.example .env               # then fill it in
cp config.example.toml config.toml # then choose your items
mm2sniper check-config
mm2sniper probe                    # one live read: shows your watched items
mm2sniper run
```

## Choosing items

Either a `config.toml` (full control) or the `WATCH` variable (quick, good for Railway).

`config.toml`:

```toml
[[watch]]
item = "Harvester"
side = "withdraw"     # withdraw | deposit | both
max_price = 330       # withdraw: only claim at or below this

[[watch]]
item = "Chroma Seer"
side = "deposit"
min_price = 30        # deposit: only if they pay at least this
```

`WATCH` (one line, prices included):

```
WATCH=Harvester<=330, Chroma Seer@deposit>=30, Icepiercer@both<=50>=10
```

`<=` is the most you pay on a withdraw, `>=` the least you accept on a deposit.
**Claim mode refuses any entry without a limit** for the side it covers, so it can
never buy at any price or give an item away cheaply. `MAX_CLAIM_PRICE` counts as
the withdraw limit for every entry. `monitor` mode needs no limits.

Names are matched loosely: case and punctuation don't matter, `C. Seer` equals
`Chroma Seer`, and the BloxSwaps slug works too. Plain and chroma are different items.
Run `mm2sniper probe` to confirm every name matches something.

Global caps in `.env` apply to every item: `MAX_CLAIM_PRICE` (largest single
withdraw) and `MAX_DAILY_SPEND` (per UTC day).

## Getting your token

Claim mode needs your logged-in BloxSwaps session.

1. Log in at bloxswaps.com, press F12, open **Network**, refresh the page.
2. Filter for `api.bloxswaps`, click a request such as `me` (not `items`).
3. Under **Request Headers**, copy the whole value after `cookie:`.
4. Put it in `.env` in double quotes: `BLOXSWAPS_TOKEN="..."`

Anyone with this value can act on your account. Never commit it or paste it anywhere.
The short-lived part renews itself and is saved in `DATA_DIR`. Paste a new value
only when the bot reports "token was rejected". Logging out of BloxSwaps ends it.

## Commands

| Command | What it does |
|---|---|
| `mm2sniper run` | Run the watcher |
| `mm2sniper check-config` | Validate settings (prints secret names, never values) |
| `mm2sniper probe` | One live read, shows watched items. Never claims |
| `mm2sniper balance` | Read-only: your account name and BloxSwaps balance |
| `mm2sniper status` | Last status snapshot |
| `mm2sniper claims` | Recent claims |
| `mm2sniper resume` | Clear the pause after you finished a trade |
| `mm2sniper dry-run FILE...` | Replay saved `/items` payloads offline |

## Deploying on Railway

1. Push this repo to GitHub, then in Railway: **New Project → Deploy from GitHub repo**.
   The `Dockerfile` is used automatically. No port or domain is needed; it is a worker.
2. **Variables**: add `MODE`, `DISCORD_WEBHOOK_URL`, `BLOXSWAPS_TOKEN` (claim mode),
   `WATCH`, and optionally `MAX_CLAIM_PRICE`, `MAX_DAILY_SPEND`, `DISCORD_MENTION_USER_ID`.
   Paste the token without quotes in Railway.
3. **Volume**: add one mounted at `/app/data`. It holds the claim history, the pause
   state and the refreshed session. Without it, a redeploy forgets an open claim and
   the session, and you would have to paste the token again.
   If the logs show a permission error on `/app/data`, set `RAILWAY_RUN_UID=0`
   (Railway volumes are owned by root; the image runs as a non-root user).
4. Watch the deploy logs for the startup line. To change items, edit `WATCH`;
   Railway redeploys.
5. Keep **1 replica** and run only one copy per token (not Railway and your PC at
   once). Two copies would each claim and fight over the session. The bot locks
   `DATA_DIR`, but separate machines don't share that lock.

After a claim, Discord gets the join link. Join, do the trade, and the bot
resumes on its own once BloxSwaps reports the trade done.

## How it stays safe

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). In short: one claim at a time,
the pause survives restarts, every claim is reserved locally before it is sent,
an unclear answer is checked with the site instead of retried, and secrets are
redacted from logs and Discord messages.

## License

MIT

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

### Marketplaces: BloxSwaps and StarPets

`MARKETS=bloxswaps` (default), `starpets` or `both`. Or switch any time in Discord with
`/market bloxswaps|starpets|both`, for example when BloxSwaps is down. If one market
fails, the other keeps being watched.

* **BloxSwaps**: alerts and claims.
* **StarPets Swap**: **alerts only.** Its item list is public, but its claim requests
  and login have not been captured yet, so the bot never guesses them on your
  account. A match there pings Discord and you swap by hand. StarPets is checked at
  most every 20 seconds.

### Managing items from Discord (recommended on Railway)

With a Discord bot, you change the watchlist with slash commands. No env edits and
no redeploys. Changes apply on the next poll.

| Command | Does |
|---|---|
| `/watch add item side max min` | Watch an item or change its limits (name autocompletes) |
| `/watch remove item` | Stop watching it |
| `/watch list` | Show what is watched |
| `/watch pause` / `/watch resume` | Stop / restart sniping |
| `/balance` | Your BloxSwaps account and balance |
| `/market` | Show or switch markets: bloxswaps, starpets, both |
| `/status` | Mode, markets, pause, open claim, last poll |

Example: `/watch add item:Harvester side:withdraw max:330`

Only the user in `DISCORD_MENTION_USER_ID` can run them; everyone else is refused,
and replies are visible only to you. Names are checked against BloxSwaps, and claim
mode still refuses an entry without a price limit.

Setup (one time):

1. https://discord.com/developers/applications → **New Application** → **Bot** →
   **Reset Token**, copy it into `DISCORD_BOT_TOKEN`. No privileged intents are needed.
2. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`, no permissions.
   Open the URL and add the bot to your server.
3. Set `DISCORD_MENTION_USER_ID` to your own user ID. Optionally set `DISCORD_GUILD_ID`
   (your server's ID) so the commands appear instantly instead of after a few minutes.

On the first start with the bot, the items from `WATCH` / `config.toml` are copied
into the bot's list once. After that the Discord list is what counts, and `WATCH`
is ignored. The list lives in `DATA_DIR`, so on Railway it needs the volume.

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

### One click (no GitHub account needed)

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/W1VcLW?referralCode=YMdSJg&utm_medium=integration&utm_source=template&utm_campaign=generic)

1. Click the button and sign in to Railway (email works).
2. Fill in the variables it asks for (see "Getting your token" and the Discord setup above).
3. Deploy. Then pick items with `/watch add` in Discord.

It runs the published image `ghcr.io/reverseddata/auto-swapper:latest`, with a volume
on `/app/data` already attached. To update, redeploy the service in Railway.

### By hand

1. In Railway: **New Project → Empty Project → New → Docker Image**, and enter
   `ghcr.io/reverseddata/auto-swapper:latest`. (Or **Deploy from GitHub repo** on your
   own fork; the `Dockerfile` is used automatically.) No port or domain is needed; it
   is a worker.
2. **Variables**: add `MODE`, `DISCORD_WEBHOOK_URL`, `BLOXSWAPS_TOKEN` (claim mode),
   `DISCORD_MENTION_USER_ID`, and either `DISCORD_BOT_TOKEN` (manage items with
   `/watch`, recommended) or `WATCH`. Optional: `MAX_CLAIM_PRICE`, `MAX_DAILY_SPEND`,
   `DISCORD_GUILD_ID`. Paste tokens without quotes in Railway.
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

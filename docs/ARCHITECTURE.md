# Architecture

Written 2026-10-17. Marketplace behaviour changes, so re-check "Verified
interfaces" before relying on it.

## What it does

Watch BloxSwaps. When a watched item becomes available (the bots restock it, or
a deposit slot opens), claim it on your account and post a Discord message:
*"X claimed, join (server link) as (username)"*. Then stop polling until the
trade is finished (the site reports it, or a timeout passes), and carry on.
A person does the in-game trade. This bot never touches Roblox.

```
 poll /items ──▶ normalize ──▶ seen-store diff ──▶ trigger? ──▶ watchlist match
                                                                     │
          ┌──────────────── monitor mode: Discord "available" ◀──────┤
          │                                                          ▼
          │                         claim mode: reserve ─▶ revalidate ─▶ claim
          │                                                          │
          │      CLAIMED: Discord "claimed, join … as …", HOLD ◀─────┘
          │                │
          │   HOLD: no polling; check claim status / timeout ─▶ resume
```

| Module | Job |
|---|---|
| `marketplaces/bloxswaps.py` | URLs, payload parsing, claim/status calls. Nothing else. |
| `store.py` | SQLite: seen listings (dedupe), claims ledger, the hold. |
| `claim.py` | Reserve → revalidate → claim → hold. Reconciles unclear outcomes. |
| `service.py` | Poll loop, triggers, backoff, outage alerts, status file. |
| `notify.py` | Discord webhook queue: embeds, batching, cooldown, 429s, redaction. |
| `http.py` | aiohttp wrapper: timeouts, error classes, per-host spacing. |
| `config.py` | `.env` + `config.toml`, validated at startup. |

## Triggers

Deposit slots are open almost all the time, so "is available" is not a signal.
A **transition** is: stock `0 → n` (withdraw) or free slots `0 → n` (deposit).
A withdraw price drop while in stock also re-evaluates, because it may newly fit
`max_price`. First sightings at startup are only acted on with `ACT_ON_FIRST_SIGHT=true`.

## Claim safety

* One claim at a time. While a claim is held, nothing is polled or claimed.
* The hold lives in SQLite, so a restart can't resume and claim again.
* Reservation (idempotency key + daily cap) is one IMMEDIATE transaction
  committed **before** the request is sent.
* Fresh state is fetched right before claiming (still available, same item and side, price OK).
* A timeout or unclear response is **UNKNOWN**. The bot asks the site for the
  claim status. If that's still unclear, it holds and alerts instead of retrying.
* Only a request that provably never left the machine (connect failure) is retried.
* A `PENDING` row found at startup (crash mid-claim) becomes UNKNOWN and holds.

## Not done on purpose

No browser impersonation, no client-IP rotation, no 2-second polling, and no
desktop automation of the in-game trade. A person does the trade.

## Verified interfaces

* `GET https://api.bloxswaps.com/items` (public): `{success, data:[…]}`, ~958 items,
  ~460 KB, not compressed, weak ETag that changes almost every request.
  Prices (`currentPrices.sellPrice` / `buyPrice`) are decimal strings in BloxSwaps value.
  `appAvailableQuantity` = bot stock; `userAvailableQuantity` = free deposit slots.
* Session: the access cookie (`bloxswaps.at`) lasts about 15 min. On a 401 the
  adapter calls `POST /auth/refresh` once, retries once, and saves the rotated
  cookies to `DATA_DIR/bloxswaps_session.json`. A new `BLOXSWAPS_TOKEN` in `.env`
  replaces the saved session.
* Claim: `POST /trades` with `{"items":[{"itemDetailsId","quantity":1,"tradeRole"}]}`,
  `APP` for withdraw and `USER` for deposit. Claim status: `GET /trades/{id}`, or
  `GET /trades/active` for reconcile. Found in the public site bundle on 2026-10-18.
  Not yet confirmed against a live account. The first real claim is the test.

# Railway template: what to enter

For the maintainer: create the template once at https://railway.com/compose (or
Workspace → Templates → New Template), then replace `TEMPLATE_CODE` in the README
button with the code Railway gives you.

## Service

| Field | Value |
|---|---|
| Source | Docker image `ghcr.io/reverseddata/auto-swapper:latest` |
| Name | `auto-swapper` |
| Start command | leave empty (the image runs `mm2sniper run`) |
| Networking | none (no public domain, no port) |
| Replicas | 1 |
| Restart policy | On failure |

## Volume

| Field | Value |
|---|---|
| Mount path | `/app/data` |

## Variables

| Name | Default | Required | Description shown to users |
|---|---|---|---|
| `MODE` | `monitor` | yes | `monitor` = only ping Discord. `claim` = claim on your BloxSwaps account. Start with monitor. |
| `MARKETS` | `bloxswaps` | no | bloxswaps, starpets or both. StarPets is alerts only. Switch later with /market. |
| `DISCORD_WEBHOOK_URL` | | yes | Discord channel webhook for alerts (Channel settings → Integrations → Webhooks). |
| `DISCORD_MENTION_USER_ID` | | yes | Your Discord user ID. Pinged on claims, and the only user allowed to use the commands. |
| `DISCORD_BOT_TOKEN` | | yes | Bot token from discord.com/developers, for the /watch commands. |
| `DISCORD_GUILD_ID` | | no | Your server ID, so the commands appear instantly. |
| `BLOXSWAPS_TOKEN` | | no (yes for claim mode) | Your BloxSwaps Cookie header (see README "Getting your token"). Secret. |
| `MAX_CLAIM_PRICE` | | no | Largest single withdraw, in BloxSwaps value. |
| `MAX_DAILY_SPEND` | | no | Withdraw spending cap per UTC day. |
| `RAILWAY_RUN_UID` | `0` | yes (keep) | Lets the app write to the Railway volume. Don't change. |

Do not put any of your own values in the template. Every user enters their own.

## Notes

- The image package must be **public**. On GitHub: your profile → Packages →
  `auto-swapper` → Package settings → Change visibility → Public. GHCR packages start
  out private, and Railway can't pull a private image without credentials.
- A new image is published on every push to `main` (`latest`) and for `v*` tags.
  For predictable updates, point the template at a version tag such as `v0.2.0`.

# Token Tracker

Local, zero-dependency (Python 3 stdlib only) watchlist + PnL dashboard for
DEX/meme tokens across several EVM networks, with real-time movement alerts to
Telegram and desktop sounds.

```
┌──────────────┐   polls     ┌───────────────────────┐
│  server.py   │ ──────────► │ DexScreener (quotes)  │
│  stdlib HTTP │ ──────────► │ Blockscout / RPC      │
│  :8765       │             │ (transfers, balances) │
└──────┬───────┘             └───────────────────────┘
       │ pushes
       ▼
 dashboard.html  ◄── browser  •  Telegram bot  •  WAV synth alerts
```

## Features

- **Movement detection** — multiple rolling windows (1m/5m/15m/1h/24h) with
  adaptive, volatility-scaled thresholds; alert cooldown & anti-spam dedupe.
- **Multi-chain** — Robinhood Chain, Base, Ethereum, Arbitrum, Optimism
  (ETH-native chains only; junk/declining chains deliberately excluded).
- **Multi-wallet, per-wallet scoping** — every wallet keeps its own watchlist;
  balances and PnL never mix between wallets.
- **Auto-tracking** — a background rescan detects fresh purchases (new
  positions / qty grew) and adds them automatically, with junk filters
  (position ≥ $1, pool liquidity ≥ $1K). Auto-added tokens whose position dies
  (< $0.10 or sold) are auto-pruned; manually added ones are never removed.
- **Per-token PnL** — FIFO cost basis reconstructed from real swap legs on
  chain (transfer logs + routers), not from a snapshot price.
- **Portfolio view** — balance = positions + stable cash + native ETH per
  chain; ⛽ gas spent tracked separately; “PnL ex-gas”.
- **Cross-chain bridge netting** — OUT-to-contract on chain A paired with the
  matching free stable IN on chain B (±6%, ≤7d) is recognized as a bridge move,
  not a withdrawal, so per-chain `injected`/PnL stop producing phantom profits.
  (Per-chain PnL is shown where the accounting is trusted; transit chains stay
  honest with a dash — see `references` notes in code.)
- **Telegram alerts** — photo-rendered alert cards with token logo, retries,
  timeout dedupe; optional.
- **Sounds** — deterministically synthesized piano/marimba WAVs
  (`sound.py`), volume baked into file names, no system beeps.
- **i18n** — EN (default) / RU switch in settings.
- **Resilient caches** — stale-if-error snapshots (explorers go down a lot),
  LRU transfer caches, negative-quote TTLs so a timeout never blinds prices
  for hours.

## Quick start

Requires Python 3.10+ (stdlib only — nothing to pip-install).

```bash
python server.py            # then open http://127.0.0.1:8765
```

Windows convenience entry points:

- `python launcher.py start|stop|status|open` — control panel / daemon helper
- `Управление трекером.bat` / `Token Tracker.lnk` — GUI control panel
- `make_desktop_shortcut.vbs` — creates the start-menu shortcut

Add wallets and tokens in the UI (Settings → wallets: paste one per line).
The first wallet is your funding chain anchor; per-wallet watchlists auto-scan.

## Configuration

`data/config.json` (created on first run, editable from the UI):

| key | meaning |
|---|---|
| `pollSeconds` | price poll interval |
| `windows` | detection windows `{name, seconds, threshold%}` |
| `adaptive` | volatility-scaled thresholds (`enabled`, `k`, `lookbackMin`) |
| `wallets` | tracked wallet addresses |
| `chains` | enabled networks (see above) |
| `scanIntervalSec` | auto-detect fresh buys interval |
| `pnlIntervalSec` / `portfolioIntervalSec` | PnL refresh cadence |
| `telegram` | `botToken` + `chatId` (leave empty to disable) |
| `volume` / `soundEnabled` | alert sound synth |

**How to get the Telegram chat id:** message your bot once, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` — `result[].message.chat.id`
is it (a plain number).

`data/` (state, caches, logs, generated sounds, logos) is gitignored.

## API (localhost)

`/api/state`, `/api/ping`, `/api/config` (POST), `/api/tokens/add|remove`,
`/api/wallets/add|remove|scan|refresh`, `/api/test-telegram`,
`/api/test-alert`, `/api/shutdown`. The dashboard only talks to these.

## Tests

```bash
python test_detection.py    # window/adaptive detection semantics
python test_adaptive.py     # threshold adaptation
python test_pnl.py          # swap-leg parsing, FIFO, cache LRU
python test_portfolio.py    # cash-flow classifier (churn, rollback, routers)
python bench_strategies.py  # detection tuning on historical noise
```

## Architecture notes

- `server.py` — stdlib HTTP server + poller/scan/portfolio background loops;
  snapshot persistence with RLock discipline (a plain Lock dead-locked nested
  saves).
- `detector.py` — multi-window movement detection, adaptive k-σ gating.
- `pnl.py` — per-token FIFO PnL from chain transfers (Blockscout v2 API + RPC
  fallbacks), DexScreener quote batching (25/token requests), negative-quote
  cache TTLs, symbol-collision guard (positions are priced **strictly by
  contract address**, never by ticker match — a spam drop named “KING” must not
  inherit the real KING's price).
- `portfolio.py` — wallet cash-flow identity `PnL = value − injected`, EOA vs
  contract classification, 2-tx swap leg pairing, router/airdrop exclusion,
  cross-chain bridge netting (`net_bridges`), per-chain breakdown.
- `telegram.py` — Telegram Bot API with connection-abort retries and dedupe.
- `sound.py` — deterministic WAV synthesis (harmonics + tanh limiter).
- `dashboard.html` — single-file UI, localStorage state, EN/RU dictionaries,
  optimistic wallet switching (render now, refresh the slice in background).

## Safety

- No keys, seeds, or credentials are ever read or stored. The Telegram bot
  token lives in the local gitignored config.
- All chain data is public read-only API calls; the app never signs or sends
  transactions.
- Wallet addresses are only used to read public explorer data — treat the
  dashboard as read-only visibility into your own addresses.

## License

MIT

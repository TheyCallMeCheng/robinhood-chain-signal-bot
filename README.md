# robinhood-chain-signal-bot

Tracks selected new pools on **Robinhood Chain mainnet** (chain ID `4663`) and
posts launch alerts to Telegram.

This is an isolated clone of the HyperEVM bot. It does not share code, state,
configuration, or a systemd unit with the running HyperEVM instance.

## Coverage

Discovery is on-chain and uses the public Robinhood RPC as its source of truth:

- Pons v2 factory `TokenLaunched` events (the emitted bonding-curve address)
- Uniswap v2 factory `PairCreated` events
- Uniswap v3 factory `PoolCreated` events
- Uniswap v4 PoolManager `Initialize` events (the emitted PoolId), restricted
  to transactions that also mint one of the currencies, so ordinary new fee
  tiers for old tokens are not mislabeled as launches

A durable SQLite block cursor is advanced only after all four log queries and
database writes succeed, so restarts and transient RPC failures do not create
gaps. Three tip blocks are held back for reorg safety.

Pons metrics are reconstructed from on-chain `CurveBuy`/`CurveSell` logs and
live curve reserves. For non-Pons V2/V3/V4 pools, the bot asks DexScreener's
separate batch API for USD market metrics only after the pool was discovered
on-chain. GeckoTerminal is not called by the running Robinhood service, so it
cannot consume the API budget used by the original HyperEVM bot.

The inherited v3 raw Swap scanner and sell simulation remain disabled because
they do not understand mixed pool types. Pons has its own dedicated on-chain
adapter; V2/V3/V4 trade enrichment remains external.

## Configuration

Copy the example and fill the Telegram values:

```bash
cp .env.example .env
python3 run.py --verify
```

You may reuse the existing Telegram bot token if that bot is added as an admin
of the new channel with permission to post. Only `CHAT_ID` then needs to be
different. Use a new BotFather token only if you want a separate bot identity.

Important defaults:

| Variable | Default |
|---|---|
| `NETWORK` | `robinhood` |
| `CHAIN_ID` | `4663` |
| `RPC_URL` | `https://rpc.mainnet.chain.robinhood.com` |
| `POLL_SECONDS` | `60` |
| `ENABLE_CHAIN_WATCH` | `true` |
| `ENABLE_GECKO_DISCOVERY` | `false` |
| `CHAIN_INITIAL_LOOKBACK` | `180` blocks |
| `ENRICH_BATCH_SIZE` | `30` |
| `NEW_LAUNCH_MIN_LIQ` | `15000` |
| `NEW_LAUNCH_MAX_AGE_H` | `6` |
| `SCORE_THRESHOLD` | `6.0` |

The score and thresholds are conservative carry-overs from the HyperEVM bot.
They have **not** yet been calibrated on Robinhood Chain outcomes. Treat this
as an attention feed, not a buy signal.

## Commands

```bash
python3 run.py --verify          # RPC, token, channel, and test message
python3 run.py --backtest        # live ranking; no Telegram and no state
python3 run.py --dry-run --once  # one full cycle; writes only the clone DB
python3 run.py                   # run continuously
python3 legend.py --print        # preview the channel legend
python3 legend.py                # post the legend
```

Python 3.8+ is sufficient; the project uses only the standard library.

## Deployment on `baby`

Code lives at `/home/cheng/robinhood-chain-signal-bot`. The unit file is
`robinhood-chain-signal-bot.service`; install it only after `.env` passes
`--verify`:

```bash
mkdir -p ~/.config/systemd/user
cp robinhood-chain-signal-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now robinhood-chain-signal-bot.service
```

The separate state database is
`/home/cheng/robinhood-chain-signal-bot/signals.db`.

## Verified network constants

- WETH: `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73`
- Pons v2 factory: `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e`
- Uniswap V2 factory: `0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f`
- Uniswap V3 factory: `0x1f7d7550B1b028f7571E69A784071F0205FD2EfA`
- Uniswap V4 PoolManager: `0x8366a39cc670b4001a1121b8f6a443a643e40951`
- Uniswap SwapRouter02: `0xCaf681a66D020601342297493863E78C959E5cb2`
- Explorer: `https://robinhoodchain.blockscout.com`

All constants remain environment-overridable.

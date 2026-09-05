#!/usr/bin/env python3
"""Post the signal legend to the channel so it can be pinned.

    python3 legend.py --print    show it without sending
    python3 legend.py            send it to CHAT_ID
"""
import sys

from hlbot import tg

LEGEND = """<b>\U0001F4CC ROBINHOOD CHAIN SIGNAL LEGEND</b>

<b>WHAT THIS BOT DOES</b>
Watches the newest Robinhood Chain pools across the DEXes indexed by GeckoTerminal and alerts on selected launches.
It detects a move <i>already in progress</i> and tries to catch it early rather than late. It does <b>not</b> predict the future.

<b>ALERT TYPES</b>
\U0001F195 <b>NEW LAUNCH</b> - pool under 6h old that cleared $15k liquidity
Momentum and reawakening alerts are disabled while this bot shares the public
GeckoTerminal quota with the HyperEVM service.

<b>THE NUMBERS</b>

<b>velocity</b> - vol_1h / liquidity. A <u>ratio</u>, not a mark out of 10.
<code>1.00</code> = the pool's whole liquidity traded once in an hour

<b>accel</b> - 5m run-rate vs the 1h average
<code>&gt;1.0</code> speeding up | <code>~1.0</code> steady | <code>&lt;1.0</code> fading

<b>zvol</b> - volume vs this pool's <i>own</i> normal hour
<code>1.0</code> normal | <code>4+</code> triggers a reawakening

<b>breadth</b> - unique buyers / buy txns (0 to 1)
<code>1.00</code> every buy a different wallet | <code>&lt;0.35</code> concentrated, maybe one actor or a bot

<b>flow</b> - (buyers - sellers) / (buyers + sellers), -1 to +1
Positive = more distinct buyers than sellers

<b>dir</b> - direction multiplier (0 to 1)
<code>0.5</code> flat | <code>&gt;0.7</code> rising | <code>&lt;0.3</code> falling
Nothing alerts unless price is <b>up 3%+ on the hour</b>

<b>SCORE</b> - velocity/zvol/accel, amplified by breadth and flow, multiplied by direction. Open-ended, no cap.
<code>&lt;6</code> not sent
<code>6-9</code> fires
<code>9-12</code> strong
<code>12+</code> rare
Threshold 6 is inherited from the HyperEVM bot and is not yet calibrated for Robinhood Chain.

<b>\U000026A0 WARNING FLAGS</b>
<code>mcap/liq Nx</code> thin float - EGG ran at 24x
<code>net sellers</code> more sellers than buyers
<code>decelerating</code> accel below 0.5, move is fading
<code>low breadth</code> under 0.35, very few wallets

<b>WHAT IT DOES NOT CHECK YET</b>
Robinhood uses V2, V3, V4 and custom AMMs. Protocol-specific honeypot, LP lock,
sell-tax, whale, accumulation and deployer checks are disabled until their
decoders are implemented and verified.
<b>A pool can score well and still be untradeable. Check the contract before buying.</b>

<b>CALLS</b>
<code>calls #4 | first 7.0h ago at $0.00041 (+93% since) | 2x momentum</code>
How many times this bot has alerted on this token, when it first did, and what
the price has done since. A high number with a negative % means the bot has
been repeatedly wrong on it - treat that as a warning, not a confirmation.

<b>NAME</b>
The token's real ERC20 name, shown when it differs from the ticker.
Ticker <code>HPOS</code> tells you nothing; name <code>HarryPotterObamaSonic10Inu</code> tells you
it is copying a famous coin. Shown right under the score.
<code>ticker shared with N</code> / <code>name shared with N</code> = N other tokens
we have indexed use the same identity. Collisions are extremely common here.

<b>LINKS</b>
<code>now</code> is what DexScreener shows today. Teams may submit this AFTER getting
traction, so treat it as a sign of attention, not a prediction.

<b>DATA LIMITS</b>
The public indexer is cached and rate-limited, so this is not a block-zero feed.
No Robinhood-specific outcome backtest exists yet.
<b>This is an attention detector, not financial advice or a buy signal.</b>"""


LIMIT = 3800          # Telegram caps a message at 4096; leave headroom


def split_parts(text: str, limit: int = LIMIT):
    """Break on blank lines so a section never splits mid-explanation."""
    blocks = text.split("\n\n")
    parts, cur = [], ""
    for b in blocks:
        candidate = (cur + "\n\n" + b) if cur else b
        if len(candidate) > limit and cur:
            parts.append(cur)
            cur = b
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    return parts


def main() -> int:
    parts = split_parts(LEGEND)
    if "--print" in sys.argv:
        for i, part in enumerate(parts, 1):
            print("----- part %d/%d (%d chars) -----" % (i, len(parts), len(part)))
            print(part)
        return 0
    for i, part in enumerate(parts, 1):
        body = part if len(parts) == 1 else (
            part + "\n\n<i>legend %d/%d</i>" % (i, len(parts)))
        if not tg.send(body):
            print("failed to send part %d/%d" % (i, len(parts)))
            return 1
        print("sent part %d/%d (%d chars)" % (i, len(parts), len(body)))
    print("pin all %d parts in the channel" % len(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())

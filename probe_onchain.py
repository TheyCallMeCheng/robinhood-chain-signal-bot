#!/usr/bin/env python3
"""Print decoded recent creations from all configured on-chain sources."""
import collections
import sys

from hlbot.chain import block_number, poll_discoveries


def main():
    lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    head = block_number()
    if head is None:
        raise SystemExit("RPC unavailable")
    rows = poll_discoveries(max(0, head - lookback), head)
    print("head", head, "lookback", lookback, "discoveries", len(rows))
    print("by protocol", dict(collections.Counter(r["protocol"] for r in rows)))
    for row in rows[-20:]:
        print(row)


if __name__ == "__main__":
    main()

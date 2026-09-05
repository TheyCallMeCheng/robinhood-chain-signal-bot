"""Pure-Python Keccak-256 (Ethereum flavour, not NIST SHA3).

Needed to compute ERC20 storage slots for eth_call state overrides. No third
party package is installed and the bot is meant to stay dependency-free, so it
lives here. Verified against known vectors in self_test().
"""

_MASK = (1 << 64) - 1

_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]

_ROT = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]


def _rol(v, n):
    n %= 64
    return ((v << n) | (v >> (64 - n))) & _MASK


def _keccak_f(A):
    for rnd in range(24):
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                A[x][y] ^= D[x]

        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])

        for x in range(5):
            for y in range(5):
                A[x][y] = B[x][y] ^ ((~B[(x + 1) % 5][y] & _MASK) & B[(x + 2) % 5][y])

        A[0][0] ^= _RC[rnd]
    return A


def keccak256(data: bytes) -> bytes:
    rate = 136                       # 1088 bits for Keccak-256
    A = [[0] * 5 for _ in range(5)]

    padded = bytearray(data)
    padded.append(0x01)              # Keccak padding, not SHA3's 0x06
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            A[i % 5][i // 5] ^= lane
        _keccak_f(A)

    out = bytearray()
    for i in range(4):               # 32 bytes
        out += A[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)


def keccak_hex(data: bytes) -> str:
    return "0x" + keccak256(data).hex()


def self_test() -> bool:
    vectors = [
        (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
        (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
        (b"testing", "5f16f4c7f149ac4f9510d9cf8cf384038ad348b3bcdc01915f95de12df9d1b02"),
    ]
    for data, want in vectors:
        got = keccak256(data).hex()
        if got != want:
            print("FAIL %r\n  got  %s\n  want %s" % (data, got, want))
            return False
    return True


if __name__ == "__main__":
    print("keccak256 self-test:", "PASS" if self_test() else "FAIL")

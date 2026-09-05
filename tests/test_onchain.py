import tempfile
import unittest
from pathlib import Path

from hlbot import config
from hlbot.chain import _decode_pons, _decode_v2, _decode_v3, _decode_v4
from hlbot.engine import fomo_token_url
from hlbot.keccak import keccak256
from hlbot.pons import BUY_TOPIC, GET_RESERVES, REAL_QUOTE_RESERVE, SELL_TOPIC
from hlbot.store import Store


def topic_address(address):
    return "0x" + "0" * 24 + address[2:].lower()


def word_address(address):
    return "0" * 24 + address[2:].lower()


class DecoderTests(unittest.TestCase):
    token0 = "0x1111111111111111111111111111111111111111"
    token1 = "0x2222222222222222222222222222222222222222"
    pool = "0x3333333333333333333333333333333333333333"
    creator = "0x4444444444444444444444444444444444444444"

    def test_fomo_token_url_includes_robinhood_referral(self):
        self.assertEqual(
            fomo_token_url(self.token0),
            "https://fomo.family/tokens/robinhood/{}?r=WarmFineHare".format(
                self.token0))

    def base_log(self):
        return {"blockNumber": "0x64", "logIndex": "0x2", "transactionHash": "0xabc"}

    def test_event_topics_are_canonical(self):
        signatures = {
            config.PONS_TOKEN_LAUNCHED_TOPIC:
                "TokenLaunched(address,address,address,address,uint256,uint256)",
            config.V2_PAIR_CREATED_TOPIC:
                "PairCreated(address,address,address,uint256)",
            config.V3_POOL_CREATED_TOPIC:
                "PoolCreated(address,address,uint24,int24,address)",
            config.V4_INITIALIZE_TOPIC:
                "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)",
        }
        for expected, signature in signatures.items():
            self.assertEqual(expected, "0x" + keccak256(signature.encode()).hex())
        self.assertEqual(BUY_TOPIC, "0x" + keccak256(
            b"CurveBuy(address,address,uint256,uint256,uint256,uint256)").hex())
        self.assertEqual(SELL_TOPIC, "0x" + keccak256(
            b"CurveSell(address,address,uint256,uint256,uint256,uint256)").hex())
        self.assertEqual(GET_RESERVES, "0x" + keccak256(b"getReserves()").hex()[:8])
        self.assertEqual(REAL_QUOTE_RESERVE,
                         "0x" + keccak256(b"realQuoteReserve()").hex()[:8])

    def test_pons_token_launched(self):
        log = self.base_log()
        log.update({
            "topics": [config.PONS_TOKEN_LAUNCHED_TOPIC,
                       topic_address(self.token0), topic_address(self.pool),
                       topic_address(self.creator)],
            "data": "0x" + word_address(config.ZERO_ADDRESS) + "%064x" % 1 + "%064x" % 2,
        })
        row = _decode_pons(log)
        self.assertEqual((row["pool"], row["base_token"], row["creator"]),
                         (self.pool, self.token0, self.creator))

    def test_uniswap_versions(self):
        v2 = self.base_log()
        v2.update({
            "topics": [config.V2_PAIR_CREATED_TOPIC,
                       topic_address(self.token0), topic_address(config.WRAPPED_NATIVE)],
            "data": "0x" + word_address(self.pool) + "%064x" % 7,
        })
        self.assertEqual(_decode_v2(v2)["pool"], self.pool)
        self.assertEqual(_decode_v2(v2)["base_token"], self.token0)

        v3 = self.base_log()
        v3.update({
            "topics": [config.V3_POOL_CREATED_TOPIC,
                       topic_address(self.token0), topic_address(config.WRAPPED_NATIVE),
                       "0x" + "%064x" % 3000],
            "data": "0x" + "%064x" % 60 + word_address(self.pool),
        })
        self.assertEqual(_decode_v3(v3)["pool"], self.pool)

        pool_id = "0x" + "55" * 32
        v4 = self.base_log()
        v4.update({
            "topics": [config.V4_INITIALIZE_TOPIC, pool_id,
                       topic_address(config.ZERO_ADDRESS), topic_address(self.token1)],
            "data": "0x" + "%064x" % 10000 + "%064x" % 200
                    + word_address(self.creator) + "%064x" % 1 + "%064x" % 0,
        })
        row = _decode_v4(v4)
        self.assertEqual(row["pool"], pool_id)
        self.assertEqual(row["base_token"], self.token1)


class DiscoveryStoreTests(unittest.TestCase):
    def test_queue_is_durable_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.db")
            store = Store(path)
            row = {
                "pool": "0x" + "aa" * 20, "protocol": "pons-v2",
                "token0": "0x" + "bb" * 20, "token1": config.ZERO_ADDRESS,
                "base_token": "0x" + "bb" * 20, "block": 123,
                "tx": "0x1", "creator": "0x" + "cc" * 20,
            }
            self.assertTrue(store.put_discovery(row))
            self.assertFalse(store.put_discovery(row))
            queued = store.pending_discoveries(60, 6)
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0]["protocol"], "pons-v2")
            store.set_state("discovery:last_block", 123)
            store.close()

            reopened = Store(path)
            self.assertEqual(reopened.get_state("discovery:last_block"), "123")
            self.assertEqual(len(reopened.pending_discoveries(60, 6)), 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()

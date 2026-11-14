"""wallet.py faucet / balance take an address too: a wallet held elsewhere (a browser page)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import server  # noqa: E402
import wallet  # noqa: E402
import wallets  # noqa: E402
from test_e2e import free_port  # noqa: E402


class WalletCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._wd, self._api = wallets.WALLET_DIR, wallet.API
        wallets.WALLET_DIR = os.path.join(self.tmp.name, "wallets")
        self.chain = ch.Chain.open_or_archive(os.path.join(self.tmp.name, "chain.json"))
        port = free_port()
        self.api = server.start(self.chain, port=port)
        wallet.API = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.api.settler_stop.set()
        self.api.shutdown()
        self.api.server_close()
        wallets.WALLET_DIR, wallet.API = self._wd, self._api
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wallet.main(list(argv)), 0)
        return json.loads([ln for ln in out.getvalue().splitlines() if ln.startswith("{")][-1])  # the node logs here too

    def test_faucet_and_balance_by_address(self):
        addr = "7e" * 32
        self.assertEqual(self.run_cli("faucet", addr), {"address": addr, "balance": 100, "currency": "PMX"})
        self.assertEqual(self.run_cli("balance", addr)["balance"], 100)
        self.assertEqual(set(os.listdir(wallets.WALLET_DIR)), {"hub.wallet.json", "seller.wallet.json"})  # the node's only
        with self.assertRaises(SystemExit):  # a name still means a local wallet, and it must exist
            wallet.main(["faucet", "nobody"])


if __name__ == "__main__":
    unittest.main()

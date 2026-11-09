import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_probe  # noqa: E402


class ServerProbe(unittest.TestCase):
    def run_probe(self, replies, state=None):
        """replies: {cmd: (ok, out)}. Returns (exit code, sent messages, saved state, commands asked)."""
        sent, asked = [], []
        with tempfile.TemporaryDirectory() as d:
            st = Path(d) / "server_probe.json"
            if state:
                st.write_text(json.dumps(state))

            def ask(cmd, cfg):
                asked.append(cmd)
                return replies[cmd]
            with mock.patch.object(server_probe, "STATE", st), mock.patch.object(server_probe, "ask", ask), \
                    mock.patch.object(server_probe, "send", lambda text, private=False: sent.append((text, private))):
                code = server_probe.main()
            return code, sent, json.loads(st.read_text()) if st.exists() else None, asked

    def test_scanning_is_silent(self):
        code, sent, st, asked = self.run_probe({"status": (True, "active 5 41 3600")})
        self.assertEqual((code, sent, asked), (0, [], ["status"]))

    def test_just_started_is_not_restarted(self):
        code, sent, st, asked = self.run_probe({"status": (True, "active 0 0 90")})
        self.assertEqual((code, sent, asked), (0, [], ["status"]))

    def test_stalled_is_restarted_once(self):
        r = {"status": (True, "active 0 0 4000"), "restart": (True, "restarted")}
        code, sent, st, asked = self.run_probe(r)
        self.assertEqual((code, asked, st["open"]), (0, ["status", "restart"], "stalled"))
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0][1])  # status group only
        _, sent2, _, _ = self.run_probe(r, st)
        self.assertEqual(sent2, [])  # open/clear only

    def test_unreachable_twice_tells_gideon_and_fails(self):
        r = {"status": (False, "ssh timed out")}
        code, sent, st, _ = self.run_probe(r)
        self.assertEqual((code, sent), (0, []))  # one miss is not an outage
        code, sent, st, _ = self.run_probe(r, st)
        self.assertEqual(code, 1)
        self.assertTrue(sent[0][1])  # private chat too
        self.assertNotIn("—", sent[0][0])
        code, sent, st, _ = self.run_probe({"status": (True, "active 3 41 100")}, st)
        self.assertEqual(code, 0)
        self.assertIn("scanning again", sent[0][0])

    def test_no_key_is_a_no_op(self):
        with mock.patch.dict("os.environ", {"SERVER_PROBE_KEY": ""}):
            self.assertEqual(server_probe.ask("status", {})[0], None)


if __name__ == "__main__":
    unittest.main()

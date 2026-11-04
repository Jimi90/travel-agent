"""Währungskurse: Auslesen, Zwischenspeicher und Rückfall auf gemerkte Kurse (mit nachgebildeter Antwort, ohne Netz)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import config, fx, store


def reply(rates=None, result="success"):
    body = {"result": result, "base_code": "CHF", "time_last_update_unix": 1793750400,
            "rates": rates if rates is not None else {"CHF": 1, "EUR": 1.07, "THB": 40.2, "VND": 29500,
                                                      **{f"X{chr(65 + i)}{chr(65 + j)}": 2.0 for i in range(5) for j in range(5)}}}
    m = mock.MagicMock()
    m.read.return_value = json.dumps(body).encode()
    m.__enter__.return_value = m
    return m


class FxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(config, "FAKE_MODE", False), mock.patch.object(config, "DATA_DIR", self.tmp),
                        mock.patch.object(config, "DB_PATH", self.tmp / "kompass.db")]
        for p in self.patches:
            p.start()
        store.reset_connection()
        fx.reset_for_tests()

    def tearDown(self):
        store.reset_connection()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fetch_and_cache(self):
        with mock.patch("urllib.request.urlopen", return_value=reply()) as u:
            a = fx.get()
            b = fx.get()
        self.assertEqual(u.call_count, 1)  # zweiter Aufruf aus dem Speicher
        self.assertFalse(a["stale"] or b["stale"])
        self.assertEqual((a["base"], a["rates"]["VND"], a["rates"]["THB"]), ("CHF", 29500.0, 40.2))
        self.assertIn("exchangerate-api.com", a["attribution_url"])

    def test_fallback_to_saved_when_offline(self):
        with mock.patch("urllib.request.urlopen", return_value=reply()):
            fx.get()
        fx.reset_for_tests()  # Neustart: Speicher leer, Datenbank bleibt
        with mock.patch("urllib.request.urlopen", side_effect=OSError("kein Netz")):
            out = fx.get()
        self.assertTrue(out["stale"])
        self.assertEqual(out["rates"]["EUR"], 1.07)

    def test_error_without_saved_rates(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("kein Netz")):
            with self.assertRaises(fx.FxError):
                fx.get()

    def test_rejects_bad_answers(self):
        for bad in (reply(result="error"), reply(rates={"CHF": 1, "EUR": 1.07}), reply(rates={"EUR": 1.07})):
            fx.reset_for_tests()
            with mock.patch("urllib.request.urlopen", return_value=bad):
                with self.assertRaises(fx.FxError):
                    fx.get()

    def test_valid_filters_nonsense(self):
        out = fx._valid({"EUR": 1.1, "usd": 1, "ABCD": 1, "GBP": -1, "JPY": 0, "THB": True, "NOK": "x", "SEK": 11})
        self.assertEqual(out, {"EUR": 1.1, "SEK": 11.0})


if __name__ == "__main__":
    unittest.main()

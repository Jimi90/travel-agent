"""Zielvorschläge: Wunsch James – Geheimtipps (nicht überlaufen, gut ausgebaut, im Kommen) statt Klassiker."""
import unittest
from datetime import date, timedelta
from unittest import mock

from app import config, store
from app.planning import destinations as D
from tests.test_replanning import household


class StyleTest(unittest.TestCase):
    def run_suggest(self, style):
        seen = {}

        def ask(purpose, **kw):
            seen["prompt"] = kw["content"]
            return D._fake(None)
        with mock.patch.object(D.store, "settings", return_value={"dest_style": style}), \
                mock.patch.object(D.llm, "ask", ask), mock.patch.object(config, "FAKE_MODE", True), \
                mock.patch.object(D.photos, "destination_photo", return_value=None):
            res = D.suggest(household(), date.today() + timedelta(days=30), 7, "", "ZRH")
        return res, seen["prompt"]

    def test_gems_are_asked_for_and_shown_first(self):
        res, prompt = self.run_suggest("geheimtipps")
        self.assertIn("Geheimtipps", prompt)
        self.assertIn("Keine Massenklassiker", prompt)
        self.assertEqual(res["style"], "geheimtipps")
        gems = [i["gem"] for i in res["ideas"] if not i.get("warning")]
        self.assertEqual(gems, sorted(gems, reverse=True))  # Geheimtipps zuerst
        self.assertTrue(any(i["why_now"] for i in res["ideas"] if i["gem"]))

    def test_classics_mode(self):
        res, prompt = self.run_suggest("klassiker")
        self.assertNotIn("Keine Massenklassiker", prompt)
        self.assertEqual(res["style"], "klassiker")

    def test_unknown_style_falls_back(self):
        with mock.patch.object(D.store, "settings", return_value={"dest_style": "quatsch"}):
            self.assertEqual(D.style(), "geheimtipps")

    def test_default_is_gems(self):
        self.assertEqual(store.DEFAULT_SETTINGS["dest_style"], "geheimtipps")


if __name__ == "__main__":
    unittest.main()

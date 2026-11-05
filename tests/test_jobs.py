"""Hintergrund-Aufträge: KI überlastet → Kompass versucht es selbst nochmals (James: Gemini 503 hielt länger an)."""
import time
import unittest
from unittest import mock

from app import llm, service


def wait_done(jid):
    for _ in range(200):
        j = service.job(jid)
        if j["status"] != "laeuft":
            return j
        time.sleep(0.01)
    raise AssertionError("Auftrag läuft noch")


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.notes = []
        orig = service._sleep

        def fake_sleep(sec):  # Hinweis für die Seite muss während der Pause sichtbar sein
            self.notes.append((sec, service.job(self.jid).get("note", "")))
        p = mock.patch.object(service, "_sleep", fake_sleep)
        p.start()
        self.addCleanup(p.stop)
        self.assertTrue(orig)

    def start(self, fn):
        with mock.patch.object(service, "new_id", return_value=f"job{id(fn)}"):
            self.jid = f"job{id(fn)}"
            return service._start_job("test", fn)

    def test_overload_retried_until_success(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise llm.LLMError("überlastet", temporary=True)
            return {"ok": True}
        j = wait_done(self.start(fn))
        self.assertEqual(j["status"], "fertig")
        self.assertEqual(len(calls), 3)
        self.assertEqual([n[0] for n in self.notes], [60, 120])
        self.assertIn("Versuch 2 von 6", self.notes[0][1])
        self.assertNotIn("note", j)  # nach Erfolg kein alter Hinweis

    def test_gives_up_after_all_waits(self):
        def fn():
            raise llm.LLMError("überlastet", temporary=True)
        j = wait_done(self.start(fn))
        self.assertEqual(j["status"], "fehler")
        self.assertEqual(len(self.notes), len(service.RETRY_WAITS))

    def test_other_errors_not_retried(self):
        def fn():
            raise llm.LLMError("Kein KI-Schlüssel hinterlegt")
        j = wait_done(self.start(fn))
        self.assertEqual((j["status"], self.notes), ("fehler", []))


if __name__ == "__main__":
    unittest.main()

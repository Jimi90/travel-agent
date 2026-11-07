"""Sehenswürdigkeiten im Tagesplan: TripAdvisor-Treffer prüfen, schwache ersetzen, Tipps (Wunsch James 1.15.0)."""
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from app import sights


def act(aid, title, cat="museum", wiki=None, fixed=False, booking=None):
    return NS(id=aid, title=title, category=cat, wiki=wiki, place=None, fixed=fixed, booking=booking)


class SubjectTest(unittest.TestCase):
    def test_only_named_sights(self):
        self.assertEqual(sights.subject(act("a", "Palast am Morgen", wiki="Wiedervereinigungspalast")),
                         "Wiedervereinigungspalast")
        self.assertEqual(sights.subject(act("a", "Insel", "natur", wiki="Isla Mujeres (Mexiko)")), "Isla Mujeres")
        # ohne Wikipedia-Titel keine Suche (deutsche Nomen sehen alle wie Eigennamen aus) – spart Kontingent
        self.assertIsNone(sights.subject(act("a", "Bootsfahrt zum Hafen", "wassersport")))
        self.assertIsNone(sights.subject(act("a", "Abendessen", "kulinarik", wiki="Tasca")))  # Essen: Google
        self.assertIsNone(sights.subject(act("a", "Ruhe", "entspannung", wiki="Strand")))


class ParseTest(unittest.TestCase):
    def test_pick_and_fields(self):
        data = {"places": [{"title": "War Remnants Museum"},
                           {"title": "Independence Palace (Reunification Palace)", "rating": "4.5", "reviews": "12,345",
                            "link": "https://www.tripadvisor.com/Attraction_Review-g293925-d311070",
                            "highlighted_review": {"text": "Go  early,\nqueues after 10."}}]}
        r = sights.pick(data, "Reunification Palace", "Ho-Chi-Minh-Stadt")
        e = sights._entry(r)
        self.assertEqual((e["rating"], e["reviews"]), (4.5, 12345))
        self.assertEqual(e["quote"], "Go early, queues after 10.")
        self.assertTrue(e["link"].startswith("https://www.tripadvisor.com/"))
        self.assertIsNone(sights._entry({"title": "X", "link": "https://evil.example/tripadvisor"})["link"])
        self.assertIsNone(sights.pick({"places": [{"title": "Ben Thanh Market"}]}, "Reunification Palace", "Saigon"))

    def test_weak_only_with_enough_reviews(self):
        self.assertTrue(sights.weak({"found": True, "rating": 3.5, "reviews": 400}))
        self.assertFalse(sights.weak({"found": True, "rating": 3.5, "reviews": 12}))
        self.assertFalse(sights.weak({"found": True, "rating": 4.5, "reviews": 400}))
        self.assertFalse(sights.weak({"found": False}))       # nicht gefunden = kein Urteil
        self.assertFalse(sights.weak(None))


class ImproveTest(unittest.TestCase):
    def trip(self, status="entwurf", **kw):
        acts = [act("a1", "Wachsmuseum", wiki="Wax Museum", **kw), act("a2", "Burg", wiki="Castelo de São Jorge")]
        return NS(status=status, days=[NS(activities=acts, base=NS(name="Lissabon"))])

    live = {"wax museum|lissabon": {"found": True, "title": "Wax Museum", "rating": 3.0, "reviews": 800},
            "castelo de são jorge|lissabon": {"found": True, "title": "Castelo de São Jorge", "rating": 4.5,
                                              "reviews": 25000}}
    alts = [{"title": "Castelo de São Jorge", "rating": 4.5, "reviews": 25000},  # schon im Plan
            {"title": "Torre de Belém", "rating": 4.5, "reviews": 30000, "description": "Turm am Tejo."}]

    def run_improvable(self, trip):
        with mock.patch.object(sights, "cached", lambda n, p: self.live.get(sights.key(n, p))), \
                mock.patch.object(sights, "alternatives", lambda p: self.alts):
            return sights.improvable(trip)

    def test_weak_replaced_by_best_not_in_plan(self):
        out = self.run_improvable(self.trip())
        self.assertEqual(list(out), ["a1"])
        self.assertEqual(out["a1"][1]["title"], "Torre de Belém")

    def test_never_fixed_booked_or_booked_trip(self):
        self.assertEqual(self.run_improvable(self.trip(fixed=True)), {})
        self.assertEqual(self.run_improvable(self.trip(booking=object())), {})
        self.assertEqual(self.run_improvable(self.trip(status="gebucht")), {})

    def test_swapped_keeps_time_and_category(self):
        from datetime import time
        from app.models import Activity
        a = Activity(title="Wachsmuseum", category="museum", exposure="indoor", start=time(14), end=time(16),
                     wiki="Wax Museum")
        n = sights.swapped(a, self.alts[1])
        self.assertEqual((n.title, n.start, n.end, str(n.category)), ("Torre de Belém", time(14), time(16), "museum"))
        self.assertIn("Turm am Tejo.", n.note)
        self.assertIn("★ 4.5 (30'000 Bewertungen)", n.note)
        self.assertEqual(sights.subject(n), "Torre de Belém")  # neuer Punkt bekommt gleich seine Bewertung


class TipFailTest(unittest.TestCase):
    def test_ai_error_waits(self):
        saved = {}
        trip = NS(id="t1", days=[NS(activities=[act("a1", "Burg", wiki="Burg Test")], base=NS(name="Ort"))])
        entry = {"found": True, "title": "Burg Test", "quote": "Go early."}
        with mock.patch.object(sights.store, "get", lambda ns, k: saved.get((ns, k)) or (entry if ns == "sight_live" else None)), \
                mock.patch.object(sights.store, "put", lambda ns, k, v: saved.__setitem__((ns, k), v)), \
                mock.patch.object(sights.llm, "ask", side_effect=sights.llm.LLMError("weg")) as ask:
            self.assertTrue(sights._tips_due(trip))
            self.assertEqual(sights.make_tips(trip), 0)
            self.assertFalse(sights._tips_due(trip))      # 6 Std. Pause statt Dauerschleife
            self.assertEqual(sights.make_tips(trip), 0)
            self.assertEqual(ask.call_count, 1)


if __name__ == "__main__":
    unittest.main()

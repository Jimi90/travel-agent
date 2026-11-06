"""Restaurants im Tagesplan: Name erkennen, Google-Treffer prüfen, Link bauen (Wunsch James)."""
import unittest
from datetime import time
from types import SimpleNamespace as NS

from unittest import mock

from app import venues


def act(title, cat="kulinarik", venue=None):
    return NS(title=title, category=cat, venue=venue)


class VenueTest(unittest.TestCase):
    def test_name_from_title_or_ai(self):
        self.assertEqual(venues.venue_of(act("Mittagessen im Riverside Restaurant")), "Riverside Restaurant")
        self.assertEqual(venues.venue_of(act("Abendessen bei Manda de Laos")), "Manda de Laos")
        self.assertEqual(venues.venue_of(act("Dinner", venue="Tamarind")), "Tamarind")

    def test_no_specific_place(self):
        for t in ("Frühstück im Hotel", "Abendessen 1", "Abendessen am Nachtmarkt", "Mittagessen", "Street Food am Markt"):
            self.assertIsNone(venues.venue_of(act(t)), t)
        self.assertIsNone(venues.venue_of(act("Besuch des Königspalast-Museums", cat="museum")))

    def test_pick_only_matching_result(self):
        data = {"local_results": [{"title": "Riverside Café"}, {"title": "Riverside Restaurant & Bar", "rating": 4.4}]}
        self.assertEqual(venues.pick(data, "Riverside Restaurant", "Luang Prabang")["rating"], 4.4)
        self.assertIsNone(venues.pick({"local_results": [{"title": "Le Banneton"}]}, "Tamarind", "Luang Prabang"))
        self.assertEqual(venues.pick({"place_results": {"title": "Tamarind", "rating": 4.6}}, "Tamarind", "x")["rating"], 4.6)

    def test_maps_link(self):
        u = venues.maps_url("Riverside Restaurant", "Luang Prabang", "abc")
        self.assertIn("query=Riverside+Restaurant%2C+Luang+Prabang", u)
        self.assertIn("query_place_id=abc", u)


class BetterTest(unittest.TestCase):
    """Wunsch James: «Es sollen schon gut bewertete Restaurants sein» – unter 4.5 ★ ein besseres vorschlagen."""

    def trip(self):
        acts = [NS(id="a1", title="Mittagessen im Peeping Som's Restaurant", category="kulinarik", venue=None, place=None),
                NS(id="a2", title="Abendessen im Organic Farm Restaurant", category="kulinarik", venue=None, place=None),
                NS(id="a3", title="Abendessen im Amigos", category="kulinarik", venue="Amigos", place=None)]
        return NS(days=[NS(activities=acts, base=NS(name="Vang Vieng"))])

    def test_weak_gets_better_alternative_not_used_twice(self):
        live = {"peeping som's restaurant|vang vieng": {"found": True, "rating": 4.1, "reviews": 939},
                "organic farm restaurant|vang vieng": {"found": True, "rating": 4.3, "reviews": 279},
                "amigos|vang vieng": {"found": True, "rating": 4.7, "reviews": 1200}}
        alts = [{"title": "Amigos", "rating": 4.7, "reviews": 1200},  # steht schon im Plan → nicht vorschlagen
                {"title": "The Sunset Bar", "rating": 4.8, "reviews": 900, "address": "Riverside"},
                {"title": "Viman Vang Vieng", "rating": 4.6, "reviews": 650}]
        with mock.patch.object(venues, "cached", lambda n, p: live.get(venues.key(n, p))), \
                mock.patch.object(venues, "alternatives", lambda p: alts):
            out = venues.for_trip(self.trip())
        self.assertTrue(out["a1"]["weak"] and out["a2"]["weak"])
        self.assertFalse(out["a3"]["weak"])
        self.assertEqual(out["a1"]["better"]["name"], "The Sunset Bar")
        self.assertEqual(out["a2"]["better"]["name"], "Viman Vang Vieng")  # nicht zweimal dasselbe
        self.assertIsNone(out["a3"]["better"])

    def test_good_needs_rating_and_reviews(self):
        self.assertTrue(venues.good({"found": True, "rating": 4.5, "reviews": 100}))
        self.assertFalse(venues.good({"found": True, "rating": 4.9, "reviews": 12}))  # zu wenig Bewertungen
        self.assertTrue(venues.weak({"found": False}))                                   # Google kennt es nicht
        self.assertFalse(venues.weak({"found": False, "error": "x"}))                    # Fehler ≠ schlecht

    def test_clean_foreign_script(self):
        self.assertEqual(venues.clean_name("ຮ້ານອາຫານຮ່ອນວັດແສນ1991 Wat Sene Alley Restaurant 1991"),
                         "Wat Sene Alley Restaurant 1991")
        self.assertEqual(venues.clean_name("Café Ñandú"), "Café Ñandú")
        self.assertEqual(venues.clean_name("ຮ້ານອາຫານ"), "ຮ້ານອາຫານ")  # nichts Lateinisches → so lassen

    def test_only_replaced_when_allowed(self):
        trip = self.trip()
        trip.status = "entwurf"
        for a in trip.days[0].activities:
            a.fixed, a.booking = False, None
        trip.days[0].activities[1].fixed = True                         # festgenagelt → nie anfassen
        live = {"peeping som's restaurant|vang vieng": {"found": True, "rating": 4.1, "reviews": 939},
                "organic farm restaurant|vang vieng": {"found": True, "rating": 4.3, "reviews": 279}}
        alts = [{"title": "The Sunset Bar", "rating": 4.8, "reviews": 900}, {"title": "Viman", "rating": 4.6, "reviews": 650}]
        with mock.patch.object(venues, "cached", lambda n, p: live.get(venues.key(n, p))), \
                mock.patch.object(venues, "alternatives", lambda p: alts):
            self.assertEqual(list(venues.improvable(trip)), ["a1"])
            trip.status = "gebucht"                                    # gebuchte Reise: nichts ungefragt ändern
            self.assertEqual(venues.improvable(trip), {})

    def test_swapped_activity(self):
        from datetime import time
        from app.models import Activity
        a = Activity(title="Mittagessen im Peeping Som's Restaurant", category="kulinarik", exposure="indoor",
                     start=time(12, 30), end=time(13, 30), note="Lokales Restaurant mit BBQ.")
        b = venues.swapped(a, {"name": "The Sunset Bar", "rating": 4.8, "reviews": 1234, "address": "Riverside"})
        self.assertEqual(b.title, "Mittagessen im The Sunset Bar")
        self.assertEqual(b.venue, "The Sunset Bar")
        self.assertIn("★ 4.8 (1'234 Google-Bewertungen)", b.note)
        self.assertEqual((b.id, b.start), (a.id, a.start))


if __name__ == "__main__":
    unittest.main()

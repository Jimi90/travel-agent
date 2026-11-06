"""Restaurants im Tagesplan: Name erkennen, Google-Treffer prüfen, Link bauen (Wunsch James)."""
import unittest
from datetime import time
from types import SimpleNamespace as NS

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


if __name__ == "__main__":
    unittest.main()

"""Tagesausflüge ab dem aktuellen Standort (Wunsch James 1.17.0): Reichweite, TripAdvisor, Wetter, Plan ohne Nacht."""
import unittest
from datetime import date, timedelta
from unittest import mock

from app import config, weather
from app.models import Place
from app.planning import daytrips as DT
from app.planning import generator
from tests.test_replanning import household

ZH = Place(name="Zürich", country="CH", lat=47.3769, lon=8.5417)


class SuggestTest(unittest.TestCase):
    def run_suggest(self, max_minutes=120, mode="egal", ta=None):
        seen = {"prompts": []}

        def ask(purpose, **kw):
            seen["prompt"], seen["purpose"] = kw["content"], purpose
            seen["prompts"].append(kw["content"])
            return kw["fake"]()

        def lookup(name, place, trip_id=None):
            return (ta or {}).get(name)
        with mock.patch.object(config, "FAKE_MODE", True), mock.patch.object(DT.llm, "ask", ask), \
                mock.patch.object(DT.sights, "lookup", lookup), mock.patch.object(DT.sights, "tips_for", lambda *a: True), \
                mock.patch.object(DT.sights, "cached", lambda n, p: (ta or {}).get(n)), \
                mock.patch.object(DT.sights, "nearby", return_value=[{"title": "Rheinfall", "rating": 4.6, "location": "Neuhausen"}]), \
                mock.patch.object(DT.photos, "destination_photo", return_value=None):
            res = DT.suggest(household(), ZH, date.today() + timedelta(days=2), max_minutes, mode, "See")
        return res, seen

    def test_start_is_current_location_and_tripadvisor_inspires(self):
        res, seen = self.run_suggest()
        self.assertIn("Startort (aktueller Standort): Zürich", seen["prompt"])
        self.assertIn("47.377, 8.542", seen["prompt"])
        self.assertIn("TripAdvisor-Lieblinge in der Umgebung: Rheinfall ★ 4.6", seen["prompt"])
        self.assertIn("höchstens 120 Minuten pro Weg", seen["prompt"])
        self.assertEqual(res["origin"]["name"], "Zürich")

    def test_too_far_and_same_place_dropped(self):
        res, _ = self.run_suggest(max_minutes=60)
        names = [i["name"] for i in res["ideas"]]
        self.assertNotIn("Mailand", names)          # 200 Min. / 219 km
        self.assertNotIn("Rigi", names)             # 75 Min. > 60 × 1.15
        self.assertNotIn("Altstadt Museum 2", names)  # am Startort
        self.assertTrue(all(i["km"] <= 60 / 60 * DT.MAX_KMH for i in res["ideas"]))
        self.assertTrue(any("zu weit" in d for d in res["dropped"]))

    def test_five_to_six_suggestions(self):
        # Wunsch James: 5–6 Vorschläge. KI fragt nach 9; bleiben weniger als 5, fragt Kompass einmal nach
        res, seen = self.run_suggest(max_minutes=60)
        self.assertIn("Schlage 9 unterschiedliche", seen["prompts"][0])
        self.assertEqual(len(seen["prompts"]), 2)
        self.assertIn("Bereits vorgeschlagen (nicht wiederholen): Luzern, Rheinfall", seen["prompts"][1])
        self.assertIn("Nicht gepasst: ", seen["prompts"][1])
        self.assertTrue(5 <= len(res["ideas"]) <= 6, [i["name"] for i in res["ideas"]])
        self.assertEqual(len({i["name"] for i in res["ideas"]}), len(res["ideas"]))  # nichts doppelt
        res, seen = self.run_suggest(max_minutes=180)
        self.assertLessEqual(len(res["ideas"]), 6)

    def test_weak_tripadvisor_dropped_good_shown(self):
        ta = {"Kapellbrücke": {"found": True, "title": "Kapellbrücke", "rating": 4.5, "reviews": 12000, "link": None},
              "Rheinfall": {"found": True, "title": "Rheinfall", "rating": 3.5, "reviews": 900}}
        res, _ = self.run_suggest(ta=ta)
        names = [i["name"] for i in res["ideas"]]
        self.assertNotIn("Rheinfall", names)
        luz = next(i for i in res["ideas"] if i["name"] == "Luzern")
        self.assertEqual(luz["ta"]["rating"], 4.5)
        self.assertTrue(luz["ta"]["url"].startswith("https://www.tripadvisor.com/Search?"))
        self.assertTrue(any("nur ★ 3.5" in d for d in res["dropped"]))

    def test_public_transport_only(self):
        orig = DT._fake
        with mock.patch.object(DT, "_fake", lambda o, more=False: DT.DayTripList(ideas=[] if more else [
                orig(o).ideas[0].model_copy(update={"travel_mode": "auto"})])):
            res, _ = self.run_suggest(mode="oev")
        self.assertEqual(res["ideas"], [])
        self.assertIn("nur mit dem Auto", res["dropped"][0])


class PlanTest(unittest.TestCase):
    def test_day_trip_without_night(self):
        idea = {"name": "Luzern", "region": "Zentralschweiz", "travel_mode": "zug", "travel_minutes": 50,
                "highlights": ["Kapellbrücke"], "tripadvisor": "Kapellbrücke", "lat": 47.05, "lon": 8.31}
        seen = {}

        def ask(purpose, **kw):
            seen["system"], seen["prompt"] = kw["system"], kw["content"]
            return kw["fake"]()
        day = date.today() + timedelta(days=3)
        with mock.patch.object(config, "FAKE_MODE", True), mock.patch.object(generator.llm, "ask", ask):
            it, notes = generator.generate_daytrip(household(), ZH, idea, day, "", "ZRH")
        self.assertIn("TAGESAUSFLUG", seen["system"])
        self.assertIn("ab Zürich (Startort = aktueller Standort", seen["prompt"])
        self.assertEqual((it.start_date, it.end_date, len(it.days)), (day, day, 1))
        self.assertEqual(it.stays, [])
        self.assertEqual([(t.origin.name, t.destination.name) for t in it.transports], [("Zürich", "Luzern"), ("Luzern", "Zürich")])
        self.assertTrue(all(t.depart.date() == day for t in it.transports))
        self.assertTrue(it.title.startswith("Tagesausflug"))
        self.assertFalse(any("Unterkunft" in n for n in notes))


class ReverseTest(unittest.TestCase):
    def test_reverse_parses_and_falls_back(self):
        data = {"address": {"town": "Wädenswil", "county": "Bezirk Horgen", "country_code": "ch"}}
        with mock.patch.object(config, "FAKE_MODE", False), mock.patch.object(weather, "_get", return_value=data) as g:
            p = weather.reverse(47.2299, 8.6717)
        self.assertEqual((p.name, p.country, p.lat, p.lon), ("Wädenswil", "CH", 47.23, 8.67))  # auf ~1 km gerundet
        self.assertEqual(g.call_args[0][1]["lat"], 47.23)
        with mock.patch.object(config, "FAKE_MODE", False), \
                mock.patch.object(weather, "_get", side_effect=weather.WeatherError("weg")):
            p = weather.reverse(47.2299, 8.6717)
        self.assertEqual((p.name, p.country), ("Standort 47.23, 8.67", "XX"))


if __name__ == "__main__":
    unittest.main()

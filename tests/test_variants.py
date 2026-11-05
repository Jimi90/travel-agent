"""Routen-Varianten: Koordinaten der KI werden per Ortssuche im richtigen Land korrigiert."""
import unittest
from unittest import mock

from app import config, weather
from app.models import Place
from app.planning import variants
from app.planning.variants import Stop


class LocateTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(config, "FAKE_MODE", False)
        p.start()
        self.addCleanup(p.stop)

    def test_wrong_ai_coordinates_are_replaced(self):
        calls = []

        def geo(name, country=None):
            calls.append((name, country))
            if name == "San José de Cúcuta":
                raise weather.WeatherError("nicht gefunden")
            return Place(name=name, country="CO", lat=7.89, lon=-72.5)

        s = Stop(name="San José de Cúcuta", country="CO", lat=9.93, lon=-84.08, nights=2)  # KI: San José, Costa Rica
        with mock.patch.object(weather, "geocode", geo):
            variants._locate(s)
        self.assertEqual((s.lat, s.lon), (7.89, -72.5))
        self.assertEqual(calls, [("San José de Cúcuta", "CO"), ("Cúcuta", "CO")])  # zuerst ganzer Name, dann Kurzform

    def test_brackets_are_dropped_and_country_is_passed(self):
        with mock.patch.object(weather, "geocode", return_value=Place(name="Funchal", country="PT", lat=32.65, lon=-16.92)) as g:
            s = Stop(name="Madeira (Funchal)", country="PT", lat=0.0, lon=0.0, nights=1)
            variants._locate(s)
        g.assert_called_once_with("Madeira", "PT")
        self.assertEqual((s.lat, s.lon), (32.65, -16.92))

    def test_nothing_found_keeps_ai_values(self):
        s = Stop(name="Nirgendwo", country="PT", lat=38.0, lon=-9.0, nights=1)
        with mock.patch.object(weather, "geocode", side_effect=weather.WeatherError("nein")):
            variants._locate(s)
        self.assertEqual((s.lat, s.lon), (38.0, -9.0))

    def test_geocode_sends_country_code(self):
        seen = {}
        with mock.patch.object(weather, "_get", lambda url, params, ttl: seen.update(params) or
                               {"results": [{"name": "Cúcuta", "country_code": "CO", "latitude": 7.9, "longitude": -72.5}]}):
            weather.geocode("Cúcuta", "co")
        self.assertEqual(seen["countryCode"], "CO")



class BudgetFilterTest(unittest.TestCase):
    """Routen deutlich über dem Budget werden nicht gezeigt (10 % Spielraum); passt keine, nur die günstigste."""

    def run_suggest(self, budget):
        from datetime import date
        from tests.test_replanning import household
        h = household(budget=budget)
        with mock.patch.object(variants.llm, "ask", return_value=variants._fake("Lissabon", 7)), \
                mock.patch.object(variants.photos, "place_photo", return_value=None), \
                mock.patch.object(variants.weather, "climate", side_effect=weather.WeatherError("x")), \
                mock.patch.object(config, "FAKE_MODE", True):
            return variants.suggest(h, "Lissabon", date(2026, 5, 4), 7, "", "ZRH")

    def test_too_expensive_route_is_dropped(self):
        res = self.run_suggest("1800")  # Totals 1920 / 2050 / 1950, Grenze mit Spielraum 1980
        names = [v["name"] for v in res["variants"]]
        self.assertEqual(names, ["Gemütlich an einem Ort", "Rundreise"])
        self.assertTrue(any("Stadt & Insel" in d for d in res["dropped"]))

    def test_nothing_fits_keeps_only_cheapest_with_warning(self):
        res = self.run_suggest("1000")
        self.assertEqual(len(res["variants"]), 1)
        self.assertEqual(res["variants"][0]["name"], "Gemütlich an einem Ort")
        self.assertIn("Keine Route im Budget", res["variants"][0]["warning"])


GEO = {  # nachgebildete Ortssuche (Open-Meteo), Schlüssel (Name, Ländercode)
    ("Malta", "MT"): Place(name="Malta", country="MT", lat=35.9, lon=14.4),
    ("Valletta", "MT"): Place(name="Valletta", country="MT", lat=35.899, lon=14.514),
    ("Gozo", "MT"): Place(name="Gozo", country="MT", lat=36.04, lon=14.24),
    ("Japan", None): Place(name="Japan", country="JP", lat=36.0, lon=138.0),
    ("Naha", "JP"): Place(name="Naha", country="JP", lat=26.21, lon=127.68),
    ("Lahore", "PK"): Place(name="Lahore", country="PK", lat=31.55, lon=74.34),
}


def _geo(name, country=None):
    if (name, country) in GEO:
        return GEO[(name, country)]
    raise weather.WeatherError(f"Ort «{name}» nicht gefunden")


def _stop(name, cc, lat, lon, n):
    return Stop(name=name, country=cc, lat=lat, lon=lon, nights=n)


def _variant(name, route):
    return variants.Variant(name=name, summary="", route=route, bullets=["x"], water_temp_c=20, flights_chf=300,
                            hotels_chf=1000, activities_chf=200, transfers_chf=50)


class DestinationCheckTest(unittest.TestCase):
    """James klickte «Routen ansehen» (Malta) und bekam «Malediven-Entspannung» und eine Route bis Pakistan."""

    def setUp(self):
        for p in (mock.patch.object(config, "FAKE_MODE", False), mock.patch.object(weather, "geocode", _geo)):
            p.start()
            self.addCleanup(p.stop)

    def test_target(self):
        cc, c = variants.target("Malta, MT")
        self.assertEqual((cc, c.country), ("MT", "MT"))
        cc, c = variants.target("Japan")
        self.assertEqual((cc, c.country), (None, "JP"))
        self.assertEqual(variants.target("Irgendwo"), (None, None))

    def test_inside(self):
        cc, c = variants.target("Malta, MT")
        val = _stop("Valletta", "MT", 30.0, 70.0, 3)  # KI-Koordinaten in Pakistan → Ortssuche korrigiert
        variants._locate(val)
        self.assertTrue(variants.inside(val, cc, c))
        self.assertFalse(variants.inside(_stop("Lahore", "PK", 31.5, 74.3, 3), cc, c))  # anderes Land
        self.assertFalse(variants.inside(_stop("Phantasia", "MT", 31.0, 74.0, 3), cc, c))  # «Malta», aber in Pakistan
        cc, c = variants.target("Japan")  # frei eingegeben: gleiches Land zählt, auch weit weg
        self.assertTrue(variants.inside(_stop("Naha", "JP", 26.2, 127.7, 3), cc, c))
        self.assertFalse(variants.inside(_stop("Lahore", "PK", 31.5, 74.3, 3), cc, c))

    def run_suggest(self, answers):
        from datetime import date, timedelta
        from tests.test_replanning import household
        calls = []

        def ask(purpose, **kw):
            calls.append(kw["content"])
            return answers[len(calls) - 1]
        clim = {"temp_max": 18, "temp_min": 12, "rain_days": 0.2, "sun_hours": 6}
        with mock.patch.object(variants.llm, "ask", ask), \
                mock.patch.object(weather, "climate", return_value=clim), \
                mock.patch.object(variants, "climate_score", return_value=(6, "")), \
                mock.patch.object(variants.photos, "place_photo", return_value=None):
            res = variants.suggest(household(), "Malta, MT", date.today() + timedelta(days=30), 8, "", "ZRH")
        return res, calls

    def test_foreign_stops_are_dropped(self):
        res, calls = self.run_suggest([variants.VariantList(variants=[
            _variant("Gemütlich in Valletta", [_stop("Valletta", "MT", 0, 0, 7)]),
            _variant("Malediven-Entspannung", [_stop("Malé", "MV", 4.17, 73.5, 7)]),
            _variant("Inseln", [_stop("Valletta", "MT", 0, 0, 4), _stop("Lahore", "PK", 31.5, 74.3, 3)]),
        ])])
        self.assertEqual(len(calls), 1)
        self.assertEqual([v["name"] for v in res["variants"]], ["Gemütlich in Valletta", "Inseln"])
        self.assertEqual([s["name"] for s in res["variants"][1]["route"]], ["Valletta"])
        self.assertEqual(res["variants"][1]["route"][0]["nights"], 7)  # Nächte wieder auf 7 ergänzt
        self.assertEqual(res["variants"][1]["route"][0]["lon"], 14.514)  # echte Koordinaten
        self.assertEqual(res["outside"], ["Lahore", "Malé"])

    def test_retry_then_error(self):
        bad = variants.VariantList(variants=[_variant("Pakistan", [_stop("Lahore", "PK", 31.5, 74.3, 7)])])
        good = variants.VariantList(variants=[_variant("Valletta", [_stop("Valletta", "MT", 0, 0, 7)])])
        res, calls = self.run_suggest([bad, good])
        self.assertEqual(len(calls), 2)
        self.assertIn("Nur Orte in Malta, MT", calls[1])
        self.assertEqual(len(res["variants"]), 1)
        with self.assertRaises(variants.llm.LLMError):
            self.run_suggest([bad, bad])

    def test_routes_go_to_gemini_first(self):  # nicht an das kleine Groq-Kontingent
        self.assertIn("varianten", variants.llm.HEAVY)


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

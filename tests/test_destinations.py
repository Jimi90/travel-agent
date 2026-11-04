import unittest
from unittest import mock

from app import weather
from app.planning import destinations as d


def idea(name, country, country_name=""):
    return d.DestinationIdea(name=name, country=country, country_name=country_name, region="", lat=0, lon=0, why="",
                             highlights=[], travel_mode="flug", travel_hours=2, typical_stops=0,
                             travel_cost_chf=100, cost_level="mittel", est_total_chf=1000)


def geo(table):
    def f(q, country=None):
        if q not in table:
            raise weather.WeatherError("nicht gefunden")
        return weather.Place(name=q, country=table[q], lat=0, lon=0, tz=None)
    return f


class ResolveCountryTest(unittest.TestCase):
    def run_resolve(self, i, table):
        with mock.patch.object(d.config, "FAKE_MODE", False), mock.patch.object(d.weather, "geocode", geo(table)):
            return d.resolve_country(i)

    def test_valid_code_confirmed_by_place(self):
        self.assertEqual(self.run_resolve(idea("Lissabon", "PT", "Portugal"), {"Lissabon": "PT"}), "PT")

    def test_country_name_instead_of_code(self):
        # «MEXICO» ergibt abgeschnitten «ME» (Montenegro) – darf nicht passieren
        self.assertEqual(self.run_resolve(idea("Cancún", "MEXICO", "Mexiko"), {"Cancún": "MX", "Mexiko": "MX"}), "MX")

    def test_wrong_but_valid_code_loses_against_country_name(self):
        self.assertEqual(self.run_resolve(idea("Dakar", "SE", "Senegal"), {"Dakar": "SN", "Senegal": "SN"}), "SN")

    def test_nothing_found_gives_empty(self):
        self.assertEqual(self.run_resolve(idea("Xyz", "Kap Verde", ""), {}), "")


if __name__ == "__main__":
    unittest.main()

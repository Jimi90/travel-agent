"""Auslesen der SerpApi-Antworten (Google Flights / Google Hotels) mit nachgebildeten Daten."""
import unittest

from app.prices import parse_flights, parse_hotels

FLIGHTS = {"search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?x"},
           "best_flights": [{"flights": [{"departure_airport": {"name": "Zürich", "id": "ZRH", "time": "2026-12-01 07:00"},
                                          "arrival_airport": {"id": "LIS", "time": "2026-12-01 09:40"},
                                          "airline": "SWISS", "flight_number": "LX 2084"}],
                             "layovers": [], "total_duration": 160, "price": 512}],
           "other_flights": [{"flights": [{"departure_airport": {"id": "ZRH", "time": "2026-12-01 23:00"},
                                           "arrival_airport": {"id": "FRA", "time": "2026-12-02 00:10"},
                                           "airline": "Lufthansa", "flight_number": "LH 1"},
                                          {"departure_airport": {"id": "FRA", "time": "2026-12-02 06:00"},
                                           "arrival_airport": {"id": "LIS", "time": "2026-12-02 08:30"},
                                           "airline": "Lufthansa", "flight_number": "LH 2"}],
                              "layovers": [{"name": "Frankfurt", "id": "FRA", "duration": 350}],
                              "total_duration": 630, "price": 301},
                             {"flights": [], "price": 100},                      # unvollständig → weg
                             {"flights": [{"departure_airport": {"time": "kaputt"}}], "price": 90}]}


class PriceParseTest(unittest.TestCase):
    def test_flights(self):
        out = parse_flights(FLIGHTS)
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]["carrier"], out[0]["price"], out[0]["via"]), ("SWISS", 512.0, []))
        self.assertEqual((out[1]["number"], out[1]["via"], out[1]["arrive"]),
                         ("LH 1, LH 2", ["Frankfurt"], "2026-12-02T08:30:00"))

    def test_hotels_budget_and_total(self):
        data = {"properties": [
            {"name": "Teuer", "rate_per_night": {"lowest": "CHF 260", "extracted_lowest": 260}, "overall_rating": 4.9},
            {"name": "Passt", "rate_per_night": {"extracted_lowest": 140}, "total_rate": {"extracted_lowest": 420},
             "overall_rating": 4.5, "link": "https://h"},
            {"name": "Ohne Preis"}]}
        out = parse_hotels(data, nights=3, max_night=180)
        self.assertEqual([h["name"] for h in out], ["Passt", "Teuer"])
        self.assertEqual((out[0]["total"], out[1]["total"], out[1]["over_budget"]), (420.0, 780.0, True))


if __name__ == "__main__":
    unittest.main()

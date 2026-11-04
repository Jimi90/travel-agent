"""Auslesen der SerpApi-Antworten (Google Flights / Google Hotels) mit nachgebildeten Daten."""
import unittest
import unittest.mock

from app import prices
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



class MoneyTest(unittest.TestCase):
    def test_swiss_thousands_separator(self):
        for t, v in (("CHF 1’600", 1600), ("1'600.50", 1600.5), ("1.600,50", 1600.5), ("1,600", 1600), ("CHF 194", 194)):
            self.assertEqual(prices.money(t), v, t)

    def test_truncated_extracted_value_loses_against_text(self):
        self.assertEqual(prices._num({"lowest": "CHF 1’600", "extracted_lowest": 1}), 1600)

    def test_implausible_total_falls_back_to_night_times_nights(self):
        self.assertEqual(prices.stay_total(1, 194, 10), 1940)
        self.assertEqual(prices.stay_total(1900, 194, 10), 1900)
        self.assertEqual(prices.stay_total(None, 100, 3), 300)



class FlightsLiveTest(unittest.TestCase):
    def seg(self, **kw):
        from datetime import datetime, timedelta
        from app.models import TransportSegment
        from app.models.common import Place
        d = datetime.now() + timedelta(days=20)
        base = dict(mode="flug", origin=Place(name="Zürich", country="CH"), destination=Place(name="Lissabon", country="PT"),
                    depart=d, arrive=d + timedelta(hours=3), origin_code="ZRH", destination_code="LIS")
        base.update(kw)
        return TransportSegment(**base)

    def test_eligible_only_future_estimated_flights_with_codes(self):
        from datetime import datetime, timedelta
        from app import flights
        with unittest.mock.patch.object(prices, "available", return_value=True):
            self.assertTrue(flights.eligible(self.seg()))
            self.assertFalse(flights.eligible(self.seg(mode="zug")))
            self.assertFalse(flights.eligible(self.seg(origin_code=None)))
            self.assertFalse(flights.eligible(self.seg(depart=datetime.now() - timedelta(days=2),
                                                       arrive=datetime.now() - timedelta(days=2) + timedelta(hours=3))))
        with unittest.mock.patch.object(prices, "available", return_value=False):
            self.assertFalse(flights.eligible(self.seg()))


if __name__ == "__main__":
    unittest.main()


class HotelPhotoRetryTest(unittest.TestCase):
    def test_retry_only_for_old_results_without_photo(self):
        from app import hotels
        self.assertTrue(hotels.needs_photo_retry({"found": False, "photos": []}))
        self.assertFalse(hotels.needs_photo_retry({"found": False, "photos": [], "v": hotels.PHOTO_FALLBACK_V}))
        self.assertFalse(hotels.needs_photo_retry({"photos": ["a"]}))
        self.assertFalse(hotels.needs_photo_retry({"error": "x", "photos": []}))
        self.assertFalse(hotels.needs_photo_retry(None))

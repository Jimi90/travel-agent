"""Tests für Datenmodelle, StatusManager (atomare Änderungen) und automatische Umplanung.

Start: python -m unittest -v
"""
import unittest
from datetime import date, datetime, time
from decimal import Decimal
from unittest import mock

from app.core.status_manager import StatusManager
from app.models import (
    Activity, AddActivity, Booking, ChangeSet, ChangeStatus, DayPlan, Household, HourlyWeather, Itinerary,
    MoveActivity, Place, PregnancyStatus, RemoveActivity, TravelConstraints, UserProfile, WeatherDisruption,
    WeatherForecast,
)
from app.models.common import ActivityCategory as C, Exposure as E, Intensity as I
from app.planning.alternatives import AlternativeActivitiesModule, AlternativeCandidate, StaticCatalog
from app.planning.replanning import AutonomousRePlanningModule

LIS = Place(name="Lissabon", country="pt")
NOW = datetime(2026, 5, 5, 7, 0)
D4, D5, D6, D7 = (date(2026, 5, d) for d in (4, 5, 6, 7))


def household(pregnant=False, budget="6000"):
    return Household(
        profiles=[
            UserProfile(name="James", role="haupt",
                        activities={C.WANDERN: 2, C.MUSEUM: 1, C.KULINARIK: 2, C.WELLNESS: 0}),
            UserProfile(name="Partnerin", role="partnerin",
                        activities={C.WANDERN: 1, C.MUSEUM: 1, C.WELLNESS: 2, C.SHOPPING: -2}),
        ],
        constraints=TravelConstraints(
            budget_total_max=Decimal(budget),
            pregnancy=PregnancyStatus(due_date=date(2026, 9, 20)) if pregnant else None,
        ),
        history={"visited_countries": ["es", "IT"]},
    )


CATALOG = StaticCatalog([
    AlternativeCandidate(title="Azulejo-Museum", category=C.MUSEUM, exposure=E.INDOOR, duration_min=150,
                         cost=Decimal("10"), place=LIS, opens=time(10), closes=time(18)),
    AlternativeCandidate(title="Hammam & Sauna", category=C.WELLNESS, exposure=E.INDOOR, duration_min=210,
                         cost=Decimal("90"), place=LIS, tags={"hitze"}, booking_needed=True),
    AlternativeCandidate(title="Massage im Day-Spa", category=C.WELLNESS, exposure=E.INDOOR, duration_min=180,
                         cost=Decimal("80"), place=LIS, booking_needed=True),
    AlternativeCandidate(title="Einkaufszentrum Colombo", category=C.SHOPPING, exposure=E.INDOOR,
                         duration_min=240, place=LIS),
    AlternativeCandidate(title="Kletterhalle", category=C.WANDERN, exposure=E.INDOOR, intensity=I.ANSTRENGEND,
                         duration_min=120, cost=Decimal("25"), place=LIS),
    AlternativeCandidate(title="Fado-Konzert", category=C.KULTUR, exposure=E.INDOOR, duration_min=90,
                         cost=Decimal("35"), place=LIS, opens=time(17)),  # muss der letzte Eintrag bleiben
])


def itinerary():
    hike = Activity(id="hike", title="Wanderung Sintra", category=C.WANDERN, exposure=E.OUTDOOR,
                    intensity=I.MITTEL, start=time(9), end=time(13), place=LIS)
    ocean = Activity(id="ocean", title="Ozeanarium", category=C.NATUR, exposure=E.INDOOR,
                     start=time(14, 30), end=time(16, 30), cost=Decimal("25"))
    dinner = Activity(id="dinner", title="Abendessen Belcanto", category=C.KULINARIK, exposure=E.INDOOR,
                      start=time(19), end=time(21, 30), cost=Decimal("300"),
                      booking=Booking(provider="restaurant", reference="B-1", cancellable=False))
    boat = Activity(id="boat", title="Bootstour Tejo", category=C.WASSERSPORT, exposure=E.OUTDOOR,
                    start=time(17), end=time(18, 30), cost=Decimal("60"),
                    booking=Booking(provider="getyourguide", reference="G-7"))
    return Itinerary(
        id="lis", title="Lissabon im Mai", start_date=D4, end_date=D7,
        days=[
            DayPlan(date=D4, base=LIS),
            DayPlan(date=D5, base=LIS, activities=[hike, ocean, boat, dinner]),
            DayPlan(date=D6, base=LIS, activities=[
                Activity(title="Tram 28", category=C.STADT, exposure=E.MIXED, start=time(10), end=time(12))]),
            DayPlan(date=D7, base=LIS),
        ],
    )


def rain(d, mm=18.0, **kw):
    return WeatherForecast(date=d, place=LIS, summary="Regen", precip_mm=mm, precip_probability=90, **kw)


def sunny(d):
    return WeatherForecast(date=d, place=LIS, summary="sonnig", precip_mm=0, wind_kmh_max=10)


def event(d=D5, fc=None, eid="ev1"):
    return WeatherDisruption(id=eid, itinerary_id="lis", detected_at=NOW, place=LIS, date_from=d, date_to=d,
                             forecasts=[fc or rain(d)])


class Base(unittest.TestCase):
    pregnant = False
    lookup = None

    def setUp(self):
        self.sm = StatusManager(":memory:")
        self.sm.create(itinerary())
        self.hh = household(self.pregnant)
        self.mod = AutonomousRePlanningModule(
            self.sm, AlternativeActivitiesModule(CATALOG, self.hh), self.hh,
            forecast_lookup=self.lookup, clock=lambda: NOW)

    def titles(self, d):
        return [a.title for a in self.sm.get("lis").day(d).activities]


class ModelTests(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            Activity(title="x", category=C.MUSEUM, exposure=E.INDOOR, start=time(10), end=time(9))
        with self.assertRaises(ValueError):
            Itinerary(title="x", start_date=D5, end_date=D5, days=[DayPlan(date=D7, base=LIS)])
        self.assertEqual(household().history.visited_countries, {"ES", "IT"})
        self.assertIsNone(household().harmonized(C.SHOPPING))  # Veto
        self.assertEqual(household().harmonized(C.WANDERN), 1.5)
        self.assertEqual(PregnancyStatus(due_date=date(2026, 9, 20)).week_on(D5), 20)

    def test_budget_and_reach_depend_on_days(self):
        from app.planning.matching import match, reach_for
        k = TravelConstraints(budget_total_max=Decimal("6000"), budget_per_day_max=Decimal("300"), max_flight_hours=12)
        self.assertEqual(k.budget_for(3), Decimal("1100"))            # 3 × 300 vor Ort + 200 Anreise (Kurztrip)
        self.assertEqual(k.budget_for(10), Decimal("3900"))           # 10 × 300 + 900 Anreise (Ferien)
        self.assertEqual(k.budget_for(30), Decimal("6000"))           # Budget pro Reise bleibt Obergrenze
        self.assertEqual((k.travel_budget_for(2), k.travel_budget_for(5), k.travel_budget_for(21)),
                         (Decimal("200"), Decimal("400"), Decimal("1800")))
        own = TravelConstraints(budget_per_day_max=Decimal("250"), travel_budget={"kurztrip": 120})
        self.assertEqual(own.budget_for(2), Decimal("620"))            # eigener Wert für Weekends
        self.assertEqual(own.travel_budget_for(8), Decimal("900"))     # nicht gesetzte Länge: Vorschlagswert
        with self.assertRaises(ValueError):
            TravelConstraints(travel_budget={"weekend": 100})
        self.assertEqual(TravelConstraints(budget_total_max=Decimal("2000")).budget_for(3), Decimal("2000"))
        self.assertIsNone(TravelConstraints().budget_for(5))
        short, long = reach_for(3, k), reach_for(21, k)
        self.assertEqual((short.flight_hours, short.stops, short.ground_hours, short.max_bases), (2.0, 0, 5.0, 1))
        self.assertEqual(long.flight_hours, 12)                        # Profil-Grenze gilt bei langen Reisen
        self.assertEqual(reach_for(5, TravelConstraints(max_flight_hours=3)).flight_hours, 3)  # strengere gewinnt
        self.assertIn("Kurztrip", match(household(), D5, 3).prompt_text())

    def test_hourly_window(self):
        fc = WeatherForecast(date=D5, place=LIS, precip_mm=8, hourly=[
            HourlyWeather(time=time(h), precip_mm=0 if h < 14 else 3) for h in range(6, 23)])
        hike = itinerary().day(D5).activities[0]
        self.assertEqual(fc.problems_for(hike.limits(), time(9), time(13)), [])
        self.assertEqual(fc.problems_for(hike.limits(), time(15), time(17)), ["starker Regen"])
        self.assertEqual(fc.problems_for(None), [])  # indoor


class StatusManagerTests(Base):
    def test_all_or_nothing(self):
        before = self.sm.get("lis")
        cs = ChangeSet(itinerary_id="lis", base_version=1, reason="Test", operations=[
            RemoveActivity(activity_id="ocean"),
            MoveActivity(activity_id="hike", to_day=D5, start=time(18), end=time(22)),  # kollidiert mit Abendessen
        ])
        res = self.sm.apply(cs, self.hh.constraints, NOW)
        self.assertIs(res.status, ChangeStatus.REJECTED)
        self.assertEqual(self.sm.get("lis"), before)  # auch das gültige Entfernen wurde nicht gespeichert

    def test_crash_mid_transaction_rolls_back(self):
        before = self.sm.get("lis")
        cs = ChangeSet(itinerary_id="lis", base_version=1, reason="Test", event_id="x",
                       operations=[RemoveActivity(activity_id="ocean")])
        with mock.patch.object(self.sm, "_write_outbox", side_effect=OSError("Disk voll")):
            with self.assertRaises(OSError):
                self.sm.apply(cs, self.hh.constraints, NOW)
        self.assertEqual(self.sm.get("lis"), before)
        self.assertEqual(self.sm.history("lis"), [])
        self.assertFalse(self.sm.event_handled("x"))

    def test_conflict_on_stale_version(self):
        ok = ChangeSet(itinerary_id="lis", base_version=1, reason="a", operations=[RemoveActivity(activity_id="ocean")])
        self.assertIs(self.sm.apply(ok, None, NOW).status, ChangeStatus.APPLIED)
        stale = ChangeSet(itinerary_id="lis", base_version=1, reason="b",
                          operations=[RemoveActivity(activity_id="hike")])
        self.assertIs(self.sm.apply(stale, None, NOW).status, ChangeStatus.CONFLICT)
        self.assertIn("Wanderung Sintra", self.titles(D5))

    def test_locked_activity_protected(self):
        cs = ChangeSet(itinerary_id="lis", base_version=1, reason="x", operations=[RemoveActivity(activity_id="dinner")])
        res = self.sm.apply(cs, None, NOW)
        self.assertIs(res.status, ChangeStatus.REJECTED)
        self.assertEqual(res.problems[0].code, "fest_veraendert")

    def test_budget(self):
        pricey = Activity(title="Helikopter", category=C.NATUR, exposure=E.OUTDOOR, start=time(10), end=time(11),
                          cost=Decimal("9000"))
        cs = ChangeSet(itinerary_id="lis", base_version=1, reason="x", operations=[AddActivity(day=D7, activity=pricey)])
        res = self.sm.apply(cs, self.hh.constraints, NOW)
        self.assertEqual([p.code for p in res.problems], ["budget"])


class ReplanTests(Base):
    def test_rain_replaces_outdoor_and_keeps_rest(self):
        out = self.mod.handle(event())
        self.assertIs(out.status, ChangeStatus.APPLIED, out)
        t = self.titles(D5)
        self.assertNotIn("Wanderung Sintra", t)
        self.assertNotIn("Bootstour Tejo", t)
        self.assertIn("Ozeanarium", t)
        self.assertEqual(t[-1], "Abendessen Belcanto")
        self.assertNotIn("Einkaufszentrum Colombo", t)  # Veto der Partnerin
        self.assertEqual(self.sm.get("lis").version, 2)
        new = [a for a in self.sm.get("lis").day(D5).activities if a.replaces == "hike"][0]
        self.assertEqual((new.title, new.origin), ("Kletterhalle", "umplanung"))  # gleiche Art, beide wandern gern
        self.assertIn("Fado-Konzert", t)
        # gebuchte, stornierbare Bootstour → Storno als Auftrag, nur mit Bestätigung
        kinds = sorted(a["kind"] for a in self.sm.pending_actions())
        self.assertIn("buchung_stornieren", kinds)
        self.assertTrue(all(a["needs_confirmation"] for a in self.sm.pending_actions()))
        self.assertIn("Kletterhalle", out.message())

    def test_same_event_only_once(self):
        self.mod.handle(event())
        again = self.mod.handle(event())
        self.assertIs(again.status, ChangeStatus.DUPLICATE)
        self.assertEqual(self.sm.get("lis").version, 2)

    def test_dry_morning_keeps_hike(self):
        fc = WeatherForecast(date=D5, place=LIS, precip_mm=10, hourly=[
            HourlyWeather(time=time(h), precip_mm=0 if h < 14 else 3, precip_probability=0 if h < 14 else 95)
            for h in range(6, 23)])
        out = self.mod.handle(event(fc=fc))
        self.assertIs(out.status, ChangeStatus.APPLIED)
        self.assertIn("Wanderung Sintra", self.titles(D5))
        self.assertNotIn("Bootstour Tejo", self.titles(D5))

    def test_no_alternative_tells_user(self):
        self.mod.alternatives = AlternativeActivitiesModule(StaticCatalog(CATALOG.items[:-1]), self.hh)
        out = self.mod.handle(event())
        self.assertIn("Bootstour Tejo", self.titles(D5))
        self.assertTrue(any("Bootstour Tejo" in n and "keine passende" in n for n in out.needs_user))

    def test_good_weather_noop(self):
        out = self.mod.handle(event(fc=sunny(D5)))
        self.assertIs(out.status, ChangeStatus.NOOP)
        self.assertEqual(self.sm.get("lis").version, 1)

    def test_locked_activity_needs_user(self):
        it = itinerary()
        it.day(D5).activities[0].fixed = True  # Wanderung von Hand festgenagelt
        self.sm = StatusManager(":memory:")
        self.sm.create(it)
        self.mod.status = self.sm
        out = self.mod.handle(event())
        self.assertIn("Wanderung Sintra", self.titles(D5))
        self.assertTrue(any("Wanderung Sintra" in n for n in out.needs_user))

    def test_already_started_is_left_alone(self):
        self.mod.clock = lambda: datetime(2026, 5, 5, 10, 0)
        out = self.mod.handle(event())
        self.assertIn("Wanderung Sintra", self.titles(D5))
        self.assertTrue(all(d.activity_id != "hike" for d in out.decisions))

    def test_retry_after_conflict(self):
        real_get = self.sm.get
        calls = {"n": 0}

        def get_then_interfere(i):
            itin = real_get(i)
            calls["n"] += 1
            if calls["n"] == 1:  # James ändert den Plan genau während der Umplanung
                self.sm.apply(ChangeSet(itinerary_id="lis", base_version=1, reason="James",
                                        operations=[RemoveActivity(activity_id="ocean")]), None, NOW)
            return itin

        with mock.patch.object(self.sm, "get", side_effect=get_then_interfere):
            out = self.mod.handle(event())
        self.assertIs(out.status, ChangeStatus.APPLIED)
        self.assertEqual(out.attempts, 2)
        self.assertNotIn("Ozeanarium", self.titles(D5))  # James' Änderung bleibt erhalten
        self.assertEqual(self.sm.get("lis").version, 3)

    def test_undo(self):
        out = self.mod.handle(event())
        res = self.sm.undo(out.result.change_id, NOW)
        self.assertIs(res.status, ChangeStatus.APPLIED)
        self.assertIn("Wanderung Sintra", self.titles(D5))
        self.assertEqual(self.sm.pending_actions(), [])


class PregnancyTests(Base):
    pregnant = True

    def test_no_heat_no_strenuous(self):
        out = self.mod.handle(event())
        self.assertIs(out.status, ChangeStatus.APPLIED)
        t = self.titles(D5)
        self.assertNotIn("Hammam & Sauna", t)  # «hitze»
        self.assertNotIn("Kletterhalle", t)  # zu anstrengend
        self.assertIn("Massage im Day-Spa", t)


class MoveTests(Base):
    lookup = staticmethod(lambda place, d: sunny(d) if d == D6 else None)

    def test_moves_hike_to_dry_day_and_fills_gap(self):
        out = self.mod.handle(event())
        self.assertIs(out.status, ChangeStatus.APPLIED)
        hike = [d for d in out.decisions if d.activity_id == "hike"][0]
        self.assertEqual(hike.action, "verschoben_mit_ersatz")
        it = self.sm.get("lis")
        day6 = {a.title: a for a in it.day(D6).activities}
        self.assertEqual((day6["Wanderung Sintra"].start, day6["Wanderung Sintra"].end), (time(12), time(16)))
        self.assertNotIn("Wanderung Sintra", self.titles(D5))
        self.assertTrue(any(a.replaces == "hike" for a in it.day(D5).activities))


if __name__ == "__main__":
    unittest.main()


class PlanOverBudgetTest(unittest.TestCase):
    def test_plan_over_budget_is_built_with_note(self):
        from app.planning import generator
        h = household(budget="500")
        draft = generator.fake_draft("Lissabon", 6, "ZRH")
        itin, notes = generator.build(draft, h, date(2026, 5, 4), 6, "ZRH")
        self.assertTrue(itin.days)
        self.assertTrue(any("über dem Budget" in n for n in notes), notes)


class HomePlaceTest(unittest.TestCase):
    def test_short_trip_prompt_mentions_home_place(self):
        from app import store
        from app.planning import matching
        h = household()
        with mock.patch.object(matching, "_home_place", return_value="Winterthur"):
            short = matching.match(h, date(2026, 5, 4), 2, "ZRH").prompt_text()
            long_ = matching.match(h, date(2026, 5, 4), 8, "ZRH").prompt_text()
        self.assertIn("Wohnort: Winterthur", short)
        self.assertIn("Shopping", short)
        self.assertNotIn("Wohnort: Winterthur", long_)
        with mock.patch.object(matching, "_home_place", return_value=""):
            self.assertNotIn("Wohnort:", matching.match(h, date(2026, 5, 4), 2, "ZRH").prompt_text())

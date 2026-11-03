"""Demo-Reise zum Ausprobieren der automatischen Umplanung (ohne echte Wetterdaten).

Die Reise beginnt immer morgen (4 Tage Lissabon). Simuliert wird der Wettercheck am Morgen
von Tag 2 um 07:00. Echte Wetterdaten (Open-Meteo) kommen in Schritt 3.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .models import (
    Activity, Booking, DayPlan, Household, HourlyWeather, Itinerary, Place, PregnancyStatus,
    TravelConstraints, UserProfile, WeatherDisruption, WeatherForecast, new_id,
)
from .models.common import ActivityCategory as C, Exposure as E, Intensity as I
from .planning.alternatives import AlternativeCandidate, StaticCatalog

LIS = Place(name="Lissabon", country="PT", lat=38.72, lon=-9.14)
DEMO_ID = "demo-lissabon"


def days(start: date | None = None) -> list[date]:
    start = start or date.today() + timedelta(days=1)
    return [start + timedelta(days=i) for i in range(4)]


def demo_clock(itin: Itinerary) -> datetime:
    """«Jetzt» in der Demo: Morgen von Tag 2, 07:00."""
    return datetime.combine(itin.start_date + timedelta(days=1), time(7))


def household(pregnant: bool = False) -> Household:
    d1 = date.today()
    return Household(
        id="demo",
        profiles=[
            UserProfile(name="James", role="haupt",
                        activities={C.WANDERN: 2, C.MUSEUM: 1, C.KULINARIK: 2, C.WELLNESS: 0}),
            UserProfile(name="Partnerin", role="partnerin",
                        activities={C.WANDERN: 1, C.MUSEUM: 1, C.WELLNESS: 2, C.SHOPPING: -2}),
        ],
        constraints=TravelConstraints(
            budget_total_max=Decimal("6000"),
            pregnancy=PregnancyStatus(due_date=d1 + timedelta(days=140)) if pregnant else None,
        ),
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
                         cost=Decimal("35"), place=LIS, opens=time(17)),
])


def itinerary() -> Itinerary:
    d1, d2, d3, d4 = days()
    return Itinerary(
        id=DEMO_ID, title="Lissabon (Demo)", start_date=d1, end_date=d4, status="unterwegs",
        days=[
            DayPlan(date=d1, base=LIS, activities=[
                Activity(title="Ankunft & Alfama-Spaziergang", category=C.STADT, exposure=E.MIXED,
                         start=time(15), end=time(17))]),
            DayPlan(date=d2, base=LIS, activities=[
                Activity(id="hike", title="Wanderung Sintra", category=C.WANDERN, exposure=E.OUTDOOR,
                         intensity=I.MITTEL, start=time(9), end=time(13), place=LIS),
                Activity(title="Ozeanarium", category=C.NATUR, exposure=E.INDOOR,
                         start=time(14, 30), end=time(16, 30), cost=Decimal("25")),
                Activity(title="Bootstour Tejo", category=C.WASSERSPORT, exposure=E.OUTDOOR,
                         start=time(17), end=time(18, 30), cost=Decimal("60"),
                         booking=Booking(provider="getyourguide", reference="G-7")),
                Activity(title="Abendessen Belcanto", category=C.KULINARIK, exposure=E.INDOOR,
                         start=time(19), end=time(21, 30), cost=Decimal("300"),
                         booking=Booking(provider="restaurant", reference="B-1", cancellable=False)),
            ]),
            DayPlan(date=d3, base=LIS, activities=[
                Activity(title="Tram 28", category=C.STADT, exposure=E.MIXED, start=time(10), end=time(12))]),
            DayPlan(date=d4, base=LIS, activities=[
                Activity(title="Abreise", category=C.ENTSPANNUNG, exposure=E.INDOOR, start=time(10), end=time(11),
                         fixed=True)]),
        ],
    )


SCENARIOS = {
    "regen": "Regen den ganzen Tag",
    "nachmittag": "Trockener Morgen, Regen ab 14 Uhr",
    "gewitter": "Gewitter-Warnung",
    "sonne": "Sonne",
}


def forecast(scenario: str, d: date) -> WeatherForecast:
    if scenario == "regen":
        return WeatherForecast(date=d, place=LIS, summary="Regen", precip_mm=18, precip_probability=90)
    if scenario == "nachmittag":
        return WeatherForecast(date=d, place=LIS, summary="Regen ab Mittag", precip_mm=10, hourly=[
            HourlyWeather(time=time(h), precip_mm=0 if h < 14 else 3, precip_probability=0 if h < 14 else 95)
            for h in range(6, 23)])
    if scenario == "gewitter":
        return WeatherForecast(date=d, place=LIS, summary="Gewitter", precip_mm=4, thunderstorm=True,
                               wind_kmh_max=55, alerts=["Gewitter mit Sturmböen (Stufe orange)"])
    return WeatherForecast(date=d, place=LIS, summary="sonnig", precip_mm=0, wind_kmh_max=10)


def event(itin: Itinerary, scenario: str) -> WeatherDisruption:
    d2 = itin.start_date + timedelta(days=1)
    return WeatherDisruption(
        id=new_id(), itinerary_id=itin.id, detected_at=demo_clock(itin), place=LIS, date_from=d2, date_to=d2,
        description=SCENARIOS[scenario], forecasts=[forecast(scenario, d2)],
    )


def lookup_dry_next_day(itin: Itinerary):
    """Wetter der anderen Tage: Tag 3 trocken, sonst unbekannt."""
    d3 = itin.start_date + timedelta(days=2)
    return lambda place, d: forecast("sonne", d) if d == d3 else None

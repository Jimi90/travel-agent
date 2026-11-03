"""Störungen (DisruptionEvent) – Wetter, Transport-Ausfall, Schliessung."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .common import Model, Place, new_id
from .itinerary import WeatherLimits


class HourlyWeather(Model):
    time: time
    precip_mm: float = Field(default=0, ge=0)
    precip_probability: int = Field(default=0, ge=0, le=100)
    wind_kmh: float = Field(default=0, ge=0)
    thunderstorm: bool = False
    temp_c: float | None = None


class WeatherForecast(Model):
    date: date
    place: Place
    summary: str = ""
    precip_mm: float = Field(default=0, ge=0)
    precip_probability: int = Field(default=0, ge=0, le=100)
    wind_kmh_max: float = Field(default=0, ge=0)
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    thunderstorm: bool = False
    alerts: list[str] = []  # amtliche Unwetterwarnungen
    hourly: list[HourlyWeather] = []
    source: str = "open-meteo"

    def problems_for(
        self, limits: WeatherLimits | None, start: time | None = None, end: time | None = None
    ) -> list[str]:
        """Gründe, warum das Wetter im Zeitfenster nicht passt (leer = passt)."""
        if limits is None:
            return []
        out: list[str] = [f"Unwetterwarnung: {a}" for a in self.alerts]
        hours = [h for h in self.hourly if start and end and start <= h.time < end]
        if hours:
            if any(h.precip_mm > limits.max_precip_mm_hour for h in hours):
                out.append("starker Regen")
            elif any(
                h.precip_probability > limits.max_precip_probability
                and h.precip_mm >= limits.max_precip_mm_hour / 2
                for h in hours
            ):
                out.append("Regen wahrscheinlich")
            if any(h.wind_kmh > limits.max_wind_kmh for h in hours):
                out.append("starker Wind")
            if not limits.thunderstorm_ok and any(h.thunderstorm for h in hours):
                out.append("Gewitter")
        else:
            if self.precip_mm > limits.max_precip_mm_day:
                out.append("starker Regen")
            elif (
                self.precip_probability > limits.max_precip_probability
                and self.precip_mm >= limits.max_precip_mm_day / 2
            ):
                out.append("Regen wahrscheinlich")
            if self.wind_kmh_max > limits.max_wind_kmh:
                out.append("starker Wind")
            if not limits.thunderstorm_ok and self.thunderstorm:
                out.append("Gewitter")
        return out


class _DisruptionBase(Model):
    id: str = Field(default_factory=new_id)
    itinerary_id: str
    severity: int = Field(default=2, ge=1, le=4)  # 1 Hinweis · 2 mittel · 3 hoch · 4 kritisch
    source: Literal["autonomer_wettercheck", "nutzer", "anbieter"] = "autonomer_wettercheck"
    detected_at: datetime
    place: Place
    date_from: date
    date_to: date
    description: str = ""

    @model_validator(mode="after")
    def _range(self):
        if self.date_to < self.date_from:
            raise ValueError("date_to liegt vor date_from")
        return self

    def dates(self) -> list[date]:
        n = (self.date_to - self.date_from).days
        return [self.date_from + timedelta(days=i) for i in range(n + 1)]


class WeatherDisruption(_DisruptionBase):
    type: Literal["wetter"] = "wetter"
    forecasts: list[WeatherForecast] = Field(min_length=1)

    def forecast_for(self, d: date) -> WeatherForecast | None:
        return next((f for f in self.forecasts if f.date == d), None)


class TransportDisruption(_DisruptionBase):
    """Fähre/Flug fällt aus: man sitzt ``delay_days`` Tage am Abfahrtsort fest."""

    type: Literal["transport"] = "transport"
    segment_id: str
    cancelled: bool = True
    delay_days: int = Field(default=1, ge=1, le=14)  # so viele Tage fährt/fliegt nichts


class ClosureDisruption(_DisruptionBase):
    type: Literal["schliessung"] = "schliessung"
    venue: str
    activity_id: str | None = None


DisruptionEvent = Annotated[
    Union[WeatherDisruption, TransportDisruption, ClosureDisruption], Field(discriminator="type")
]

# Architektur – Reise-Assistent

## Technik-Entscheid

| Teil | Wahl | Warum |
|---|---|---|
| Backend | **Python 3.12 + Pydantic v2**, Webserver aus der Standardbibliothek wie Hörwerk | Pydantic prüft jede Eingabe streng. Statt FastAPI (ursprünglicher Vorschlag) übernimmt der Server die erprobte Anmeldung/Sicherheit von Hörwerk 1:1 – weniger Risiko, weniger RAM. |
| Datenbank | **SQLite** (Datei in `data/`) | Echte ACID-Transaktionen ohne eigenen Datenbank-Server; eine Datei = einfache Sicherung. |
| Oberfläche | **Vanilla-JS als PWA** (wie Hörwerk, kein Build-Schritt) | Für zwei Personen reicht das. React/Next.js bräuchte Node, einen Build und mehr RAM, ohne Nutzen. |
| Zeitpläne | Scheduler-Thread in der App (wie Hörwerk) | Morgen-Wettercheck, Outbox abarbeiten. Kein Cron, kein zweiter Container. |
| Wetter | Open-Meteo | Gratis, ohne Schlüssel, stündliche Prognose. |
| KI | Claude (Bilder/Speisekarten, Entwürfe, Routen) + Gratis-Anbieter wie in Hörwerk | Kosten pro Aktion sichtbar machen. |
| Betrieb | Docker, GitHub Actions → `ghcr.io/jimi90/travel-agent`, Updater, **Tailscale Funnel** | 1:1 wie Hörwerk: kein Port am NAS, Passwort Pflicht. |

## Ordnerstruktur

Fett = existiert schon (Schritt 1), der Rest kommt in den nächsten Schritten.

```
travel-agent/
├── **app/**
│   ├── **models/**              Pydantic-Schemas (die «Sprache» des ganzen Systems)
│   │   ├── **common.py**        Ort, Kategorien, Geld, IDs
│   │   ├── **profile.py**       UserProfile, TravelConstraints, TravelHistory, Household (+ Matching v1)
│   │   ├── **itinerary.py**     Itinerary, DayPlan, Activity, Accommodation, TransportSegment, SavedItinerary
│   │   ├── **disruption.py**    DisruptionEvent (Wetter | Transport | Schliessung), WeatherForecast
│   │   └── **changes.py**       ChangeSet, Operationen, ExternalAction (Outbox), ChangeResult
│   ├── **core/**
│   │   ├── **status_manager.py**  StatusManager: einzige Stelle, die Pläne ändert (SQLite-Transaktion)
│   │   ├── **operations.py**      Operationen auf Plan-Kopie anwenden (rein, ohne Speichern)
│   │   ├── **plausibility.py**    MiddlewareConfig + Plausibilitätsprüfung
│   │   └── outbox.py            Externe Aufträge nach Bestätigung ausführen
│   ├── **planning/**
│   │   ├── **replanning.py**    AutonomousRePlanningModule
│   │   ├── **alternatives.py**  AlternativeActivitiesModule
│   │   ├── matching.py          MatchingModule (Ausbau von Household.harmonized)
│   │   ├── disruption_ext.py    ExtendedDisruptionHandler (Hotel verlängern, Folgetage schieben)
│   │   └── itinerary_gen.py     Routengenerierung (KI + Trends)
│   ├── integrations/            je Anbieter eine Datei, alle mit Fake-Modus
│   │   ├── weather.py           Open-Meteo (AutonomousWeatherCheck)
│   │   ├── hotels.py            Hotel-Verfügbarkeit
│   │   ├── flights.py           Flugpreise/Routen inkl. Zwischenstopps
│   │   └── llm.py               Claude/Gratis-KI (Vision, Übersetzung, Entwürfe)
│   ├── features/
│   │   ├── vision.py            Speisekarten/Schilder übersetzen
│   │   └── drafting.py          Nachrichten-Entwürfe für Buchungen/Nachfragen
│   ├── **server.py**            Routen, Anmeldung (`auth.py` aus Hörwerk), Sicherheits-Header
│   ├── **demo.py**              Demo-Reise Lissabon zum Ausprobieren
│   ├── scheduler.py             Morgen-Wettercheck, Outbox
│   └── **web/**                 index.html, login.html, Symbole (PWA)
├── **tests/**
├── **docs/**
├── **Dockerfile, docker-compose.yaml, tailscale-serve.json**
└── **.github/workflows/bauen.yml**   Tests bei jedem Push, Image nur auf main
```

## Datenfluss bei Schlechtwetter

```
Scheduler 06:30 ─► AutonomousWeatherCheck (Open-Meteo, Ort des Tages)
                    │  Grenzwerte überschritten?
                    ▼
               WeatherDisruption (id = Plan+Ort+Datum → nie doppelt)
                    ▼
     AutonomousRePlanningModule
       1. betroffene Aktivitäten finden (draussen, stündliche Prognose im Zeitfenster)
       2. pro Aktivität: verschieben auf trockenen Tag → sonst ersetzen (AlternativeActivitiesModule)
          jede Option wird gegen die Plausibilitätsprüfung probiert, die erste gültige gewinnt
       3. fest Gebuchtes bleibt → Hinweis an James
                    ▼
     StatusManager.apply(ChangeSet)   ── eine SQLite-Transaktion ──
       doppelt? · Version passt? · Operationen gültig? · plausibel?
       → Plan v+1 + Protokoll + Outbox-Aufträge  (oder gar nichts)
                    ▼
     Push-Meldung «05.05. Wanderung Sintra → Kletterhalle» · «Rückgängig» möglich
     Outbox: «Bootstour stornieren?» → erst nach Bestätigung an den Anbieter
```

## Warum «ACID» so umgesetzt ist

- **Atomar:** Alle Operationen eines ChangeSets laufen in einer SQLite-Transaktion. Scheitert eine, bleibt alles beim Alten (getestet, auch mit simuliertem Absturz mitten im Schreiben).
- **Konsistent:** Vor dem Speichern prüft `PlausibilityChecker` den ganzen neuen Plan (Überschneidungen, Tagesgrenzen, fest Gebuchtes unverändert, Budget, Schwangerschafts-Regeln).
- **Isoliert:** `BEGIN IMMEDIATE` + Versionsnummer. Ändert James den Plan, während die Automatik plant, gibt es «Konflikt» → die Automatik plant mit dem neuen Stand neu und überschreibt nichts.
- **Dauerhaft:** SQLite mit WAL und `synchronous=FULL`.
- **Externe Buchungen** lassen sich nicht in eine Datenbank-Transaktion holen. Darum die **Outbox**: Der Auftrag («stornieren», «verlängern») wird zusammen mit der Planänderung gespeichert und erst danach – mit Bestätigung – ausgeführt. «Rückgängig» verwirft offene Aufträge.

## Offene Punkte / Risiken (ehrlich)

- **Hotel-API:** Die Booking.com-Schnittstelle gibt es nur für zugelassene Partner (Affiliate-Programm mit Prüfung). Für eine private App ist das unsicher. Plan B: Suche über einen Anbieter mit offener API oder KI-Websuche + Links zum Buchen von Hand. Muss vor Schritt 4 geklärt werden.
- **Flug-API:** Anbieter, Zugang und Kosten noch offen (Kandidaten prüfen, nichts aus dem Gedächtnis festlegen).
- **Social-Media-Trends:** Instagram/TikTok haben keine offene API dafür; realistisch ist KI-Websuche nach aktuellen Artikeln/Listen.
- **Schwangerschaft:** Die Regeln (`PREGNANCY_AVOID_TAGS`, max. Intensität «mittel») sind Vorgaben, keine medizinische Aussage – mit Ärztin/Hebamme abstimmen; Airlines haben eigene Fristen.
- **Zeitzonen:** Alle Zeiten gelten als Ortszeit am Aufenthaltsort. Bei Flügen über Zeitzonen braucht `TransportSegment` später Zeitzonen-Angaben.

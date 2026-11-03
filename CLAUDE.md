# Kompass (Reise-Assistent) – Hinweise für Claude

Autonomer Reise-Assistent «Kompass» für James und seine Freundin: plant Reisen aus beiden Profilen und passt den Plan
unterwegs selbständig an (Wetter, Ausfälle). Repo `travel-agent`, Image `ghcr.io/jimi90/travel-agent`. Läuft wie Hörwerk
(`jimi90/hoerwerk`) als Docker-Container auf dem Ugreen DXP4800 Plus (UGOS, 8 GB RAM, nur CPU), Zugriff nur über
Tailscale Funnel (`https://kompass.<tailnet>.ts.net`, Container `kompass` + `kompass-tailscale`). Besitzer: James (Schweiz).

## Zusammenarbeit
- Sprache: Deutsch, Schweizer Rechtschreibung (kein ß, «ss»). Oberfläche Deutsch. Bezeichner im Code englisch.
- James ist kein Entwickler: Anleitungen in kleinen Schritten, mit genauen Menünamen.
- Oberfläche: schlicht wie eine Apple-App (Hell/Dunkel, iPhone zuerst, 390 px prüfen).
- Nichts als erledigt melden, was nicht getestet ist. Ehrlich sagen, was nur auf dem NAS/unterwegs prüfbar ist.
- Geheimnisse (`einstellungen.env`, `data/keys.json`) und Daten (`data/`) nie ins Repo. Kosten jeder KI-Aktion sichtbar.
- **Kompass bucht nie selbst.** Alles Externe (Hotel verlängern, umbuchen, stornieren) wird als Auftrag mit Bestätigung
  in die Outbox geschrieben, mit Nachrichten-Entwurf. Keine Buchungs-APIs ohne ausdrücklichen Wunsch.

## Aufbau
- Python 3.12, Pydantic v2, SQLite (`data/kompass.db`), `anthropic`-SDK. Webserver aus der Standardbibliothek
  (`app/server.py`), Anmeldung `app/auth.py` 1:1 aus Hörwerk (Cookie `kompass_sitzung`, CSRF-Header `X-Kompass`,
  Sperren, CSP `img-src 'self' data:` – deshalb liest die Seite Fotos per FileReader als data:-URL, nie blob:). Nicht aufweichen.
- `app/models/` Schemas: `profile.py` (UserProfile, TravelConstraints inkl. Schwangerschaft, TravelHistory, Household),
  `itinerary.py` (Itinerary, DayPlan, Activity, Accommodation, TransportSegment, AlternativeCandidate, HotelSuggestion),
  `disruption.py` (Wetter/Transport/Schliessung), `changes.py` (ChangeSet + Operationen).
- `app/core/status_manager.py` ist die **einzige** Stelle, die Pläne ändert: SQLite-Transaktion (BEGIN IMMEDIATE),
  Versionsnummer, Protokoll (`changes`), Outbox, Rückgängig, `event_id` eindeutig. `operations.py` wendet Operationen auf
  eine Kopie an; `plausibility.py` (MiddlewareConfig) prüft jede Änderung. Strukturprobleme, die vorher schon bestanden,
  sind nur Warnung (sonst würde ein einmal unstimmiger Plan jede Änderung sperren). `acknowledged_locked` erlaubt bewusst
  geänderte feste Punkte (nur mit Auftrag).
- `app/planning/matching.py` Schnittmenge beider Profile (Mittelwert, −2 = Veto, Kompromiss bei grosser Differenz,
  Klima = Überschneidung, strengere Regentoleranz, tiefere Budgetgrenze). `prompt_text()` = was die KI bekommt.
- `destinations.py` Zielvorschläge: KI schlägt vor, Code filtert hart (besuchte Länder, Flugzeit, Umstiege) und bewertet
  das Klima mit echten Messwerten derselben Tage im Vorjahr (Open-Meteo-Archiv).
- `generator.py` Reiseplan: KI liefert `PlanDraft` (strukturierte Ausgabe), `build()` macht daraus einen geprüften Plan:
  Randbedingungen hart (zu anstrengend / ausgeschlossene Merkmale fliegen raus, mit Notiz), Überschneidungen weg,
  Unterkünfte aus Orten+Nächten, Booking.com-/Google-Flights-Suchlinks. Hotels/Flüge sind Vorschläge/Schätzungen.
- `replanning.py` AutonomousRePlanningModule: verschieben auf trockenen Tag → sonst ersetzen; fest Gebuchtes bleibt
  + Hinweis; `handle_closure` für «fällt aus». `alternatives.py` bewertet Ersatz; `providers.py`: zuerst das
  Schlechtwetter-Programm aus dem Plan, sonst KI (30 Tage pro Ort gemerkt).
- `disruption_ext.py` ExtendedDisruptionHandler: Fähre/Flug X Tage aus → `ShiftSchedule` (Tage, Unterkünfte,
  Verbindungen, Reiseende +X, Lücke = Tage am Ort), Unterkunft verlängern (oder neue vormerken), Lücke mit Programm
  füllen, Aufträge für Hotel/Umbuchung/spätere Buchungen. Budget wird dort nur gemeldet, nicht gesperrt.
- `app/weather.py` Open-Meteo (Prognose 16 Tage stündlich, Archiv, Ortssuche), gratis. `app/llm.py` Claude über
  `client.beta.messages.parse` (output_format = Pydantic), `fallbacks="default"` bei Opus/Sonnet 5.5, Websuche optional,
  Kostenbremse `DAILY_BUDGET_USD`, Kosten je Aufruf in `costs`. Modell in den Einstellungen (Standard `claude-opus-5-5`).
- `app/vision.py` Foto → Übersetzung (bei Schwangerschaft Hinweise), `app/drafting.py` Entwürfe (Vorlagen DE/EN ohne KI).
- `app/service.py` Steuerzentrale: Ortszeit über Zeitzone des Tagesorts, Wettercheck, Ausfälle, Hintergrund-Aufträge
  (Planen/Ziele, im Speicher), Zeitplan (jede Minute; Reise «gebucht»/«unterwegs» → ab `check_time` Ortszeit einmal pro
  Tag Wettercheck, Event-ID `wetter:<reise>:<datum>`; setzt Status unterwegs/abgeschlossen).
- `app/store.py` kv-Ablage (Haushalt, Einstellungen, Favoriten, Entwürfe, Ideen), Meldungen, Kosten. RLock (verschachtelt).
- `app/web/index.html` ganze Oberfläche (Vanilla-JS), Leiste: Reisen · Unterwegs · Helfer · Profil; Reise als Vollbild-Blatt.

## Testen
- `python -m unittest -v` (19 Tests, ohne Netz/Kosten; `tests/test_app.py` = ganzer Ablauf über die API im Testmodus).
- App: `APP_PASSWORD=<≥16> FAKE_MODE=1 DATA_DIR=<Ordner> PORT=8000 python -m app.server`. Testwetter:
  `<DATA_DIR>/fake_weather.json` = `{"YYYY-MM-DD": "regen|nachmittag|gewitter|sonne"}`. Testplan ab 5 Tagen mit Fähre.
- Playwright bei 390 px, hell/dunkel. Open-Meteo und Claude sind aus der Cloud nicht erreichbar bzw. ohne Schlüssel:
  dort nur mit nachgebildeten Antworten geprüft. Docker baut nur GitHub.

## Stand (03.11.2026, Version 1.0.0)
- Vollständige App: Profile + Matching, Zielvorschläge mit Klima, Reiseplan mit Hotels/Verbindungen/Schlechtwetter-
  Programm, Favoriten, Morgen-Wettercheck mit Umplanung, Ausfall mehrtägig, «fällt aus», festnageln, als gebucht
  markieren, Aufträge mit Entwürfen, Rückgängig, Foto-Übersetzung, Nachrichten, Kosten. Im Testmodus geprüft.
- Nicht prüfbar aus der Cloud: echte Claude-Antworten (Qualität, Dauer, Kosten pro Plan), echte Open-Meteo-Antworten
  (Format nach Doku nachgebildet), ob iOS beim Foto die Kamera anbietet, Docker-Start auf dem NAS.
- Bewusst nicht gebaut: echte Hotel-/Flug-Verfügbarkeit (Booking.com-API nur für Partner; Flug-APIs offen) → Suchlinks.
  Transport-Ausfälle erkennt Kompass nicht selbst (keine freie Datenquelle) → man meldet sie mit «Fällt aus …».
- Ideen: Push-Mitteilung aufs iPhone bei Umplanung (z. B. ntfy), Tagesvorschau am Vorabend.

# Architektur – Kompass

## Technik

| Teil | Wahl | Warum |
|---|---|---|
| Backend | Python 3.12, Pydantic v2, Webserver aus der Standardbibliothek | Wie Hörwerk; Anmeldung/Sicherheit 1:1 übernommen, wenig RAM. |
| Datenbank | SQLite (`data/kompass.db`) | Echte Transaktionen (ACID) ohne eigenen Server; eine Datei. |
| KI | Claude über das offizielle SDK, strukturierte Ausgaben | Jede Antwort ist ein geprüftes Objekt, nie freier Text. Kosten je Aufruf gezählt, Kostenbremse. |
| Wetter | Open-Meteo | Gratis, ohne Schlüssel: Prognose (stündlich), Klima-Archiv, Ortssuche. |
| Oberfläche | Vanilla-JS, eine Seite, kein Build | iPhone zuerst, Hell/Dunkel. |
| Betrieb | Docker, GitHub Actions (Tests, dann Image), Updater, Tailscale Funnel | Wie Hörwerk. |

## Ordner

```
app/
  models/       Schemas: Profile/Randbedingungen, Reiseplan, Störungen, Änderungen
  core/         StatusManager (einzige Schreibstelle), Operationen, Plausibilitätsprüfung
  planning/     matching, destinations, generator, replanning, alternatives, providers, disruption_ext
  weather.py    Open-Meteo        llm.py      Claude (Kosten, Bremse, Testmodus)
  vision.py     Foto-Übersetzung (nicht in der Oberfläche)  drafting.py Nachrichten-Entwürfe  fx.py Währungskurse
  service.py    Steuerzentrale + Morgen-Zeitplan
  store.py      Ablage (Haushalt, Einstellungen, Meldungen, Kosten, Favoriten, Entwürfe)
  server.py     Routen        auth.py  Anmeldung        web/  Oberfläche
tests/          Modelle/Umplanung (18) + ganzer Ablauf über die API (1)
```

## Abläufe

**Reise planen:** Profil beider Personen → Matching (Schnittmenge, Vetos, Randbedingungen) → optional Zielvorschläge
(KI, danach harter Filter + Klima aus echten Vorjahreswerten) → KI-Entwurf (Tage, Orte, Hotels, Verbindungen,
Schlechtwetter-Programm) → Code prüft und setzt Randbedingungen durch → Plan im StatusManager.

**Jeden Morgen (Reise «gebucht»/«unterwegs»):** Zeitplan → Wetter am heutigen Ort (Ortszeit) → betroffene
Draussen-Programmpunkte → verschieben auf trockenen Tag (mit Ersatz in der Lücke) oder wetterfest ersetzen →
**ein** ChangeSet → Meldung. Fest Gebuchtes bleibt, Kompass gibt einen Hinweis.

**Fähre/Flug fällt X Tage aus:** gemeldet in der App → ein ChangeSet: Plan ab Abfahrtstag +X, Unterkunft verlängert
(oder neue vorgemerkt), gewonnene Tage mit Programm, Aufträge (Hotel verlängern, umbuchen, spätere Buchungen
verschieben) mit Nachrichten-Entwurf.

## Fehlersicherheit (ACID)
- **Atomar:** alle Operationen einer Anpassung in einer SQLite-Transaktion – ganz oder gar nicht (getestet, auch Absturz).
- **Konsistent:** Plausibilitätsprüfung vor jedem Speichern (Überschneidungen, Tagesgrenzen, Unterkünfte, Reihenfolge der
  Verbindungen, fest Gebuchtes, Budget, Schwangerschaft).
- **Isoliert:** Versionsnummer + `BEGIN IMMEDIATE`; bei gleichzeitiger Änderung plant die Automatik mit dem neuen Stand neu.
- **Dauerhaft:** WAL + `synchronous=FULL`. Externe Schritte über die Outbox, nie direkt.

## Grenzen (ehrlich)
- Hotels und Flüge: KI-Vorschläge und Schätzungen mit Suchlinks; keine echte Verfügbarkeit, nichts wird gebucht.
- Transport-Ausfälle muss man melden (keine freie Datenquelle für Fähren/Flüge).
- Schwangerschafts-Regeln sind Vorgaben, keine medizinische Aussage.

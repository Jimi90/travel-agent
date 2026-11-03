# Reise-Assistent – Hinweise für Claude

Persönlicher Reise-Assistent (James + Partnerin). Läuft wie Hörwerk (`jimi90/hoerwerk`) als Docker-Container
auf dem Ugreen DXP4800 Plus (UGOS, 8 GB RAM, nur CPU), Zugriff nur über Tailscale Funnel. Besitzer: James (Schweiz).

## Zusammenarbeit
- Sprache: Deutsch, Schweizer Rechtschreibung (kein ß, «ss»). Oberfläche Deutsch. Bezeichner im Code englisch.
- James ist kein Entwickler: Anleitungen in kleinen Schritten, mit genauen Menünamen.
- Oberfläche: schlicht wie eine Apple-App (Hell/Dunkel, iPhone zuerst, 390 px).
- Nichts als erledigt melden, was nicht getestet ist. Ehrlich sagen, was nur auf dem NAS/unterwegs prüfbar ist.
- Geheimnisse (`einstellungen.env`, API-Schlüssel) und Daten (`data/`) nie ins Repo. Kosten sichtbar machen.
- Geld/Buchungen nie ohne Bestätigung: externe Aktionen laufen über die Outbox (`ExternalAction.needs_confirmation`).

## Aufbau (siehe docs/architektur.md)
- Python 3.12, Pydantic v2, SQLite. Später FastAPI + Vanilla-JS-PWA (kein Build), Sicherheit wie Hörwerk.
- `app/models/` Schemas. `app/core/status_manager.py` ist die **einzige** Stelle, die Pläne ändert
  (Transaktion, Versionsnummer, Protokoll, Outbox, Rückgängig). `app/core/plausibility.py` = MiddlewareConfig.
- `app/planning/replanning.py` AutonomousRePlanningModule: verschieben auf trockenen Tag → sonst ersetzen;
  fest Gebuchtes (`fixed` oder nicht stornierbar) bleibt + Hinweis. Ein ChangeSet pro Störung, `event_id` verhindert Doppelungen.
- Schwangerschafts-Regeln in `profile.PREGNANCY_AVOID_TAGS` sind Vorgaben, keine medizinische Aussage.

## Testen
- `python -m unittest -v` (18 Tests, ohne Netz, ohne Kosten).

## Stand (03.11.2026)
- Schritt 1 fertig: Modelle, StatusManager, Umplanung bei Schlechtwetter. Getestet: alles-oder-nichts, Absturz
  mitten in der Transaktion, Konflikt + Neuplanung, Doppel-Ereignis, Rückgängig, stündliche Prognose,
  Schwangerschaft, Veto, Budget, Verschieben auf trockenen Tag. Echte Wetterdaten noch nicht angebunden.
- Nächste Schritte: 2) FastAPI + Anmeldung + Docker/Funnel wie Hörwerk, 3) AutonomousWeatherCheck (Open-Meteo)
  + ExtendedDisruptionHandler, 4) Hotel-/Flug-Anbindung (Anbieter erst klären), 5) Vision/Übersetzung, Entwürfe.

# Kompass (Reise-Assistent) – Hinweise für Claude

Persönlicher Reise-Assistent «Kompass» (James + Partnerin; Name von James gewählt, Repo heisst weiter `travel-agent`, Image `ghcr.io/jimi90/travel-agent`). Läuft wie Hörwerk (`jimi90/hoerwerk`) als Docker-Container
auf dem Ugreen DXP4800 Plus (UGOS, 8 GB RAM, nur CPU), Zugriff nur über Tailscale Funnel. Besitzer: James (Schweiz).

## Zusammenarbeit
- Sprache: Deutsch, Schweizer Rechtschreibung (kein ß, «ss»). Oberfläche Deutsch. Bezeichner im Code englisch.
- James ist kein Entwickler: Anleitungen in kleinen Schritten, mit genauen Menünamen.
- Oberfläche: schlicht wie eine Apple-App (Hell/Dunkel, iPhone zuerst, 390 px).
- Nichts als erledigt melden, was nicht getestet ist. Ehrlich sagen, was nur auf dem NAS/unterwegs prüfbar ist.
- Geheimnisse (`einstellungen.env`, API-Schlüssel) und Daten (`data/`) nie ins Repo. Kosten sichtbar machen.
- Geld/Buchungen nie ohne Bestätigung: externe Aktionen laufen über die Outbox (`ExternalAction.needs_confirmation`).

## Aufbau (siehe docs/architektur.md)
- Python 3.12, Pydantic v2, SQLite (`data/kompass.db`). Webserver aus der Standardbibliothek (`app/server.py`), Anmeldung
  `app/auth.py` 1:1 aus Hörwerk (Cookie `kompass_sitzung`, CSRF-Header `X-Kompass`, Sperren, CSP). Nicht aufweichen.
- `app/demo.py` Demo-Reise (beginnt immer morgen, «jetzt» = Tag 2, 07:00), 4 Wetter-Lagen, Schalter Schwangerschaft
  und «Folgetag trocken». Adresse `https://kompass.<tailnet>.ts.net`, Container `kompass` + `kompass-tailscale`. Symbol `app/web/icon.svg`, `apple-touch-icon.png` ohne runde Ecken (Playwright: SVG direkt öffnen, `rx=0`). Oberfläche `app/web/index.html` (Vanilla-JS, kein Build). «Demo zurücksetzen» löscht `kompass.db`.
- `app/models/` Schemas. `app/core/status_manager.py` ist die **einzige** Stelle, die Pläne ändert
  (Transaktion, Versionsnummer, Protokoll, Outbox, Rückgängig). `app/core/plausibility.py` = MiddlewareConfig.
- `app/planning/replanning.py` AutonomousRePlanningModule: verschieben auf trockenen Tag → sonst ersetzen;
  fest Gebuchtes (`fixed` oder nicht stornierbar) bleibt + Hinweis. Ein ChangeSet pro Störung, `event_id` verhindert Doppelungen.
- Schwangerschafts-Regeln in `profile.PREGNANCY_AVOID_TAGS` sind Vorgaben, keine medizinische Aussage.

## Testen
- `python -m unittest -v` (18 Tests, ohne Netz, ohne Kosten). GitHub Actions führt sie bei jedem Push aus.
- App: `APP_PASSWORD=<mind. 16 Zeichen> DATA_DIR=<Ordner> PORT=8000 python -m app.server`, dann Playwright bei 390 px, hell/dunkel.
- Docker läuft in der Cloud-Umgebung nicht; das Image baut nur GitHub (Push auf `main` → `ghcr.io/jimi90/travel-agent`).

## Stand (03.11.2026)
- Schritt 1 fertig: Modelle, StatusManager, Umplanung bei Schlechtwetter. Getestet: alles-oder-nichts, Absturz
  mitten in der Transaktion, Konflikt + Neuplanung, Doppel-Ereignis, Rückgängig, stündliche Prognose,
  Schwangerschaft, Veto, Budget, Verschieben auf trockenen Tag. Echte Wetterdaten noch nicht angebunden.
- Schritt 2 (0.2.0): Test-Oberfläche + Anmeldung + Docker/Funnel. Lokal geprüft (Anmeldung, CSRF, Sperre falsches PW,
  Wettercheck, Rückgängig, Zurücksetzen, 390 px hell/dunkel). Nicht geprüft: Docker-Bau und Start auf dem NAS, Funnel.
- Nächste Schritte: 3) AutonomousWeatherCheck (Open-Meteo)
  + ExtendedDisruptionHandler, 4) Hotel-/Flug-Anbindung (Anbieter erst klären), 5) Vision/Übersetzung, Entwürfe.

# Kompass (Repo travel-agent)

Autonomer Reise-Assistent für zwei Personen: plant Reisen aus beiden Profilen und passt den Plan unterwegs selbständig
an – bei Regen wird der Tag umgestellt, fällt eine Fähre aus, verlängert Kompass die Unterkunft und schiebt alle
Folgetage nach hinten. Läuft wie Hörwerk als Docker-Container auf dem NAS, erreichbar über Tailscale Funnel.

Details: [docs/architektur.md](docs/architektur.md).

## Tests

```bash
pip install -r requirements.txt
python -m unittest -v
```

## Auf dem NAS einrichten (wie Hörwerk)

1. Ordner `docker/kompass` anlegen, darin `docker-compose.yaml` und `tailscale-serve.json` aus diesem Repo.
2. `einstellungen.env` (aus `einstellungen.env.beispiel`: Passwort ≥ 16 Zeichen, optional Claude-Schlüssel) und
   `tailscale.env` (aus `tailscale.env.beispiel`, eigener Tailscale-Schlüssel) daneben legen.
3. UGOS → Docker → Projekt → Erstellen, Pfad `docker/kompass` wählen, starten.
4. Aufrufen: `https://kompass.<dein-tailnet>.ts.net`

# Kompass (Repo travel-agent)

Persönlicher Reise-Assistent für James und Partnerin: plant Reisen nach beiden Profilen und stellt
unterwegs den Tagesplan bei Schlechtwetter oder Ausfällen selbständig um. Läuft wie Hörwerk als
Docker-Container auf dem NAS, erreichbar über Tailscale Funnel.

Stand 0.2.0: Testversion mit Demo-Reise – Schlechtwetter per Knopf auslösen und zusehen, wie der Plan
umgestellt wird. Noch keine echten Wetterdaten, keine Buchungen. Details: [docs/architektur.md](docs/architektur.md).

## Tests

```bash
pip install -r requirements.txt
python -m unittest -v
```

## Auf dem NAS einrichten (wie Hörwerk)

1. Ordner `docker/kompass` anlegen, darin `docker-compose.yaml` und `tailscale-serve.json` aus diesem Repo.
2. `einstellungen.env` (aus `einstellungen.env.beispiel`, eigenes Passwort ≥ 16 Zeichen) und
   `tailscale.env` (aus `tailscale.env.beispiel`, neuer Tailscale-Schlüssel) daneben legen.
3. UGOS → Docker → Projekt → Erstellen, Pfad `docker/kompass` wählen, starten.
4. Aufrufen: `https://kompass.<dein-tailnet>.ts.net`

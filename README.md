# Reise-Assistent (travel-agent)

Persönlicher Reise-Assistent für James und Partnerin: plant Reisen nach beiden Profilen und stellt
unterwegs den Tagesplan bei Schlechtwetter oder Ausfällen selbständig um. Läuft wie Hörwerk als
Docker-Container auf dem NAS, erreichbar über Tailscale Funnel.

Stand: Schritt 1 – Datenmodelle, StatusManager (atomare Planänderungen), automatische Umplanung bei
Schlechtwetter. Noch keine Web-Oberfläche, kein Docker. Details: [docs/architektur.md](docs/architektur.md).

## Tests

```bash
pip install -r requirements.txt
python -m unittest -v
```

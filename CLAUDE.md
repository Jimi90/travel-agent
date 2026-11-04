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
  Seit 1.7.0 Reichweite nach Reisedauer (`matching.reach_for`, `REACH_TABLE`; James: «2–3 Tage: max. 2 Std. Flug oder
  5 Std. Auto»): ≤3 Tage 2 Std. Flug direkt / 5 Std. Zug-Auto / 1 Ort, ≤6: 4 Std./1 Umstieg/7 Std./2 Orte, ≤13: 9 Std./3 Orte,
  länger: Profil-Grenze; strengere Profil-Grenzen gelten immer. Ideen haben `travel_mode` (flug|zug|auto) + `travel_hours`,
  gefiltert im Code. Budget (seit 1.7.1, Wunsch James): `budget_per_day_max` = **vor Ort** pro Tag (Unterkunft, Essen,
  Programm) × Tage + Anreisebudget dieser Reise, höchstens `budget_total_max` (optionaler Deckel) → `budget_for(days, travel)`.
  Das Anreisebudget (hin und zurück für beide) wählt James **im Formular «Neue Reise»** («Anreise bis», Vorschlag je Dauer
  aus `travel_budget` = 200/400/900/1800 für kurztrip/kurzurlaub/ferien/lang, `TRIP_CATEGORIES`; eigener Wert bleibt beim
  Ändern der Dauer; in der Oberfläche heisst es «Hin- und Rückreise», nicht «Anreise» – Wunsch James); es geht als `travel_budget` an Zielvorschläge/Varianten/Plan und bleibt am Plan (`Itinerary.travel_budget`,
  `budget_for_trip(it)` für Plausibilität, Umplanung, Ausfall, finalen Vorschlag). Nicht in den Einstellungen (Wunsch James).
  Ideen mit `travel_cost_chf` > Anreisebudget × 1.2 fallen weg (mit Grund); Ideen im Budget zuerst.
  Varianten: höchstens `reach.max_bases` Orte (Rest wird gekürzt), Warnung «Weit für X Tage» per Luftlinie ab Heimatflughafen.
- `variants.py` (1.5.0) Routen-Varianten: Ziel → KI liefert 3 Routen (Orte + Nächte, Stichpunkte, Wassertemperatur,
  Kosten Flüge/Unterkünfte/Programm/Transfers) → Code gleicht Nächte ab, ergänzt echtes Wetter (Archiv, grösster Ort),
  Distanz (Haversine), Foto. Gemerkt in kv «variants»; `POST /api/trips {variant_set, index}` plant genau diese Route
  (`generator.generate(route=…)`, Abweichung → Notiz). Ablauf in der App: Ziel (oder Zielvorschlag «Routen ansehen»)
  → Varianten → Plan → Hotel je Ort wählen (3 Preisklassen `guenstig|mittel|gehoben` in `HotelSuggestion.tags`,
  `choose-hotel` nimmt Index in `hotel_options`, exakter Ortsname vor ungefährem) → «Finaler Vorschlag» mit Kosten.
- `app/hotels.py` (1.6.0) Hotelvorschläge mit echten Preisen + 3–4 Fotos: je Vorschlag eine SerpApi-Suche «Name Ort»
  für genau die Nächte, im Hintergrund (`enrich`, max. 15 Suchen pro Lauf) nach dem Planen bzw. wenn etwas fehlt;
  dauerhaft gemerkt (kv «hotel_live», auch «nicht gefunden»). `same_hotel()`: Treffer nur übernehmen, wenn der Name passt
  (Ortswörter zählen nicht; bei ≥ 2 bezeichnenden Wörtern müssen 2 übereinstimmen – sonst wäre «Rex Hotel Saigon» =
  «Liberty … Saigon»). «Hotel wählen» nimmt den echten Preis, sonst die Schätzung; «Preise aktualisieren» = `hotel-prices`.
  Ohne SerpApi: Foto per Wikipedia, keine Preise. Fotos von `.serpapi.com`/`.googleusercontent.com` erlaubt.
- `app/tiles.py` (1.5.0) OpenStreetMap-Kacheln über `/api/tile/z/x/y` (eigener Absender, max. 2 gleichzeitig, 30 Tage
  Cache in `data/tiles/`, Nutzungsrichtlinie OSM), Karte im Browser: Kacheln + SVG-Route (`mapHtml`/`drawMaps`),
  «© OpenStreetMap» auf jeder Karte. tile.openstreetmap.org aus der Cloud nicht prüfbar.
- `generator.py` Reiseplan: KI liefert `PlanDraft` (strukturierte Ausgabe), `build()` macht daraus einen geprüften Plan:
  Randbedingungen hart (zu anstrengend / ausgeschlossene Merkmale fliegen raus, mit Notiz), Überschneidungen weg,
  Unterkünfte aus Orten+Nächten, Booking.com-/Google-Flights-Suchlinks. Hotels/Flüge sind Vorschläge/Schätzungen.
- `replanning.py` AutonomousRePlanningModule: verschieben auf trockenen Tag → sonst ersetzen; fest Gebuchtes bleibt
  + Hinweis; `handle_closure` für «fällt aus». `alternatives.py` bewertet Ersatz; `providers.py`: zuerst das
  Schlechtwetter-Programm aus dem Plan, sonst KI (30 Tage pro Ort gemerkt).
- `disruption_ext.py` ExtendedDisruptionHandler: Fähre/Flug X Tage aus → `ShiftSchedule` (Tage, Unterkünfte,
  Verbindungen, Reiseende +X, Lücke = Tage am Ort), Unterkunft verlängern (oder neue vormerken), Lücke mit Programm
  füllen, Aufträge für Hotel/Umbuchung/spätere Buchungen. Budget wird dort nur gemeldet, nicht gesperrt.
- `app/weather.py` Open-Meteo (Prognose 16 Tage stündlich, Archiv, Ortssuche), gratis.
- `app/llm.py` (seit 1.2.0) drei Anbieter, `route(purpose)`: grosse Aufgaben (`HEAVY`: reiseplan, zielvorschlaege,
  bild_uebersetzen) Gemini → Groq → Claude, kleine Groq → Gemini → Claude; Einstellung `prefer` = gratis|claude.
  Bilder nur an Anbieter mit `vision` (Groq nicht). Gemini/Groq über OpenAI-kompatible Schnittstelle (`config.FREE_PROVIDERS`,
  wie Hörwerk; eigener `User-Agent` wegen Cloudflare/Groq – nicht entfernen), `response_format: json_object` + Schema im
  Systemtext, Antwort per Pydantic geprüft, einmal Korrektur mit Fehlerhinweis; unbekannte Felder (response_format,
  reasoning_effort) werden bei 400 weggelassen. Limit/Fehler → nächster Anbieter (`_Skip`), Meldung nennt jeden Grund.
  429 (seit 1.4.1, `_limit_info`): Google schreibt bei JEDEM Limit «quota» – massgebend ist die Kennung (…PerDay… vs.
  …PerMinute…); Minutenlimit → `retryDelay` abwarten (≤ 65 s, höchstens 2×) und denselben Anbieter nochmals fragen;
  «limit: 0» = Modell ohne Gratis-Kontingent. Groq: `tpm` 8000 (Frage + Antwort) – zu grosse Aufgaben (Antwort müsste
  um > 2/3 gekürzt werden, z. B. ganzer Reiseplan) gehen gar nicht erst an Groq. Jeder Anbieterfehler mit Rohtext im
  Container-Protokoll («[KI] … HTTP …»). Gemini-Modell in Profil → KI wählbar (`settings.gemini_model`; Limits gelten
  pro Projekt UND Modell). Tageslimit-Hinweis: eigener Gemini-Schlüssel/Projekt für Kompass, nicht den von Hörwerk teilen.
  Claude über `client.beta.messages.parse`, `fallbacks="default"` bei Opus/Sonnet 5.5, Websuche nur Claude, Kostenbremse
  nur für Claude. Schlüssel: `store.key(provider)`, `data/keys.json` (chmod 600) vor `.env`; `key_info()` nur letzte 4 Zeichen.
- `app/bookings.py` (1.3.0) Buchungsbestätigung (Foto oder Mailtext) → KI liest `BookingExtract` → `propose()` ordnet
  im Code zu (gleiche Art + Datum + ähnlicher Ort/Name/IATA, sonst neu) → Vorschau mit Plausibilitätsprüfung, gemerkt
  unter Kennung (`store` «import») → erst «Übernehmen» schreibt ein ChangeSet (`UpdateStay`/`UpdateTransport`/`AddTransport`/
  `AddStay`/`AddActivity`). Fremdwährung nur als Hinweis (kein falscher CHF-Betrag).
- `app/prices.py` (1.3.0) SerpApi: `google_flights` (type=2 einfach, adults=2, CHF, gl=ch, stops nach Randbedingung) und
  `google_hotels`; Feldnamen aus Doku/Fremdprojekten (serpapi.com aus der Cloud gesperrt). 6 Std. Zwischenspeicher, jede
  Suche als Kosten-Zeile `model=serpapi` gezählt (Gratis 250/Monat). Übernehmen nur per gemerkter Kennung + Index, nie mit
  Daten aus dem Browser; übernommene Preise bleiben «Vorschlag». Flughafen-Codes an `TransportSegment.origin_code/destination_code`.
- `app/photos.py` (1.4.0) Fotos: Wikipedia-Suche (`generator=search`, `prop=pageimages`, `pilicense=free`, de vor en),
  Titel muss ein Wort des Suchbegriffs enthalten (sonst kein Bild); Allgemeines («Abendessen», «Ankunft» …, `GENERIC`)
  bekommt keins. Hotels: Bild aus Google Hotels (SerpApi `images[].thumbnail`). Der Server lädt selbst (nur https von
  `ALLOWED_HOSTS`, auch bei Weiterleitungen, ≤ 2 MB, JPEG/PNG/WebP) nach `data/photos/`, Auslieferung `/api/photo/<id>`
  (30 Tage zwischenspeichern). CSP bleibt `img-src 'self' data:` – nie lockern. Zuordnung pro Reise in kv «trip_photos»
  (Orte, Programmpunkte nach Titel, Unterkünfte); `fill_trip` im Hintergrund nach dem Planen und wenn etwas fehlt.
  Titelbild mit Quellenangabe (Wikipedia-Lizenzen). Wikipedia aus der Cloud gesperrt: nur mit nachgebildeten Antworten geprüft.
  Seit 1.6.0: KI nennt pro Programmpunkt `wikipedia` (→ `Activity.wiki`, Foto per Titel mit Weiterleitungen), Suche prüft
  ALLE Treffer (vorher nur den ersten – meist der Stadtartikel → kein Bild), dann «Titel» ohne Ort. Tagesplan: Bildleiste
  je Tag (`.strip`), Foto antippen = Grossansicht (`#lightbox`, Klick-Abfang in der Capture-Phase, damit kein Hotel gewählt wird).
- `app/vision.py` Foto → Übersetzung (bei Schwangerschaft Hinweise; seit 1.7.3 nicht mehr in der Oberfläche), `app/drafting.py` Entwürfe (Vorlagen DE/EN ohne KI).
- `app/flights.py` (1.8.0) automatische echte Flugangebote (SerpApi), siehe «Stand».
- `app/fx.py` (1.7.3) Währungskurse für den Helfer-Rechner, siehe «Stand».
- `app/service.py` Steuerzentrale: Ortszeit über Zeitzone des Tagesorts, Wettercheck, Ausfälle, Hintergrund-Aufträge
  (Planen/Ziele, im Speicher), Zeitplan (jede Minute; Reise «gebucht»/«unterwegs» → ab `check_time` Ortszeit einmal pro
  Tag Wettercheck, Event-ID `wetter:<reise>:<datum>`; setzt Status unterwegs/abgeschlossen).
- `app/store.py` kv-Ablage (Haushalt, Einstellungen, Favoriten, Entwürfe, Ideen), Meldungen, Kosten. Eine SQLite-Verbindung
  für alle Threads: JEDER Zugriff (auch Lesen) unter einer RLock (`_LockedDB`); StatusManager ebenso (`_ex`). Ohne das
  gab es sporadisch «InterfaceError: bad parameter or other API misuse» (Foto-Thread + Webseite gleichzeitig).
- `app/web/index.html` ganze Oberfläche (Vanilla-JS), Leiste: Reisen · Unterwegs · Helfer · Profil; Reise als Vollbild-Blatt.
  Profil (seit 1.1.0) wie die iPhone-Einstellungen: Hauptliste mit Wert rechts + «›», Unterseiten im Blatt `#subSheet`
  (`openSub(page)`, `PAGES`, `personPage(i)`), Auswahl per Aktionsblatt, Häkchen-Listen, automatisch speichern
  (`saveSoon`, HH bleibt lokale Wahrheit). Besuchte Länder: Liste aller Länder mit Flagge, Suche Deutsch/Englisch/Code,
  Filter «Alle/Ausgewählt»; Namen per `Intl.DisplayNames` (kein Länder-Wörterbuch im Code nötig).

## Testen
- `python -m unittest -v` (67 Tests inkl. Varianten/Hotelwahl/Kacheln im Ende-zu-Ende-Test, ohne Netz/Kosten; `tests/test_app.py` = ganzer Ablauf über die API im Testmodus).
- App: `APP_PASSWORD=<≥16> FAKE_MODE=1 DATA_DIR=<Ordner> PORT=8000 python -m app.server`. Testwetter:
  `<DATA_DIR>/fake_weather.json` = `{"YYYY-MM-DD": "regen|nachmittag|gewitter|sonne"}`. Testplan ab 5 Tagen mit Fähre.
- Playwright bei 390 px, hell/dunkel. Open-Meteo und Claude sind aus der Cloud nicht erreichbar bzw. ohne Schlüssel:
  dort nur mit nachgebildeten Antworten geprüft. Docker baut nur GitHub.

## Stand (04.11.2026, Version 1.8.3)
- 1.8.3: James: Bildschirm zoomt beim schnellen Tippen auf «+» (Tage). Ursache: iOS-Doppeltipp-Zoom. `touch-action:
  manipulation` auf html und allen Bedienelementen (Pinch-Zoom bleibt möglich). Zoom-Verhalten nur auf dem iPhone prüfbar.
- 1.8.2: James: im Tagesplan fehlten Fotos oft (Sharm el Sheikh Tag 3–7). `activity_photo` versucht zuletzt Commons
  (`commons_photo(Titel Ort)`), die Tageszeile zeigt sonst das Ortsfoto (`photos.places`), `PHOTO_VERSION` 3 → leere
  Zuordnungen älterer Reisen werden neu geholt (Hotelfotos bleiben). 66 Tests; echte Treffer nur auf dem NAS prüfbar.
- 1.8.1: Wunsch James: Zielkarten ohne Bilder, Infos schwer lesbar; auf einen Blick Wetter + Wassertemperatur (Vorbild tui.ch).
  Fotos: `photos.commons_photo` (Wikimedia Commons, Dateisuche, nur JPEG-Querformat, Titel muss Ortswort enthalten, Cache
  «commons1:») ist jetzt Teil von `destination_photo` (Wahrzeichen → Highlights → Commons → Wikipedia) und von
  `place_photo` (Orte im Plan, erstes Bild der Varianten). Karte: drei Kacheln Wetter (Ø max/min + Regen % aus dem Vorjahr,
  `item["climate"]`) · Wasser (`weather.sea_temperature`, Open-Meteo Marine, Vorjahr; sonst KI `water_temp_c` = «geschätzt»)
  · Anreise (Dauer, Art, Kosten hin/zurück). 66 Tests; Testmodus 390 px hell/dunkel geprüft; echte Commons-Treffer und
  Marine-Daten nur auf dem NAS prüfbar. Alte Favoriten haben die neuen Felder nicht → Vorschläge neu laden.
- 1.8.0: Wunsch James: Tagespläne «mager», Bilder passten nicht («Abendessen» zeigte Tacos), Flüge ohne konkrete Zahlen
  trotz hinterlegter API, «Wetter jetzt prüfen» unsinnig, auf der Reise «welcher Tag, welches Wetter». Neu:
  (1) Prompt `generator.SYSTEM`: 4–6 Punkte pro Tag, konkrete Namen, `note` = 2–3 Sätze mit Tipps, `theme` = Tagesmotto
  (neu `DayPlan.theme`, im Tagesplan «Tag N», Motto, Beschreibung in `.desc`); `wikipedia` nur für Orte/Sehenswürdigkeiten,
  nie Essen. Fotos: `photos.has_photo_subject` – Kategorie «kulinarik» bekommt kein Foto (auch nicht im Streifen/Titelbild);
  `wiki_photo(strict=True)` für Programmpunkte (zwei gemeinsame Wörter, bester Treffer; Cache «wiki3:»).
  (2) `app/flights.py`: echte Flugangebote automatisch im Hintergrund (SerpApi, wie `hotels.py`; nur geschätzte Flüge mit
  Codes, Zukunft ≤ 330 Tage, Neuabruf alle 2 Tage, Fehler 6 Std. gemerkt, max. 6 Suchen/Lauf, kv «flight_live»);
  `trip_detail.flight_live`; Zeile zeigt Zeit, Dauer, direkt/via, Airline, Preis; Antippen → «Diesen Flug übernehmen»
  (`prices/apply` mit gemerktem Token), «Alle Flüge ansehen». Übernehmen bleibt Handarbeit, der Plan ändert sich nie ungefragt.
  (3) «Wetter jetzt prüfen» in Reiseblatt und «Unterwegs» entfernt (Zeitplan prüft automatisch; Endpunkt bleibt).
  (4) `server.live_day` + `liveCard()`: auf der Reise oben im Reiseblatt und im Tab «Unterwegs» die Karte «Heute · Tag N von M»
  mit Ort, Vorhersage genau für diesen Ort (Symbol, Min/Max, Regen, Wind, 3-Stunden-Verlauf, Warnungen) und Tagesmotto.
  Alte Pläne bleiben dünn (Inhalt kommt von der KI beim Planen) → neu planen. Im Testmodus 390 px hell/dunkel geprüft;
  echte Flugdaten, Wetter und Fotos nur auf dem NAS prüfbar.
- 1.7.7: Wunsch James: Reiseblatt «schöner und übersichtlicher, wie von Apple». Neu: Titelbild mit Titel/Datum darauf,
  drei Kennzahlen (Dauer/Orte/Kosten), Status + Wetter/Import als Listenzeilen, Tage einzeln aufklappbar (heute bzw.
  erster Tag offen, `OPEN_DAYS`), Unterkünfte als Karte (Preis + Status rechts, Hotelwahl kompakt, Fusszeile Booking.com ·
  Andere Hotels · Gebucht), Verbindungen als antippbare Zeilen (Optionen im Aktionsblatt: Flüge suchen, Echte Preise,
  gebucht, «Fällt aus …»), Finaler Vorschlag nur Route + Kosten, «So geht's weiter» nur im Entwurf, Verlauf eingeklappt.
  Behoben: Titelbild war das Cancún-Wappen (Altbestand vor dem Bildfilter) → `photos.PHOTO_VERSION` 2 verwirft ältere
  Ort-/Programmfotos (Hotelfotos bleiben), Cache-Schlüssel `wiki2:`/`title2:`, Titelbild zuerst aus Programmpunkt-Fotos.
  Preis «CHF 1 für eure Nächte»: SerpApi liest «1’600» (Schweizer Strich) als 1 → `prices.money` liest den Text,
  `prices.stay_total` ersetzt unplausible Summen durch Nacht × Nächte (auch für schon gemerkte Preise). 64 Tests;
  Testmodus 390 px hell/dunkel geprüft, echte Fotos/Preise nur auf dem NAS prüfbar.
- 1.7.6: James: «Das stimmt ja jetzt gar nicht mehr» – Karten zeigten falsche Länder/Flaggen (Cancún «Montenegro», Dakar
  «Schweden»), ein besuchtes Land wurde trotzdem vorgeschlagen, Sal zeigte einen Baum. Ursache: `country[:2]` kürzte den
  von der KI gelieferten Landesnamen («MEXICO» → «ME»). Jetzt `destinations.resolve_country`: KI liefert `country` (ISO)
  + `country_name` (Deutsch); Code gleicht mit der Open-Meteo-Ortssuche ab (Ort, Landesname), bei Widerspruch gewinnt der
  Landesname, sonst leer (dann keine Flagge). Der Besucht-Filter nutzt den geprüften Code. Anzeige: Landesname aus dem
  Code (`Intl.DisplayNames`). Fotos: Wahrzeichen → Suche → Highlights → Ortsname nur bei ≥ 5 Buchstaben (kein «Sal»-Baum).
  60 Tests; echte KI-/Ortssuche-Antworten nur auf dem NAS prüfbar.
- 1.7.5: Wunsch James: Routen-Varianten waren «zu klein, zu viel, nicht strukturiert». Jetzt je Route eine Karte: grosse
  Karte oben (250 px, Nummern = Liste), Name + Preis («für 2 Personen»), ein Satz, **nummerierte Orte** mit Nächten und
  kurzer Notiz (`Stop.note`), Kacheln Wetter/Meer/Strecke aus `Variant`-`facts` (statt Text in `bullets`), höchstens 3
  Stichpunkte, Kosten eingeklappt («Kosten im Detail»). Die KI soll in `bullets`/`note` keine Kosten/Wetter/Distanzen
  schreiben (Prompt). Alte gemerkte Varianten ohne `facts` zeigen weiter alle Stichpunkte. Dabei gefunden: Die KI-
  Koordinaten waren falsch (Marker in Panama/Costa Rica statt Kolumbien, «San José de Cúcuta» → Costa Rica) → `variants._locate`
  überschreibt sie per Open-Meteo-Ortssuche **im angegebenen Land** (`weather.geocode(name, country)` mit `countryCode`;
  Klammern weg, bei «… de X» auch «X»); findet die Suche nichts, bleiben die KI-Werte. Im Testmodus bei 390 px
  hell/dunkel geprüft; echte Ortssuche und Kartenkacheln nur auf dem NAS prüfbar.
- 1.7.4: Wunsch James: Zielvorschläge «auf den ersten Blick» – grosses ikonisches Bild, darauf Ort, Flagge + Land + Region
  und der Preis (ganze Reise, 2 Personen), darunter Highlights als Chips, Anreise, Klima, Begründung. Das Bild kommt
  nicht mehr vom Stadtartikel (zeigte Siegel/Baum: «San Jose», «Sal»), sondern von `DestinationIdea.landmark_wiki`
  (Wikipedia-Titel eines Wahrzeichens/einer Landschaft, von der KI) → `photos.destination_photo`: Titel → Suche danach →
  zuletzt Ortsname. `photos.bad_image` sortiert Wappen, Siegel, Flaggen, Logos, Karten, SVG aus (gilt für alle
  Wikipedia-Fotos). Land = `Intl.DisplayNames` (Code → Name), Region von der KI. Im Testmodus bei 390 px hell/dunkel
  geprüft (Platzhalterbilder); ob die KI brauchbare Wahrzeichen-Titel liefert und Wikipedia dazu ein Bild hat,
  nur auf dem NAS prüfbar. Alte gemerkte Favoriten haben kein `landmark_wiki` (nur Ortsname-Suche).
- 1.7.3: Wunsch James: im Helfer ersetzt ein **Währungsrechner** den Block «Speisekarte & Schilder» (Betrag, Von/Nach,
  Tauschen, merkt die letzte Wahl im Browser). Kurse: `app/fx.py` holt `open.er-api.com/v6/latest/CHF` (gratis, ohne
  Schlüssel, 1× täglich neu, ~160 Währungen inkl. VND/THB/AED/QAR), 6 Std. im Speicher, dauerhaft in kv «fx» → ohne Netz
  gelten die zuletzt gespeicherten Kurse mit Hinweis; `GET /api/fx`. Quellenhinweis «Rates By Exchange Rate API» (Pflicht
  der Nutzungsbedingungen) steht unter dem Rechner – nicht entfernen. Tab-Symbol Helfer: Münze statt Kamera.
  `/api/vision` + `app/vision.py` + Tests bleiben im Code (nur aus der Oberfläche entfernt), `shrink()` wird vom
  Buchungsimport weiter gebraucht. Im Testmodus bei 390 px hell/dunkel geprüft (46 Tests); echte Kurse vom Dienst nur auf
  dem NAS prüfbar (open.er-api.com aus der Cloud gesperrt).
- 1.7.2: Texte «Anreise» → «Hin- und Rückreise» (Formular, Vorschläge, Varianten, Begründungen, Budget-Seite).
- 1.7.1: Budget = vor Ort pro Tag × Tage + Anreisebudget pro Reise, gewählt im Formular «Neue Reise» mit Vorschlag je
  Dauer (James: «nicht 600.– Anreise für ein Weekend», «gehört zur Suche, nicht in die Einstellungen»).
  Im Testmodus bei 390 px hell/dunkel geprüft.
- 1.7.0: James bekam für 3 Tage Doha vorgeschlagen. Jetzt Reichweite und Budget abhängig von der Reisedauer, Anreise
  auch per Zug/Auto, Hinweis im Formular. Im Testmodus bei 390 px hell/dunkel geprüft; echte Vorschläge nur auf dem NAS.
- 1.6.0: Wunsch James: Hotelvorschläge mit 3–4 Fotos und echten Preisen für die Nächte; Bilder im Tagesplan (in seinem
  Plan fehlten sie bei Sehenswürdigkeiten wie dem Wiedervereinigungspalast). Im Testmodus bei 390 px hell/dunkel geprüft;
  echte Google-/Wikipedia-Treffer nur auf dem NAS. Pro Reise ca. 3 SerpApi-Suchen je Ort.
- 1.5.0: Wunsch James: 2–3 Routen-Varianten mit Stichpunkten/Kosten/Karte, dann Plan mit Routenkarte, Hotelwahl aus
  3 Vorschlägen je Ort, «Finaler Vorschlag». Im Testmodus (Platzhalter-Kacheln) bei 390 px hell/dunkel geprüft; echte
  Karten und Qualität der Varianten nur auf dem NAS. Dabei behoben: Hotels von «Lissabon Insel» erschienen bei «Lissabon».
- 1.4.2: «Neue Reise» neu (James: «Ziel (optional)» war verwirrend): Auswahl «Ziele vorschlagen / Ich weiss das Ziel»
  (`NEW_MODE`, gemerkt), Ziel-Zeile nur im zweiten Modus und dort Pflicht, Dauer mit −/+ (2–30). Globale Regel
  `[hidden]{display:none!important}` (vorher überstimmte `.row{display:flex}` das Ausblenden). Mit Playwright 390 px hell/dunkel geprüft.
- 1.4.1: Erster echter Lauf (James) scheiterte: «Gemini: Tageslimit · Groq: Tempolimit». Behoben: Minutenlimit wurde als
  Tageslimit gedeutet, Groq bekam ganze Reisepläne (passt nie ins Minutenkontingent). Ob Gemini echt am Tageslimit war
  (evtl. gleicher Schlüssel wie Hörwerk), zeigt jetzt Googles Text in der Meldung. Ausserdem DB-Zugriffe threadsicher.
- 1.4.0: Fotos (Wikipedia, Google Hotels) zu Zielen, Orten, Programmpunkten, Hotels. Im Testmodus mit Platzhaltern geprüft;
  echte Bildqualität/Trefferquote nur auf dem NAS prüfbar. Dabei behoben: doppelte Cache-Kopfzeile (no-store gewann).
- 1.3.0: Buchung importieren + echte Preise (SerpApi). Im Testmodus und mit nachgebildeten Antworten geprüft; echte
  SerpApi-Antworten und wie gut Gemini Bestätigungen liest, nur auf dem NAS prüfbar.
- 1.2.0: Gemini + Groq gratis mit Aufgabenteilung und gegenseitiger Reserve; Profil → KI. Mit nachgebildetem Server
  getestet (`tests/test_llm.py`), echte Gemini-/Groq-Antworten (Planqualität, JSON-Treue, Limits) nur auf dem NAS prüfbar.
- 1.1.0: Profil aufgeräumt (iPhone-Stil), Länderauswahl mit Suche. Mit Playwright bei 390 px hell/dunkel geprüft.
- Vollständige App: Profile + Matching, Zielvorschläge mit Klima, Reiseplan mit Hotels/Verbindungen/Schlechtwetter-
  Programm, Favoriten, Morgen-Wettercheck mit Umplanung, Ausfall mehrtägig, «fällt aus», festnageln, als gebucht
  markieren, Aufträge mit Entwürfen, Rückgängig, Foto-Übersetzung, Nachrichten, Kosten. Im Testmodus geprüft.
- Nicht prüfbar aus der Cloud: echte Claude-Antworten (Qualität, Dauer, Kosten pro Plan), echte Open-Meteo-Antworten
  (Format nach Doku nachgebildet), ob iOS beim Foto die Kamera anbietet, Docker-Start auf dem NAS.
- Keine Buchungs-APIs (Booking.com nur für Partner, Amadeus Self-Service seit 17.7.2026 abgeschaltet): Suchlinks,
  echte Preise über SerpApi, Buchung von Hand, dann Bestätigung importieren.
  Transport-Ausfälle erkennt Kompass nicht selbst (keine freie Datenquelle) → man meldet sie mit «Fällt aus …».
- Ideen: Push-Mitteilung aufs iPhone bei Umplanung (z. B. ntfy), Tagesvorschau am Vorabend.

"""Ende-zu-Ende-Test der ganzen App im Testmodus (KI und Wetter simuliert, kein Netz, keine Kosten).

Start: python -m unittest -v
"""
import base64
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app import config, server, service, store

PW = "Test-Passwort-1234567"
PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.patches = [mock.patch.object(config, "FAKE_MODE", True), mock.patch.object(config, "DATA_DIR", cls.tmp),
                       mock.patch.object(config, "DB_PATH", cls.tmp / "kompass.db"),
                       mock.patch.object(config, "APP_PASSWORD", PW)]
        for p in cls.patches:
            p.start()
        service.reset_for_tests()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.cookie = ""

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        service.reset_for_tests()
        for p in cls.patches:
            p.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, csrf=True, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if csrf:
            req.add_header("X-Kompass", "1")
        if auth and AppTest.cookie:
            req.add_header("Cookie", AppTest.cookie)
        try:
            with urllib.request.urlopen(req) as r:
                if "Set-Cookie" in r.headers:
                    AppTest.cookie = r.headers["Set-Cookie"].split(";")[0]
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def wait_job(self, jid):
        for _ in range(100):
            code, j = self.call("GET", f"/api/jobs/{jid}")
            if j["status"] != "laeuft":
                return j
            time.sleep(0.05)
        self.fail("Auftrag wurde nicht fertig")

    def test_full_flow(self):
        # --- Sicherheit
        self.assertEqual(self.call("GET", "/api/info", auth=False)[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"password": PW}, csrf=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/login", {"password": PW})[0], 200)
        code, info = self.call("GET", "/api/info")
        self.assertTrue(info["fake"])

        # --- Profile + Matching (Schwangerschaft, Veto, besuchte Länder)
        code, hh = self.call("GET", "/api/household")
        h = hh["household"]
        h["profiles"][0].update(name="James", activities={"wandern": 2, "nachtleben": 1, "museum": 1})
        h["profiles"][1].update(name="Lea", activities={"wandern": 1, "wellness": 2, "shopping": -2},
                                climate={"temp_min_c": 18, "temp_max_c": 26, "rain_tolerance": "gering"})
        h["constraints"].update(budget_total_max="6000", max_flight_hours=5,
                                pregnancy={"due_date": (date.today() + timedelta(days=150)).isoformat()})
        h["history"]["visited_countries"] = ["IT", "gr"]
        code, hh = self.call("POST", "/api/household", {"household": h})
        self.assertEqual(code, 200, hh)
        self.assertIn("Shopping", hh["prompt"])
        self.assertIn("schwanger", hh["prompt"])
        code, err = self.call("POST", "/api/household", {"household": {**h, "profiles": []}})
        self.assertEqual(code, 400)

        # --- Zielvorschläge: Italien/Griechenland fallen weg (schon besucht)
        start = date.today() + timedelta(days=30)
        code, r = self.call("POST", "/api/destinations", {"start": start.isoformat(), "days": 6, "wish": "Meer"})
        j = self.wait_job(r["job"])
        self.assertEqual(j["status"], "fertig", j)
        names = [i["name"] for i in j["result"]["ideas"]]
        self.assertNotIn("Rom", names)
        self.assertNotIn("Kreta (Chania)", names)
        self.assertEqual(len(j["result"]["dropped"]), 4)
        self.assertIn("Madeira (Funchal): Hin- und Rückreise ca. CHF 520 – über dem Budget dafür (CHF 400)", j["result"]["dropped"])
        self.assertNotIn("Doha", names)                           # 6.5 Std. Flug – zu weit für 6 Tage
        self.assertIn("Kurzurlaub (6 Tage)", j["result"]["reach"])

        # --- Kurztrip 3 Tage (James: «max. 2 Std. Flug oder 5 Std. Auto»): nur Nahes bleibt
        code, r = self.call("POST", "/api/destinations", {"start": start.isoformat(), "days": 3, "wish": ""})
        j3 = self.wait_job(r["job"])
        near = sorted(i["name"] for i in j3["result"]["ideas"])
        self.assertEqual(near, ["Colmar", "Lyon"], j3["result"]["dropped"])
        self.assertIn("Valencia: Hin- und Rückreise ca. CHF 260 – über dem Budget dafür (CHF 200)", j3["result"]["dropped"])
        self.assertEqual(j3["result"]["travel_budget"], 200.0)
        # eigenes Anreisebudget im Formular: CHF 300 → Valencia (Anreise ca. 260) bleibt
        code, r = self.call("POST", "/api/destinations", {"start": start.isoformat(), "days": 3, "wish": "",
                                                          "travel_budget": 300})
        j3b = self.wait_job(r["job"])
        self.assertIn("Valencia", [i["name"] for i in j3b["result"]["ideas"]])
        self.assertEqual(j3b["result"]["travel_budget"], 300.0)
        self.assertEqual(self.call("POST", "/api/destinations", {"start": start.isoformat(), "days": 3,
                                                                 "travel_budget": "viel"})[0], 400)
        self.assertTrue(any("Lissabon: 2.8 Std. Flug – zu weit für 3 Tage" == d for d in j3["result"]["dropped"]))
        self.assertIsNotNone(j["result"]["ideas"][0]["climate_score"])
        self.assertTrue(all(i["photo"] for i in j["result"]["ideas"]))
        self.assertTrue(all(i["region"] and i["landmark_wiki"] for i in j["result"]["ideas"]))
        self.assertEqual(self.call("POST", "/api/destinations", {"start": "2020-01-01", "days": 6})[0], 400)

        # --- Routen-Varianten → Plan aus Variante → Hotel wählen → Kostenaufstellung
        code, r = self.call("POST", "/api/variants", {"destination": "Lissabon", "start": start.isoformat(),
                                                      "days": 6, "wishes": "", "travel_budget": 650})
        j = self.wait_job(r["job"])
        self.assertEqual(j["status"], "fertig", j)
        vs = j["result"]
        self.assertEqual(len(vs["variants"]), 3)
        v1 = vs["variants"][1]
        self.assertTrue(v1["facts"]["weather"]["place"].startswith("Lissabon"))       # echte Klimadaten (Testmodus)
        self.assertTrue(v1["facts"]["weather"]["max"] > v1["facts"]["weather"]["min"])
        self.assertEqual((v1["facts"]["water_c"], v1["facts"]["stops"]), (20, 2))
        self.assertGreater(v1["facts"]["km"], 100)                           # Strecke zwischen den 2 Orten
        self.assertTrue(all(st["note"] for st in v1["route"]))
        self.assertFalse(any("Wetter" in b or "Wasser" in b or " km" in b for b in v1["bullets"]))
        self.assertEqual(sum(st["nights"] for st in v1["route"]), 5)
        self.assertEqual(v1["costs"]["Total"], sum(c for k, c in v1["costs"].items() if k != "Total"))
        code, r = self.call("POST", "/api/trips", {"variant_set": vs["id"], "index": 1})
        j = self.wait_job(r["job"])
        self.assertEqual(j["status"], "fertig", j)
        vt = j["result"]["trip_id"]
        for _ in range(80):  # Fotos + echte Hotelpreise kommen im Hintergrund
            code, d = self.call("GET", f"/api/trips/{vt}")
            if not d["photos_pending"]:
                break
            time.sleep(0.05)
        self.assertFalse(d["photos_pending"])
        bases = list(dict.fromkeys(day["base"]["name"] for day in d["trip"]["days"]))
        self.assertEqual(bases, [st["name"] for st in v1["route"]])
        self.assertFalse(d["trip"]["summary"].startswith("Stadt & Insel"))  # kein zusammengeklebter Doppeltext mehr
        self.assertTrue(d["trip"]["summary"].strip())
        self.assertEqual(float(d["trip"]["travel_budget"]), 650.0)      # Anreisebudget aus dem Formular bleibt beim Plan
        s0 = d["trip"]["stays"][0]
        opts = d["hotel_options"][s0["id"]]
        self.assertEqual([o["tier"] for o in opts], ["mittel", "guenstig", "gehoben"])
        self.assertTrue(opts[0]["chosen"])
        self.assertTrue(all(o["live"] and o["live"]["night"] and len(o["photos"]) == 4 for o in opts), opts)
        code, d = self.call("POST", f"/api/trips/{vt}/choose-hotel", {"stay_id": s0["id"], "index": 1})
        self.assertEqual(d["outcome"]["status"], "angewendet", d["outcome"])
        nights = (date.fromisoformat(s0["check_out"]) - date.fromisoformat(s0["check_in"])).days
        live_night = opts[1]["live"]["night"]
        self.assertEqual((d["trip"]["stays"][0]["name"], float(d["trip"]["stays"][0]["cost"])),
                         ("Casa Lissabon", live_night * nights))   # echter Preis statt Schätzung
        self.assertIn("Google Hotels", d["trip"]["stays"][0]["note"])
        self.assertEqual(d["photos"]["stays"][s0["id"]], opts[1]["photos"][0])
        used = self.call("GET", "/api/info")[1]["prices"]["month"]
        code, d = self.call("POST", f"/api/trips/{vt}/hotel-prices", {"stay_id": s0["id"]})
        self.assertEqual(code, 200)
        self.assertEqual(self.call("GET", "/api/info")[1]["prices"]["month"], used + 3)  # je Vorschlag 1 Suche
        self.assertTrue(d["hotel_options"][s0["id"]][1]["chosen"])
        self.assertEqual(set(d["costs"]), {"Flüge & Verbindungen", "Unterkünfte", "Programm"})
        self.assertEqual(self.call("POST", f"/api/trips/{vt}/choose-hotel", {"stay_id": s0["id"], "index": 9})[0], 400)
        self.assertEqual(self.call("POST", "/api/trips", {"variant_set": "gibtsnicht", "index": 0})[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/trips/{vt}")[0], 200)

        # --- Kartenkacheln (Testmodus: Platzhalter), ungültige Kachel → 404
        req = urllib.request.Request(f"{self.base}/api/tile/3/4/2", headers={"Cookie": AppTest.cookie})
        with urllib.request.urlopen(req) as rr:
            self.assertEqual(rr.headers["Content-Type"], "image/svg+xml")
        self.assertEqual(self.call("GET", "/api/tile/3/9/2")[0], 404)
        self.assertEqual(self.call("GET", "/api/tile/3/4/2", auth=False)[0], 401)

        # --- Reiseplan erstellen (heute beginnend, 6 Tage, mit Fähre an Tag 3)
        code, r = self.call("POST", "/api/trips", {"destination": "Lissabon", "start": date.today().isoformat(),
                                                   "days": 6, "wishes": ""})
        j = self.wait_job(r["job"])
        self.assertEqual(j["status"], "fertig", j)
        tid = j["result"]["trip_id"]
        code, d = self.call("GET", f"/api/trips/{tid}")
        trip = d["trip"]
        self.assertEqual(len(trip["days"]), 6)
        self.assertEqual(len(trip["stays"]), 2)
        self.assertTrue(trip["stays"][0]["search_url"].startswith("https://www.booking.com/"))
        flights = [t for t in trip["transports"] if t["mode"] == "flug"]
        self.assertTrue(flights[0]["search_url"].startswith("https://www.google.com/travel/flights"))
        self.assertEqual(code, 200)

        # --- Fotos: werden im Hintergrund geholt, dann über /api/photo ausgeliefert
        for _ in range(60):
            code, d = self.call("GET", f"/api/trips/{tid}")
            if not d["photos_pending"]:
                break
            time.sleep(0.05)
        self.assertFalse(d["photos_pending"])
        self.assertTrue(d["hero"]["id"])
        # --- Heute-Karte: Tag der Planung, Ort, Vorhersage für genau diesen Ort
        L = d["live_day"]
        self.assertEqual((L["index"], L["total"]), (1, 6))
        self.assertEqual(L["place"], d["trip"]["days"][0]["base"]["name"])
        self.assertTrue(L["forecast"]["summary"])
        self.assertIn("Küste und Altstadt 1", L["theme"])
        # --- echte Flugangebote werden automatisch geholt (Testmodus: nachgebaute SerpApi-Antwort)
        live = d["flight_live"]
        fl = [t for t in d["trip"]["transports"] if t["mode"] == "flug"]
        self.assertTrue(live[fl[0]["id"]]["options"][0]["price"] > 0)
        self.assertTrue(live[fl[0]["id"]]["token"])
        acts = d["photos"]["activities"]
        dinner = "abendessen im restaurant tasca 1"
        self.assertFalse(acts[dinner])                     # Essen bekommt kein Foto
        self.assertTrue(d["hero"]["id"] != acts[dinner])
        # Restaurants: Link zu Google Maps immer, Bewertung (Testmodus: nachgebaute SerpApi-Antwort)
        meal = next(a for day in d["trip"]["days"] for a in day["activities"] if a["title"].startswith("Abendessen"))
        v = d["venues"][meal["id"]]
        self.assertEqual(v["name"], "Tasca 1")
        self.assertTrue(v["url"].startswith("https://www.google.com/maps/search/?api=1&query=Tasca+1"))
        self.assertTrue(4.0 <= v["rating"] <= 5.0 and v["reviews"] > 0)
        # schwach bewertet → Kompass ersetzt selbst durch ein gut bewertetes (Testmodus: nachgebaute Antworten)
        for _ in range(100):
            swapped = [x for day in d["trip"]["days"] for x in day["activities"] if "Bistro Estrela" in (x.get("venue") or "")]
            if swapped:
                break
            time.sleep(0.05)
            code, d = self.call("GET", f"/api/trips/{tid}")
        self.assertTrue(swapped)
        self.assertIn("Sehr gut bewertet", swapped[0]["note"])
        self.assertFalse(any(x["better"] for x in d["venues"].values()))   # nie zwei Lokale nebeneinander
        h = next(x for x in d["history"] if x["reason"].startswith("Besser bewertete Restaurants"))
        self.assertEqual(h["actor"], "autonom")
        # Sehenswürdigkeiten: TripAdvisor-Bewertung + Tipp (Testmodus: nachgebaute SerpApi- und KI-Antworten)
        for _ in range(100):
            sw = [x for day in d["trip"]["days"] for x in day["activities"] if x["title"].startswith("Castelo de")]
            if sw and any(s["tip"] for s in d["sights"].values()):
                break
            time.sleep(0.05)
            code, d = self.call("GET", f"/api/trips/{tid}")
        museum = next(a for a in d["trip"]["days"][2]["activities"] if a["title"] == "Altstadt-Museum 3")
        s1 = d["sights"][museum["id"]]
        self.assertTrue(s1["url"].startswith("https://www.tripadvisor.com/"))
        self.assertTrue(s1["rating"] >= 4.0 and s1["reviews"] > 0 and not s1["weak"])
        self.assertIn("Früh", s1["tip"])
        kueste = next(a for a in d["trip"]["days"][1]["activities"] if a["title"].startswith("Küstenwanderung"))
        self.assertNotIn(kueste["id"], d["sights"])        # ohne Wikipedia-Titel keine Suche (allgemeiner Punkt)
        self.assertEqual(len(sw), 1)                       # «Museu 2» (3.5 ★) → beste Sehenswürdigkeit am Ort
        self.assertEqual(sw[0]["category"], "museum")      # Zeit und Kategorie bleiben
        self.assertIn("TripAdvisor", sw[0]["note"])
        self.assertFalse(any(x["weak"] for x in d["sights"].values()))
        h2 = next(x for x in d["history"] if x["reason"].startswith("Besser bewertet bei TripAdvisor"))
        self.assertEqual(h2["actor"], "autonom")
        self.assertTrue(h["can_undo"] or h2["can_undo"])   # der jüngere der beiden Täusche ist rückgängig machbar
        self.assertTrue(acts["altstadt-museum 3"])         # Bild über den Wikipedia-Titel «Museu 3»
        self.assertTrue(acts["altstadt-museum 2"])
        req = urllib.request.Request(f"{self.base}/api/photo/{d['hero']['id']}", headers={"Cookie": AppTest.cookie})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
            self.assertEqual(r.headers.get_all("Cache-Control"), ["private, max-age=2592000"])
        self.assertEqual(self.call("GET", "/api/photo/" + "0" * 24)[0], 404)
        # --- Reise als PDF (1.16.0): echte PDF-Datei mit Dateiname
        req = urllib.request.Request(f"{self.base}/api/trips/{tid}/pdf", headers={"Cookie": AppTest.cookie})
        with urllib.request.urlopen(req) as r:
            pdf = r.read()
            self.assertEqual(r.headers["Content-Type"], "application/pdf")
            self.assertIn("filename*=UTF-8''Kompass%20-%20", r.headers["Content-Disposition"])
        self.assertTrue(pdf.startswith(b"%PDF-") and len(pdf) > 5000)
        code, tl = self.call("GET", "/api/trips")
        self.assertTrue(tl["trips"][0]["photo"])

        # --- Wettercheck: Tag 2, 07:00, Regen → Wanderung wird ersetzt (keine Sauna wegen Schwangerschaft)
        day2 = date.today() + timedelta(days=1)
        (self.tmp / "fake_weather.json").write_text(json.dumps({day2.isoformat(): "regen"}))
        with mock.patch.object(service, "local_now", lambda it: datetime.combine(day2, datetime.min.time()).replace(hour=7)):
            code, d = self.call("POST", f"/api/trips/{tid}/weathercheck", {})
            self.assertEqual(d["outcome"]["status"], "angewendet", d["outcome"])
            titles = [a["title"] for day in d["trip"]["days"] if day["date"] == day2.isoformat() for a in day["activities"]]
            self.assertFalse(any(t.startswith("Küstenwanderung") for t in titles), titles)
            self.assertFalse(any("Sauna" in t for t in titles), titles)
            self.assertTrue(any(a["origin"] == "umplanung" for day in d["trip"]["days"] for a in day["activities"]))

            # --- Morgen-Zeitplan: Reise «gebucht» → läuft genau einmal pro Tag
            code, d = self.call("POST", f"/api/trips/{tid}/status", {"status": "gebucht"})
            self.assertEqual(d["trip"]["status"], "gebucht")
            store.save_settings({"check_time": "06:30"})
            self.assertEqual(service.scheduler_tick(), [tid])
            self.assertEqual(service.scheduler_tick(), [])
            self.assertEqual(service.sm().get(tid).status, "unterwegs")

            # --- Programmpunkt fällt aus → Ersatz
            act = next(a for day in d["trip"]["days"] if day["date"] == day2.isoformat()
                       for a in day["activities"] if a["category"] == "museum")  # an Tag 2 schon «Castelo de …»
            code, d = self.call("POST", f"/api/trips/{tid}/closure", {"activity_id": act["id"], "reason": "geschlossen"})
            self.assertEqual(d["outcome"]["status"], "angewendet", d["outcome"])

        # --- Fähre fällt 2 Tage aus → Hotel verlängert, alles verschoben, Aufträge mit Entwurf
        before = service.sm().get(tid)
        ferry = next(t for t in before.transports if t.mode == "faehre")
        code, d = self.call("POST", f"/api/trips/{tid}/transport", {"segment_id": ferry.id, "days": 2,
                                                                    "reason": "Sturm"})
        self.assertEqual(d["outcome"]["status"], "angewendet", d["outcome"])
        after = service.sm().get(tid)
        self.assertEqual(after.end_date, before.end_date + timedelta(days=2))
        self.assertEqual(len(after.days), 8)
        self.assertEqual(after.stays[0].check_out, before.stays[0].check_out + timedelta(days=2))
        self.assertEqual(after.stays[1].check_in, before.stays[1].check_in + timedelta(days=2))
        new_ferry = next(t for t in after.transports if t.mode == "faehre")
        self.assertEqual(new_ferry.depart, ferry.depart + timedelta(days=2))
        stuck_day = after.day(ferry.depart.date())
        self.assertEqual(stuck_day.base.name, "Lissabon")
        self.assertTrue(stuck_day.activities, "gewonnene Tage sollen Programm haben")
        kinds = sorted(a["kind"] for a in d["actions"])
        self.assertIn("hotel_verlaengern", kinds)
        self.assertIn("transport_umbuchen", kinds)
        hotel = next(a for a in d["actions"] if a["kind"] == "hotel_verlaengern")
        code, dr = self.call("POST", f"/api/actions/{hotel['id']}/draft", {})
        self.assertIn("extend", dr["draft"]["message"])
        self.assertEqual(self.call("POST", f"/api/actions/{hotel['id']}", {"status": "erledigt"})[0], 200)

        # --- Rückgängig macht den Ausfall wieder rückgängig
        last = d["history"][0]
        self.assertTrue(last["can_undo"])
        code, d = self.call("POST", f"/api/trips/{tid}/undo", {"change_id": last["id"]})
        self.assertTrue(d["ok"])
        self.assertEqual(service.sm().get(tid).end_date, before.end_date)

        # --- Währungsrechner (Testkurse)
        code, r = self.call("GET", "/api/fx")
        self.assertEqual(code, 200, r)
        self.assertEqual((r["base"], r["rates"]["CHF"], r["stale"]), ("CHF", 1.0, False))
        self.assertIn("VND", r["rates"])
        self.assertEqual(self.call("GET", "/api/fx", auth=False)[0], 401)

        # --- Bild übersetzen (mit Schwangerschafts-Hinweisen)
        code, v = self.call("POST", "/api/vision", {"image": PNG, "media_type": "image/png"})
        self.assertEqual(code, 200, v)
        self.assertTrue(any(i["warnings"] for i in v["result"]["items"]))
        self.assertEqual(self.call("POST", "/api/vision", {"image": PNG, "media_type": "text/html"})[0], 502)

        # --- Freier Entwurf, Favorit, Einstellungen, Meldungen
        code, dr = self.call("POST", "/api/draft", {"kind": "nachricht", "details": "Gibt es einen Parkplatz?"})
        self.assertIn("Parkplatz", dr["draft"]["message_de"])
        code, d = self.call("POST", f"/api/trips/{tid}/favorite", {})
        self.assertTrue(d["favorite"])
        self.assertEqual(self.call("POST", "/api/settings", {"model": "gpt"})[0], 400)
        code, info = self.call("POST", "/api/settings", {"model": "claude-haiku-5-5", "check_time": "07:15"})
        self.assertEqual(info["settings"]["model"], "claude-haiku-5-5")
        self.assertEqual(info["settings"]["dest_style"], "geheimtipps")             # Standard: Geheimtipps
        self.assertEqual(self.call("POST", "/api/settings", {"dest_style": "alles"})[0], 400)
        code, info = self.call("POST", "/api/settings", {"dest_style": "gemischt"})
        self.assertEqual(info["settings"]["dest_style"], "gemischt")
        code, n = self.call("GET", "/api/notices")
        self.assertTrue(any(x["kind"] == "umplanung" for x in n["notices"]))
        # --- Buchung importieren: Vorschau, dann übernehmen (Testmodus: Hinflug + erstes Hotel)
        code, r = self.call("POST", f"/api/trips/{tid}/import", {"text": "Kurz"})
        self.assertEqual(code, 400)
        code, r = self.call("POST", f"/api/trips/{tid}/import", {"text": "Buchungsbestätigung Flug und Hotel …" * 2})
        pv = r["preview"]
        self.assertTrue(pv["can_apply"], pv)
        self.assertEqual(len(pv["lines"]), 2)
        self.assertEqual(service.sm().get(tid).stays[0].confirmed, False)  # Vorschau ändert nichts
        code, d = self.call("POST", f"/api/trips/{tid}/import/apply", {"token": pv["token"]})
        self.assertTrue(d["ok"], d.get("error"))
        it = service.sm().get(tid)
        fl = next(t for t in it.transports if t.mode == "flug")
        self.assertEqual((fl.estimated, fl.booking.reference, fl.depart.hour, fl.number), (False, "ABC123", 6, "TA 123"))
        self.assertFalse(fl.note)  # Hinweis «Schätzung» ist weg
        self.assertEqual((it.stays[0].name, it.stays[0].confirmed), ("Hotel Testpalast", True))
        self.assertIn("EUR", it.stays[0].note)  # Fremdwährung nur als Hinweis, kein falscher CHF-Betrag
        self.assertEqual(self.call("POST", f"/api/trips/{tid}/import/apply", {"token": pv["token"]})[0], 404)

        # --- Echte Preise (Testmodus): Rückflug und zweites Hotel übernehmen
        back = [t for t in it.transports if t.mode == "flug"][-1]
        code, pr = self.call("POST", f"/api/trips/{tid}/prices", {"target": "verbindung", "item_id": back.id})
        self.assertEqual(code, 200, pr)
        self.assertEqual(len(pr["options"]), 2)
        code, d = self.call("POST", f"/api/trips/{tid}/prices/apply", {"token": pr["token"], "index": 1})
        self.assertEqual(d["outcome"]["status"], "angewendet", d["outcome"])
        back2 = next(t for t in service.sm().get(tid).transports if t.id == back.id)
        self.assertEqual((back2.depart.hour, back2.via, float(back2.cost)), (12, ["Madrid"], 268.0))
        self.assertTrue(back2.estimated)  # bleibt Vorschlag, bis gebucht
        stay2 = service.sm().get(tid).stays[1]
        code, pr = self.call("POST", f"/api/trips/{tid}/prices", {"target": "unterkunft", "item_id": stay2.id})
        self.assertEqual(pr["options"][-1]["over_budget"], False)
        pick = next(i for i, o in enumerate(pr["options"]) if o["photo"])  # Bild aus Google Hotels
        code, d = self.call("POST", f"/api/trips/{tid}/prices/apply", {"token": pr["token"], "index": pick})
        self.assertEqual(service.sm().get(tid).stays[1].cost, pr["options"][pick]["total"])
        self.assertEqual(d["photos"]["stays"][stay2.id], pr["options"][pick]["photo"])
        self.assertEqual(self.call("POST", f"/api/trips/{tid}/prices/apply", {"token": "falsch", "index": 0})[0], 404)
        code, info = self.call("GET", "/api/info")
        self.assertGreaterEqual(info["prices"]["month"], 2)
        self.assertEqual(self.call("DELETE", f"/api/trips/{tid}")[0], 200)


if __name__ == "__main__":
    unittest.main()

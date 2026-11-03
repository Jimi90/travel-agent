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
        self.assertEqual(len(j["result"]["dropped"]), 2)
        self.assertIsNotNone(j["result"]["ideas"][0]["climate_score"])
        self.assertEqual(self.call("POST", "/api/destinations", {"start": "2020-01-01", "days": 6})[0], 400)

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
                       for a in day["activities"] if a["title"].startswith("Altstadt-Museum"))
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
        code, d = self.call("POST", f"/api/trips/{tid}/prices/apply", {"token": pr["token"], "index": 0})
        self.assertEqual(service.sm().get(tid).stays[1].cost, pr["options"][0]["total"])
        self.assertEqual(self.call("POST", f"/api/trips/{tid}/prices/apply", {"token": "falsch", "index": 0})[0], 404)
        code, info = self.call("GET", "/api/info")
        self.assertGreaterEqual(info["prices"]["month"], 2)
        self.assertEqual(self.call("DELETE", f"/api/trips/{tid}")[0], 200)


if __name__ == "__main__":
    unittest.main()

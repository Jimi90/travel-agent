"""Fotos: nur erlaubte Bild-Server, Wikipedia-Antwort auslesen, keine Zufallsbilder."""
import unittest
from unittest import mock

from app import photos


class PhotoTest(unittest.TestCase):
    def test_only_known_image_servers(self):
        self.assertTrue(photos._allowed("https://upload.wikimedia.org/a.jpg"))
        self.assertTrue(photos._allowed("https://lh3.googleusercontent.com/x"))
        self.assertFalse(photos._allowed("http://upload.wikimedia.org/a.jpg"))       # kein https
        self.assertFalse(photos._allowed("https://192.168.1.10/a.jpg"))             # Heimnetz
        self.assertFalse(photos._allowed("https://evil.example/upload.wikimedia.org"))
        self.assertIsNone(photos.download("https://evil.example/x.jpg", "test"))
        with self.assertRaises(Exception):
            photos._SafeRedirect().redirect_request(None, None, 302, "", {}, "https://10.0.0.1/x")

    def test_parse_wiki_takes_best_ranked_with_image(self):
        data = {"query": {"pages": [
            {"title": "Ohne Bild", "index": 1},
            {"title": "Torre de Belém", "index": 2, "fullurl": "https://de.wikipedia.org/wiki/Torre_de_Bel%C3%A9m",
             "thumbnail": {"source": "https://upload.wikimedia.org/t.jpg", "width": 800, "height": 533}}]}}
        self.assertEqual(photos.parse_wiki(data)["title"], "Torre de Belém")
        self.assertIsNone(photos.parse_wiki({"query": {"pages": [{"title": "x", "index": 1}]}}))
        self.assertIsNone(photos.parse_wiki({}))

    def test_generic_activity_without_wiki_title_gets_no_photo(self):
        self.assertIsNone(photos.activity_photo("Abendessen im Hotel", None, "Lissabon"))
        self.assertIsNone(photos.activity_photo("Ankunft und Check-in", "", "Lissabon"))

    def test_search_checks_all_hits(self):
        data = {"query": {"pages": [
            {"title": "Ho-Chi-Minh-Stadt", "index": 1, "thumbnail": {"source": "https://upload.wikimedia.org/stadt.jpg"}},
            {"title": "Wiedervereinigungspalast", "index": 2, "thumbnail": {"source": "https://upload.wikimedia.org/p.jpg"}}]}}
        hits = photos.parse_wiki_all(data)
        need = photos._words("Wiedervereinigungspalast")
        self.assertEqual(next(h for h in hits if need & photos._words(h["title"]))["title"], "Wiedervereinigungspalast")

    def test_generic_activities_and_word_match(self):
        self.assertTrue(photos.GENERIC.match("Abendessen im Hotel"))
        self.assertFalse(photos.GENERIC.match("Mosteiro dos Jerónimos"))
        self.assertEqual(photos._words("Wanderung zum Palácio da Pena"), {"palacio", "pena"})

    def test_seals_flags_and_maps_are_not_destination_photos(self):
        bad = ["https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Seal_of_San_Jose.svg/800px-Seal_of_San_Jose.svg.png",
               "https://upload.wikimedia.org/x/Coat_of_arms_of_Sal.png", "https://upload.wikimedia.org/x/Flag_of_Cape_Verde.png",
               "https://upload.wikimedia.org/x/Wappen_Colmar.jpg", "https://upload.wikimedia.org/x/Costa_Rica_locator_map.png",
               "https://upload.wikimedia.org/x/Logo.png"]
        for u in bad:
            self.assertTrue(photos.bad_image(u), u)
        self.assertTrue(photos.bad_image("https://upload.wikimedia.org/x/a.jpg", "Wappen von Colmar"))
        for u in ["https://upload.wikimedia.org/x/Torre_de_Belem_Lisbon.jpg",
                  "https://upload.wikimedia.org/x/Seaside_Kreta.jpg", "https://upload.wikimedia.org/x/Cathedral_view.jpg"]:
            self.assertFalse(photos.bad_image(u), u)


class DestinationPhotoTest(unittest.TestCase):
    """destination_photo: erst Wahrzeichen, dann Suche danach, erst zuletzt der Ortsname."""

    def setUp(self):
        # Commons nie wirklich fragen (und nie die Ablage anfassen) – ausser ein Test setzt es selbst
        pc = mock.patch.object(photos, "commons_photo", return_value=None)
        self.commons = pc.start()
        self.addCleanup(pc.stop)
        pg = mock.patch.object(photos, "google_photo", return_value=None)  # nie SerpApi/Ablage anfassen
        self.google = pg.start()
        self.addCleanup(pg.stop)

    def test_commons_before_wikipedia_place_name(self):
        self.commons.side_effect = [None, "pc"]
        with mock.patch.object(photos, "wiki_photo", return_value="w") as w:
            self.assertEqual(photos.destination_photo("", "Sal", "Kap Verde", []), "pc")
        w.assert_not_called()

    def run_chain(self, title_hit, search_hits):
        calls = []

        def title(t):
            calls.append(("title", t))
            return title_hit

        def search(q, must_match=""):
            calls.append(("search", q, must_match))
            return search_hits.pop(0) if search_hits else None

        with mock.patch.object(photos, "wiki_title_photo", title), mock.patch.object(photos, "wiki_photo", search):
            return photos.destination_photo("Torre de Belém", "Lissabon (Stadt)", "Region Lissabon"), calls

    def test_landmark_title_wins(self):
        pid, calls = self.run_chain("p1", [])
        self.assertEqual((pid, calls), ("p1", [("title", "Torre de Belém")]))

    def test_landmark_search_then_place_name(self):
        pid, calls = self.run_chain(None, [None, "p3"])
        self.assertEqual(pid, "p3")
        self.assertEqual(calls[1:], [("search", "Torre de Belém", "Torre de Belém"),
                                     ("search", "Lissabon Region Lissabon", "Lissabon")])

    def test_without_landmark_uses_place_name(self):
        with mock.patch.object(photos, "wiki_photo", return_value="p9") as w:
            self.assertEqual(photos.destination_photo("", "Venedig", ""), "p9")
        w.assert_called_once_with("Venedig", must_match="Venedig")

    def test_highlight_before_place_name(self):
        with mock.patch.object(photos, "wiki_photo", side_effect=[None, "ph"]) as w:
            self.assertEqual(photos.destination_photo("", "Sal", "Kap Verde", ["Strand", "Pedra de Lume"]), "ph")
        self.assertEqual(w.call_args_list[1].kwargs["must_match"], "Pedra de Lume")

    def test_short_place_name_gets_no_guess(self):
        with mock.patch.object(photos, "wiki_photo", return_value="baum") as w:
            self.assertIsNone(photos.destination_photo("", "Sal", "Kap Verde", []))
        w.assert_not_called()

    def test_google_is_last_resort(self):
        self.google.return_value = "g1"
        with mock.patch.object(photos, "wiki_photo", return_value=None):
            self.assertEqual(photos.destination_photo("", "Sal", "Kap Verde", []), "g1")
        self.assertEqual(self.commons.call_count, 3)  # erst alle freien Quellen, dann Google



class GooglePhotoTest(unittest.TestCase):
    def setUp(self):
        self.mem = {}
        for name, fn in (("get", lambda kind, key: self.mem.get((kind, key))),
                         ("put", lambda kind, key, data: self.mem.__setitem__((kind, key), data)),
                         ("add_cost", lambda *a, **k: None)):
            p = mock.patch.object(photos.store, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def run_search(self, results, usage=0):
        from app import prices
        with mock.patch.object(prices, "available", return_value=True), \
                mock.patch.object(photos, "image_usage", return_value=usage), \
                mock.patch.object(prices, "_search", return_value={"images_results": results}) as s, \
                mock.patch.object(photos, "download", side_effect=lambda u, *a: "pid-" + u[-1]):
            return photos.google_photo("Landhaus Gassner Triesenberg Hotel", must_match="Landhaus Gassner"), s

    def test_picks_matching_title_and_remembers(self):
        res = [{"title": "Irgendein Strand", "thumbnail": "https://encrypted-tbn0.gstatic.com/a"},
               {"title": "Landhaus Gassner – Zimmer", "thumbnail": "https://encrypted-tbn0.gstatic.com/b"}]
        pid, s = self.run_search(res)
        self.assertEqual(pid, "pid-b")
        self.assertEqual(photos.google_photo("Landhaus Gassner Triesenberg Hotel", must_match="Landhaus Gassner"), "pid-b")
        self.assertEqual(s.call_count, 1)  # zweite Frage kommt aus der Ablage

    def test_monthly_cap_stops_searches(self):
        pid, s = self.run_search([{"title": "Landhaus Gassner", "thumbnail": "https://x.gstatic.com/c"}],
                                 usage=photos.IMAGE_MONTH_CAP)
        self.assertIsNone(pid)
        s.assert_not_called()


class TripPhotoVersionTest(unittest.TestCase):
    def test_old_assignments_are_dropped_but_stays_kept(self):
        with mock.patch.object(photos.store, "get", return_value={"places": {"Cancún": "logo"}, "activities": {"a": "x"},
                                                                  "stays": {"s1": "h"}}):
            m = photos.trip_photos("t1")
        self.assertEqual((m["places"], m["activities"], m["stays"], m["v"]), ({}, {}, {"s1": "h"}, photos.PHOTO_VERSION))


if __name__ == "__main__":
    unittest.main()


class CommonsPhotoTest(unittest.TestCase):
    def test_parse_commons_keeps_landscape_jpeg(self):
        from app import photos
        data = {"query": {"pages": [
            {"index": 1, "title": "File:Hochformat.jpg", "imageinfo": [{"thumburl": "https://upload.wikimedia.org/a.jpg",
                                                                   "mime": "image/jpeg", "thumbwidth": 400, "thumbheight": 800}]},
            {"index": 2, "title": "File:Strand Sal.jpg", "imageinfo": [{"thumburl": "https://upload.wikimedia.org/b.jpg",
                                                                      "mime": "image/jpeg", "thumbwidth": 800, "thumbheight": 533,
                                                                      "descriptionurl": "https://commons.wikimedia.org/x"}]},
            {"index": 3, "title": "File:Karte.svg", "imageinfo": [{"thumburl": "https://upload.wikimedia.org/c.png",
                                                                "mime": "image/svg+xml"}]}]}}
        hits = photos.parse_commons(data)
        self.assertEqual([h["title"] for h in hits], ["Strand Sal.jpg"])

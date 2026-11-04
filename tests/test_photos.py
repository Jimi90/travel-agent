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
            self.assertEqual(photos.destination_photo("", "Rom", ""), "p9")
        w.assert_called_once_with("Rom", must_match="Rom")


if __name__ == "__main__":
    unittest.main()

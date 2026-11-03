"""Fotos: nur erlaubte Bild-Server, Wikipedia-Antwort auslesen, keine Zufallsbilder."""
import unittest

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

    def test_generic_activities_and_word_match(self):
        self.assertTrue(photos.GENERIC.match("Abendessen im Hotel"))
        self.assertFalse(photos.GENERIC.match("Mosteiro dos Jerónimos"))
        self.assertEqual(photos._words("Wanderung zum Palácio da Pena"), {"palacio", "pena"})


if __name__ == "__main__":
    unittest.main()

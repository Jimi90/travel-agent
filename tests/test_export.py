"""Reise als PDF (Wunsch James 1.16.0): Inhalt, Fotos, Schrift-Ersatz."""
import io
import unittest
from datetime import date
from unittest import mock

from app import export


class HelperTest(unittest.TestCase):
    def test_dates_and_names(self):
        self.assertEqual(export.span(date(2026, 12, 7), date(2026, 12, 12)), "7.–12. Dezember 2026")
        self.assertEqual(export.span(date(2026, 12, 28), date(2027, 1, 3)), "28.12.2026 – 03.01.2027")
        self.assertEqual(export.d_long(date(2026, 12, 28)), "Montag, 28. Dezember")
        self.assertEqual(export.chf(2760), "CHF 2'760")
        self.assertEqual(export.filename(type("T", (), {"title": "Laos / Entdeckung"})()), "Kompass - Laos  Entdeckung.pdf")

    def test_plus_code_removed(self):
        n = "Sehr gut bewertet: ★ 4.8 (290 Google-Bewertungen), V4WR+6Q Luang Prabang. Reservation empfohlen."
        self.assertEqual(export._PLUS_CODE.sub(".", n), "Sehr gut bewertet: ★ 4.8 (290 Google-Bewertungen). Reservation empfohlen.")

    def test_core_font_fallback(self):
        with mock.patch.object(export, "_font_files", return_value=None):
            pdf = export.TripPDF("Test")
        self.assertEqual(pdf.t("★ 4.5 → Hội An"), "* 4.5 -> H?i An")  # Helvetica kann nur Latin-1

    def test_image_cropped_jpeg(self):
        from PIL import Image
        b = io.BytesIO()
        Image.new("RGB", (1200, 400), (10, 20, 30)).save(b, "PNG")
        with mock.patch.object(export.photos, "get", return_value=(b.getvalue(), "image/png", {})):
            out = export._image("a" * 24, 1.5, 300)
        im = Image.open(out)
        self.assertEqual(im.format, "JPEG")
        self.assertAlmostEqual(im.size[0] / im.size[1], 1.5, places=1)
        with mock.patch.object(export.photos, "get", return_value=(b"<svg/>", "image/svg+xml", {})):
            self.assertIsNone(export._image("a" * 24, 1.5))

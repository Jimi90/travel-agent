"""Reise als PDF (Wunsch James 1.16.0: «damit ich die Reise als PDF exportieren kann»).

Der Server baut das PDF selbst (fpdf2, reines Python), damit es auf dem iPhone, am PC und offline gleich aussieht –
keine Druckansicht des Browsers. Aufbau wie ein Reisebüro-Angebot: Titelbild, Eckdaten, Route, Preis, Unterkünfte,
Verbindungen, dann Tag für Tag mit Zeiten, Beschreibung, Restaurant-/TripAdvisor-Bewertung, Tipp und kleinem Foto.
Schrift DejaVu Sans (im Image über fonts-dejavu-core, kann ä/ö/ü, Vietnamesisch, ★, →); fehlt sie, Helvetica mit
ersetzten Sonderzeichen. Fotos nur JPEG/PNG/WebP aus dem eigenen Fotoordner – nichts wird dafür aus dem Netz geholt.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import MethodReturnValue, XPos, YPos

from . import photos, sights, store, venues

LOGO = Path(__file__).parent / "web" / "apple-touch-icon.png"  # App-Symbol (180 px) oben rechts auf Seite 1
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/local/share/fonts"]
ACCENT = (36, 89, 224)
TEXT = (28, 28, 30)
MUTED = (110, 110, 115)
LINE = (220, 220, 225)
SOFT = (242, 242, 247)
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember"]
CATS = {"wandern": "Wandern", "natur": "Natur", "strand": "Strand", "wassersport": "Wassersport", "kultur": "Kultur",
        "museum": "Museum", "stadt": "Stadt", "kulinarik": "Essen & Trinken", "wellness": "Wellness",
        "entspannung": "Entspannung", "shopping": "Shopping", "nachtleben": "Nachtleben"}
MODES = {"flug": "Flug", "faehre": "Fähre", "zug": "Zug", "bus": "Bus", "auto": "Auto"}
STATUS = {"entwurf": "Entwurf", "gebucht": "Gebucht", "unterwegs": "Unterwegs", "abgeschlossen": "Abgeschlossen"}
_CORE_REPL = {"★": "*", "→": "->", "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "·": "·",
              " ": " ", " ": " "}


_PLUS_CODE = re.compile(r", [23456789CFGHJMPQRVWX]{2,8}\+[23456789CFGHJMPQRVWX]{0,3}[^.]*\.")  # ältere Restaurant-Notizen


def chf(v) -> str:
    return "CHF " + f"{float(v or 0):,.0f}".replace(",", "'")


def count(n) -> str:
    return f"{int(n or 0):,}".replace(",", "'")


def d_long(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]}"


def d_short(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def span(a: date, b: date) -> str:
    if a == b:  # Tagesausflug
        return f"{d_long(a)} {a.year}"
    if a.year == b.year and a.month == b.month:
        return f"{a.day}.–{b.day}. {MONTHS[a.month - 1]} {a.year}"
    if a.year == b.year:
        return f"{a.day}. {MONTHS[a.month - 1]} – {b.day}. {MONTHS[b.month - 1]} {a.year}"
    return f"{d_short(a)} – {d_short(b)}"


def filename(it) -> str:
    t = re.sub(r"[^\w äöüÄÖÜéèàç&()-]+", "", it.title).strip() or "Reise"
    return f"Kompass - {t[:60]}.pdf"  # kein Gedankenstrich: Chrome verwirft sonst den Namen («download»)


def _font_files() -> tuple[str, str] | None:
    for d in FONT_DIRS:
        r, b = Path(d) / "DejaVuSans.ttf", Path(d) / "DejaVuSans-Bold.ttf"
        if r.exists() and b.exists():
            return str(r), str(b)
    return None


def _image(pid: str | None, ratio: float, px: int = 1400):
    """Foto aus dem eigenen Fotoordner, mittig auf das Seitenverhältnis (Breite/Höhe) zugeschnitten, oder None."""
    if not pid:
        return None
    got = photos.get(pid)
    if not got or got[1] not in ("image/jpeg", "image/png", "image/webp"):
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(got[0])).convert("RGB")
        w, h = im.size
        if w / h > ratio:
            nw = int(h * ratio)
            im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:
            nh = int(w / ratio)
            im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
        im.thumbnail((px, px))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=82, optimize=True)  # als JPEG einbetten – sonst speichert das PDF Rohpixel (MB gross)
        out.seek(0)
        return out
    except Exception:  # kaputtes Bild – lieber ohne Foto als gar kein PDF
        return None


class TripPDF(FPDF):
    def __init__(self, title: str):
        super().__init__(format="A4")
        self.trip_title = title
        self.set_margins(16, 16, 16)
        self.set_auto_page_break(True, 18)
        fonts = _font_files()
        self.unicode = bool(fonts)
        if fonts:
            self.add_font("Sans", "", fonts[0])
            self.add_font("Sans", "B", fonts[1])
            self.family = "Sans"
        else:
            self.family = "Helvetica"
        self.set_title(title)
        self.set_author("Kompass")
        self.set_creator("Kompass Reise-Assistent")

    # Text für die Schrift passend machen (Helvetica kann nur Latin-1)
    def t(self, s) -> str:
        s = str(s or "")
        if self.unicode:
            return s
        for a, b in _CORE_REPL.items():
            s = s.replace(a, b)
        return s.encode("latin-1", "replace").decode("latin-1")

    def font(self, size: float, bold: bool = False, color=TEXT):
        self.set_font(self.family, "B" if bold else "", size)
        self.set_text_color(*color)

    def footer(self):
        self.set_y(-12)
        self.font(8, color=MUTED)
        self.cell(0, 5, self.t(f"Kompass · {self.trip_title} · Stand {d_short(date.today())}"),
                  new_x=XPos.LMARGIN, new_y=YPos.TOP)
        self.cell(0, 5, f"Seite {self.page_no()} von {{nb}}", align="R")

    @property
    def width(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def para(self, text, size=10, bold=False, color=TEXT, w=0, h=None, x=None, link="", md=False) -> None:
        if x is not None:
            self.set_x(x)
        self.font(size, bold, color)
        self.multi_cell(w or self.width, h or size * 0.5, self.t(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT, link=link,
                        align="L", markdown=md)

    def height_of(self, text, size, w, bold=False) -> float:
        self.font(size, bold)
        return self.multi_cell(w, size * 0.5, self.t(text), dry_run=True, output=MethodReturnValue.HEIGHT, align="L")

    def need(self, h: float) -> None:
        if self.get_y() + h > self.h - self.b_margin:
            self.add_page()

    def section(self, title: str) -> None:
        self.need(20)
        self.ln(3)
        self.font(9, True, MUTED)
        self.cell(0, 6, self.t(title.upper()), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*LINE)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2.5)


def _route(it) -> list[tuple[str, int]]:
    out: list[list] = []
    for d in it.days:
        if out and out[-1][0] == d.base.name:
            out[-1][1] += 1
        else:
            out.append([d.base.name, 1])
    if out:
        out[-1][1] = max(0, out[-1][1] - 1)  # letzter Tag = Abreise
    return [(n, k) for n, k in out]


def _hero(it, tp) -> str | None:
    for d in it.days:
        for a in d.activities:
            pid = tp["activities"].get(photos.activity_key(a.title))
            if pid and photos.has_photo_subject(a):
                return pid
    return next((tp["places"].get(d.base.name) for d in it.days if tp["places"].get(d.base.name)), None)


def build(trip_id: str) -> tuple[bytes, str]:
    """PDF der Reise → (Bytes, Dateiname)."""
    from . import service  # spät laden (service → server → export)
    it = service.sm().get(trip_id)
    tp = photos.trip_photos(it.id)
    ven, sig = venues.for_trip(it), sights.for_trip(it)
    pdf = TripPDF(it.title)
    pdf.add_page()

    # --- Titelseite: alles Wichtige auf einen Blick (Wunsch James: Unterkünfte und Flüge auf Seite 1, Logo oben rechts)
    route = _route(it)
    days = len(it.days)
    flights = any(t.mode == "flug" for t in it.transports)
    costs = [("Flüge & Verbindungen" if flights else "Fahrten", sum(t.cost for t in it.transports))] \
        + ([("Unterkünfte", sum(s.cost for s in it.stays))] if it.stays else []) \
        + [("Programm", sum(a.cost for d in it.days for a in d.activities))]
    budget = store.household().constraints.budget_for_trip(it)
    if budget:
        costs.append(("Budget", budget))
    trans = sorted(it.transports, key=lambda x: x.depart)
    route_txt = " → ".join(f"{n} ({k} {'Nacht' if k == 1 else 'Nächte'})" if k else n for n, k in route)
    sub_txt = (f"{span(it.start_date, it.end_date)} · {'Tagesausflug' if days == 1 else f'{days} Tage'} · "
               f"{len(route)} {'Ort' if len(route) == 1 else 'Orte'} · für 2 Personen")
    ROW_W = pdf.width - 44  # Platz links neben Preis/Status

    def row_h(title: str, meta: str) -> float:
        return pdf.height_of(title, 10.5, ROW_W, True) + pdf.height_of(meta, 9, ROW_W) + 2.2

    stay_rows = [(s.name, f"{s.place.name} · {d_short(s.check_in)} – {d_short(s.check_out)} · "
                  f"{(s.check_out - s.check_in).days} {'Nacht' if (s.check_out - s.check_in).days == 1 else 'Nächte'}"
                  + (f" · Buchung {s.booking.reference}" if s.booking and s.booking.reference else ""),
                  chf(s.cost), ("gebucht", True) if (s.confirmed or s.booking) else ("Vorschlag", False),
                  s.search_url or "") for s in it.stays]
    trans_rows = []
    for t in trans:
        head = (f"{MODES.get(t.mode, t.mode)} {t.origin.name}{f' ({t.origin_code})' if t.origin_code else ''} → "
                f"{t.destination.name}{f' ({t.destination_code})' if t.destination_code else ''}")
        bits = [f"{d_short(t.depart.date())} {t.depart:%H:%M} – {t.arrive:%H:%M}"
                + (f" (+{(t.arrive.date() - t.depart.date()).days})" if t.arrive.date() > t.depart.date() else "")]
        if t.carrier or t.number:
            bits.append(" ".join(x for x in (t.carrier, t.number) if x))
        if t.via:
            bits.append("via " + ", ".join(t.via))
        trans_rows.append((head, " · ".join(bits), chf(t.cost) if t.cost else "im Ticket enthalten",
                           ("gebucht", True) if t.booking else ("bestätigt", True) if not t.estimated else ("geschätzt", False),
                           t.search_url or ""))

    # Höhe von allem ausser dem Titelbild schätzen → Bild so gross, dass alles auf Seite 1 passt
    need = (14 + pdf.height_of(it.title, 20, pdf.width, True) + 7 + pdf.height_of(route_txt, 10.5, pdf.width, True) + 2
            + (pdf.height_of(it.summary, 10, pdf.width) + 2 if it.summary else 0)
            + 4 + 13 + 6 * ((len(costs) + 1) // 2) + 6
            + sum(9 + sum(row_h(r[0], r[1]) for r in rows) for rows in (stay_rows, trans_rows) if rows))
    room = pdf.h - pdf.t_margin - pdf.b_margin - need - 6
    hero_h = min(66, room)

    # Kopfzeile: Kompass + Status links, App-Logo rechts
    top = pdf.get_y()
    if LOGO.exists():
        pdf.image(str(LOGO), x=pdf.w - pdf.r_margin - 11, y=top - 1, w=11, h=11)
    pdf.font(13, True, ACCENT)
    pdf.cell(0, 6, "Kompass", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.font(8.5, True, MUTED)
    pdf.cell(0, 4.5, pdf.t(f"REISEPLAN · {STATUS.get(it.status, it.status).upper()}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(top + 14)
    hero = _image(_hero(it, tp), pdf.width / hero_h) if hero_h >= 30 else None
    if hero:
        pdf.image(hero, x=pdf.l_margin, y=pdf.get_y(), w=pdf.width, h=hero_h)
        pdf.set_y(pdf.get_y() + hero_h + 4)
    pdf.para(it.title, 20, True, h=9)
    pdf.para(sub_txt, 10.5, color=MUTED, h=6)
    pdf.ln(1)
    pdf.para(route_txt, 10.5, True, h=5.5)
    if it.summary:
        pdf.ln(2)
        pdf.para(it.summary, 10, h=5)

    # Preis: Gesamt gross, Einzelposten in zwei Spalten
    pdf.ln(4)
    rows = (len(costs) + 1) // 2
    box_h = 13 + 6 * rows
    pdf.need(box_h + 2)
    y0 = pdf.get_y()
    pdf.set_fill_color(*SOFT)
    pdf.rect(pdf.l_margin, y0, pdf.width, box_h, style="F", round_corners=True, corner_radius=3)
    pdf.set_xy(pdf.l_margin + 5, y0 + 4)
    pdf.font(9, True, MUTED)
    pdf.cell(80, 5, pdf.t("GESAMTPREIS (SCHÄTZUNG)"))
    pdf.font(16, True)
    pdf.cell(pdf.width - 90, 5, pdf.t(chf(it.total_cost())), align="R")
    col_w = (pdf.width - 10 - 8) / 2
    for i, (label, v) in enumerate(costs):
        x = pdf.l_margin + 5 + (i % 2) * (col_w + 8)
        pdf.set_xy(x, y0 + 11 + 6 * (i // 2))
        pdf.font(9.5, color=MUTED if label == "Budget" else TEXT)
        pdf.cell(col_w * 0.6, 6, pdf.t(label))
        pdf.cell(col_w * 0.4, 6, pdf.t(chf(v)), align="R")
    pdf.set_y(y0 + box_h + 1)
    pdf.para("Preise sind Vorschläge bzw. Schätzungen, solange nichts als gebucht markiert ist. "
             "Kompass bucht nie selbst.", 8, color=MUTED, h=4.5)

    def rows_out(rows):
        for title, meta, price, (state, ok), link in rows:
            pdf.need(row_h(title, meta))
            y = pdf.get_y()
            pdf.para(title, 10.5, True, w=ROW_W, h=5.2, link=link)
            y2 = pdf.get_y()
            pdf.set_xy(pdf.w - pdf.r_margin - 42, y)
            pdf.font(10.5, True)
            pdf.cell(42, 5.2, pdf.t(price), align="R")
            pdf.set_xy(pdf.w - pdf.r_margin - 42, y + 5.2)
            pdf.font(8.5, color=(36, 140, 70) if ok else (200, 110, 0))
            pdf.cell(42, 4.6, pdf.t(state), align="R")
            pdf.set_y(y2)
            pdf.para(meta, 9, color=MUTED, w=ROW_W, h=4.6)
            pdf.ln(2.2)

    if stay_rows:
        pdf.section("Unterkünfte")
        rows_out(stay_rows)
    if trans_rows:
        pdf.section(("Flüge und Verbindungen" if flights else "Verbindungen") if days > 1 else "Hin- und Rückfahrt")
        rows_out(trans_rows)

    # --- Tagesplan ---------------------------------------------------------------------------------
    if days > 1:  # Tagesausflug: Programm gleich auf der ersten Seite
        pdf.add_page()
    else:
        pdf.ln(6)
        pdf.need(60)
    pdf.font(18, True)
    pdf.cell(0, 9, pdf.t("Tag für Tag" if days > 1 else "Programm"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)
    TIME_W, THUMB_W, THUMB_H = 24, 30, 22
    for i, d in enumerate(it.days, 1):
        pdf.need(30)
        pdf.ln(3)
        y = pdf.get_y()
        pdf.set_fill_color(*ACCENT)
        pdf.rect(pdf.l_margin, y, pdf.width, 9, style="F", round_corners=True, corner_radius=2)
        pdf.set_xy(pdf.l_margin + 4, y + 1.5)
        pdf.font(11, True, (255, 255, 255))
        pdf.cell(pdf.width - 8, 6, pdf.t(f"Tag {i} von {days} · {d_long(d.date)} · {d.base.name}"))
        pdf.set_y(y + 11)
        if d.theme:
            pdf.para(d.theme, 10.5, True, color=ACCENT, h=5.5)
        pdf.ln(1)
        if not d.activities:
            pdf.para("Freier Tag – nichts geplant.", 10, color=MUTED, h=5)
        for a in d.activities:
            pic = tp["activities"].get(photos.activity_key(a.title)) if photos.has_photo_subject(a) else None
            img = _image(pic, THUMB_W / THUMB_H, 480)
            text_w = pdf.width - TIME_W - (THUMB_W + 4 if img else 0)
            full_w = pdf.width - TIME_W
            meta = " · ".join(x for x in (CATS.get(str(getattr(a.category, "value", a.category)), ""),
                                          "draussen" if str(getattr(a.exposure, "value", a.exposure)) == "outdoor" else "",
                                          "anstrengend" if int(a.intensity) == 3 else "",
                                          chf(a.cost) if a.cost else "",
                                          "gebucht" if a.booking else ("fest" if a.fixed else "")) if x)
            v, s = ven.get(a.id), sig.get(a.id)
            extra = []
            if v:
                r = f" ★ {v['rating']:.1f} ({count(v['reviews'])} Google-Bewertungen)" if v.get("rating") else ""
                extra.append((f"Restaurant: {v['name']}{r}", v["url"]))
            if s:
                r = f"★ {s['rating']:.1f} ({count(s['reviews'])} Bewertungen)" if s.get("rating") else "Bewertungen"
                extra.append((f"TripAdvisor {r}", s["url"]))
                if s.get("tip"):
                    extra.append(("**Tipp:** " + s["tip"].replace("*", ""), ""))
            h = (pdf.height_of(a.title, 11, text_w, True) + pdf.height_of(meta, 9, text_w)
                 + (pdf.height_of(a.note, 10, text_w if img else full_w) if a.note else 0) + 5.2 * len(extra) + 4)
            pdf.need(max(h, THUMB_H + 4) if img else h)
            y = pdf.get_y()
            pdf.set_xy(pdf.l_margin, y)
            pdf.font(10.5, True)
            pdf.cell(TIME_W, 5.5, f"{a.start:%H:%M}", new_x=XPos.LEFT, new_y=YPos.NEXT)
            pdf.font(9, color=MUTED)
            pdf.cell(TIME_W, 4.5, f"bis {a.end:%H:%M}")
            if img:
                pdf.image(img, x=pdf.w - pdf.r_margin - THUMB_W, y=y + 0.5, w=THUMB_W, h=THUMB_H)
            x = pdf.l_margin + TIME_W
            pdf.set_y(y)
            pdf.para(a.title, 11, True, w=text_w, h=5.5, x=x)
            if meta:
                pdf.para(meta, 9, color=MUTED, w=text_w, h=4.5, x=x)
            note = _PLUS_CODE.sub(".", a.note or "")
            if note:
                pdf.ln(0.8)
                # neben dem Foto schmal, sonst über die volle Breite
                pdf.para(note, 10, w=text_w if img and pdf.get_y() < y + THUMB_H else full_w, h=5, x=x)
            for txt, link in extra:
                pdf.para(txt, 9.5, color=ACCENT if link else TEXT, w=full_w, h=5.2, x=x, link=link, md=not link)
            if img and pdf.get_y() < y + THUMB_H + 1:
                pdf.set_y(y + THUMB_H + 1)
            pdf.ln(3)

    # --- Hinweise ----------------------------------------------------------------------------------
    notes = [n for n in store.get("plan_notes", it.id, []) if isinstance(n, str)] + ([it.cost_note] if it.cost_note else [])
    if notes:
        pdf.section("Hinweise")
        for n in notes:
            pdf.para(f"• {n}", 9.5, color=MUTED, h=5)
            pdf.ln(1)
    return bytes(pdf.output()), filename(it)

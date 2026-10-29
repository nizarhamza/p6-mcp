"""Wordmark files of assets/brand: the lockups and the README banner of "MCP for Primavera P6", text outlined
(no font needed to show them), and their PNG renderings. The mark and the app icons are not touched.

"MCP" is set large and "for Primavera P6" smaller in another color: the tag line form Oracle's trademark
guidelines ask for ("XYZ for Oracle database"). Needs PySide6 and IBM Plex Sans (SIL OFL) Bold, Medium and
Regular as TTF files:

    python tools/make_brand.py <IBMPlexSans-Bold.ttf> <IBMPlexSans-Medium.ttf> <IBMPlexSans-Regular.ttf>
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QImage, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND = os.path.join(ROOT, "assets", "brand")

NAVY, BLUE, LIGHT_BLUE, SLATE, MIST = "#0A1A33", "#1F6FEB", "#58A6FF", "#4A5B73", "#9FB3D1"
DESCRIPTOR = "Local MCP server for P6 schedules"
PILLS = ("Runs locally", "Any MCP client", "DCMA 14-point check", "Safe write-back")


def mark(body: str, bars: str) -> str:
    """The plug mark (two Gantt bars and a milestone), 120 x 120 units, as in svg/p6mcp-mark.svg."""
    return (f'<g transform="translate(6 0)"><rect x="4" y="56" width="16" height="8" rx="4" fill="{body}"/>'
            f'<path d="M36 28H48Q52 28 52 32V88Q52 92 48 92H36A20 20 0 0 1 16 72V48A20 20 0 0 1 36 28Z" '
            f'fill="{body}"/><rect x="58" y="42" width="46" height="12" rx="6" fill="{bars}"/>'
            f'<rect x="58" y="66" width="26" height="12" rx="6" fill="{bars}"/>'
            f'<path d="M96 64L104 72L96 80L88 72Z" fill="{bars}"/></g>')


class Type:
    def __init__(self, bold: str, medium: str, regular: str):
        self.files = {700: bold, 500: medium, 400: regular}
        self.family = {}
        for weight, path in self.files.items():
            fid = QFontDatabase.addApplicationFont(path)
            if fid < 0:
                sys.exit(f"cannot load the font {path}")
            self.family[weight] = QFontDatabase.applicationFontFamilies(fid)[0]

    def font(self, weight: int, px: float, tracking_em: float = 0.0) -> QFont:
        f = QFont(self.family[weight])
        f.setWeight(QFont.Weight(weight))
        f.setPixelSize(round(px))
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        f.setKerning(True)
        if tracking_em:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, tracking_em * px)
        return f


def width(font: QFont, text: str) -> float:
    return QFontMetricsF(font).horizontalAdvance(text)


def path_d(text: str, font: QFont, x: float, baseline: float) -> str:
    """SVG path data of text set at (x, baseline)."""
    p = QPainterPath()
    p.addText(QPointF(x, baseline), font, text)
    out, i, n = [], 0, p.elementCount()

    def num(v: float) -> str:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    while i < n:
        e = p.elementAt(i)
        if e.isMoveTo():
            out.append(f"M{num(e.x)} {num(e.y)}")
            i += 1
        elif e.isLineTo():
            out.append(f"L{num(e.x)} {num(e.y)}")
            i += 1
        else:  # a cubic curve: this element and the next two (control 2, end point)
            c2, end = p.elementAt(i + 1), p.elementAt(i + 2)
            out.append(f"C{num(e.x)} {num(e.y)} {num(c2.x)} {num(c2.y)} {num(end.x)} {num(end.y)}")
            i += 3
    return "".join(out) + "Z"


def svg(w: float, h: float, body: str, title: str = "MCP for Primavera P6") -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}" height="{h:g}" '
            f'role="img"><title>{title}</title>{body}</svg>\n')


def wordmark(t: Type, x: float, baseline: float, name_px: float, name: str, tag: str) -> tuple[str, float]:
    """'MCP' (Bold) then 'for Primavera P6' (Medium, half size) on the same baseline; returns (svg, right edge)."""
    big = t.font(700, name_px, -0.03)
    small = t.font(500, name_px * 0.5, -0.01)
    gap = name_px * 0.22
    x2 = x + width(big, "MCP") + gap
    body = (f'<path d="{path_d("MCP", big, x, baseline)}" fill="{name}"/>'
            f'<path d="{path_d("for Primavera P6", small, x2, baseline)}" fill="{tag}"/>')
    return body, x2 + width(small, "for Primavera P6")


def lockup(t: Type, body_color: str, bars: str, name: str, tag: str, descriptor: str | None) -> str:
    text, right = wordmark(t, 150, 68, 68, name, tag)
    if descriptor:
        reg = t.font(400, 22)
        text += f'<path d="{path_d(DESCRIPTOR, reg, 151, 104)}" fill="{descriptor}"/>'
        right = max(right, 151 + width(reg, DESCRIPTOR))
    return svg(round(right + 12), 120, mark(body_color, bars) + text)


def banner(t: Type) -> str:
    parts = [f'<rect width="1280" height="320" fill="{NAVY}"/>',
             f'<g transform="translate(72 92) scale(1.1333)">{mark("#FFFFFF", LIGHT_BLUE)}</g>']
    text, _ = wordmark(t, 240, 142, 76, "#FFFFFF", LIGHT_BLUE)
    parts.append(text)
    sub = t.font(400, 22)
    parts.append(f'<path d="{path_d(DESCRIPTOR, sub, 242, 178)}" fill="{MIST}"/>')
    pill, x = t.font(500, 13.5), 240.5
    for label in PILLS:
        w = width(pill, label) + 26
        parts.append(f'<rect x="{x:.2f}" y="201" width="{w:.2f}" height="29" rx="14.5" fill="none" '
                      f'stroke="#2B4166"/><path d="{path_d(label, pill, x + 13, 220)}" fill="#DCE6F5"/>')
        x += w + 9.5
    bars = ('<rect x="0" y="16" width="120" height="14" rx="7"/><rect x="60" y="52" width="150" height="14" '
            'rx="7"/><rect x="132" y="88" width="96" height="14" rx="7"/><rect x="196" y="124" width="104" '
            'height="14" rx="7"/>')
    parts.append(f'<g transform="translate(888 60)" fill="#1B3156">{bars}</g>'
                 f'<g transform="translate(888 60)" fill="{LIGHT_BLUE}"><rect x="132" y="88" width="96" '
                 f'height="14" rx="7" fill-opacity="0.85"/><path d="M300 152L310 162L300 172L290 162Z"/></g>')
    return svg(1280, 320, "".join(parts))


def png(svg_text: str, dest: str, height: int) -> None:
    r = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    size = r.defaultSize()
    w = round(size.width() * height / size.height())
    img = QImage(w, height, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, w, height))
    p.end()
    if not img.save(dest):
        sys.exit(f"cannot write {dest}")


def write(rel: str, text: str) -> None:
    with open(os.path.join(BRAND, rel), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(rel)


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    _app = QGuiApplication(sys.argv[:1])  # noqa: F841  (fonts need an application)
    t = Type(*sys.argv[1:4])
    files = {
        "p6mcp-lockup.svg": lockup(t, NAVY, BLUE, NAVY, BLUE, SLATE),
        "p6mcp-lockup-on-dark.svg": lockup(t, "#FFFFFF", LIGHT_BLUE, "#FFFFFF", LIGHT_BLUE, MIST),
        "p6mcp-lockup-black.svg": lockup(t, "#000000", "#000000", "#000000", "#000000", None),
        "p6mcp-lockup-white.svg": lockup(t, "#FFFFFF", "#FFFFFF", "#FFFFFF", "#FFFFFF", None),
        "p6mcp-banner.svg": banner(t),
    }
    for name, text in files.items():
        write(f"svg/{name}", text)
    for name, height in (("p6mcp-lockup", 96), ("p6mcp-lockup-on-dark", 96), ("p6mcp-banner", 320)):
        png(files[f"{name}.svg"], os.path.join(BRAND, "png", f"{name}.png"), height)
        png(files[f"{name}.svg"], os.path.join(BRAND, "png", f"{name}@2x.png"), height * 2)
        print(f"png/{name}.png, png/{name}@2x.png")


if __name__ == "__main__":
    main()

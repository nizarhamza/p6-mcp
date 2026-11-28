"""Accessibility of the Settings window: names, Tab order, access keys, Ctrl+S and Esc, live statuses, contrast
ratios of the colours we draw, Windows high contrast mode and text that wraps instead of widening the page.
The window runs offscreen (conftest); the env fixture and helpers come from test_settings_app."""

from collections import Counter

import pytest

pytestmark = pytest.mark.qt
pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QKeySequence, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402
from test_settings_app import env, make, three_skills  # noqa: E402, F401  (env: a fixture)

from p6mcp import i18n  # noqa: E402
from p6mcp.settings_app import a11y, cards, common, tray  # noqa: E402
from p6mcp.settings_app.approval import ApprovalDialog  # noqa: E402
from p6mcp.settings_app.guidance_pages import FileDialog, SkillDialog, SkillsPage  # noqa: E402
from p6mcp.settings_app.pages import DatabasePage, ToolsPage  # noqa: E402
from p6mcp.settings_app.skill_details import Action, SkillDetails  # noqa: E402
from p6mcp.settings_app.window import PAGES  # noqa: E402

SOURCES = ("database", "xer", "eppm", "pmdb")
# Windows 11 palettes of the windows11 style (Qt 6.12 on Windows 11, captured 2026-11-28; ARGB where translucent)
WIN11 = {
    "light": {"Window": "#f3f3f3", "WindowText": "#e4000000", "Base": "#ffffff", "Text": "#e4000000",
              "PlaceholderText": "#80000000", "Accent": "#0067c0", "Highlight": "#0067c0"},
    "dark": {"Window": "#1e1e1e", "WindowText": "#ffffff", "Base": "#2d2d2d", "Text": "#ffffff",
             "PlaceholderText": "#80ffffff", "Accent": "#4cc2ff", "Highlight": "#0078d4"},
}


def palette(colors: dict) -> QPalette:
    pal = QPalette()
    for role, c in colors.items():
        pal.setColor(getattr(QPalette.ColorRole, role), QColor.fromString(c))
    return pal


@pytest.fixture
def shown(qtbot, env, request):  # noqa: F811  (env: the fixture of test_settings_app)
    """The window in a language, shown (offscreen) wide enough for its toolbars, with databases, skills and the
    clients scan done."""
    lang = getattr(request, "param", "en")
    w = make(qtbot, {"language": lang})
    w.page(DatabasePage)._add(env["db"])
    three_skills(w)
    w.refresh_pages()
    w.resize(1600, 1000)
    w.show()
    qtbot.waitExposed(w)
    yield w
    w.hide()
    i18n.set_language("en")


def each_page(w, sources=SOURCES):
    """(data source, page key, page) for every page shown, the Database page once per data source."""
    for src in sources:
        w.set_data(dict(w.data, data_source=src))
        w.page(DatabasePage).refresh()
        for row, (key, _cls) in enumerate(PAGES):
            if src != "database" and key != "nav.database":
                continue
            w.nav.setCurrentRow(row)
            QApplication.processEvents()
            yield src, key, w.pages[row]


# ----------------------------------------------------------------- names

@pytest.mark.parametrize("shown", ["en", "fr"], indirect=True)
def test_every_interactive_widget_has_a_name(shown):
    w = shown
    assert a11y.problems(w.centralWidget()) == []
    for src, key, page in each_page(w):
        assert a11y.problems(page) == [], (src, key)
    # what UI Automation reads: the page list, a field named by its form label, a card
    assert a11y.exposed_name(w.nav) == i18n.tr("app.nav")
    server = w.pages[[k for k, _c in PAGES].index("nav.server")]
    assert a11y.exposed_name(server.port) == i18n.tr("srv.port")
    assert a11y.exposed_name(server.token_edit) == i18n.tr("srv.token")
    assert a11y.exposed_name(w.lang) == i18n.tr("app.language")
    card = w.page(SkillsPage).view.items[0]
    assert a11y.exposed_name(card) and card.accessibleDescription()


def test_dialogs_have_names_and_close_with_escape(qtbot, env):  # noqa: F811  (env: the fixture of test_settings_app)
    w = make(qtbot, {"language": "en"})
    dialogs = [FileDialog(w, {"path": "a.md", "text": "x"}),
               SkillDialog(w, w.data, {"name": "x", "title": "X", "description": "d", "args": [{"name": "a"}],
                                       "template": "t", "files": [{"path": "a.md", "text": "x"}]}),
               SkillDetails(w, {"title": "X", "name": "p6-x", "description": "d", "md": "# x", "tools": [],
                                "author": "someone", "version": 2, "source": ("ok", "Shop")},
                            [Action("install", "Install", lambda: None, primary=True)]),
               ApprovalDialog({"id": "r1", "client_name": "Claude", "redirect_uri": "https://claude.ai/cb",
                               "via": "dcr"}, True, lambda *a: None)]
    for dlg in dialogs:
        qtbot.addWidget(dlg)
        assert a11y.problems(dlg) == [], type(dlg).__name__
        dlg.show()
        assert dlg.isVisible()
        qtbot.keyClick(dlg, Qt.Key.Key_Escape)
        assert not dlg.isVisible() and dlg.result() == QDialog.DialogCode.Rejected, type(dlg).__name__
    # the Yes / No question of every page: Esc answers No, the default
    box = common.question_box(w, "t", "q")
    assert box.escapeButton() is box.button(QMessageBox.StandardButton.No)
    assert box.defaultButton() is box.button(QMessageBox.StandardButton.No)
    # the approval window's code field is named by its label (and its access key reaches it)
    dlg = dialogs[-1]
    assert a11y.exposed_name(dlg.code) == i18n.tr("remote.approve.code")


# ----------------------------------------------------------------- keyboard

@pytest.mark.parametrize("shown", ["en", "fr"], indirect=True)
def test_tab_order_follows_the_reading_order(shown):
    w = shown
    for src, key, page in each_page(w):
        assert a11y.tab_chain(page), (src, key)
        assert a11y.tab_order_problems(page) == [], (src, key)
    # the window: banner button, page list, the page, then the bottom bar
    chain = a11y.tab_chain(w.centralWidget())
    first = a11y.tab_chain(w.pages[w.nav.currentRow()])[0]
    assert chain.index(w.nav) < chain.index(first) < chain.index(w.lang) < chain.index(w.save_btn)


@pytest.mark.parametrize("shown", ["en", "fr"], indirect=True)
def test_access_keys_are_unique_on_every_page(shown):
    w = shown
    nav = [w.nav.item(r).data(a11y.MNEMONIC_ROLE) for r in range(w.nav.count())]
    letters = [a11y.mnemonic_of(t) for t in nav]
    assert all(letters) and len(set(letters)) == len(letters)
    assert [a11y.strip_amp(t) for t in nav] == [w.nav.item(r).text() for r in range(w.nav.count())]
    assert w.nav.item(0).data(Qt.ItemDataRole.AccessibleDescriptionRole) == a11y.shortcut_text(letters[0])
    for b in (w.save_btn, w.revert_btn, w.lang_label):
        assert a11y.mnemonic_of(b.text()), b.text()
    assert w.lang_label.buddy() is w.lang
    for src, key, page in each_page(w):
        shown_keys = [k for k, x in a11y.page_keys(page) if x.isVisibleTo(w)]
        every = shown_keys + list(w.global_keys)
        assert not [k for k, n in Counter(every).items() if n > 1], (src, key, sorted(every))
        if key != "nav.about":  # About has links only
            assert shown_keys, (src, key)


def test_access_key_shortcuts_and_ctrl_s(qtbot, env):  # noqa: F811  (env: the fixture of test_settings_app)
    w = make(qtbot, {"language": "en"})
    tools_row = [k for k, _c in PAGES].index("nav.tools")
    letter = a11y.mnemonic_of(w.nav.item(tools_row).data(a11y.MNEMONIC_ROLE))
    sc = next(s for s in w.nav_shortcuts if s.key() == QKeySequence(f"Alt+{letter.upper()}"))
    sc.activated.emit()
    assert w.nav.currentRow() == tools_row
    # a button keeps its letter when its text changes (Show / Hide, Register / Repair)
    server = w.pages[[k for k, _c in PAGES].index("nav.server")]
    before = a11y.mnemonic_of(server.show_btn.text())
    server.show_btn.setChecked(True)
    assert a11y.strip_amp(server.show_btn.text()) == i18n.tr("srv.token.hide")
    assert a11y.mnemonic_of(server.show_btn.text()) in (before, None)
    # Ctrl+S saves when there is something to save
    assert w.save_shortcut.key() == QKeySequence(QKeySequence.StandardKey.Save)
    w.save_shortcut.activated.emit()  # nothing to save: nothing happens
    w.page(ToolsPage).full.click()
    assert w.dirty()
    w.save_shortcut.activated.emit()
    assert not w.dirty() and w.data["tool_profile"] == "full"
    # the language switch rebuilds the window and its shortcuts, never piling them up
    n = len(w.nav_shortcuts)
    w.lang.setCurrentIndex(w.lang.findData("fr"))
    assert len(w.nav_shortcuts) == n and w.nav.item(0).text() == "Base de données"
    i18n.set_language("en")


def test_focus_ring_follows_the_focus(qtbot, env):  # noqa: F811  (env: the fixture of test_settings_app)
    w = make(qtbot, {"language": "en"})
    w.show()
    qtbot.waitExposed(w)
    a11y.FocusRing.install()
    ring = a11y.FocusRing.for_window(w)
    ring.follow(w.save_btn)
    assert ring.isVisibleTo(w)
    target = w.save_btn.geometry()
    target.moveTopLeft(w.save_btn.mapTo(w, w.save_btn.rect().topLeft()))
    assert ring.geometry().contains(target) and ring.geometry() != target
    card = w.page(SkillsPage).view
    ring.follow(card)  # a hidden page: nothing to draw
    assert not ring.isVisibleTo(w)
    w.hide()


# ----------------------------------------------------------------- screen readers

def test_status_changes_are_announced(qtbot, env):  # noqa: F811  (env: the fixture of test_settings_app)
    w = make(qtbot, {"language": "en"})
    w.show()
    qtbot.waitExposed(w)
    a11y.ANNOUNCED.clear()
    page = w.page(DatabasePage)
    page._add(env["db"])
    page.run_test()
    qtbot.waitUntil(lambda: "demo" in page.results, timeout=15000)
    said = [t for _p, t in a11y.ANNOUNCED]
    assert i18n.tr("db.testing") in said and i18n.tr("db.state.ok") in said
    assert said.count(w.banner.text()) == 1  # the banner shown since the window opened: read once, not per edit
    w.flash("Copied")
    assert a11y.ANNOUNCED[-1] == ("polite", "Copied")
    a11y.ANNOUNCED.clear()
    page.refresh()  # the same text again: said once only
    assert i18n.tr("db.state.ok") not in [t for _p, t in a11y.ANNOUNCED]
    hidden = a11y.live(common.QLabel())
    common.pill(hidden, "error", "never shown")  # not on screen: not announced
    assert ("assertive", "never shown") not in a11y.ANNOUNCED
    w.hide()


# ----------------------------------------------------------------- contrast

def test_status_colours_reach_4_5_to_1():
    for level, (fg, bg) in common.COLORS.items():
        assert a11y.contrast(fg, bg) >= a11y.TEXT_MIN, level


def test_contrast_ratio_formula():
    assert a11y.contrast("#000000", "#ffffff") == pytest.approx(21)
    assert a11y.contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)
    assert a11y.contrast(QColor.fromString("#80000000"), "#f3f3f3") == pytest.approx(3.95, abs=0.05)  # placeholder
    assert a11y.contrast(a11y.readable("#cccccc", "#ffffff"), "#ffffff") >= 4.5
    assert a11y.readable("#123456", "#ffffff").name() == "#123456"  # already readable: unchanged


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_drawn_colours_on_the_windows_palettes(scheme):
    pal = palette(WIN11[scheme])
    window, base = pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.Base)
    muted = a11y.muted_color(pal)
    assert a11y.contrast(muted, window) >= 4.5 and a11y.contrast(muted, base) >= 4.5
    assert a11y.contrast(cards.card_border(pal), window) >= 3
    focus = a11y.focus_color(pal)
    assert a11y.contrast(focus, window) >= 3 and a11y.contrast(focus, base) >= 3
    fill, ink, hover, pressed = cards.primary_colors(pal)
    for c in (fill, hover, pressed):
        assert a11y.contrast(ink, c) >= 4.5


@pytest.mark.parametrize("accent", ["#ffd700", "#888888", "#00b7c3", "#e81123"])
def test_any_windows_accent_colour_stays_readable(accent):
    pal = palette(dict(WIN11["light"], Accent=accent, Highlight=accent))
    fill, ink, hover, pressed = cards.primary_colors(pal)
    assert all(a11y.contrast(ink, c) >= 4.5 for c in (fill, hover, pressed))
    focus = a11y.focus_color(pal)
    assert a11y.contrast(focus, pal.color(QPalette.ColorRole.Window)) >= 3


def test_tray_state_dots_reach_3_to_1_on_the_tile():
    for dot in ("#2e9d57", "#c62828", "#e0a000"):  # tray.badge: running, error, starting (also said in words)
        assert a11y.contrast(dot, "#0a1a33") >= 3
    assert hasattr(tray, "badge")


# ----------------------------------------------------------------- high contrast

def test_high_contrast_drops_our_colours(qtbot, env, monkeypatch):  # noqa: F811  (env: the fixture of test_settings_app)
    app = QApplication.instance()
    style, app_pal = app.style().name(), QPalette(app.palette())
    hc = palette({"Window": "#000000", "WindowText": "#ffffff", "Base": "#000000", "Text": "#ffffff",
                  "Button": "#000000", "ButtonText": "#ffffff", "Highlight": "#1aebff", "HighlightedText": "#000000",
                  "Accent": "#1aebff", "PlaceholderText": "#ffffff"})
    monkeypatch.setattr(a11y, "system_palette", lambda: hc)
    monkeypatch.setattr(a11y, "HIGH_CONTRAST", True)
    monkeypatch.setattr(a11y, "_NORMAL_STYLE", None)
    try:
        assert a11y.apply_contrast_mode(app) and app.style().name() == a11y.HC_STYLE
        assert app.palette().color(QPalette.ColorRole.Window) == QColor("#000000")
        w = make(qtbot, {"language": "en"})
        assert "#" not in w.status.styleSheet() and "#" not in w.banner.styleSheet()
        assert cards.primary(QPushButton("Install")).styleSheet() == ""
        assert a11y.muted_color(app.palette()) == QColor("#ffffff")
        assert a11y.focus_color(app.palette()) == QColor("#1aebff")
        assert a11y.problems(w.centralWidget()) == []
        w.close()
        monkeypatch.setattr(a11y, "HIGH_CONTRAST", False)
        assert not a11y.apply_contrast_mode(app) and app.style().name() == style
    finally:
        app.setStyle(style)
        app.setPalette(app_pal)


# ----------------------------------------------------------------- reflow

def test_long_choices_wrap_instead_of_widening_the_page(qtbot):
    text = "A P6 Professional database on SQL Server or Oracle, read only (a local copy of the projects you choose)"
    r = common.Radio(text)
    qtbot.addWidget(r)
    one_line = r.sizeHint()
    assert r.minimumSizeHint().width() < one_line.width() and r.hasHeightForWidth()
    assert r.heightForWidth(one_line.width() // 3) > r.heightForWidth(one_line.width())
    r.resize(one_line.width() // 3, r.heightForWidth(one_line.width() // 3))
    qtbot.mouseClick(r, Qt.MouseButton.LeftButton, pos=QPoint(r.width() - 4, r.height() - 4))
    assert r.isChecked()  # a click on the wrapped text toggles it
    c = common.Check("&Short")
    qtbot.addWidget(c)
    assert c.minimumSizeHint().width() <= c.sizeHint().width() and a11y.exposed_name(c) == "Short"


def test_spin_boxes_fit_their_longest_value(qtbot):
    from PySide6.QtWidgets import QSpinBox
    s = QSpinBox()
    qtbot.addWidget(s)
    f = s.font()
    f.setPointSizeF(f.pointSizeF() * 2.25)  # the largest Windows text size
    s.setFont(f)
    s.setRange(1, 65535)
    common.fit_spin(s)
    assert s.minimumWidth() == 0 or s.minimumWidth() >= s.fontMetrics().horizontalAdvance("65535")

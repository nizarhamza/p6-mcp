import glob
import json
import os
import re

import pytest

from p6mcp import config, i18n, policy, settings_logic

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def catalog(code):
    with open(os.path.join(i18n.LOCALE_DIR, f"{code}.json"), encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}


def used_keys() -> set:
    keys = set()
    for path in glob.glob(os.path.join(ROOT, "p6mcp", "settings_app", "*.py")):
        src = open(path, encoding="utf-8").read()
        keys |= set(re.findall(r"""tr\(\s*["']([a-z_.0-9]+)["']""", src))
        keys |= set(re.findall(r"""["']((?:nav|db\.col|db\.kind|db\.r)\.[a-z_.0-9]+)["']""", src))
    keys |= {f"kind.{k}" for k in policy.KINDS} | {f"restart.{k}" for k in settings_logic.RESTART_KEYS}
    states = ("not_installed", "not_registered", "registered", "outdated", "error")
    keys |= {f"cl.state.{k}" for k in states} | {f"cl.detail.{k}" for k in states}
    return keys


def test_every_used_key_is_in_english():
    missing = {k for k in used_keys() if not k.endswith(".")} - set(catalog("en"))  # "kind." + ...: dynamic
    assert not missing, missing


@pytest.mark.parametrize("code", [c for c in i18n.available() if c != "en"])
def test_translations_are_complete_with_the_same_placeholders(code):
    en, other = catalog("en"), catalog(code)
    assert set(other) == set(en), set(en) ^ set(other)
    for k, v in en.items():
        assert set(re.findall(r"{(\w+)}", v)) == set(re.findall(r"{(\w+)}", other[k])), k


@pytest.mark.parametrize("code", ["en", "fr"])
def test_house_style(code):
    for k, v in catalog(code).items():
        assert not re.search("[–—«»“”]", v) and "--" not in v, k


def test_french_is_accented():
    fr = catalog("fr")
    assert fr["nav.database"] == "Base de données" and fr["db.state.failed"].startswith("Échec")


def test_fallback_rtl_and_resolution(monkeypatch):
    assert i18n.set_language("fr") == "fr" and i18n.tr("nav.tools") == "Outils et sécurité"
    assert i18n.tr("db.projects", n=2, eps=1, bl=0).startswith("projets")
    assert i18n.set_language("xx") == "en" and i18n.tr("nav.tools") == "Tools and safety"
    assert i18n.tr("no.such.key") == "no.such.key"
    assert i18n.is_rtl("ar") and not i18n.is_rtl("fr")
    monkeypatch.setattr(i18n, "system_language", lambda: "fr")
    assert i18n.resolve("auto") == "fr"
    assert config.DEFAULTS["language"] == "auto"
    i18n.set_language("en")

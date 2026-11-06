"""Theme behavior without a browser dependency; rendered pages get local visual QA."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

STATIC = Path("src/xskill/dashboard/static")


def _theme_script():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<script id="theme-init">(.*?)</script>', html, re.S)
    assert match, "theme must initialize in the head before first paint"
    assert match.end() < html.index('<style id="twcss">')
    return match.group(1)


def _run_theme(scenario):
    script = """
const vm = require('node:vm');
const source = SOURCE;
const scenario = SCENARIO;
const classes = new Set();
const root = {classList: {
  toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
  contains(name) { return classes.has(name); }
}, style: {}};
const buttons = ['light', 'dark'].map(theme => ({
  dataset: {theme}, attrs: {}, events: {},
  setAttribute(key, value) { this.attrs[key] = String(value); },
  addEventListener(key, fn) { this.events[key] = fn; }
}));
const events = {}, windowEvents = {}, writes = [];
const document = {
  documentElement: root,
  querySelectorAll() { return buttons; },
  addEventListener(key, fn) { events[key] = fn; }
};
const localStorage = {
  getItem() { if (scenario.blocked) throw new Error('blocked'); return scenario.saved ?? null; },
  setItem(key, value) { if (scenario.blocked) throw new Error('blocked'); writes.push([key, value]); }
};
vm.runInNewContext(source, {document, localStorage,
  window: {addEventListener(key, fn) { windowEvents[key] = fn; }}
});
const initial = classes.has('dark');
events.DOMContentLoaded();
for (const value of scenario.clicks || []) buttons.find(b => b.dataset.theme === value).events.click();
if (scenario.storage) windowEvents.storage(scenario.storage);
process.stdout.write(JSON.stringify({initial, dark: classes.has('dark'),
  scheme: root.style.colorScheme, pressed: buttons.map(b => b.attrs['aria-pressed']), writes}));
""".replace("SOURCE", json.dumps(_theme_script())).replace("SCENARIO", json.dumps(scenario))
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_default_and_invalid_preference_are_light():
    for saved in (None, "unknown", "light"):
        result = _run_theme({"saved": saved})
        assert result == {"initial": False, "dark": False, "scheme": "light",
                          "pressed": ["true", "false"], "writes": []}


def test_saved_dark_is_applied_before_dom_ready():
    result = _run_theme({"saved": "dark"})
    assert result["initial"] is True
    assert result["scheme"] == "dark"
    assert result["pressed"] == ["false", "true"]


def test_toggle_updates_theme_buttons_and_persists_choice():
    result = _run_theme({"clicks": ["dark", "light", "dark"]})
    assert result["dark"] is True
    assert result["scheme"] == "dark"
    assert result["pressed"] == ["false", "true"]
    assert result["writes"] == [["xskill.dashboard.theme", theme]
                                for theme in ("dark", "light", "dark")]


def test_storage_failure_does_not_disable_switching():
    result = _run_theme({"blocked": True, "clicks": ["dark"]})
    assert result["initial"] is False
    assert result["dark"] is True
    assert result["pressed"] == ["false", "true"]


def test_other_tabs_update_theme_without_writing_back():
    result = _run_theme({"storage": {"key": "xskill.dashboard.theme", "newValue": "dark"}})
    assert result["dark"] is True
    assert result["writes"] == []
    result = _run_theme({"saved": "dark", "storage": {"key": "other", "newValue": "light"}})
    assert result["dark"] is True
    result = _run_theme({"saved": "dark", "storage": {"key": None, "newValue": None}})
    assert result["dark"] is False


def test_theme_controls_are_accessible_and_not_login_gated():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<div[^>]*id="theme-toggle".*?</div>', html, re.S)
    assert match
    control = match.group()
    assert 'role="group"' in control
    assert 'aria-label="主题"' in control
    assert 'auth-user' not in control and 'auth-admin' not in control
    for theme, label in (("light", "浅色主题"), ("dark", "深色主题")):
        button = re.search(rf'<button[^>]*data-theme="{theme}"[^>]*>', control)
        assert button and f'aria-label="{label}"' in button.group()
        assert 'type="button"' in button.group()
        assert 'aria-pressed=' in button.group()


def test_compiled_styles_include_dark_mode_and_match_inlined_css():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "tw.css").read_text(encoding="utf-8").strip()
    inline = re.search(r'<style id="twcss">(.*?)</style>', html, re.S)
    assert inline and inline.group(1).strip() == css
    assert '.dark' in css and 'dark\\:bg-slate-900' in css

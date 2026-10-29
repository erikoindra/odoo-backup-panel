#!/usr/bin/env python3
"""Check WCAG contrast of the colour tokens in the app stylesheet.

Reads the light tokens (the first :root block) and the dark tokens
(@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { ... } }), then checks every
text/background pair the UI uses: 4.5:1 for text, 3:1 for the borders of form controls. Translucent
tokens would be blended, but this palette uses solid colours only. Exit 1 if any pair fails.

    python3 scripts/check_contrast.py [path/to/app.css]
"""

import re
import sys
from pathlib import Path

DEFAULT_CSS = Path(__file__).parent.parent / "backup_panel" / "panel" / "static" / "panel" / "app.css"

TEXT, CONTROL = 4.5, 3.0

# (foreground, background, where it is used, minimum ratio)
PAIRS = [
    ("text", "bg", "body text", TEXT),
    ("text", "panel", "text in cards", TEXT),
    ("text", "panel-2", "text on hovered rows", TEXT),
    ("muted", "bg", "secondary text on the page", TEXT),
    ("muted", "panel", "secondary text in cards", TEXT),
    ("muted", "panel-2", "table headers, chips", TEXT),
    ("faint", "bg", "hints, placeholders", TEXT),
    ("faint", "panel", "sub-lines in cards", TEXT),
    ("faint", "panel-2", "sub-lines on hovered rows", TEXT),
    ("accent", "bg", "links on the page", TEXT),
    ("accent", "panel", "links in cards", TEXT),
    ("accent", "panel-2", "links on hovered rows", TEXT),
    ("accent", "accent-soft", "running step, info alert, active step number", TEXT),
    ("on-accent", "accent", "primary button", TEXT),
    ("good", "panel", "status Fresh", TEXT),
    ("good", "good-soft", "success alert, done step number", TEXT),
    ("warn", "panel", "status Stale", TEXT),
    ("warn", "warn-soft", "warning alert", TEXT),
    ("bad", "panel", "status Container stopped, errors", TEXT),
    ("bad", "bg", "errors under inputs", TEXT),
    ("bad", "bad-soft", "error alert", TEXT),
    ("bg", "bad", "danger button (page colour on red)", TEXT),
    ("line-strong", "panel", "border of buttons and inputs on cards", CONTROL),
    ("line-strong", "bg", "border of buttons and inputs on the page", CONTROL),
]


def parse_tokens(css):
    declarations = re.compile(r"--([a-z0-9-]+):\s*([^;]+);")
    light_block = re.search(r"^:root\s*\{(.*?)\n\}", css, re.DOTALL | re.MULTILINE)
    dark_block = re.search(
        r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme=\"light\"\]\)\s*\{(.*?)\n  \}",
        css,
        re.DOTALL,
    )
    light = dict(declarations.findall(light_block.group(1)))
    dark = {**light, **dict(declarations.findall(dark_block.group(1)))}
    return {"light": light, "dark": dark}


def rgb(value):
    value = value.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"unsupported colour: {value}")
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def luminance(colour):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def main():
    css_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSS
    themes = parse_tokens(css_path.read_text())
    failures = 0
    for theme, tokens in themes.items():
        for fg, bg, where, minimum in PAIRS:
            r = ratio(rgb(tokens[fg]), rgb(tokens[bg]))
            ok = r >= minimum
            failures += not ok
            print(f"{theme:5}  {fg:12} on {bg:12} {r:5.2f} (>= {minimum:g})  {'ok' if ok else 'FAIL'}  ({where})")
    print(f"\n{failures} pair(s) below their minimum" if failures else f"\nall {len(PAIRS) * 2} pairs pass")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

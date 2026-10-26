import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "tapo_web" / "static"
EXTERNAL = re.compile(r"^(https?:)?//", re.I)


def _asset_refs(html):
    refs = re.findall(r'<(?:link|script|img)\b[^>]*?(?:href|src)="([^"]+)"', html)
    return refs


def test_login_page_loads_nothing_from_the_internet(web):
    html = web.client.get("/login").get_data(as_text=True)
    refs = _asset_refs(html)
    assert refs, "the page should reference its own CSS and JS"
    assert not [ref for ref in refs if EXTERNAL.match(ref)]
    for ref in refs:
        assert ref.startswith("/static/"), ref


def test_vendored_css_has_no_external_urls():
    for css in STATIC.rglob("*.css"):
        text = css.read_text(encoding="utf-8")
        assert "@import" not in text or not re.search(r"@import\s+url\((['\"]?)(https?:)?//", text), css
        assert not [u for u in re.findall(r"url\((['\"]?)([^)'\"]+)\1\)", text) if EXTERNAL.match(u[1])], css


def test_every_referenced_static_file_exists(web):
    html = web.client.get("/login").get_data(as_text=True)
    for ref in _asset_refs(html):
        assert web.client.get(ref).status_code == 200, ref


def test_licenses_kept_and_nucleo_font_absent():
    assert (STATIC / "vendor" / "now-ui-kit" / "LICENSE.md").read_text(encoding="utf-8").startswith("MIT License")
    assert (STATIC / "vendor" / "material-icons" / "LICENSE").exists()
    assert not [p for p in STATIC.rglob("*") if "nucleo" in p.name.lower()]

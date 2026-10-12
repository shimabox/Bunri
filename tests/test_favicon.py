"""The tab icon embedded in Bunri's pages."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from bunri.favicon import FAVICON_HREF, FAVICON_SVG

_PREFIX = "data:image/svg+xml,"


def test_favicon_is_a_coral_tile_with_three_centred_circles():
    root = ET.fromstring(FAVICON_SVG)

    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.attrib == {"viewBox": "0 0 64 64"}
    assert [(child.tag.rpartition("}")[2], child.attrib) for child in root] == [
        ("rect", {"width": "64", "height": "64", "rx": "14", "fill": "#e2674e"}),
        ("circle", {"cx": "32", "cy": "32", "r": "23", "fill": "#fff2e5"}),
        ("circle", {"cx": "32", "cy": "32", "r": "15", "fill": "#3a322c"}),
        ("circle", {"cx": "32", "cy": "32", "r": "6", "fill": "#f3c45b"}),
    ]


def test_favicon_href_carries_the_svg_and_needs_no_escaping():
    assert FAVICON_HREF.startswith(_PREFIX)
    assert unquote(FAVICON_HREF[len(_PREFIX):]) == FAVICON_SVG
    # Safe as an HTML attribute value as is, and names no address a page
    # could be said to load from.
    assert re.fullmatch(r"[A-Za-z0-9%._~:/+,;-]+", FAVICON_HREF)
    assert "://" not in FAVICON_HREF

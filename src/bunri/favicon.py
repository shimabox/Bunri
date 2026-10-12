"""The icon Bunri's pages show in the browser tab."""

from __future__ import annotations

from urllib.parse import quote

FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#e2674e"/>'
    '<circle cx="32" cy="32" r="23" fill="#fff2e5"/>'
    '<circle cx="32" cy="32" r="15" fill="#3a322c"/>'
    '<circle cx="32" cy="32" r="6" fill="#f3c45b"/>'
    "</svg>"
)

# The icon travels inside the page instead of as a file next to it: a
# practice player is one HTML file that also opens straight from disk, where
# there is no server to ask for an icon. Everything is percent-encoded so the
# page still names no outside address, not even the SVG namespace.
FAVICON_HREF = "data:image/svg+xml," + quote(FAVICON_SVG, safe="")

"""StemLab local web UI: upload audio, run the existing `stemlab` CLI as a
background subprocess, and browse the resulting practice packages from a
browser. Deliberately import-light (no torch / audio_separator at module
load time) so `stemlab-web` starts instantly; the actual separation work
happens in a subprocess of the already-existing `stemlab` CLI."""

from __future__ import annotations

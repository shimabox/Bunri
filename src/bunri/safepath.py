"""Writing to paths that are what they claim to be.

Two rules, both about symlinks, both needed everywhere this app puts a file:
create directories a component at a time, and produce files by renaming a
temporary over the destination.

This module exists rather than the rules being copied into each layer. The
CLI's package/cache code and the web layer's job store deliberately keep
their import graphs apart -- see web/jobs.py's module docstring -- and small
rules like `_safe_filename` are duplicated between them on purpose. These
are not, for the opposite reason: they are security checks, and the last time
two copies of one of those existed here they drifted apart and reopened the
hole they were both written to close (the package-path rule that casefolded
on one side and not the other). One rule, one place. Nothing here imports
anything heavier than the standard library, so it stays cheap for the web
layer to depend on.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Callable


class UnsafeOutputPath(OSError):
    """Refusal to touch something that is not what it claims to be.

    Deliberately an OSError: callers of file I/O already handle OSError from
    a missing or unreadable file, so a refusal degrades along a path that
    already exists instead of needing a new one.
    """


def real_subdir(base: Path, *parts: str) -> Path:
    """Where `<base>/<parts...>` must *really* be: base's own resolved
    location plus the literal components.

    The construction matters, and getting it wrong was a live bug. The
    obvious check -- resolve the candidate, resolve the directory it is
    supposed to be in, compare -- is a tautology whenever that directory is
    itself the symlink: with `web/logs -> /outside`, both sides resolve to
    `/outside` and the comparison passes, and the runner then truncates files
    there. Only `base` is resolved here, so the expected value stays a real
    path inside the real output tree, and a component that points somewhere
    else lands the candidate somewhere this value can never equal.

    Resolving `base` (rather than taking it literally) is equally deliberate
    in the other direction: an output directory legitimately sits under a
    symlink on plenty of systems -- /tmp on macOS is one -- and those
    installations must keep working.
    """
    return base.resolve().joinpath(*parts)


def is_really(candidate: Path, expected: Path) -> bool:
    """True if `candidate` resolves to exactly `expected`. `expected` is used
    as given -- it is a value built by `real_subdir`, and resolving it again
    is precisely the tautology described there."""
    try:
        return candidate.resolve() == expected
    except OSError:
        return False


def verified_mkdir(base: Path, *parts: str) -> Path:
    """`<base>/<parts...>`, created if missing, with every component checked
    as it is walked.

    Splitting the walk matters: `mkdir(parents=True)` on the whole path
    follows any symlink it meets and happily creates the rest of the tree on
    the far side, so a check afterwards is too late -- with `web -> /outside`
    and nothing at the other end, the server made `/outside/uploads` and
    `/outside/logs` before deciding it did not like them. Nothing outside
    `base` may be created, not even an empty directory.

    Each component is either created here (so it cannot be a link) or
    verified with `is_symlink()` -- an lstat, which does not follow what it is
    testing. The base is resolved, so a `base` that itself lives under a
    symlink keeps working; see `real_subdir`.
    """
    path = base.resolve()
    if not path.is_dir():
        raise UnsafeOutputPath(f"{base} is not a directory")
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise UnsafeOutputPath(f"{path} is a symlink; refusing to use it")
        try:
            path.mkdir(exist_ok=True)
        except OSError as exc:
            raise UnsafeOutputPath(f"cannot use {path}: {exc}") from exc
        if not path.is_dir():
            raise UnsafeOutputPath(f"{path} is not a directory")
    return path


def is_real_file_in(path: Path, expected_dir: Path) -> bool:
    """True if `path` is an ordinary file sitting directly in `expected_dir`.

    Guarding writes is only half the job, and the missing half was a real
    hole: a cache that answers "already done" for a symlink hands the link's
    target to whatever reads the cache next, and here that means copying it
    into a package the user shares. `exists()` follows links and says yes to
    all of it. So the read side asks the same questions the write side does
    -- is this a link, is it even a regular file, is it where it claims to be.

    `expected_dir` is used as given: a value the caller built (see
    `real_subdir`), never a resolve of `path`'s own parent, which would make
    the last comparison a tautology.
    """
    try:
        if path.is_symlink():  # lstat, so it does not follow what it is testing
            return False
        if not path.is_file():  # not a directory, a fifo, a device
            return False
        return path.resolve().parent == expected_dir
    except OSError:
        return False


def replace_into(dest: Path, write: Callable[[Path], None]) -> Path:
    """Produce `dest` by writing a uniquely-named temporary beside it and
    renaming that into place.

    Every file this app creates goes through here, and the reason is
    symlinks. Writing straight to `dest` -- copyfile, ffmpeg's output file,
    write_text, soundfile -- all follow a link sitting there and clobber
    whatever it points at, and every name involved is derivable from the
    input, so a link can be waiting at any of them. `os.replace` does not
    follow: it swaps the *name*, so a planted link is what gets replaced, not
    its target. Containment checks on the enclosing directory cannot help,
    because the link is the final component and resolves wherever it likes.

    Atomicity comes free with it -- a reader never sees a half-written file
    -- and it is the same write-then-rename shape the job records already
    use.

    The temporary keeps `dest`'s suffix: ffmpeg picks its muxer from the
    output extension, and soundfile picks its format the same way, so
    ".../x.tmp-ab12" would leave both guessing.
    """
    tmp = dest.with_name(f".{dest.stem}.tmp-{secrets.token_hex(4)}{dest.suffix}")
    try:
        write(tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace
    return dest

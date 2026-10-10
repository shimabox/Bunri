"""Regenerate the Bunri Pocket protocol snapshot under tests/fixtures.

The snapshot is a copy of the contract published by the bunri-pocket
repository: its JSON Schemas, its protocol fixtures, and goldens produced by
its own `stableJson()`. This script rebuilds all of that from one commit of a
local bunri-pocket checkout, so the copy never has to be edited by hand.

    uv run python scripts/sync_pocket_protocol_snapshot.py --pocket ../bunri-pocket
    uv run python scripts/sync_pocket_protocol_snapshot.py --pocket ../bunri-pocket --ref v0.3.2
    uv run python scripts/sync_pocket_protocol_snapshot.py --pocket ../bunri-pocket --check

The checkout is only read (`git archive`); its working tree and HEAD stay as
they are. The goldens are made by running the upstream TypeScript with Node's
built-in type stripping, so Node 22.18 or later is needed and `npm ci` is not.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bunri_pocket_protocol_v1"
UPSTREAM_NOTE = SNAPSHOT / "UPSTREAM.md"
COMMIT_LINE = re.compile(r"^- Commit: `([0-9a-f]{40})`$", re.MULTILINE)

# Upstream directory -> snapshot directory. Each one is mirrored whole, so a
# file removed upstream disappears here too.
MIRRORED = {
    "schemas": "schemas",
    "fixtures/protocol-v1/valid": "valid",
    "fixtures/protocol-v1/invalid": "invalid",
    "fixtures/protocol-v1/media": "media",
}
STABLE_JSON_SOURCE = "src/protocol/stable-json.ts"
# Upstream documents whose stableJson() output is kept as a golden.
UPSTREAM_GOLDEN_INPUTS = {
    "manifest-v1": "fixtures/protocol-v1/valid/manifest-v1.json",
    "library-v1": "fixtures/protocol-v1/valid/library-v1.json",
}
# A golden with no input file: JSON cannot spell these values, so the driver
# builds them in JavaScript.
NON_FINITE_GOLDEN = "non-finite"

DRIVER = """\
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const [modulePath, jobsPath] = process.argv.slice(2);
const { stableJson } = await import(pathToFileURL(modulePath).href);
const nonFinite = { nan: NaN, negative: -Infinity, positive: Infinity };
for (const { input, output } of JSON.parse(readFileSync(jobsPath, "utf8"))) {
  const value = input === null ? nonFinite : JSON.parse(readFileSync(input, "utf8"));
  writeFileSync(output, stableJson(value));
}
"""


class SnapshotError(Exception):
    pass


def _git(pocket: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(["git", "-C", str(pocket), *args], capture_output=True)
    except FileNotFoundError:
        raise SnapshotError("git が見つかりません") from None
    if done.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)} が失敗しました:\n{done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _recorded_commit() -> str:
    match = COMMIT_LINE.search(UPSTREAM_NOTE.read_text(encoding="utf-8"))
    if match is None:
        raise SnapshotError(f"{UPSTREAM_NOTE.name} に「- Commit: `<40桁>`」の行がありません")
    return match.group(1)


def _extract(pocket: Path, commit: str, dest: Path) -> None:
    archive = _git(pocket, "archive", "--format=tar", commit, *MIRRORED, STABLE_JSON_SOURCE)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def _run_stable_json(upstream: Path, jobs: list[dict[str, str | None]], work: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise SnapshotError("node が見つかりません(Node 22.18 以降が必要です)")
    # .mts makes Node treat the file as an ES module regardless of where the
    # temporary directory sits.
    module = work / "stable-json.mts"
    shutil.copyfile(upstream / STABLE_JSON_SOURCE, module)
    driver = work / "driver.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    jobs_file = work / "jobs.json"
    jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    done = subprocess.run([node, str(driver), str(module), str(jobs_file)], capture_output=True, text=True)
    if done.returncode != 0:
        raise SnapshotError(f"stableJson() の実行に失敗しました(Node 22.18 以降が必要です):\n{done.stderr.strip()}")


def _build(pocket: Path, commit: str, work: Path) -> dict[str, bytes]:
    """Return the upstream-owned part of the snapshot as {relative path: bytes}."""
    upstream = work / "upstream"
    _extract(pocket, commit, upstream)
    built: dict[str, bytes] = {}
    for source, name in MIRRORED.items():
        for path in sorted((upstream / source).rglob("*")):
            if path.is_file():
                built[f"{name}/{path.relative_to(upstream / source).as_posix()}"] = path.read_bytes()

    out = work / "stable"
    out.mkdir()
    inputs: dict[str, Path | None] = {name: upstream / source for name, source in UPSTREAM_GOLDEN_INPUTS.items()}
    for path in sorted((SNAPSHOT / "stable").glob("*.input.json")):
        inputs[path.name.removesuffix(".input.json")] = path
    inputs[NON_FINITE_GOLDEN] = None
    jobs = [
        {"input": None if source is None else str(source), "output": str(out / f"{name}.stable.json")}
        for name, source in inputs.items()
    ]
    _run_stable_json(upstream, jobs, work)
    for name in inputs:
        built[f"stable/{name}.stable.json"] = (out / f"{name}.stable.json").read_bytes()
    return built


def _current() -> dict[str, bytes]:
    """The same set of files as `_build`, read from the snapshot on disk."""
    paths = [path for name in MIRRORED.values() for path in (SNAPSHOT / name).rglob("*")]
    paths += (SNAPSHOT / "stable").glob("*.stable.json")
    return {path.relative_to(SNAPSHOT).as_posix(): path.read_bytes() for path in paths if path.is_file()}


def _diff(current: dict[str, bytes], built: dict[str, bytes]) -> list[str]:
    lines = [f"  追加: {name}" for name in sorted(built.keys() - current.keys())]
    lines += [f"  削除: {name}" for name in sorted(current.keys() - built.keys())]
    lines += [f"  変更: {name}" for name in sorted(built.keys() & current.keys()) if built[name] != current[name]]
    return lines


def _write(current: dict[str, bytes], built: dict[str, bytes], commit: str) -> None:
    for name in current.keys() - built.keys():
        (SNAPSHOT / name).unlink()
    for name, data in built.items():
        path = SNAPSHOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    note = UPSTREAM_NOTE.read_text(encoding="utf-8")
    UPSTREAM_NOTE.write_text(COMMIT_LINE.sub(f"- Commit: `{commit}`", note, count=1), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bunri Pocket の契約スナップショットを再生成します。")
    parser.add_argument("--pocket", required=True, type=Path, help="bunri-pocket リポジトリの checkout")
    parser.add_argument("--ref", help="取り込む commit / タグ(既定: HEAD。--check では記録済みの commit)")
    parser.add_argument("--check", action="store_true", help="書き換えずに、スナップショットとの差分だけを調べる")
    args = parser.parse_args()

    try:
        recorded = _recorded_commit()
        ref = args.ref or (recorded if args.check else "HEAD")
        commit = _git(args.pocket, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        with tempfile.TemporaryDirectory() as tmp:
            built = _build(args.pocket, commit, Path(tmp))
        current = _current()
    except SnapshotError as exc:
        print(exc, file=sys.stderr)
        return 2

    diff = _diff(current, built)
    if args.check:
        if diff:
            print(f"スナップショットが bunri-pocket {commit} と一致しません:", *diff, sep="\n")
            return 1
        print(f"スナップショットは bunri-pocket {commit} と一致しています。")
        return 0

    _write(current, built, commit)
    if diff:
        print(f"bunri-pocket {commit} から再生成しました:", *diff, sep="\n")
    elif commit != recorded:
        print(f"中身の変更はありません。記録する commit だけ {commit} に更新しました。")
    else:
        print(f"変更はありません(bunri-pocket {commit})。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

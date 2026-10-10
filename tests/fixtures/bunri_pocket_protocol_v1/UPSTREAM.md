# Bunri Pocket protocol v1 snapshot

- Repository: `github.com/shimabox/bunri-pocket`
- Commit: `6766d7dd95d66023b8a2828a044ee0d88756d509`

This directory is a copy of the contract published by the repository above, taken at the
commit above. Do not edit the upstream-owned files by hand; regenerate them.

## Regenerating

From the Bunri repository root, with a local checkout of bunri-pocket:

```sh
uv run python scripts/sync_pocket_protocol_snapshot.py --pocket <bunri-pocket checkout>
uv run python scripts/sync_pocket_protocol_snapshot.py --pocket <bunri-pocket checkout> --ref v0.3.2
uv run python scripts/sync_pocket_protocol_snapshot.py --pocket <bunri-pocket checkout> --check
```

Without `--ref` the script takes the checkout's `HEAD`. It rewrites the upstream-owned files,
lists what changed, and updates the commit recorded above. `--check` writes nothing and exits
with 1 when the snapshot differs from the recorded commit (or from `--ref` when given).

The checkout is only read through `git archive`, so its working tree and `HEAD` are left alone
and `npm ci` is not needed. Node 22.18 or later is required to run the upstream `stableJson()`.

## What is upstream-owned

| Here | Upstream source |
| --- | --- |
| `schemas/` | `schemas/` |
| `valid/` | `fixtures/protocol-v1/valid/` |
| `invalid/` | `fixtures/protocol-v1/invalid/` |
| `media/` | `fixtures/protocol-v1/media/` |
| `stable/*.stable.json` | output of `stableJson()` in `src/protocol/stable-json.ts` |

The four directories are mirrored whole: a file removed upstream is removed here too.

## What Bunri owns

- `stable/*.input.json`: inputs for the stable goldens that upstream has no fixture for. Adding
  a new `<name>.input.json` and regenerating produces `<name>.stable.json`.
- `generated/`: documents built by Bunri's own generator. They exercise Bunri's Japanese
  `"ギター"` label separately from the upstream `"Guitar"` documents, which are protocol
  acceptance fixtures.

The upstream commit has no invalid fixtures for the L/R stems; Bunri's tests build those cases
directly.

## Stable golden inputs

Each golden is `stableJson(value)` written out as is, where `value` is:

- `manifest-v1.stable.json`: `JSON.parse()` of the upstream
  `fixtures/protocol-v1/valid/manifest-v1.json`.
- `library-v1.stable.json`: `JSON.parse()` of the upstream
  `fixtures/protocol-v1/valid/library-v1.json`.
- `number-forms.stable.json`: `JSON.parse()` of the adjacent `number-forms.input.json`. The
  source text intentionally retains `1.0` and `-0`.
- `unicode-keys-nested.stable.json`: `JSON.parse()` of the adjacent
  `unicode-keys-nested.input.json`. This is also the array-index key ordering input.
- `lone-surrogates.stable.json`: `JSON.parse()` of the adjacent `lone-surrogates.input.json`.
  The input contains lone high and low surrogates in both a string value and object keys, plus
  ordinary non-ASCII text and a valid pair.
- `non-finite.stable.json`: the JavaScript value
  `{ nan: NaN, negative: -Infinity, positive: Infinity }`, which JSON cannot represent.
  `JSON.parse()` rejects textual `NaN` and `Infinity`, so this golden records only the
  serializer rule for JavaScript values that already exist in memory.

A stable output file is never used as its own regeneration input.

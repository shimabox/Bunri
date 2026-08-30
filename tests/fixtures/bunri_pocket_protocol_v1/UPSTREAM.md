# Bunri Pocket protocol v1 snapshot

- Repository: `github.com/shimabox/bunri-pocket`
- Commit: `a8efc3e20a5009c72c1d5bcdd07db6e046c84ceb`
- Sources:
  - `schemas/manifest-v1.schema.json`
  - `schemas/library-v1.schema.json`
  - `fixtures/protocol-v1/valid/library-v1.json`
  - `fixtures/protocol-v1/valid/manifest-v1-no-original.json`
  - `fixtures/protocol-v1/valid/manifest-v1-unknown-fields.json`
  - `fixtures/protocol-v1/valid/manifest-v1.json`
  - `fixtures/protocol-v1/invalid/library-duplicate-song-id.json`
  - `fixtures/protocol-v1/invalid/library-invalid-manifest-path.json`
  - `fixtures/protocol-v1/invalid/manifest-bad-cache-key.json`
  - `fixtures/protocol-v1/invalid/manifest-bad-major.json`
  - `fixtures/protocol-v1/invalid/manifest-duplicate-target.json`
  - `fixtures/protocol-v1/invalid/manifest-invalid-path.json`
  - `fixtures/protocol-v1/invalid/manifest-missing-backing.json`
  - `fixtures/protocol-v1/invalid/manifest-reserved-target.json`
  - `fixtures/protocol-v1/invalid/manifest-wav-path.json`
  - `fixtures/protocol-v1/media/sample.mp3`
  - `src/protocol/stable-json.ts`

To regenerate, check out the commit in detached mode, run `npm ci`, and run a temporary
TypeScript script outside both repositories with the project-local `npx tsx`. The script
imports `src/protocol/stable-json.ts` and writes one `stableJson(value)` result directly to
stdout. Remove the temporary script afterward.

The exact input for each stable golden is:

- `manifest-v1.stable.json`: `JSON.parse()` the upstream
  `fixtures/protocol-v1/valid/manifest-v1.json` bytes.
- `library-v1.stable.json`: `JSON.parse()` the upstream
  `fixtures/protocol-v1/valid/library-v1.json` bytes.
- `number-forms.stable.json`: `JSON.parse()` the adjacent
  `number-forms.input.json` bytes. The source text intentionally retains `1.0` and `-0`.
- `unicode-keys-nested.stable.json`: `JSON.parse()` the adjacent
  `unicode-keys-nested.input.json` bytes. This is also the array-index key ordering input.
- `non-finite.stable.json`: use this JavaScript value, which JSON cannot represent:
  `{ nan: NaN, negative: -Infinity, positive: Infinity }`.

For every case, pass that parsed or literal JavaScript value to `stableJson()` and write
the returned string directly to the corresponding `*.stable.json` file on stdout. Do not
parse a stable output file as its own regeneration input.

The upstream `"Guitar"` documents are protocol acceptance fixtures. Files under
`generated/` exercise Bunri's Japanese `"ギター"` label generation separately.

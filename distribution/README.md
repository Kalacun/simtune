# Public distribution policy

The sdist includes an explicitly selected native preparation kit, examples,
configs, tests and public docs. The wheel contains only the installable simtune
package and standard package metadata/license. No runtime dependency is added.
The version remains 0.1.0. Packaging establishes no native acceptance or study.

[policy.json](policy.json) lists every selected source destination. Most files
match their checkout bytes exactly. Its curated mapping identifies separate
portable sources and their SHA-256 hashes for the implementation index, release
guide/status, R06 assignment, pending report and protocol. The build places those
curated bytes at the original linked locations; the original records remain
unchanged in the checkout. Curated source duplicates are excluded from archives.
When rebuilding an unpacked sdist, the same destination bytes must match those
hashes. Package code and test/fixture identities always match exact sources.

Original implementation/release handoffs, internal task prompts and raw logs,
personal host/account paths, unpublished studies, runs, reports, environments,
cache files, root compare exports and generated binaries/build products are
excluded. They are private or unavailable historical evidence, not public
placeholders or evidence of success. This policy must be updated explicitly for
new public files; a broad docs glob is never the selection mechanism.

The [R06 runbook](../docs/implementation/release-plan/r06/RUNBOOK.md) stays the
single authoritative command source. Its portable prerequisite prose replaces
private acquisition paths; its corrected command blocks retain their original
implementation. Helpers stay in that directory. Pending JSON/report sources
remain not run; their curated protocol leaves transfer-specific hashes null
until the operator records the actual supplied artifacts. The full tests and
fixtures support the native helper's transitive imports and full regression gate.

Run `python -m build --outdir EXTERNAL_DIST`, then
`python tests/check_archives.py EXTERNAL_DIST SOURCE_ROOT`. The writer normalizes UID/GID to zero, account/group names to empty, modes to
644/755 and archive times to SOURCE_DATE_EPOCH (zero by default). It omits gzip
filenames and filesystem PAX metadata. The audit reads
members without extraction, rejects identifying ownership and unexpected PAX
metadata as well as unsafe paths/links, checks exact selection
and identities, resolves Markdown links solely against archive members, checks
Python import and code-block resource prerequisites, and scans text for common
private account/host patterns. Narrow synthetic test-path exceptions apply only
to identified test/fixture occurrences. The scan is a heuristic, not proof of
complete privacy: manually review all public text before sharing.

The kit is inside the actual sdist; no separate upload or invented download URL
is required. Build/install instructions are in the runbook. Historical raw
checks remain private; no published study or independently accepted release is
claimed. See [public status](../docs/implementation/release-plan/STATUS.md).

For a direct source upload, prepare a **new separate tree** by copying only
`files` destinations, using each identified curated source (or its hash-checked
public destination when rebuilding). Preserve safe source modes and include
the selected `.gitignore`, guide and candidate summary. The private checkout
itself is not suitable for a direct GitHub upload: an sdist hook sanitizes only
the built sdist. Inspect the new tree's filenames and all public text, then build
and audit there without relying on the private checkout. No public repository
URL, publishing action or release approval is implied by that preparation.

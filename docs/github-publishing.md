# Publishing simtune on GitHub

Publish only the explicit selection in [distribution/policy.json](../distribution/policy.json).
The private maintainer checkout contains historical runs and review material that
are not public source files. The selected tree includes portable documentation
at the original documentation paths; source and test bytes remain unchanged.

## Prepare a clean tree

From the maintainer checkout, choose a new directory outside it:

```bash
python distribution/prepare_public.py /absolute/path/to/simtune-public
cd /absolute/path/to/simtune-public
python -m pip install '.[dev,yaml,rich]'
python -m pytest -ra
python -m ruff check .
python -m ruff format --check .
python -m build
python tests/check_archives.py dist .
```

The helper refuses to overwrite a destination, checks curated hashes, rejects
unsafe paths and symlinks, and prints a file inventory. It also works from an
already selected public tree. Review the files and the [current evidence](candidate-review.md).

The source-archive writer now normalizes ownership and filesystem metadata.
The audit checks those headers as well as selection, links, package bytes and
common private-text patterns. Its privacy scan is a heuristic: inspect selected
text before sharing. Old archives built before this repair remain unsuitable
for upload; build fresh artifacts from the reviewed source.

## Commit and upload

Create an empty repository under the intended GitHub account, without generated
README, license or ignore files. Inspect the exact staged selection:

```bash
git init -b main
git add --all
git diff --cached --check
git diff --cached --stat
git ls-files
git commit -m "Publish experimental simtune source"
git remote add origin YOUR_GITHUB_REPOSITORY_URL
git push -u origin main
```

Keep environments, binaries, private handoffs, raw local runs and build outputs
out of Git. Benchmark data should be deliberately curated with a protocol and
all relevant observations. Source publication does not require uploading packages
to PyPI or attaching archives to a GitHub release.

## Verify the published commit

Check the Actions run for the exact pushed SHA. Inspect source and installed-wheel
jobs across Linux/macOS and Python 3.11–3.14, base/extras dependencies, actual
Linux affinity/OpenMP tests, skips, and archive checks. Record the run URL and
conclusion. Fix failures before claiming the matrix passed.

A successful hosted Linux check establishes behavior on that runner. It does not
establish performance on dedicated hardware, physical NUMA locality, working
perf permissions, Slurm integration or transfer of a measured gain. Keep these
limits and the [result reports](results/README.md) visible in release documentation.

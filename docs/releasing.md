# Releasing DAOGraph

_Prepare and publish alpha releases and distributions._

---

## ✅ Verify the source

The repository is [kai6589c-svg/DAOGraph](https://github.com/kai6589c-svg/DAOGraph).
GitHub releases and PyPI uploads are separate publishing actions. Distribution
version `0.2.0` is an alpha release.

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
mypy
python -m build
python -m twine check dist/*
python benchmarks/run.py --split evaluation --output benchmark-results
```

The sdist includes source, tests, examples, the frozen benchmark corpus, and documentation. The wheel includes
the typed `daograph` package. The core has no third-party runtime dependencies.

## 🚀 Push changes to GitHub

Run these from a checkout with write access to the repository:

```bash
git push origin main
```

GitHub Actions starts on pushes and pull requests. It runs the test matrix,
formatting, static type checking, examples, and package validation. Tagged pushes
also trigger the same checks. The package job attaches distributions as workflow
artifacts. The workflow does not automatically publish a package or GitHub release.

## 📦 Create an alpha release

After the exact implementation commit passes all GitHub CI jobs, including the
offline benchmark:

```bash
git tag -a v0.2.0 -m 'DAOGraph 0.2.0 alpha'
git push origin v0.2.0
```

Wait for the tag's CI run. Download its `daograph-distributions` and
`daograph-research-benchmark` artifacts with `gh run download RUN_ID`. Publish
those CI-built files, the corpus lock, benchmark JSON/Markdown reports, source
archive, and SHA256 sums. Then publish the alpha release:

```bash
gh release create v0.2.0 dist/daograph-0.2.0-py3-none-any.whl \
  dist/daograph-0.2.0.tar.gz --verify-tag --prerelease \
  --title 'DAOGraph 0.2.0 alpha' --notes-file CHANGELOG.md
```

## 📚 Optional PyPI publication

The distribution name `daograph` has not been reserved or checked for ownership.
Confirm availability and your publishing rights before uploading. Until it is
published, users install from the local checkout, a GitHub clone, or the wheel.

```bash
python -m twine upload --repository testpypi dist/*
# After verifying the TestPyPI package and your ownership:
# python -m twine upload dist/*
```

Bump both project version and `daograph.__version__` for subsequent releases.

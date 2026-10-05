# Contributing to DAOGraph

_Keep the adaptive control loop small, explicit, and testable._

---

## 🚀 Development setup

Use Python 3.11 or later and install the development extras:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

On Windows, activate with `.venv\Scripts\activate`.

## ✅ Required checks

```bash
pytest --cov=daograph --cov-report=term-missing --cov-fail-under=90
ruff check .
ruff format --check .
mypy
python examples/adaptive_research.py
python examples/approval.py
python examples/async_tools.py
python benchmarks/run.py --split evaluation --output benchmark-results
python -m build
python -m twine check dist/*
```

GitHub Actions checks Python 3.11 through 3.14 on Linux and includes Python 3.13
on macOS and Windows. A separate package job builds distributions, installs the
wheel without runtime dependencies, and runs the examples. CI runs after this
repository is pushed to GitHub; local verification is recorded in the delivery.

## 🎯 Contribution scope

Preserve the three-module implementation. Propose a concrete use case before
adding another subsystem. Provider integrations can usually be examples of
ordinary node callbacks. Keep host authority out of writable state. Cover
behavioral changes with meaningful tests, especially execution order, replanning,
approval boundaries, and resume behavior. Update the API documentation alongside
public interface changes.

Submit pull requests from a branch with the problem, changed behavior, and
validation. The public API is still alpha and may change before 1.0.

Do not retune on frozen evaluation cases or relabel them after inspecting results.
The offline runner denies network operations. Online demonstrations are manual
and never determine the offline release gate. See [the research guide](docs/research.md).

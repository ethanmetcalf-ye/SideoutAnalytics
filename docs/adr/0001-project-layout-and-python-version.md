# ADR-001: Use a `src/`-Layout Packaged Application on Python 3.13

## Context

The project will combine data pipelines, API access, and analysis code. These
parts need to import from one package, and tests should check the import of
that package as it will be installed in production. The project also needs a
Python version
supported by its current and anticipated dependencies; adopting the newest
available interpreter could create compatibility issues before those
dependencies catch up.

## Decision

We will build Sideout Analytics as an installable application package using a
src/ layout, and target Python 3.13 (`requires-python = ">=3.13"`). We will
use uv to manage the project environment and dependencies, Ruff for linting,
and pytest for testing.

## Consequences

- The `src/` layout and package installation make imports explicit and let
  tests catch packaging problems early.
- Python 3.13 offers a more established compatibility
target for dependencies at the cost of not using newer interpreter features.
- uv provides a single workflow for project, dependency, and environment
  management.
- Ruff provides fast linting with
the selected rule groups (`F`, `S`, `I`, `PERF`, `E`, `W`, `UP`, `N`, `ANN`,
`DTZ`, `SIM`, and `PTH`), a 79-character line length, and excludes `docs/`.
- The broad ruleset may require occasional per-file exceptions, as configured
for tests.
- Pytest makes automated checks straightforward to run locally and in
CI, but tests need to be added and updated as behavior is implemented.

# Test fixtures

`linkwatch/` is an unmodified snapshot of [rishrek/LinkWatch](https://github.com/rishrek/LinkWatch)
at commit `f52f3930c8e7c33d934ccf614955a38e26984bd5`, embedded so our tests run offline against
the real target. It is MIT licensed; see `linkwatch/LICENSE`.

Do not edit files inside `linkwatch/`. Tests assert exact file names and line numbers from it.
Its own test suite is excluded from collection by `norecursedirs` in `pyproject.toml`.

# Project Agent Instructions

## Python Runtime Rule

Use the repo virtualenv for all ordinary project work. The canonical interpreter is:

```powershell
.\.venv\Scripts\python.exe
```

- Run project tests, CLIs, FastAPI services, package imports, and dependency installs with that interpreter.
- Use `.\.venv\Scripts\python.exe -m pip ...` for installs on Windows.
- Use `.\.venv\Scripts\python.exe -m pytest ...` for tests on Windows.
- On POSIX shells, use `./.venv/bin/python -m pip ...` and `./.venv/bin/python -m pytest ...`.
- Do not use the active `python` on PATH for this repo; it may be Miniconda or another unrelated interpreter.
- Do not install project dependencies into global Windows Python or Miniconda to "fix" missing imports.
- The only exception is Resolve-side automation: run that through Resolve's own scripting runtime (`fuscript.exe` or Resolve's Scripts menu), not `.venv`.

## Generic Code vs Profile Defaults

`src/` is for generic application code. Do not hardcode project, game, character, platform, video-series, or profile-specific vocabulary, defaults, prompts, scoring assumptions, paths, or fallback behavior in `src/`.

Profile-specific behavior belongs under `profiles/`. If a workflow needs default domain terms, search hints, ingestion prompts, query expansions, profile IDs, or editing preferences, add or update profile configuration/data there and have generic `src/` code load it through explicit configuration.

Tests may use concrete sample domains when they are fixtures for behavior, but the production implementation being tested should remain domain-agnostic unless the file is explicitly profile-owned.

Before adding a new default to `src/`, ask whether it would still be correct for a different footage domain. If the answer is no, put it in `profiles/` or pass it in as data.

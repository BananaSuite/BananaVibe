# Contributing

Thanks for helping. BananaVibe is deliberately small and dependency-free (Python standard library only).

```bash
git clone https://github.com/BananaSuite/BananaVibe && cd BananaVibe
python3 -m pytest -q          # unit and integration tests, using a scripted fake agent; no API usage
python3 -m ruff check .
```

- New agent CLI or a changed output format? Add real sample output to `tests/test_adapters.py` and make sure
  limits, outages and auth errors are classified correctly; that is what keeps runs alive.
- Supervisor behaviour is tested end to end in `tests/test_supervisor.py` with `tests/fake_agent.py`; add a step
  to the fake agent rather than mocking internals.
- Keep the docs in `docs/` in sync with behaviour.

Contributions are accepted under the project's license (AGPL-3.0-only).

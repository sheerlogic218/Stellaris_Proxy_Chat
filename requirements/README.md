# Requirements

| File | For | Used by |
|---|---|---|
| `api.in` / `api.txt` | The API container | `deploy/Dockerfile` (target `api`) |
| `bot.in` / `bot.txt` | The server bot container | `deploy/Dockerfile` (target `bot`) |
| `relay.in` / `relay.txt` | The relay on hosts' Windows PCs | `setup_relay.bat` |
| `dev.in` | Development and CI | `pip install -r requirements/dev.in` |

- **Lock files:** the `.txt` files are hash-locked, and every install uses `pip install --require-hashes`, so a package that doesn't match its recorded hash is refused.
- **Which platform:** `api.txt` and `bot.txt` are resolved for Linux (the containers); `relay.txt` for Windows.
- **Regenerating them:** after changing an `.in` file, run `deploy/lock-requirements.sh` (needs Docker) for the server locks, and on Windows:

```bash
.venv\Scripts\python -m piptools compile --generate-hashes --allow-unsafe -o requirements/relay.txt requirements/relay.in
```

The root `requirements.txt` is the old all-in-one list for local mode, and is kept for it.

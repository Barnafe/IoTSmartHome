# SmartHome — Start Here

Everything for this project lives in this one folder now.

## What's in here

- **The app itself** — everything at this top level (`dashboard/`, `security/`, `energy/`,
  `database.py`, `engine.py`, `worker.py`, `run_local.py`, etc.) is the actual runnable
  SmartHome system.
- **Project_Report/** — the full final-year report (`Complete_Report_v8_updated.docx`).
- **Project_Notes/** — the plain-language working jotter covering how the whole project
  was built, version by version (`SmartHome_Project_Jotter.md`).

## Running it locally (one command)

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in your real Brevo key + alert email
python run_local.py
```

Then open **http://127.0.0.1:5000** in your browser. `run_local.py` starts both the
web dashboard and the background monitoring worker in one process, so this single
command is all you need locally — no second terminal required.

## Running the tests

```bash
python -m pytest -q
```

Currently 26/26 passing.

## Deploying it (Render)

The included `Procfile` already defines the two services Render needs:

```
web: gunicorn dashboard.app:app
worker: python worker.py
```

Set `BREVO_API_KEY`, `ALERT_EMAIL`, `FLASK_SECRET_KEY`, and (optionally) `DATABASE_URL`
as environment variables in each Render service's dashboard — see `.env.example` for
what each one is for.

## Full history

`README.md` (in this same folder) has the fuller technical write-up. `Project_Notes/`
has the plain-language version of how this all came together across every stage.

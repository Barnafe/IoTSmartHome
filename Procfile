# Render FREE plan (no Background Worker service available): the "web" line
# alone is enough. dashboard/app.py auto-starts the monitoring engine as a
# background thread inside this same process (EMBEDDED_WORKER=true by
# default). --workers 1 is REQUIRED so the engine doesn't start more than once.
web: gunicorn dashboard.app:app --workers 1 --timeout 120

# Render PAID plans only: if you add a real separate Background Worker
# service using this line, set EMBEDDED_WORKER=false on the WEB service's
# environment variables so the engine isn't running in both places at once.
worker: python worker.py

# gunicorn loads this file automatically from the project folder, so it applies
# on Render even if the service's Start Command is the older, shorter one.
# The live dashboard keeps one open connection per device (Server-Sent Events);
# a plain "sync" worker would let a single device block everyone else, which
# shows up as "No connection - the change was not applied". Threads fix that.
workers = 1                 # the embedded monitoring engine must start only once
worker_class = "gthread"
threads = 64                # up to 64 simultaneous requests / open live screens
timeout = 120
graceful_timeout = 20
keepalive = 30

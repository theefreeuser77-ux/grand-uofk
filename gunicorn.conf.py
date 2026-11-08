# Gunicorn reads this file automatically when started from the project folder,
# as Render's start command `gunicorn config.wsgi:application` does.

# A whole-site backup import can take a few minutes on a small server; the default
# 30-second limit stops it partway (the import is all-or-nothing, so nothing changes).
timeout = 600

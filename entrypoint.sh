#!/bin/sh

if [ "$APP_ROLE" = "worker" ]; then
    # Start Celery worker
    celery -A celery.app:celery worker --loglevel=info
else
    # Start Gunicorn server
    gunicorn -b 0.0.0.0:5000 app:app
fi

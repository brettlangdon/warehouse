#!/usr/bin/env bash
set +x
pip install git+https://github.com/DataDog/dd-trace-py@brettlangdon/ld#egg=ddtrace
ddtrace-run gunicorn --reload -b 0.0.0.0:8000 --access-logfile - --error-logfile - warehouse.wsgi:application

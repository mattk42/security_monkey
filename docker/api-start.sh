#!/usr/bin/env bash

exec nginx

cd /usr/local/src/security_monkey
python manage.py run_api_server -b 0.0.0.0:5001

"""Load with gunicorn --config deploy/gunicorn.conf.py config.wsgi:application."""

bind = "127.0.0.1:8001"
workers = 2
worker_class = "gthread"
threads = 4
timeout = 90
graceful_timeout = 120
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
preload_app = False
raw_env = ["DJANGO_SETTINGS_MODULE=config.settings.production"]
# No URL/query/header/cookie/body in access logs; request ID correlates incidents.
accesslog = "-"
access_log_format = "%(t)s %(m)s %(s)s %(L)s request_id=%({x-request-id}o)s"
errorlog = "-"
capture_output = False
forwarded_allow_ips = "127.0.0.1"

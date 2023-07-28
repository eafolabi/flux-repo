bind = "0.0.0.0:8000"
workers = 4
daemon = False

errorlog = '-'
loglevel = 'info'
capture_output = True

accesslog = '-'
access_log_format = (
    '%(h)s %({X-Real-IP}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" "%(L)s"'
)

proc_name = "flux-suspend-checker"

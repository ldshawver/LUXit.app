# Gunicorn configuration file for production deployment

# Server socket
bind = "127.0.0.1:8000"
backlog = 2048

# Worker processes
workers = 4
worker_class = "sync"
worker_connections = 1000
timeout = 30
keepalive = 2

# Restart workers after this many requests, to help prevent memory leaks
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"  # Log to stdout
errorlog = "-"   # Log to stderr
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process naming
proc_name = "email-marketing-app"

# Daemon mode
daemon = False

# Security (commented out for development)
# user = "www-data"
# group = "www-data"

# Preload app for better performance
preload_app = True

# Enable auto-reload in development (disable in production)
reload = False

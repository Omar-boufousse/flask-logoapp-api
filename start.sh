
#!/bin/bash
gunicorn --worker-class eventlet -w 1 --timeout 180 app:app -b 0.0.0.0:${PORT:-5000}

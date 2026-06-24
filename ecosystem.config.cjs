module.exports = {
  apps: [{
    name: 'paylink',
    script: 'gunicorn',
    args: '--config gunicorn.conf.py wsgi:app',
    exec_mode: 'fork',
    instances: 1,
    autorestart: true,
    max_restarts: 10,
    env: { PORT: '8000' }
  }]
};

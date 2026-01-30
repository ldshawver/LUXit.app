"""WSGI entry point for new lux app factory (testing)."""
from app import app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

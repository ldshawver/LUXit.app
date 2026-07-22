"""Run LUXit's database-backed APScheduler as a dedicated worker process."""

import signal
import time

from app import app
from scheduler import init_scheduler, shutdown_scheduler


def main():
    init_scheduler(app)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        time.sleep(1)
    shutdown_scheduler()


if __name__ == "__main__":
    main()

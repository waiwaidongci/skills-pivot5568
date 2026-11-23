import logging
import time
from pathlib import Path
from src.services.closing import close_due
from src.services.execution import claim, execute, materialize_due
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            close_due()
            materialize_due()
            lease = claim()
            heartbeat.touch()
            if lease:
                execute(lease)
                heartbeat.touch()
            else:
                wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()

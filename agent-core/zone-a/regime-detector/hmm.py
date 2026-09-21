"""Container entry point for the regime detector (``python hmm.py``).

The implementation lives in the ``regime_detector`` package; see
``regime_detector/service.py`` for the loop and ``tests/`` for the tests.
"""

from regime_detector.main import run

if __name__ == "__main__":
    run()

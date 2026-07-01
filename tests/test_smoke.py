# workloadlens/tests/test_smoke.py
from workloadlens.version import __version__

def test_version():
    assert isinstance(__version__, str) and __version__


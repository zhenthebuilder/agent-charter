import time


def test_slow_ci():
    time.sleep(180)
    assert True

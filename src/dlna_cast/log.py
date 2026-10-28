import time


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

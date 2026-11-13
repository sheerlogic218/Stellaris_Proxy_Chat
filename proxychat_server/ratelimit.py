"""In-memory token buckets. They reset when the API restarts, which is fine for this."""

import time
from collections import OrderedDict


class TokenBucket:
    def __init__(self, rate, burst, clock=time.monotonic):
        self.rate = rate          # tokens per second
        self.burst = burst
        self.clock = clock
        self.tokens = float(burst)
        self.at = clock()

    def take(self, n=1):
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class BucketMap:
    """One bucket per key (e.g. per relay), keeping at most max_keys of them."""

    def __init__(self, rate, burst, clock=time.monotonic, max_keys=10_000):
        self.rate, self.burst, self.clock, self.max_keys = rate, burst, clock, max_keys
        self.buckets = OrderedDict()

    def take(self, key, n=1):
        bucket = self.buckets.pop(key, None) or TokenBucket(self.rate, self.burst, self.clock)
        self.buckets[key] = bucket
        while len(self.buckets) > self.max_keys:
            self.buckets.popitem(last=False)
        return bucket.take(n)

"""Coalesce device telemetry reads without changing any capture timestamps."""
import asyncio
import time


class StateSampler:
    def __init__(self, read, interval=.1, clock=time.monotonic):
        self.read, self.interval, self.clock = read, interval, clock
        self.lock = asyncio.Lock()
        self.value = None
        self.error = None
        self.completed = -float('inf')

    async def get(self):
        async with self.lock:
            if self.error is not None and self.clock()-self.completed < self.interval:
                raise RuntimeError(str(self.error))
            if self.value is None or self.clock()-self.completed >= self.interval:
                # A failed refresh invalidates the cache. Never substitute an
                # earlier successful response for an unavailable business API.
                self.value = None
                self.completed = -float('inf')
                started = self.clock()
                try:
                    value = await asyncio.to_thread(self.read)
                    completed = self.clock()
                    if completed-started > .5:
                        raise RuntimeError('telemetry_response_delayed')
                except Exception as error:
                    self.error, self.completed = error, self.clock()
                    raise
                self.error = None
                self.value, self.completed = value, completed
            return self.value

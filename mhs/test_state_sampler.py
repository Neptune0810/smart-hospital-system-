import asyncio
import unittest
from .state_sampler import StateSampler


class StateSamplerTests(unittest.IsolatedAsyncioTestCase):
    async def test_eight_simultaneous_devices_share_one_read(self):
        reads = []
        value = {'capture_wall_time': 100., 'seq': 4}
        def read():
            reads.append(1)
            return value
        sampler = StateSampler(read)
        results = await asyncio.gather(*(sampler.get() for _ in range(8)))
        self.assertEqual(len(reads), 1)
        self.assertTrue(all(result is value for result in results))
        self.assertEqual(value, {'capture_wall_time': 100., 'seq': 4})

    async def test_expiry_and_error_do_not_return_prior_data(self):
        now = [1.]
        calls = [0]
        def read():
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError('API unavailable')
            return {'seq': calls[0]}
        sampler = StateSampler(read, clock=lambda: now[0])
        self.assertEqual(await sampler.get(), {'seq': 1})
        now[0] += .11
        with self.assertRaisesRegex(RuntimeError, 'API unavailable'):
            await sampler.get()
        self.assertIsNone(sampler.value)
        with self.assertRaisesRegex(RuntimeError, 'API unavailable'):
            await sampler.get()
        self.assertEqual(calls[0], 2)
        now[0] += .11
        self.assertEqual(await sampler.get(), {'seq': 3})

    async def test_slow_fetch_still_coalesces_waiters(self):
        import threading
        calls = []
        gate = threading.Event()
        def read():
            calls.append(1)
            gate.wait(1)
            return {'seq': 1}
        sampler = StateSampler(read)
        tasks = [asyncio.create_task(sampler.get()) for _ in range(8)]
        await asyncio.sleep(.01)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(len(calls), 1)

    async def test_response_delay_invalidates_snapshot(self):
        now = [1.]
        def read():
            now[0] += .6
            return {'fresh': True, 'capture_wall_time': 1.}
        sampler = StateSampler(read, clock=lambda: now[0])
        with self.assertRaisesRegex(RuntimeError, 'telemetry_response_delayed'):
            await sampler.get()
        self.assertIsNone(sampler.value)


if __name__ == '__main__':
    unittest.main()

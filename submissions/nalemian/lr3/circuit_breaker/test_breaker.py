import asyncio
import unittest

from breaker import (
    BusinessRejected, CircuitBreaker, CircuitOpen, InvalidRequest,
    ServiceTimeout, ServiceUnavailable, client_error,
)
from demo import FakeClock, FakeReturnsService


class BreakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = FakeReturnsService()
        self.breaker = CircuitBreaker(
            window_seconds=60, failure_threshold=0.5, minimum_calls=4,
            open_seconds=10, max_open_seconds=40, call_timeout=2,
            half_open_timeout=5, clock=self.clock,
        )

    async def fail(self):
        with self.assertRaises(ServiceUnavailable):
            await self.breaker.call(self.service.create_return, "same-key")

    async def open_chain(self):
        for _ in range(4):
            await self.fail()
        self.assertEqual(self.breaker.state, "OPEN")

    async def test_three_scenarios_and_no_external_call_while_open(self):
        await self.open_chain()
        count = self.service.calls
        with self.assertRaises(CircuitOpen) as raised:
            await self.breaker.call(self.service.create_return, "same-key")
        self.assertEqual(raised.exception.retry_after, 10)
        self.assertEqual(self.service.calls, count)
        self.clock.advance(10)
        await self.fail()
        self.assertEqual(self.breaker.state, "OPEN")
        with self.assertRaises(CircuitOpen) as raised:
            await self.breaker.call(self.service.create_return, "same-key")
        self.assertEqual(raised.exception.retry_after, 20)
        self.clock.advance(20)
        self.service.healthy = True
        seen = []
        self.service.on_call = lambda: seen.append(self.breaker.state)
        await self.breaker.call(self.service.create_return, "same-key")
        self.assertEqual(seen, ["HALF_OPEN"])
        self.assertEqual(self.breaker.state, "CLOSED")

    async def test_minimum_and_failure_ratio(self):
        await self.fail()
        await self.fail()
        self.assertEqual(self.breaker.state, "CLOSED")
        self.service.healthy = True
        await self.breaker.call(self.service.create_return, "key2")
        await self.breaker.call(self.service.create_return, "key3")
        self.assertEqual(self.breaker.state, "OPEN")

    async def test_old_results_leave_time_window(self):
        await self.fail()
        await self.fail()
        self.clock.advance(60)
        self.service.healthy = True
        for _ in range(4):
            await self.breaker.call(self.service.create_return, "key")
        self.assertEqual(self.breaker.state, "CLOSED")

    async def test_request_errors_do_not_dilute_failure_ratio(self):
        await self.fail()
        await self.fail()

        async def invalid():
            raise InvalidRequest("Неверный идентификатор")

        for _ in range(5):
            with self.assertRaises(InvalidRequest):
                await self.breaker.call(invalid)
        self.service.healthy = True
        for _ in range(2):
            await self.breaker.call(self.service.create_return, "key")
        self.assertEqual(self.breaker.state, "OPEN")

    async def test_business_error_in_probe_is_ignored_until_half_open_deadline(self):
        await self.open_chain()
        self.clock.advance(10)

        async def rejected():
            raise BusinessRejected("Возврат недопустим")

        with self.assertRaises(BusinessRejected):
            await self.breaker.call(rejected)
        self.assertEqual(self.breaker.state, "HALF_OPEN")
        self.clock.advance(5)
        self.assertEqual(self.breaker.state, "OPEN")

    async def test_only_one_probe_is_allowed(self):
        await self.open_chain()
        self.clock.advance(10)
        entered, release = asyncio.Event(), asyncio.Event()

        async def probe():
            entered.set()
            await release.wait()
            return "ok"

        task = asyncio.create_task(self.breaker.call(probe))
        await entered.wait()
        try:
            with self.assertRaises(CircuitOpen):
                await self.breaker.call(probe)
        finally:
            release.set()
            await task
        self.assertEqual(self.breaker.state, "CLOSED")

    async def test_hanging_call_has_real_timeout(self):
        breaker = CircuitBreaker(
            window_seconds=60, failure_threshold=0.5, minimum_calls=1,
            open_seconds=10, max_open_seconds=40, call_timeout=0.01,
            half_open_timeout=5, clock=self.clock,
        )

        async def slow():
            await asyncio.sleep(60)

        with self.assertRaises(ServiceTimeout):
            await breaker.call(slow)
        self.assertEqual(breaker.state, "OPEN")

    async def test_probe_backoff_is_capped(self):
        await self.open_chain()
        for pause in (10, 20, 40, 40):
            self.clock.advance(pause)
            await self.fail()
        with self.assertRaises(CircuitOpen) as raised:
            await self.breaker.call(self.service.create_return, "key")
        self.assertEqual(raised.exception.retry_after, 40)

    def test_client_can_distinguish_errors(self):
        self.assertEqual(client_error(CircuitOpen(2.1))[1], {"Retry-After": "3"})
        errors = [CircuitOpen(10), ServiceUnavailable("down"), InvalidRequest("bad")]
        self.assertEqual(
            [client_error(error)[2]["code"] for error in errors],
            ["CIRCUIT_OPEN", "DEPENDENCY_UNAVAILABLE", "INVALID_REQUEST"],
        )


if __name__ == "__main__":
    unittest.main()

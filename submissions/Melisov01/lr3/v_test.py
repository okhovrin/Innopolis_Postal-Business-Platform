"""Тесты размыкателя. Запуск: python3 -m unittest v_test -v"""

import unittest

from v_circuit_breaker import (
    CircuitBreaker, CircuitOpenError, FakeClock, FakeShipmentService,
    InvalidBarcode, ServiceUnavailable, ShipmentNotFound, to_http_error,
)

OWN, FOREIGN = "80080012345678", "80080087654321"


def setup(probes=1, half_open_limit=15):
    clock = FakeClock()
    svc = FakeShipmentService(clock, timeout_seconds=3)
    br = CircuitBreaker("отправления", window_seconds=60, failure_rate_threshold=0.5,
                        minimum_calls=4, open_seconds=10, backoff_multiplier=2,
                        max_open_seconds=80, half_open_probes=probes,
                        max_half_open_seconds=half_open_limit, clock=clock)
    return clock, svc, br


def fail(br, svc, n, clock, step=1):
    svc.mode = "down"
    for _ in range(n):
        try:
            br.call(svc.get_source_shipment, "client-A", OWN)
        except (ServiceUnavailable, CircuitOpenError):
            pass
        clock.advance(step)


class BreakerTests(unittest.TestCase):
    def test_opens_after_failures(self):
        clock, svc, br = setup()
        fail(br, svc, 4, clock)
        self.assertEqual(br.state, "open")

    def test_no_decision_below_minimum_calls(self):
        clock, svc, br = setup()
        fail(br, svc, 3, clock)
        self.assertEqual(br.state, "closed")

    def test_open_does_not_call_service_and_tells_when(self):
        clock, svc, br = setup()
        fail(br, svc, 4, clock)
        calls = svc.calls
        with self.assertRaises(CircuitOpenError) as ctx:
            br.call(svc.get_source_shipment, "client-A", OWN)
        self.assertEqual(svc.calls, calls)
        self.assertAlmostEqual(ctx.exception.retry_after, 9)

    def test_request_errors_do_not_count(self):
        clock, svc, br = setup()
        for bc in ["1", FOREIGN, "x", FOREIGN, "2", FOREIGN]:
            with self.assertRaises((InvalidBarcode, ShipmentNotFound)):
                br.call(svc.get_source_shipment, "client-A", bc)
        self.assertEqual(br.window_stats(), (0, 0))
        self.assertEqual(br.state, "closed")

    def test_failed_probe_reopens_with_growing_pause(self):
        clock, svc, br = setup()
        fail(br, svc, 4, clock)
        clock.advance(10)
        self.assertEqual(br.state, "half_open")
        with self.assertRaises(ServiceUnavailable):
            br.call(svc.get_source_shipment, "client-A", OWN)
        self.assertEqual(br.state, "open")
        self.assertEqual(br.current_open_seconds, 20)

    def test_pause_is_capped(self):
        clock, svc, br = setup()
        fail(br, svc, 4, clock)
        for _ in range(6):
            clock.advance(br.current_open_seconds)
            with self.assertRaises(ServiceUnavailable):
                br.call(svc.get_source_shipment, "client-A", OWN)
        self.assertEqual(br.current_open_seconds, 80)

    def test_successful_probe_closes_and_resets(self):
        clock, svc, br = setup()
        fail(br, svc, 4, clock)
        clock.advance(10)
        svc.mode = "healthy"
        br.call(svc.get_source_shipment, "client-A", OWN)
        self.assertEqual(br.state, "closed")
        self.assertEqual(br.current_open_seconds, 10)
        self.assertEqual(br.window_stats(), (0, 0))

    def test_only_limited_probes_pass(self):
        clock, svc, br = setup(probes=1)
        fail(br, svc, 4, clock)
        clock.advance(10)
        svc.mode = "healthy"

        def nested():
            # Пока идёт проба, второй вызов не должен пройти к сервису
            with self.assertRaises(CircuitOpenError):
                br.call(svc.get_source_shipment, "client-A", OWN)
            return "ok"

        self.assertEqual(br.call(nested), "ok")
        self.assertEqual(br.state, "closed")

    def test_request_error_during_probe_frees_slot(self):
        clock, svc, br = setup()
        fail(br, svc, 4, clock)
        clock.advance(10)
        svc.mode = "healthy"
        with self.assertRaises(InvalidBarcode):
            br.call(svc.get_source_shipment, "client-A", "bad")
        self.assertEqual(br.state, "half_open")
        br.call(svc.get_source_shipment, "client-A", OWN)
        self.assertEqual(br.state, "closed")

    def test_half_open_time_limit(self):
        clock, svc, br = setup(probes=3)
        fail(br, svc, 4, clock)
        clock.advance(10)
        svc.mode = "healthy"
        br.call(svc.get_source_shipment, "client-A", OWN)
        self.assertEqual(br.state, "half_open")
        clock.advance(16)
        self.assertEqual(br.state, "open")

    def test_http_mapping_distinguishes_three_classes(self):
        s1, h1, b1 = to_http_error(CircuitOpenError("отправления", 7.2, "разомкнута"))
        s2, h2, b2 = to_http_error(ServiceUnavailable("down"))
        s3, h3, b3 = to_http_error(InvalidBarcode("bad"))
        self.assertEqual((s1, b1["error"]["code"], h1["Retry-After"]), (503, "CIRCUIT_OPEN", "8"))
        self.assertEqual((s2, b2["error"]["code"]), (503, "DEPENDENCY_UNAVAILABLE"))
        self.assertEqual((s3, b3["error"]["category"], b3["error"]["retryable"]), (400, "request", False))


if __name__ == "__main__":
    unittest.main()

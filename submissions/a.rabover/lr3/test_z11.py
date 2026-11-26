# НЕ МЕНЯЙТЕ ЭТУ ЯЧЕЙКУ
"""
Тесты З-11. Запуск: python test_z11.py
В Colab: последняя строка ячейки - unittest.main(argv=[""], exit=False).

Каждый тест проверяет один пункт обязательного объёма; номер пункта - в имени теста.
Тесты из класса Extra - дополнительные: на оценку обязательного объёма не влияют.
"""

import unittest

from v_circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    FakeClock,
    FakeReturnsService,
    InvalidBarcode,
    ServiceError,
    ServiceUnavailable,
)

GOOD = "12345678901234"
BAD = "12345"


def make(clock, **overrides):
    params = dict(window_seconds=60, failure_rate_threshold=0.5,
                  minimum_calls=4, open_seconds=10)
    params.update(overrides)
    return CircuitBreaker("возвраты", clock=clock, **params)


def ok(breaker, service):
    service.mode = "healthy"
    return breaker.call(service.create_return, GOOD)


def fail(breaker, service):
    service.mode = "down"
    try:
        breaker.call(service.create_return, GOOD)
    except ServiceUnavailable:
        return
    raise AssertionError("ожидался отказ сервиса ServiceUnavailable")


class Required(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.service = FakeReturnsService(self.clock)

    def test_p2_no_decision_below_minimum_calls(self):
        """П. 2: три вызова, два неудачных - это ещё не статистика."""
        b = make(self.clock, minimum_calls=4)
        ok(b, self.service); fail(b, self.service); fail(b, self.service)
        self.assertEqual(b.state, CircuitBreaker.CLOSED)

    def test_p3_opens_at_threshold(self):
        """П. 3: доля отказов достигла порога при достаточном числе вызовов - цепь размыкается."""
        b = make(self.clock)
        ok(b, self.service); ok(b, self.service)
        fail(b, self.service); fail(b, self.service)          # 2 из 4 = 50 %
        self.assertEqual(b.state, CircuitBreaker.OPEN)

    def test_p3_stays_closed_below_threshold(self):
        b = make(self.clock)
        for _ in range(3):
            ok(b, self.service)
        fail(b, self.service)                                   # 1 из 4 = 25 %
        self.assertEqual(b.state, CircuitBreaker.CLOSED)

    def test_p1_old_results_leave_the_window(self):
        """П. 1: отказы старше окна не считаются."""
        b = make(self.clock, window_seconds=60)
        for _ in range(3):
            fail(b, self.service)                               # 3 вызова < минимума
        self.clock.advance(61)                                  # старые отказы вышли из окна
        for _ in range(3):
            ok(b, self.service)
        fail(b, self.service)                                   # в окне 1 из 4 = 25 %
        self.assertEqual(b.state, CircuitBreaker.CLOSED,
                         "без окна было бы 4 отказа из 7 - цепь разомкнулась бы")

    def test_p1_p2_parameters_are_parameters(self):
        """П. 1-2: одно и то же поведение сервиса, разные параметры - разный результат."""
        strict = make(self.clock, minimum_calls=3, failure_rate_threshold=0.5)
        lenient = make(self.clock, minimum_calls=6, failure_rate_threshold=0.9)
        for b in (strict, lenient):
            ok(b, self.service); fail(b, self.service); fail(b, self.service)
        self.assertEqual(strict.state, CircuitBreaker.OPEN)
        self.assertEqual(lenient.state, CircuitBreaker.CLOSED)

    def test_p4_open_fails_fast_without_calling_service(self):
        """П. 4: разомкнутая цепь отвечает сразу, сервис не вызывается, ошибка временная."""
        b = make(self.clock, open_seconds=10)
        ok(b, self.service); ok(b, self.service)
        fail(b, self.service); fail(b, self.service)
        calls_before = self.service.calls
        self.service.mode = "healthy"
        with self.assertRaises(CircuitOpenError) as ctx:
            b.call(self.service.create_return, GOOD)
        self.assertEqual(self.service.calls, calls_before, "сервис не должен вызываться")
        self.assertTrue(ctx.exception.temporary)
        self.assertAlmostEqual(ctx.exception.retry_after, 10, delta=0.01)
        self.assertNotIsInstance(ctx.exception, ServiceError,
                                 "ошибка размыкателя должна отличаться от отказа сервиса")

    def test_p4_retry_after_counts_down(self):
        b = make(self.clock, open_seconds=10)
        ok(b, self.service); ok(b, self.service)
        fail(b, self.service); fail(b, self.service)
        self.clock.advance(4)
        with self.assertRaises(CircuitOpenError) as ctx:
            b.call(self.service.create_return, GOOD)
        self.assertAlmostEqual(ctx.exception.retry_after, 6, delta=0.01)

    def test_p5_resumes_after_pause(self):
        """П. 5: после паузы вызовы снова доходят до сервиса."""
        b = make(self.clock, open_seconds=10)
        ok(b, self.service); ok(b, self.service)
        fail(b, self.service); fail(b, self.service)
        self.clock.advance(10)
        self.assertEqual(b.state, CircuitBreaker.CLOSED)
        calls_before = self.service.calls
        self.assertEqual(ok(b, self.service)["status"], "CREATED")
        self.assertEqual(self.service.calls, calls_before + 1)

    def test_p5_pause_is_a_parameter(self):
        b = make(self.clock, open_seconds=30)
        ok(b, self.service); ok(b, self.service)
        fail(b, self.service); fail(b, self.service)
        self.clock.advance(10)
        self.assertEqual(b.state, CircuitBreaker.OPEN, "пауза 30 с ещё не истекла")

    def test_p5_window_is_fresh_after_pause(self):
        """После паузы старые отказы не должны тут же разомкнуть цепь снова."""
        b = make(self.clock, open_seconds=10)
        ok(b, self.service); ok(b, self.service)
        fail(b, self.service); fail(b, self.service)
        self.clock.advance(10)
        fail(b, self.service)
        self.assertEqual(b.state, CircuitBreaker.CLOSED)

    def test_p6_request_errors_do_not_open(self):
        """П. 6: пять кривых запросов подряд - не повод отключать сервис для всех."""
        b = make(self.clock, minimum_calls=4)
        for _ in range(6):
            with self.assertRaises(InvalidBarcode):
                b.call(self.service.create_return, BAD)
        self.assertEqual(b.state, CircuitBreaker.CLOSED)
        self.assertEqual(self.service.calls, 6, "ошибку запроса нужно пробросить клиенту")


class Extra(unittest.TestCase):
    """Дополнительно: ошибка запроса не должна и «разбавлять» долю отказов."""

    def test_request_errors_are_neither_success_nor_failure(self):
        clock = FakeClock()
        service = FakeReturnsService(clock)
        b = make(clock)
        ok(b, service); ok(b, service)
        for _ in range(10):
            with self.assertRaises(InvalidBarcode):
                b.call(service.create_return, BAD)
        fail(b, service); fail(b, service)                  # учтённых 4, отказов 2 = 50 %
        self.assertEqual(b.state, CircuitBreaker.OPEN,
                         "если считать ошибки запроса успехами, доля отказов занижается")


if __name__ == "__main__":
    unittest.main(verbosity=2)
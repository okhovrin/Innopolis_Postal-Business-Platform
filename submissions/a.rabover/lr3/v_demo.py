"""З-11, дополнительная часть: полуоткрытое состояние с пределом времени и растущая пауза.
Запуск: python test_z11_extra.py  (тесты курса test_z11.py не изменены)."""
import unittest

from v_circuit_breaker import (CircuitBreaker, CircuitOpenError, FakeClock,
                                 FakeReturnsService, ServiceTimeout, ServiceUnavailable)

GOOD = "12345678901234"


def make(clock, **kw):
    p = dict(window_seconds=60, failure_rate_threshold=0.5, minimum_calls=4, open_seconds=10,
             half_open_calls=2, half_open_max_seconds=20, backoff_multiplier=2, max_open_seconds=40)
    p.update(kw)
    return CircuitBreaker("возвраты", clock=clock, **p)


def trip(b, s):
    s.mode = "down"
    for _ in range(4):
        try: b.call(s.create_return, GOOD)
        except ServiceUnavailable: pass


class Extra2(unittest.TestCase):
    def setUp(self):
        self.c = FakeClock(); self.s = FakeReturnsService(self.c)

    def test_after_pause_goes_half_open(self):
        b = make(self.c); trip(b, self.s); self.c.advance(10)
        self.assertEqual(b.state, CircuitBreaker.HALF_OPEN)

    def test_failed_probe_reopens_and_pause_grows(self):
        b = make(self.c); trip(b, self.s); self.c.advance(10)
        with self.assertRaises(ServiceUnavailable):
            b.call(self.s.create_return, GOOD)
        self.assertEqual(b.state, CircuitBreaker.OPEN)
        self.assertEqual(b.current_open_seconds, 20)
        with self.assertRaises(CircuitOpenError) as e:
            b.call(self.s.create_return, GOOD)
        self.assertAlmostEqual(e.exception.retry_after, 20, delta=0.01)

    def test_pause_growth_is_capped(self):
        b = make(self.c); trip(b, self.s)
        for _ in range(5):                       # 10 -> 20 -> 40 -> 40 -> 40
            self.c.advance(b.current_open_seconds)
            try: b.call(self.s.create_return, GOOD)
            except ServiceUnavailable: pass
        self.assertEqual(b.current_open_seconds, 40)

    def test_successful_probes_close_and_reset_pause(self):
        b = make(self.c); trip(b, self.s); self.c.advance(10)
        try: b.call(self.s.create_return, GOOD)
        except ServiceUnavailable: pass           # пауза выросла до 20
        self.c.advance(20); self.s.mode = "healthy"
        b.call(self.s.create_return, GOOD)
        self.assertEqual(b.state, CircuitBreaker.HALF_OPEN)   # 1 проба из 2
        b.call(self.s.create_return, GOOD)
        self.assertEqual(b.state, CircuitBreaker.CLOSED)
        self.assertEqual(b.current_open_seconds, 10)

    def test_half_open_time_limit(self):
        b = make(self.c); trip(b, self.s); self.c.advance(10); self.s.mode = "healthy"
        b.call(self.s.create_return, GOOD)        # 1 удачная проба из 2
        self.c.advance(20)                        # предел полуоткрытого состояния истёк
        self.assertEqual(b.state, CircuitBreaker.OPEN)
        self.assertEqual(b.current_open_seconds, 20)

    def test_probe_completes_after_half_open_deadline(self):
        b = make(self.c, half_open_calls=1); trip(b, self.s); self.c.advance(10)
        def late_success():
            self.c.advance(20)
            return {"status": "CREATED"}
        b.call(late_success)
        self.assertEqual(b.state, CircuitBreaker.OPEN)
        self.assertEqual(b.current_open_seconds, 20)

    def test_timeout_counts_as_service_failure(self):
        b = make(self.c, half_open_calls=0); self.s.mode = "slow"
        for _ in range(4):
            with self.assertRaises(ServiceTimeout):
                b.call(self.s.create_return, GOOD)
        self.assertEqual(b.state, CircuitBreaker.OPEN)

    def test_defaults_are_two_state(self):
        c = FakeClock(); s = FakeReturnsService(c)
        b = CircuitBreaker("возвраты", window_seconds=60, failure_rate_threshold=0.5,
                           minimum_calls=4, open_seconds=10, clock=c)
        trip(b, s); c.advance(10)
        self.assertEqual(b.state, CircuitBreaker.CLOSED)


def demo_lr3() -> None:
    """Три случая ЛР-3: последовательные отказы, неудачный пробный запрос, восстановление."""
    c = FakeClock(); s = FakeReturnsService(c); b = make(c)

    def attempt(label):
        before = s.calls
        try:
            out = "успех: " + b.call(s.create_return, GOOD)["status"]
        except CircuitOpenError as e:
            out = f"ОТКАЗ РАЗМЫКАТЕЛЯ, повтор через {e.retry_after:.0f} с"
        except ServiceUnavailable as e:
            out = f"отказ сервиса: {e}"
        called = "да" if s.calls > before else "НЕТ"
        print(f"t={c():4.0f} с | {label:<8} | вызван: {called:<3} | цепь: {b.state:<9} | "
              f"пауза {b.current_open_seconds:.0f} с | {out}")

    print("1. Последовательные отказы -> размыкание")
    s.mode = "down"
    for _ in range(5): attempt("down"); c.advance(1)
    print("2. Пауза истекла, сервис ещё лежит -> неудачная проба, пауза растёт")
    c.advance(10); attempt("down"); c.advance(1); attempt("down")
    print("3. Сервис восстановился -> две удачные пробы -> цепь замкнута")
    s.mode = "healthy"; c.advance(b.retry_after())
    for _ in range(3): attempt("healthy"); c.advance(1)


if __name__ == "__main__":
    demo_lr3()
    print()
    unittest.main(verbosity=1)

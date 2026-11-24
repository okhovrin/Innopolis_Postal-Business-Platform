"""
ЛР-3, пункт (в). Размыкатель цепи в полном варианте
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Tuple, Type


# -----
# Часы
# -----
class FakeClock:
    """Часы, которыми управляет вызывающий код."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ----------------------------------------------------------------
# Модель ошибок: «мы не смогли» против «вы неправильно попросили»
# ----------------------------------------------------------------

class ServiceError(Exception):
    """Ошибка говорит о состоянии смежного сервиса."""


class ServiceUnavailable(ServiceError):
    """Смежный сервис недоступен."""


class ServiceTimeout(ServiceError):
    """Смежный сервис не ответил за отведённое время."""


class RequestError(Exception):
    """Ошибка говорит о запросе, а не о сервисе."""


class InvalidBarcode(RequestError):
    """Идентификатор отправления указан с ошибкой."""


class CircuitOpenError(Exception):
    """Цепь разомкнута: вызов не выполнялся.

    Это НЕ отказ смежного сервиса и НЕ ошибка запроса: сервис в этот раз
    никто не вызывал. Ошибка временная - клиенту сообщается время повтора
    """

    temporary = True
    code = "CIRCUIT_OPEN"
    http_status = 503

    def __init__(self, breaker_name: str, retry_after: float) -> None:
        self.breaker_name = breaker_name
        self.retry_after = retry_after
        super().__init__(
            f"Цепь «{breaker_name}» разомкнута; повторите через {retry_after:.0f} с"
        )


# ----------------------------------------------------------------
# Фиктивный смежный сервис: проверка исходного отправления по ШПИ
# ----------------------------------------------------------------

class FakeShipmentService:
    """Смежный сервис отправлений.

    healthy - отвечает быстро и успешно;
    down - сразу отказывает (ServiceUnavailable);
    slow - «висит» timeout_seconds и обрывается таймаутом (ServiceTimeout).
    """

    def __init__(self, clock: FakeClock, timeout_seconds: float = 5.0) -> None:
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.mode = "healthy"
        self.calls = 0

    def get_shipment(self, barcode: str) -> dict:
        self.calls += 1
        if not (isinstance(barcode, str) and len(barcode) == 14 and barcode.isdigit()):
            raise InvalidBarcode(f"Идентификатор {barcode!r}: нужно 14 цифр")
        if self.mode == "down":
            raise ServiceUnavailable("Сервис отправлений недоступен")
        if self.mode == "slow":
            self.clock.advance(self.timeout_seconds)
            raise ServiceTimeout(f"Нет ответа за {self.timeout_seconds:.0f} с")
        return {"barcode": barcode, "status": "DELIVERED", "client_id": "C-1024"}


# ----------------
# Размыкатель цепи
# ----------------
class CircuitBreaker:
    """Размыкатель для одного направления вызовов.

    Состояния:
        CLOSED - вызовы проходят, результаты учитываются в окне;
        OPEN - вызовов нет, клиент сразу получает ошибку с временем повтора;
        HALF_OPEN - пропускаются пробные вызовы; по их исходу цепь
                     замыкается или снова размыкается с более длинной паузой
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        *,
        window_seconds: float,
        failure_rate_threshold: float,
        minimum_calls: int,
        open_seconds: float,
        clock: Callable[[], float],
        recorded_errors: Tuple[Type[BaseException], ...] = (ServiceError,),
        half_open_probes: int = 1,
        half_open_timeout_seconds: float = 30.0,
        pause_multiplier: float = 2.0,
        max_open_seconds: float = 300.0,
    ) -> None:
        self.name = name
        self.window_seconds = window_seconds
        self.failure_rate_threshold = failure_rate_threshold
        self.minimum_calls = minimum_calls
        self.open_seconds = open_seconds
        self.clock = clock
        self.recorded_errors = recorded_errors

        self.half_open_probes = half_open_probes
        self.half_open_timeout_seconds = half_open_timeout_seconds
        self.pause_multiplier = pause_multiplier
        self.max_open_seconds = max_open_seconds

        self._results: Deque[Tuple[float, bool]] = deque()
        self._state = self.CLOSED
        self._opened_at: float | None = None
        self._half_open_at: float | None = None
        self._probes_used = 0
        self._failure_streak = 0

    # ---- пауза -----

    @property
    def current_pause(self) -> float:
        """Длительность текущей паузы: растёт с каждой неудачной пробой."""
        if self._failure_streak <= 1:
            return self.open_seconds
        pause = self.open_seconds * (self.pause_multiplier ** (self._failure_streak - 1))
        return min(pause, self.max_open_seconds)

    # ---- состояние ----

    @property
    def state(self) -> str:
        self._sync(self.clock())
        return self._state

    def _sync(self, now: float) -> None:
        """Переходы по времени: OPEN → HALF_OPEN → OPEN."""
        if self._state == self.OPEN and self._opened_at is not None:
            if now - self._opened_at >= self.current_pause:
                self._state = self.HALF_OPEN
                self._half_open_at = now
                self._probes_used = 0
        elif self._state == self.HALF_OPEN and self._half_open_at is not None:
            if now - self._half_open_at > self.half_open_timeout_seconds:
                self._reopen(now)

    # ---- вызов ----

    def call(self, fn: Callable, *args, **kwargs):
        now = self.clock()
        self._sync(now)

        if self._state == self.OPEN:
            raise CircuitOpenError(self.name, self._retry_after(now))

        if self._state == self.HALF_OPEN:
            if self._probes_used >= self.half_open_probes:
                raise CircuitOpenError(self.name, self._retry_after(now))
            self._probes_used += 1
            try:
                result = fn(*args, **kwargs)
            except self.recorded_errors:
                self._reopen(now)
                raise
            except BaseException:
                self._probes_used -= 1
                raise
            self._close()
            return result

        try:
            result = fn(*args, **kwargs)
        except self.recorded_errors:
            self._record(now, True)
            raise
        except BaseException:
            raise
        self._record(now, False)
        return result

    def _retry_after(self, now: float) -> float:
        """Сколько секунд осталось до следующей попытки.

        В OPEN - остаток паузы. В HALF_OPEN - остаток предела времени:
        пробные вызовы исчерпаны, ждём закрытия окна полуоткрытого состояния.
        """
        if self._state == self.OPEN and self._opened_at is not None:
            return max(0.0, self.current_pause - (now - self._opened_at))
        if self._state == self.HALF_OPEN and self._half_open_at is not None:
            left = self.half_open_timeout_seconds - (now - self._half_open_at)
            return max(0.0, left)
        return 0.0

    # ---- переходы ----

    def _record(self, now: float, failed: bool) -> None:
        self._results.append((now, failed))
        self._prune(now)
        if self._should_open():
            self._state = self.OPEN
            self._opened_at = now
            self._failure_streak += 1

    def _reopen(self, now: float) -> None:
        self._state = self.OPEN
        self._opened_at = now
        self._half_open_at = None
        self._probes_used = 0
        self._failure_streak += 1

    def _close(self) -> None:
        self._state = self.CLOSED
        self._opened_at = None
        self._half_open_at = None
        self._probes_used = 0
        self._failure_streak = 0
        self._results.clear()

    # ---- окно ----

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._results and self._results[0][0] < cutoff:
            self._results.popleft()

    def _should_open(self) -> bool:
        total = len(self._results)
        if total < self.minimum_calls:
            return False
        failures = sum(1 for _, failed in self._results if failed)
        return failures / total >= self.failure_rate_threshold
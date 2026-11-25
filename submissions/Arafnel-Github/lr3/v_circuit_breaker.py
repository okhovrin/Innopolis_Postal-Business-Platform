"""
ЛР-3, пункт (в). Полная реализация размыкателя цепи. Состояния: CLOSED, OPEN, HALF_OPEN
Дополнительные возможности:
  - ограничение количества пробных вызовов в HALF_OPEN;
  - таймаут нахождения в HALF_OPEN (защита от зависания на редких направлениях);
  - экспоненциально растущая пауза при повторных неудачных пробах
"""

from __future__ import annotations

import math
from collections import deque
from typing import Callable, Deque, Tuple, Type


class FakeClock:
    """Управляемые часы для детерминированного тестирования без ожидания в реальном времени"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ServiceError(Exception):
    """Ошибка смежного сервиса («мы не смогли»): учитывается размыкателем"""


class ServiceUnavailable(ServiceError):
    """Смежный сервис вернул 503 или недоступен по сети"""


class ServiceTimeout(ServiceError):
    """Смежный сервис не уложился в отведённый таймаут"""


class RequestError(Exception):
    """Ошибка входных данных клиента ("вы неправильно попросили"): НЕ учитывается размыкателем"""


class InvalidBarcode(RequestError):
    """Некорректный формат идентификатора отправления"""


class CircuitOpenError(Exception):
    """Исключение быстрого отказа при разомкнутой цепи
    Не является ошибкой смежного сервиса (реальный сетевой вызов не выполнялся)
    """

    temporary = True
    error_code = "CIRCUIT_BREAKER_OPEN"

    def __init__(self, breaker_name: str, retry_after: float) -> None:
        self.breaker_name = breaker_name
        self.retry_after = retry_after
        super().__init__(
            f"Цепь «{breaker_name}» разомкнута ({self.error_code}); "
            f"повторите запрос через {math.ceil(retry_after)} с"
        )


class FakeDownstreamService:
    """Имитация смежного сервиса"""

    def __init__(self, clock: FakeClock, timeout_seconds: float = 5.0) -> None:
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.mode = "healthy"
        self.calls = 0

    def execute(self, payload: str) -> dict:
        self.calls += 1
        if not (isinstance(payload, str) and len(payload) == 14 and payload.isdigit()):
            raise InvalidBarcode(f"Неверный формат ШПИ: {payload!r}")
        if self.mode == "down":
            raise ServiceUnavailable("Смежный сервис отклонил соединение")
        if self.mode == "slow":
            self.clock.advance(self.timeout_seconds)
            raise ServiceTimeout(f"Таймаут ожидания ответа ({self.timeout_seconds:.0f} с)")
        return {"payload": payload, "result": "OK"}


class CircuitBreaker:
    """Конечный автомат размыкателя цепи для одного направления вызовов"""

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
            permitted_calls_in_half_open: int,
            half_open_timeout_seconds: float,
            clock: Callable[[], float],
            recorded_errors: Tuple[Type[BaseException], ...] = (ServiceError,),
            max_backoff_exponent: int = 4,
    ) -> None:
        self.name = name
        self.window_seconds = window_seconds
        self.failure_rate_threshold = failure_rate_threshold
        self.minimum_calls = minimum_calls
        self.open_seconds = open_seconds
        self.permitted_calls_in_half_open = permitted_calls_in_half_open
        self.half_open_timeout_seconds = half_open_timeout_seconds
        self.clock = clock
        self.recorded_errors = recorded_errors
        self.max_backoff_exponent = max_backoff_exponent

        self._results: Deque[Tuple[float, bool]] = deque()
        self._state = self.CLOSED
        self._opened_at: float | None = None
        self._half_opened_at: float | None = None
        self._consecutive_open_cycles = 0

    @property
    def current_open_duration(self) -> float:
        """Длительность текущей паузы с учётом экспоненциального роста при повторных неудачах"""
        if self._consecutive_open_cycles <= 0:
            return self.open_seconds
        exponent = min(self._consecutive_open_cycles, self.max_backoff_exponent)
        return self.open_seconds * (2 ** exponent)

    @property
    def state(self) -> str:
        """Вычисляет и при необходимости обновляет состояние автомата по текущему времени"""
        now = self.clock()

        if self._state == self.OPEN and self._opened_at is not None:
            if now - self._opened_at >= self.current_open_duration:
                self._transition_to_half_open(now)

        elif self._state == self.HALF_OPEN and self._half_opened_at is not None:
            # Если пробные вызовы не набрались за отведённое время, возвращаем цепь в OPEN
            if now - self._half_opened_at >= self.half_open_timeout_seconds:
                self._transition_to_open(now, increment_backoff=True)

        return self._state

    def call(self, fn: Callable, *args, **kwargs):
        """Выполнить вызов fn под защитой размыкателя"""
        now = self.clock()
        current_state = self.state

        if current_state == self.OPEN:
            assert self._opened_at is not None
            elapsed = now - self._opened_at
            retry_after = max(0.0, self.current_open_duration - elapsed)
            raise CircuitOpenError(self.name, retry_after)

        if current_state == self.HALF_OPEN:
            if len(self._results) >= self.permitted_calls_in_half_open:
                raise CircuitOpenError(self.name, 1.0)

        try:
            response = fn(*args, **kwargs)
            self._record_outcome(self.clock(), is_failure=False)
            return response
        except Exception as exc:
            if isinstance(exc, self.recorded_errors):
                self._record_outcome(self.clock(), is_failure=True)
            # Ошибки RequestError (например, InvalidBarcode) пробрасываются без записи в статистику
            raise

    def _record_outcome(self, now: float, is_failure: bool) -> None:
        self._results.append((now, is_failure))

        if self._state == self.CLOSED:
            self._prune_window(now)
            if self._is_threshold_exceeded():
                self._transition_to_open(now, increment_backoff=False)

        elif self._state == self.HALF_OPEN:
            # В полуоткрытом состоянии решение принимается после завершения серии пробных вызовов
            # либо досрочно, если пробный запрос упал (при лимите 1 проба)
            if len(self._results) >= self.permitted_calls_in_half_open:
                failures = sum(1 for _, fail in self._results if fail)
                failure_rate = failures / len(self._results)
                if failure_rate >= self.failure_rate_threshold:
                    self._transition_to_open(now, increment_backoff=True)
                else:
                    self._transition_to_closed()

    def _transition_to_open(self, now: float, increment_backoff: bool) -> None:
        self._state = self.OPEN
        self._opened_at = now
        self._half_opened_at = None
        self._results.clear()
        if increment_backoff:
            self._consecutive_open_cycles += 1

    def _transition_to_half_open(self, now: float) -> None:
        self._state = self.HALF_OPEN
        self._half_opened_at = now
        self._results.clear()

    def _transition_to_closed(self) -> None:
        self._state = self.CLOSED
        self._opened_at = None
        self._half_opened_at = None
        self._consecutive_open_cycles = 0
        self._results.clear()

    def _prune_window(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._results and self._results[0][0] < cutoff:
            self._results.popleft()

    def _is_threshold_exceeded(self) -> bool:
        if len(self._results) < self.minimum_calls:
            return False
        failures = sum(1 for _, fail in self._results if fail)
        return (failures / len(self._results)) >= self.failure_rate_threshold
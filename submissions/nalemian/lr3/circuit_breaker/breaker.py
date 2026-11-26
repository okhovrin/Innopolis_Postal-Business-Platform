"""Размыкатель для вызова API отправлений."""

import asyncio
import math
import time
from collections import deque


class DependencyError(Exception):
    """Ошибка доступности смежного сервиса, учитывается в окне."""


class ServiceUnavailable(DependencyError):
    pass


class ServiceTimeout(DependencyError):
    pass


class InvalidRequest(Exception):
    """Ошибка данных запроса, не учитывается в окне."""


class BusinessRejected(Exception):
    """Окончательный бизнес-отказ, не учитывается в окне."""


class CircuitOpen(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, math.ceil(retry_after))
        super().__init__("Вызов временно заблокирован размыкателем")


class CircuitBreaker:
    """Один экземпляр на направление."""

    def __init__(
        self, *, window_seconds, failure_threshold, minimum_calls,
        open_seconds, max_open_seconds, call_timeout, half_open_timeout,
        clock=time.monotonic,
    ):
        if not 0 < failure_threshold <= 1:
            raise ValueError("failure_threshold должен быть в (0, 1]")
        if not isinstance(minimum_calls, int) or minimum_calls < 1:
            raise ValueError("minimum_calls должен быть положительным целым")
        if min(window_seconds, open_seconds, call_timeout, half_open_timeout) <= 0:
            raise ValueError("Интервалы должны быть положительными")
        if max_open_seconds < open_seconds:
            raise ValueError("max_open_seconds должен быть не меньше open_seconds")
        self.window_seconds = window_seconds
        self.failure_threshold = failure_threshold
        self.minimum_calls = minimum_calls
        self.open_seconds = open_seconds
        self.max_open_seconds = max_open_seconds
        self.call_timeout = call_timeout
        self.half_open_timeout = half_open_timeout
        self.clock = clock
        self._state = "CLOSED"
        self._results = deque()
        self._pause = open_seconds
        self._open_until = 0
        self._half_open_until = 0
        self._probe_in_flight = False
        self._generation = 0

    @property
    def state(self):
        self._refresh()
        return self._state

    def _refresh(self):
        if self._state == "HALF_OPEN" and self.clock() >= self._half_open_until:
            self._open(grow=True)

    def _open(self, *, grow):
        if grow:
            self._pause = min(self._pause * 2, self.max_open_seconds)
        self._state = "OPEN"
        self._open_until = self.clock() + self._pause
        self._probe_in_flight = False
        self._generation += 1

    def _close(self):
        self._state = "CLOSED"
        self._results.clear()
        self._pause = self.open_seconds
        self._probe_in_flight = False
        self._generation += 1

    def _permit(self):
        self._refresh()
        now = self.clock()
        if self._state == "OPEN":
            if now < self._open_until:
                raise CircuitOpen(self._open_until - now)
            self._state = "HALF_OPEN"
            self._half_open_until = now + self.half_open_timeout
        is_probe = self._state == "HALF_OPEN"
        if is_probe:
            if self._probe_in_flight:
                raise CircuitOpen(self._half_open_until - now)
            self._probe_in_flight = True
        return is_probe, self._generation

    def _record(self, failed, is_probe, generation):
        self._refresh()
        if generation != self._generation:
            return
        if is_probe:
            if failed:
                self._open(grow=True)
            else:
                self._close()
            return
        now = self.clock()
        while self._results and self._results[0][0] <= now - self.window_seconds:
            self._results.popleft()
        self._results.append((now, failed))
        if len(self._results) >= self.minimum_calls:
            failures = sum(result for _, result in self._results)
            if failures / len(self._results) >= self.failure_threshold:
                self._open(grow=False)

    async def call(self, operation, *args, **kwargs):
        is_probe, generation = self._permit()
        timeout = self.call_timeout
        if is_probe:
            timeout = min(timeout, self._half_open_until - self.clock())
        try:
            result = await asyncio.wait_for(operation(*args, **kwargs), timeout)
        except asyncio.TimeoutError as error:
            self._record(True, is_probe, generation)
            raise ServiceTimeout("Смежный сервис не ответил вовремя") from error
        except DependencyError:
            self._record(True, is_probe, generation)
            raise
        except BaseException:
            if is_probe and generation == self._generation:
                self._probe_in_flight = False
            raise
        else:
            self._record(False, is_probe, generation)
            return result


def client_error(error):
    """HTTP-представление ошибки адаптера; воркер сохраняет его в строке задания."""
    if isinstance(error, CircuitOpen):
        status, code, message = 503, "CIRCUIT_OPEN", str(error)
        headers = {"Retry-After": str(error.retry_after)}
    elif isinstance(error, ServiceTimeout):
        status, code, message = 503, "DEPENDENCY_TIMEOUT", str(error)
        headers = {}
    elif isinstance(error, DependencyError):
        status, code, message = 503, "DEPENDENCY_UNAVAILABLE", str(error)
        headers = {}
    elif isinstance(error, InvalidRequest):
        status, code, message = 400, "INVALID_REQUEST", str(error)
        headers = {}
    elif isinstance(error, BusinessRejected):
        status, code, message = 422, "RETURN_NOT_ALLOWED", str(error)
        headers = {}
    else:
        raise TypeError("Неизвестный класс ошибки")
    body = {"code": code, "message": message, "details": []}
    if isinstance(error, CircuitOpen):
        body["retry_after_seconds"] = error.retry_after
    return status, headers, body

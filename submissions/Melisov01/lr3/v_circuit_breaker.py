"""
ЛР-3 (в). Размыкатель цепи — полный вариант: замкнута / разомкнута / полуоткрыта.

Связь из интеграционной карты: сервис возвратов -> API отправлений,
синхронная проверка «исходное отправление принадлежит клиенту и допускает возврат»
в операции POST /v1/returns (см. README, связь С2, и v-notes.md).

Основа — заготовка З-11 (FakeClock, классы ошибок, CircuitOpenError).
Добавлено по сравнению с З-11:
  * полуоткрытое состояние с ограниченным числом пробных запросов;
  * растущая пауза: после каждой неудачной пробы пауза умножается, с верхней границей;
  * предел времени полуоткрытого состояния: если пробы не дали решения вовремя,
    цепь снова размыкается (а не висит полуоткрытой часами, как в конфигурации по умолчанию
    промышленной библиотеки — занятие 11);
  * отображение исключений в ответ публичного API (что видит клиент).

Только стандартная библиотека Python 3.8+.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Optional, Tuple, Type


# --------------------------------------------------------------------------
# Часы (из заготовки З-11): тест двигает время вручную, ничего не ждёт
# --------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------
# Классы ошибок (модель ошибок занятия 10)
# --------------------------------------------------------------------------

class ServiceError(Exception):
    """«Мы не смогли»: ошибка говорит о состоянии смежного сервиса. Считается размыкателем."""


class ServiceUnavailable(ServiceError):
    """Смежный сервис отказал."""


class ServiceTimeout(ServiceError):
    """Смежный сервис не ответил за отведённое время."""


class RequestError(Exception):
    """«Вы неправильно попросили»: ошибка о запросе. Размыкатель её НЕ считает."""


class InvalidBarcode(RequestError):
    """ШПИ указан с ошибкой."""


class ShipmentNotFound(RequestError):
    """У клиента нет такого отправления (в том числе — оно чужое)."""


class CircuitOpenError(Exception):
    """Цепь разомкнута (или полуоткрыта и проба уже идёт): вызов НЕ выполнялся.

    Ошибка временная. retry_after — точное число секунд до момента,
    когда размыкатель пропустит пробный запрос.
    """

    temporary = True

    def __init__(self, breaker_name: str, retry_after: float, state: str) -> None:
        self.breaker_name = breaker_name
        self.retry_after = retry_after
        self.state = state
        super().__init__(
            f"Цепь «{breaker_name}» {state}; вызов не выполнялся; повторите через {retry_after:.0f} с"
        )


# --------------------------------------------------------------------------
# Размыкатель цепи
# --------------------------------------------------------------------------

class CircuitBreaker:
    """Размыкатель для ОДНОГО направления вызовов (здесь — «отправления»).

    Параметры (все — в конструкторе, чисел в теле класса нет):
      window_seconds          — длина скользящего окна по времени;
      failure_rate_threshold  — доля отказов в окне, при которой цепь размыкается;
      minimum_calls           — до этого числа вызовов в окне решение не принимается;
      open_seconds            — базовая пауза в разомкнутом состоянии;
      backoff_multiplier      — во сколько раз растёт пауза после каждой неудачной пробы;
      max_open_seconds        — верхняя граница паузы;
      half_open_probes        — сколько пробных вызовов пропускается и сколько успехов нужно для замыкания;
      max_half_open_seconds   — предел времени в полуоткрытом состоянии;
      recorded_errors         — какие исключения считаются отказом сервиса.
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
        backoff_multiplier: float = 2.0,
        max_open_seconds: Optional[float] = None,
        half_open_probes: int = 1,
        max_half_open_seconds: Optional[float] = None,
        recorded_errors: Tuple[Type[BaseException], ...] = (ServiceError,),
        on_transition: Optional[Callable[[str, str, float], None]] = None,
    ) -> None:
        if not 0 < failure_rate_threshold <= 1:
            raise ValueError("failure_rate_threshold должен быть в (0, 1]")
        if half_open_probes < 1:
            raise ValueError("half_open_probes >= 1")
        self.name = name
        self.window_seconds = window_seconds
        self.failure_rate_threshold = failure_rate_threshold
        self.minimum_calls = minimum_calls
        self.base_open_seconds = open_seconds
        self.backoff_multiplier = backoff_multiplier
        self.max_open_seconds = max_open_seconds if max_open_seconds is not None else open_seconds
        self.half_open_probes = half_open_probes
        self.max_half_open_seconds = max_half_open_seconds
        self.clock = clock
        self.recorded_errors = recorded_errors
        self.on_transition = on_transition

        self._results: Deque[Tuple[float, bool]] = deque()  # (момент, был ли отказ)
        self._state = self.CLOSED
        self._opened_at: float = 0.0
        self._current_open_seconds: float = open_seconds
        self._half_open_since: float = 0.0
        self._probes_started = 0
        self._probes_succeeded = 0
        self._probes_in_flight = 0

    # ---- состояние -------------------------------------------------------

    @property
    def state(self) -> str:
        """Текущее состояние с учётом времени (переходы по времени выполняются лениво)."""
        self._advance_by_time()
        return self._state

    @property
    def current_open_seconds(self) -> float:
        return self._current_open_seconds

    def retry_after(self) -> float:
        """Сколько секунд до следующего пробного запроса (0, если вызов пройдёт сейчас)."""
        self._advance_by_time()
        if self._state == self.OPEN:
            return max(0.0, self._opened_at + self._current_open_seconds - self.clock())
        if self._state == self.HALF_OPEN and self._probes_started >= self.half_open_probes:
            # Все пробные места заняты: ответ на пробу придёт не позже таймаута вызова,
            # а решение — не позже предела полуоткрытого состояния.
            if self.max_half_open_seconds is not None:
                return max(1.0, self._half_open_since + self.max_half_open_seconds - self.clock())
            return 1.0
        return 0.0

    def _transition(self, new_state: str) -> None:
        old = self._state
        self._state = new_state
        now = self.clock()
        if new_state == self.OPEN:
            self._opened_at = now
        elif new_state == self.HALF_OPEN:
            self._half_open_since = now
            self._probes_started = 0
            self._probes_succeeded = 0
            self._probes_in_flight = 0
        elif new_state == self.CLOSED:
            self._results.clear()
            self._current_open_seconds = self.base_open_seconds
        if self.on_transition and old != new_state:
            self.on_transition(old, new_state, now)

    def _advance_by_time(self) -> None:
        now = self.clock()
        if self._state == self.OPEN and now >= self._opened_at + self._current_open_seconds:
            self._transition(self.HALF_OPEN)
        if (
            self._state == self.HALF_OPEN
            and self.max_half_open_seconds is not None
            and self._probes_in_flight == 0
            and now >= self._half_open_since + self.max_half_open_seconds
        ):
            # Пробы не дали решения за отведённое время — считаем это неудачей пробы.
            self._reopen_after_failed_probe()

    def _reopen_after_failed_probe(self) -> None:
        self._current_open_seconds = min(
            self._current_open_seconds * self.backoff_multiplier, self.max_open_seconds
        )
        self._transition(self.OPEN)

    # ---- окно ------------------------------------------------------------

    def _prune(self, now: float) -> None:
        border = now - self.window_seconds
        while self._results and self._results[0][0] <= border:
            self._results.popleft()

    def _should_open(self) -> bool:
        total = len(self._results)
        if total < self.minimum_calls:
            return False
        failures = sum(1 for _, failed in self._results if failed)
        return failures / total >= self.failure_rate_threshold

    def window_stats(self) -> Tuple[int, int]:
        self._prune(self.clock())
        return len(self._results), sum(1 for _, f in self._results if f)

    # ---- вызов -----------------------------------------------------------

    def call(self, fn: Callable, *args, **kwargs):
        self._advance_by_time()

        if self._state == self.OPEN:
            raise CircuitOpenError(self.name, self.retry_after(), "разомкнута")

        is_probe = self._state == self.HALF_OPEN
        if is_probe:
            if self._probes_started >= self.half_open_probes:
                raise CircuitOpenError(self.name, self.retry_after(), "полуоткрыта, проба уже идёт")
            self._probes_started += 1
            self._probes_in_flight += 1

        try:
            result = fn(*args, **kwargs)
        except self.recorded_errors:
            if is_probe:
                self._probes_in_flight -= 1
                self._reopen_after_failed_probe()
            else:
                self._record(failed=True)
            raise
        except BaseException:
            # Ошибка запроса: о сервисе ничего не говорит — не считаем ни в окне, ни как пробу.
            if is_probe:
                self._probes_in_flight -= 1
                self._probes_started -= 1  # пробное место освобождается
            raise

        if is_probe:
            self._probes_in_flight -= 1
            self._probes_succeeded += 1
            if self._probes_succeeded >= self.half_open_probes:
                self._transition(self.CLOSED)
        else:
            self._record(failed=False)
        return result

    def _record(self, failed: bool) -> None:
        now = self.clock()
        self._results.append((now, failed))
        self._prune(now)
        if self._state == self.CLOSED and self._should_open():
            self._transition(self.OPEN)


# --------------------------------------------------------------------------
# Фиктивный смежный сервис: API отправлений (по образцу FakeReturnsService из З-11)
# --------------------------------------------------------------------------

class FakeShipmentService:
    """Проверка исходного отправления: принадлежит ли клиенту и допускает ли возврат.

    mode = "healthy" | "down" | "slow". calls — сколько раз сервис действительно вызывали.
    owners — какие ШПИ каким клиентам принадлежат (учебные данные).
    """

    def __init__(self, clock: FakeClock, timeout_seconds: float = 3.0) -> None:
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.mode = "healthy"
        self.calls = 0
        self.owners = {"80080012345678": "client-A", "80080087654321": "client-B"}

    def get_source_shipment(self, client_id: str, barcode: str) -> dict:
        self.calls += 1
        if not (isinstance(barcode, str) and len(barcode) == 14 and barcode.isdigit()):
            raise InvalidBarcode(f"ШПИ {barcode!r}: нужно 14 цифр")
        if self.mode == "down":
            raise ServiceUnavailable("API отправлений отказал")
        if self.mode == "slow":
            self.clock.advance(self.timeout_seconds)
            raise ServiceTimeout(f"API отправлений не ответил за {self.timeout_seconds:.0f} с")
        # Область видимости вшита в запрос: ищем ШПИ среди отправлений клиента.
        if self.owners.get(barcode) != client_id:
            raise ShipmentNotFound(f"Отправление {barcode} не найдено среди отправлений клиента")
        return {"barcode": barcode, "clientId": client_id, "returnAllowed": True}


# --------------------------------------------------------------------------
# Что видит клиент публичного API: исключение -> (код протокола, заголовки, тело)
# --------------------------------------------------------------------------

DEPENDENCY_RETRY_HINT_SECONDS = 5  # учебное допущение: рекомендация, а не обещание


def to_http_error(exc: BaseException, request_id: str = "req-demo") -> Tuple[int, dict, dict]:
    if isinstance(exc, CircuitOpenError):
        seconds = max(1, int(-(-exc.retry_after // 1)))  # округление вверх
        return 503, {"Retry-After": str(seconds)}, {"error": {
            "code": "CIRCUIT_OPEN", "category": "circuit_open", "retryable": True,
            "retryAfterSeconds": seconds, "dependency": "shipments", "requestId": request_id,
            "message": f"Проверка отправлений временно приостановлена; повторите через {seconds} с"}}
    if isinstance(exc, ServiceError):
        s = DEPENDENCY_RETRY_HINT_SECONDS
        return 503, {"Retry-After": str(s)}, {"error": {
            "code": "DEPENDENCY_UNAVAILABLE", "category": "dependency", "retryable": True,
            "retryAfterSeconds": s, "dependency": "shipments", "requestId": request_id,
            "message": str(exc)}}
    if isinstance(exc, ShipmentNotFound):
        return 404, {}, {"error": {
            "code": "SOURCE_SHIPMENT_NOT_FOUND", "category": "request", "retryable": False,
            "requestId": request_id, "message": str(exc)}}
    if isinstance(exc, RequestError):
        return 400, {}, {"error": {
            "code": "VALIDATION_FAILED", "category": "request", "retryable": False,
            "requestId": request_id, "message": str(exc)}}
    return 500, {}, {"error": {
        "code": "INTERNAL_ERROR", "category": "internal", "retryable": True,
        "requestId": request_id, "message": "Внутренняя ошибка"}}

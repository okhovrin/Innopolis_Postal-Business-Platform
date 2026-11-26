"""
З-11. Размыкатель цепи (circuit breaker) для одного вызова смежного сервиса.
Курс «Почтовый бизнес-кабинет», занятие 11, 30.10.2026.

Что уже дано, менять не нужно:
    FakeClock            - управляемые часы: тесты двигают время вручную и ничего не ждут;
    исключения           - два класса ошибок смежного сервиса и два класса ошибок запроса;
    FakeReturnsService   - фиктивный смежный сервис направления «возвраты»
                           с тремя режимами: "healthy", "down", "slow".

Что сделать вам - класс CircuitBreaker, места с пометкой TODO.
Обязательный объём (двухсостоянийный вариант: замкнута / разомкнута):
    1. скользящее окно по времени и ДОЛЯ отказов в нём; окно и порог - параметры;
    2. минимальное число вызовов в окне, до которого решение не принимается, - параметр;
    3. размыкание при достижении порога;
    4. при разомкнутой цепи - немедленная ошибка БЕЗ вызова сервиса,
       временная, с числом секунд до следующей попытки;
    5. возобновление вызовов после паузы; пауза - параметр;
    6. считаются только ошибки о состоянии сервиса; ошибки запроса счёт не меняют.

Проверка: `python test_z11.py` (в Colab - ячейка с тестами).
Демонстрация: `python z11_circuit_breaker.py` - сценарий «отказы -> размыкание -> восстановление».
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque, Tuple, Type


# --------------------------------------------------------------------------
# Дано: часы
# --------------------------------------------------------------------------

class FakeClock:
    """Часы, которыми управляет тест. clock() возвращает текущее время в секундах."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------
# Дано: два класса ошибок (модель ошибок занятия 10)
# --------------------------------------------------------------------------

class ServiceError(Exception):
    """«Мы не смогли»: ошибка говорит о состоянии смежного сервиса."""


class ServiceUnavailable(ServiceError):
    """Смежный сервис недоступен."""


class ServiceTimeout(ServiceError):
    """Смежный сервис не ответил за отведённое время."""


class RequestError(Exception):
    """«Вы неправильно попросили»: ошибка связана с самим запросом."""


class InvalidBarcode(RequestError):
    """Идентификатор отправления указан с ошибкой."""


class CircuitOpenError(Exception):
    """Цепь разомкнута: вызов не выполнялся.

    Ошибка временная. retry_after - сколько секунд осталось до следующей попытки.
    Это НЕ ошибка смежного сервиса: сервис в этот раз никто не вызывал.
    """

    temporary = True

    def __init__(self, breaker_name: str, retry_after: float) -> None:
        self.breaker_name = breaker_name
        self.retry_after = retry_after
        super().__init__(
            f"Цепь «{breaker_name}» разомкнута; повторите через {retry_after:.0f} с"
        )


# --------------------------------------------------------------------------
# Дано: фиктивный смежный сервис направления «возвраты»
# --------------------------------------------------------------------------

class FakeReturnsService:
    """Фиктивный смежный сервис.

    mode = "healthy" - отвечает быстро и успешно;
    mode = "down"    - сразу отказывает (ServiceUnavailable);
    mode = "slow"    - «висит» timeout_seconds и обрывается таймаутом (ServiceTimeout).
    Неверный идентификатор отправления - InvalidBarcode в любом режиме:
    запрос проверяется до обращения к хранилищу.
    calls - сколько раз сервис действительно вызывали.
    """

    def __init__(self, clock: FakeClock, timeout_seconds: float = 5.0) -> None:
        self.clock = clock
        self.timeout_seconds = timeout_seconds
        self.mode = "healthy"
        self.calls = 0

    def create_return(self, barcode: str) -> dict:
        self.calls += 1
        if not (isinstance(barcode, str) and len(barcode) == 14 and barcode.isdigit()):
            raise InvalidBarcode(f"Идентификатор {barcode!r}: нужно 14 цифр")
        if self.mode == "down":
            raise ServiceUnavailable("Сервис возвратов недоступен")
        if self.mode == "slow":
            self.clock.advance(self.timeout_seconds)
            raise ServiceTimeout(f"Нет ответа за {self.timeout_seconds:.0f} с")
        return {"barcode": barcode, "status": "CREATED"}


# --------------------------------------------------------------------------
# Сделать вам: размыкатель цепи
# --------------------------------------------------------------------------

class CircuitBreaker:
    """Размыкатель цепи для ОДНОГО направления вызовов (например, «возвраты»).

    Пример:
        breaker = CircuitBreaker("возвраты", window_seconds=60, failure_rate_threshold=0.5,
                                 minimum_calls=4, open_seconds=10, clock=clock)
        breaker.call(service.create_return, "12345678901234")

    Обязательный объём - состояния CLOSED / OPEN (параметры по умолчанию).
    Дополнительно (включается параметрами, по умолчанию выключено):
        half_open_calls        - сколько удачных пробных вызовов подряд нужно,
                                 чтобы после паузы замкнуть цепь (0 - без полуоткрытого состояния);
        half_open_max_seconds  - предел времени в полуоткрытом состоянии: не набрали
                                 удачных проб за это время - цепь снова размыкается;
        backoff_multiplier     - во сколько раз растёт пауза после каждой неудачной пробы;
        max_open_seconds       - верхняя граница растущей паузы.
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
        half_open_calls: int = 0,
        half_open_max_seconds: float | None = None,
        backoff_multiplier: float = 1.0,
        max_open_seconds: float | None = None,
    ) -> None:
        if window_seconds <= 0 or open_seconds < 0:
            raise ValueError("окно должно быть > 0, пауза - не меньше 0")
        if not 0 < failure_rate_threshold <= 1:
            raise ValueError("порог доли отказов - число в (0; 1]")
        if minimum_calls < 1:
            raise ValueError("минимум вызовов - не меньше 1")
        self.name = name
        self.window_seconds = window_seconds
        self.failure_rate_threshold = failure_rate_threshold
        self.minimum_calls = minimum_calls
        self.open_seconds = open_seconds
        self.clock = clock
        self.recorded_errors = recorded_errors
        # Дополнительно: полуоткрытое состояние и растущая пауза.
        self.half_open_calls = half_open_calls
        self.half_open_max_seconds = half_open_max_seconds
        self.backoff_multiplier = backoff_multiplier
        self.max_open_seconds = open_seconds if max_open_seconds is None else max_open_seconds

        # Результаты учтённых вызовов в окне: (момент, был ли отказ).
        self._results: Deque[Tuple[float, bool]] = deque()
        self._state = self.CLOSED
        self._opened_at: float | None = None
        self._current_open_seconds = open_seconds   # текущая (возможно, выросшая) пауза
        self._half_open_since: float | None = None
        self._half_open_successes = 0

    # ---------------------------------------------------------------- состояние

    @property
    def state(self) -> str:
        """Текущее состояние: CLOSED, OPEN (или HALF_OPEN, если оно включено).

        Пункт 5: если цепь разомкнута и пауза истекла - цепь замыкается
        (или переходит в полуоткрытое состояние), окно очищается.
        """
        self._update_state(self.clock())
        return self._state

    @property
    def current_open_seconds(self) -> float:
        """Текущая длительность паузы (растёт, если включена растущая пауза)."""
        return self._current_open_seconds

    def _update_state(self, now: float) -> None:
        if self._state == self.OPEN and now - self._opened_at >= self._current_open_seconds:
            if self.half_open_calls > 0:
                self._state = self.HALF_OPEN
                self._half_open_since = now
                self._half_open_successes = 0
                self._results.clear()
            else:
                self._close()
        if (self._state == self.HALF_OPEN and self.half_open_max_seconds is not None
                and now - self._half_open_since >= self.half_open_max_seconds):
            self._trip(now, grow=True)          # восстановление не подтвердилось вовремя

    def _trip(self, now: float, grow: bool) -> None:
        """Разомкнуть цепь; grow=True - после неудачной пробы, пауза растёт."""
        if grow:
            self._current_open_seconds = min(
                self._current_open_seconds * self.backoff_multiplier, self.max_open_seconds)
        self._state = self.OPEN
        self._opened_at = now
        self._results.clear()

    def _close(self) -> None:
        self._state = self.CLOSED
        self._opened_at = None
        self._half_open_since = None
        self._current_open_seconds = self.open_seconds
        self._results.clear()

    def retry_after(self) -> float:
        """Сколько секунд осталось до следующей попытки (0, если цепь не разомкнута)."""
        if self.state != self.OPEN:
            return 0.0
        return self._opened_at + self._current_open_seconds - self.clock()

    # ---------------------------------------------------------------- вызов

    def call(self, fn: Callable, *args, **kwargs):
        """Вызвать fn через размыкатель.

        П. 4: разомкнутая цепь - fn не вызывается, сразу CircuitOpenError с retry_after.
        П. 6: в окне учитываются успех (False) и ошибки из recorded_errors (True);
              прочие исключения (ошибки запроса) не учитываются и пробрасываются клиенту.
        П. 1-3: после каждого учтённого результата - очистка окна и проверка порога.
        """
        if self.state == self.OPEN:
            raise CircuitOpenError(self.name, self.retry_after())
        try:
            result = fn(*args, **kwargs)
        except self.recorded_errors:
            self._record(self.clock(), failed=True)
            raise
        # Любое другое исключение уходит клиенту отсюда же, не попадая в окно.
        self._record(self.clock(), failed=False)
        return result

    def _record(self, now: float, failed: bool) -> None:
        if self._state == self.HALF_OPEN:
            if (self.half_open_max_seconds is not None
                    and now - self._half_open_since >= self.half_open_max_seconds):
                self._trip(now, grow=True)
                return
            if failed:
                self._trip(now, grow=True)
            else:
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_calls:
                    self._close()
            return
        self._results.append((now, failed))
        self._prune(now)
        if self._should_open():
            self._trip(now, grow=False)

    def _prune(self, now: float) -> None:
        """П. 1: удалить из self._results записи старше now - window_seconds."""
        border = now - self.window_seconds
        while self._results and self._results[0][0] <= border:
            self._results.popleft()

    def _should_open(self) -> bool:
        """П. 1-3: вызовов в окне не меньше minimum_calls
        и доля отказов не меньше failure_rate_threshold."""
        total = len(self._results)
        if total < self.minimum_calls:
            return False
        failures = sum(1 for _, failed in self._results if failed)
        return failures / total >= self.failure_rate_threshold


# --------------------------------------------------------------------------
# Демонстрация: что происходит при отказах и при восстановлении
# --------------------------------------------------------------------------

def demo() -> None:
    clock = FakeClock()
    service = FakeReturnsService(clock)
    breaker = CircuitBreaker(
        "возвраты",
        window_seconds=60,
        failure_rate_threshold=0.5,
        minimum_calls=4,
        open_seconds=10,
        clock=clock,
    )

    def attempt(label: str) -> None:
        before = service.calls
        try:
            result = breaker.call(service.create_return, "12345678901234")
            outcome = f"успех: {result['status']}"
        except CircuitOpenError as e:
            outcome = f"ОТКАЗ РАЗМЫКАТЕЛЯ, повтор через {e.retry_after:.0f} с"
        except ServiceError as e:
            outcome = f"отказ сервиса: {e}"
        called = "да" if service.calls > before else "НЕТ"
        print(f"t={clock():5.0f} с | {label:<10} | вызван сервис: {called:<3} | "
              f"цепь: {breaker.state:<6} | {outcome}")

    print("1. Сервис здоров")
    for _ in range(3):
        attempt("healthy"); clock.advance(1)

    print("2. Сервис отказал - серия отказов")
    service.mode = "down"
    for _ in range(4):
        attempt("down"); clock.advance(1)

    print("3. Цепь разомкнута - вызовы не доходят до сервиса")
    for _ in range(3):
        attempt("down"); clock.advance(2)

    print("4. Сервис восстановился, пауза истекла")
    service.mode = "healthy"
    clock.advance(10)
    for _ in range(3):
        attempt("healthy"); clock.advance(1)


if __name__ == "__main__":
    demo()

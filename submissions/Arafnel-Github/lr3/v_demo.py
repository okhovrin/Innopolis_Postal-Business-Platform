"""
ЛР-3, пункт (в). Демонстрация работы размыкателя цепи в трёх обязательных сценариях
и проверка таймаута полуоткрытого состояния
Запуск одной командой: python v_demo.py
"""

from v_circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    FakeClock,
    FakeDownstreamService,
    InvalidBarcode,
    ServiceError,
)


def run_demo() -> None:
    clock = FakeClock()
    service = FakeDownstreamService(clock, timeout_seconds=5.0)
    breaker = CircuitBreaker(
        name="шины-и-баланса",
        window_seconds=60.0,
        failure_rate_threshold=0.5,
        minimum_calls=4,
        open_seconds=10.0,
        permitted_calls_in_half_open=1,  # 1 пробный запрос для наглядности переходов
        half_open_timeout_seconds=30.0,
        clock=clock,
    )

    def invoke(barcode: str = "12345678901234") -> None:
        calls_before = service.calls
        try:
            res = breaker.call(service.execute, barcode)
            status_msg = f"УСПЕХ ({res['result']})"
        except CircuitOpenError as e:
            status_msg = f"БЛОКИРОВКА РАЗМЫКАТЕЛЯ [{e.error_code}], повтор через {e.retry_after:.0f} с"
        except ServiceError as e:
            status_msg = f"ОТКАЗ СЕРВИСА ({type(e).__name__}: {e})"
        except InvalidBarcode as e:
            status_msg = f"ОШИБКА ЗАПРОСА ({type(e).__name__}: {e})"

        was_called = "ДА" if service.calls > calls_before else "НЕТ"
        print(
            f"t={clock():5.0f}с | вызов сервиса: {was_called:<3} | "
            f"состояние: {breaker.state:<9} | {status_msg}"
        )

    print("ЭТАП 0: Штатная работа и ошибки запроса")
    invoke("12345678901234")
    clock.advance(1)
    invoke("BAD_ID")  # Ошибка клиента не должна портить статистику
    clock.advance(1)
    invoke("12345678901234")
    clock.advance(1)

    print("\nСЛУЧАЙ 1: Последовательные отказы смежного сервиса -> размыкание")
    service.mode = "down"
    invoke()  # 3-й учтённый (1 отказ из 3 = 33%) -> ещё CLOSED
    clock.advance(1)
    invoke()  # 4-й учтённый (2 отказа из 4 = 50% >= порога) -> переход в OPEN!
    clock.advance(2)
    invoke()  # Цепь разомкнута: сервис НЕ вызывается, клиенту сразу возвращается Retry-After=8с

    print("\nСЛУЧАЙ 2: Пауза истекла -> HALF_OPEN -> неудачный пробный запрос -> рост паузы")
    clock.advance(8)  # Итого прошло 10с с момента открытия
    print(f"t={clock():5.0f}с | проверка статуса перед запросом: {breaker.state}")
    invoke()  # Пробный запрос уходит в сервис, падает -> цепь снова OPEN, пауза выросла до 20с!
    clock.advance(5)
    invoke()  # Проверяем, что retry_after теперь считается от новой 20-секундной паузы (осталось 15с)

    print("\nСЛУЧАЙ 3: Восстановление сервиса -> успешный пробный запрос -> замыкание")
    service.mode = "healthy"
    clock.advance(15)  # Прошло 20с новой паузы
    invoke()  # Пробный запрос успешен -> цепь переходит в CLOSED, счётчик роста паузы сброшен
    clock.advance(1)
    invoke()  # Обычный штатный вызов

    print("\nДОПОЛНИТЕЛЬНО: Защита от зависания в HALF_OPEN по таймауту")
    service.mode = "down"
    for _ in range(4):
        invoke()
        clock.advance(1)
    # Цепь снова в OPEN (пауза 10с)
    clock.advance(10)
    print(f"t={clock():5.0f}с | перешли в {breaker.state}, но трафик прекратился на 31 секунду...")
    clock.advance(31)  # Превышен half_open_timeout_seconds (30с)
    invoke()  # Автомат превентивно откатился в OPEN, запрос отбит без обращения к сервису

if __name__ == "__main__":
    run_demo()
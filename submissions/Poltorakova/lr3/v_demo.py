"""
ЛР-3, пункт (в). Демонстрация размыкателя в трёх случаях

1. Последовательные отказы смежного сервиса - цепь размыкается
2. Неудачный пробный запрос в полуоткрытом состоянии - цепь снова размыкается, пауза растёт
3. Восстановление сервиса - пробный запрос успешен, цепь замыкается

Запуск: python v_demo.py
С часов управляемого времени сценарий выполняется мгновенно
"""

from v_circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    FakeClock,
    FakeShipmentService,
    InvalidBarcode,
    RequestError,
    ServiceError,
)

BARCODE = "12345678901234"
BAD_BARCODE = "12345"


def attempt(breaker, service, barcode=BARCODE, label=""):
    """Один вызов через размыкатель. Возвращает строку для отчёта"""
    before = service.calls
    try:
        result = breaker.call(service.get_shipment, barcode)
        outcome = f"успех: {result['status']}"
    except CircuitOpenError as e:
        outcome = f"ОШИБКА РАЗМЫКАТЕЛЯ ({e.code}), повтор через {e.retry_after:.0f} с"
    except RequestError as e:
        outcome = f"ошибка запроса: {e}"
    except ServiceError as e:
        outcome = f"отказ сервиса: {type(e).__name__}"
    called = "да" if service.calls > before else "НЕТ"
    print(
        f"  t={breaker.clock():>4.0f} с | {label:<22} | сервис вызван: {called:<3} | "
        f"цепь: {breaker.state:<9} | {outcome}"
    )
    return outcome


def main() -> None:
    clock = FakeClock()
    service = FakeShipmentService(clock)
    breaker = CircuitBreaker(
        "отправления",
        window_seconds=60,
        failure_rate_threshold=0.5,
        minimum_calls=4,
        open_seconds=10,
        half_open_probes=1,
        half_open_timeout_seconds=30,
        clock=clock,
    )

    print("=" * 100)
    print("СЛУЧАЙ 1. Последовательные отказы смежного сервиса - цепь размыкается")
    print("=" * 100)
    service.mode = "down"
    for _ in range(4):
        attempt(breaker, service, label="отказ сервиса")
        clock.advance(1)

    print(f"\n  Итог: цепь в состоянии {breaker.state}, пауза {breaker.current_pause:.0f} с")
    print("Вызовы больше не доходят до сервиса - клиент получает ошибку сразу\n")

    attempt(breaker, service, label="вызов при открытой цепи")

    print()
    print("=" * 100)
    print("СЛУЧАЙ 2. Неудачный пробный запрос - цепь снова размыкается, пауза растёт")
    print("=" * 100)
    clock.advance(breaker.current_pause)
    print(f"  Прошла пауза. Состояние: {breaker.state} (полуоткрытое, ждём пробный запрос)\n")
    attempt(breaker, service, label="пробный запрос")

    print(f"\n  Итог: цепь снова {breaker.state}, пауза выросла до {breaker.current_pause:.0f} с")
    print("  Растущая пауза даёт зависимости больше времени на восстановление\n")

    print()
    print("=" * 100)
    print("СЛУЧАЙ 3. Восстановление сервиса - пробный запрос успешен, цепь замыкается")
    print("=" * 100)
    clock.advance(breaker.current_pause)
    print(f"  Прошла пауза. Состояние: {breaker.state}\n")
    service.mode = "healthy"
    attempt(breaker, service, label="пробный запрос")
    clock.advance(1)

    print(f"\n  Итог: цепь {breaker.state}, счётчик неудач сброшен")
    print("  Вызовы снова проходят к сервису в обычном режиме\n")

    for _ in range(2):
        attempt(breaker, service, label="обычный вызов")
        clock.advance(1)

    print()
    print("=" * 100)
    print("ЧТО ВИДИТ КЛИЕНТ: три разных класса ошибок")
    print("=" * 100)

    # Отдельный размыкатель: чистая демонстрация трёх классов ошибок.
    clock2 = FakeClock()
    service2 = FakeShipmentService(clock2)
    b2 = CircuitBreaker(
        "отправления",
        window_seconds=60,
        failure_rate_threshold=0.5,
        minimum_calls=4,
        open_seconds=10,
        clock=clock2,
    )

    service2.mode = "down"
    for _ in range(4):
        try:
            b2.call(service2.get_shipment, BARCODE)
        except ServiceError:
            pass
        clock2.advance(1)

    print("\n  1) Отказ смежного сервиса (пока цепь замкнута):")
    service2.mode = "down"
    try:
        b2.call(service2.get_shipment, BARCODE)
    except CircuitOpenError:
        pass
    except ServiceError as e:
        print(f"     отказ сервиса: {type(e).__name__}")
        print("     Сервис вызывался, но не ответил. Цепь учла отказ.")
    else:
        print("     (цепь уже разомкнута)")

    service2.mode = "healthy"
    try:
        b2.call(service2.get_shipment, BARCODE)
    except CircuitOpenError as e:
        print("\n  2) Цепь разомкнута:")
        print(f"     HTTP {e.http_status}, машиночитаемый код {e.code}, Retry-After: {e.retry_after:.0f}")
        print("     Сервис НЕ вызывался. Ошибка временная, клиент знает, когда повторить.")
        print("     Отличима от отказа сервиса: есть код CIRCUIT_OPEN и время повтора.")

    clock2.advance(b2.current_pause)
    service2.mode = "healthy"
    try:
        b2.call(service2.get_shipment, BAD_BARCODE)
    except InvalidBarcode as e:
        print("\n  3) Ошибка запроса:")
        print(f"     {e}")
        print("     Данные клиента неверны - повтор с тем же телом бесполезен.")
        print("     Такая ошибка не влияет на статистику размыкателя.")

    print("\n  Различие: отказ сервиса - сервис вызывался и не ответил;")
    print("             размыкатель - сервис не вызывался, но известно время повтора;")
    print("             ошибка запроса - сервис ответил отказом по данным клиента.")


if __name__ == "__main__":
    main()
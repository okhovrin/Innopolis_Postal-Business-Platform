"""
ЛР-3 (в). Демонстрация размыкателя: три обязательных случая и два дополнительных.

Запуск:  python3 v_demo.py
Каждый шаг печатает: время, режим сервиса, вызван ли сервис, состояние цепи, что получил клиент.
В конце каждого случая стоят проверки (assert): если поведение не то, прогон упадёт.

Параметры — учебные допущения (обоснование — v-notes.md). minimum_calls в демонстрации
уменьшен до 4, чтобы прогон был коротким; в v-notes.md — значение для эксплуатации.
"""

from __future__ import annotations

import json

from v_circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    FakeClock,
    FakeShipmentService,
    RequestError,
    ServiceError,
    to_http_error,
)

CLIENT = "client-A"
OWN = "80080012345678"      # отправление клиента A
FOREIGN = "80080087654321"  # отправление клиента B


def make(probes: int = 1):
    clock = FakeClock()
    service = FakeShipmentService(clock, timeout_seconds=3)
    log = []
    breaker = CircuitBreaker(
        "отправления",
        window_seconds=60,
        failure_rate_threshold=0.5,
        minimum_calls=4,
        open_seconds=10,
        backoff_multiplier=2,
        max_open_seconds=80,
        half_open_probes=probes,
        max_half_open_seconds=15,
        clock=clock,
        on_transition=lambda a, b, t: log.append((t, a, b)),
    )
    return clock, service, breaker, log


def attempt(clock, service, breaker, barcode=OWN, show_body=False):
    before = service.calls
    t = clock()
    try:
        breaker.call(service.get_source_shipment, CLIENT, barcode)
        status, outcome, body = 202, "202 принято (проверка пройдена)", None
    except (CircuitOpenError, ServiceError, RequestError) as exc:
        status, headers, body = to_http_error(exc)
        ra = f", Retry-After: {headers['Retry-After']}" if "Retry-After" in headers else ""
        outcome = f"{status} {body['error']['code']}{ra}"
    called = "да" if service.calls > before else "НЕТ"
    print(f"  t={t:5.0f} с | сервис: {service.mode:<7} | вызван: {called:<3} | "
          f"цепь после: {breaker.state:<9} | клиент: {outcome}")
    if show_body and body:
        print("           тело:", json.dumps(body, ensure_ascii=False))
    return status, body, called == "да"


def case_1_failures_open():
    print("\nСЛУЧАЙ 1. Последовательные отказы смежного сервиса — цепь размыкается")
    clock, service, breaker, log = make()
    print(" 1а. Сервис здоров")
    for _ in range(2):
        attempt(clock, service, breaker); clock.advance(1)
    print(" 1б. Ошибки ЗАПРОСА (неверный ШПИ, чужое отправление) — счёт отказов не меняют")
    for bc in ("123", FOREIGN, "abc", FOREIGN, "1"):
        attempt(clock, service, breaker, barcode=bc); clock.advance(1)
    assert breaker.state == "closed", "ошибки запроса не должны размыкать цепь"
    print(" 1в. Сервис отказал: вызовы выполняются и проваливаются (DEPENDENCY_UNAVAILABLE)")
    service.mode = "down"
    _, body, _ = attempt(clock, service, breaker, show_body=True); clock.advance(1)
    assert body["error"]["category"] == "dependency"
    attempt(clock, service, breaker)
    total, failed = breaker.window_stats()
    print(f"     в окне {total} учтённых вызова, из них {failed} отказа: доля {failed / total:.2f} >= 0.5 "
          f"и вызовов не меньше минимума 4 -> цепь разомкнута (ошибки запроса в окно не попали)")
    assert breaker.state == "open" and (total, failed) == (4, 2)
    clock.advance(1)
    print(" 1г. Цепь разомкнута: сервис НЕ вызывается, клиент сразу получает CIRCUIT_OPEN с точным временем")
    for i in range(3):
        status, body, called = attempt(clock, service, breaker, show_body=(i == 0))
        assert status == 503 and body["error"]["code"] == "CIRCUIT_OPEN" and not called
        clock.advance(2)
    return log


def case_2_failed_probe():
    print("\nСЛУЧАЙ 2. Неудачный пробный запрос — цепь снова размыкается, пауза растёт")
    clock, service, breaker, log = make()
    service.mode = "down"
    for _ in range(4):
        attempt(clock, service, breaker); clock.advance(1)
    assert breaker.state == "open" and breaker.current_open_seconds == 10
    clock.advance(10)
    print(f" 2а. Пауза 10 с истекла -> полуоткрыта: {breaker.state}. Пробный запрос — сервис всё ещё лежит")
    assert breaker.state == "half_open"
    _, _, called = attempt(clock, service, breaker)
    assert called and breaker.state == "open"
    print(f"     проба провалилась -> снова разомкнута, пауза теперь {breaker.current_open_seconds:.0f} с")
    assert breaker.current_open_seconds == 20
    status, body, called = attempt(clock, service, breaker)
    assert body["error"]["retryAfterSeconds"] == 20 and not called
    clock.advance(20)
    service.mode = "slow"
    print(" 2б. Через 20 с — вторая проба; сервис «висит» и обрывается таймаутом 3 с")
    _, body, called = attempt(clock, service, breaker)
    assert called and breaker.state == "open" and breaker.current_open_seconds == 40
    print(f"     проба провалилась по таймауту -> пауза {breaker.current_open_seconds:.0f} с (растущая, предел 80 с)")
    return clock, service, breaker, log


def case_3_recovery(clock, service, breaker, log):
    print("\nСЛУЧАЙ 3. Восстановление сервиса — пробный запрос успешен, цепь замыкается")
    service.mode = "healthy"
    attempt(clock, service, breaker)  # ещё разомкнута
    clock.advance(40)
    print(f" 3а. Пауза 40 с истекла -> {breaker.state}; пробный запрос")
    status, _, called = attempt(clock, service, breaker)
    assert status == 202 and called and breaker.state == "closed"
    print(f"     проба успешна -> замкнута; окно очищено, пауза сброшена до {breaker.current_open_seconds:.0f} с")
    assert breaker.current_open_seconds == 10
    clock.advance(1)
    for _ in range(2):
        attempt(clock, service, breaker); clock.advance(1)
    print(" Журнал переходов:", " -> ".join(f"{b}@{t:.0f}" for t, _, b in log))


def extra_half_open_limit():
    print("\nДОПОЛНИТЕЛЬНО. Предел времени полуоткрытого состояния (нужно 3 успешные пробы)")
    clock, service, breaker, log = make(probes=3)
    service.mode = "down"
    for _ in range(4):
        attempt(clock, service, breaker); clock.advance(1)
    clock.advance(10)
    service.mode = "healthy"
    attempt(clock, service, breaker)  # проба 1 из 3 успешна
    assert breaker.state == "half_open"
    print("  ...16 с запросов нет (редкое направление); предел полуоткрытого — 15 с")
    clock.advance(16)
    st = breaker.state
    print(f"  t={clock():5.0f} с | цепь: {st} — решение не принято вовремя, цепь снова разомкнута, "
          f"пауза {breaker.current_open_seconds:.0f} с")
    assert st == "open" and breaker.current_open_seconds == 20


def main():
    case_1_failures_open()
    clock, service, breaker, log = case_2_failed_probe()
    case_3_recovery(clock, service, breaker, log)
    extra_half_open_limit()
    print("\nВсе проверки прошли.")


if __name__ == "__main__":
    main()

import asyncio

from breaker import (
    CircuitBreaker, CircuitOpen, DependencyError, ServiceUnavailable, client_error,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeReturnsService:
    def __init__(self):
        self.healthy = False
        self.calls = 0
        self.on_call = lambda: None

    async def create_return(self, operation_key):
        self.calls += 1
        self.on_call()
        if not self.healthy:
            raise ServiceUnavailable("API отправлений недоступен")
        return {"return_id": "r501", "operation_key": operation_key}


async def main():
    clock = FakeClock()
    service = FakeReturnsService()
    breaker = CircuitBreaker(
        window_seconds=60, failure_threshold=0.5, minimum_calls=4,
        open_seconds=10, max_open_seconds=40, call_timeout=2,
        half_open_timeout=5, clock=clock,
    )

    def show_probe():
        if breaker.state == "HALF_OPEN":
            print("  Пробный вызов: HALF_OPEN")

    service.on_call = show_probe

    async def attempt(label):
        before = service.calls
        try:
            result = await breaker.call(service.create_return, "client1:r501:REGISTER")
            print(f"{label}: 200, return_id={result['return_id']}")
        except (DependencyError, CircuitOpen) as error:
            status, headers, body = client_error(error)
            print(f"{label}: {status} {body['code']}, Retry-After={headers.get('Retry-After', '-')}")
        print(f"  Цепь: {breaker.state}; вызовов сервиса за попытку: {service.calls - before}")

    print("1. Последовательные отказы")
    for number in range(1, 5):
        await attempt(f"Отказ {number}")
    await attempt("Запрос при OPEN")

    print("2. Неудачный пробный запрос")
    clock.advance(10)
    await attempt("Неудачная проба")
    await attempt("Запрос после неудачной пробы")

    print("3. Восстановление сервиса")
    clock.advance(20)
    service.healthy = True
    await attempt("Успешная проба")
    await attempt("Обычный запрос после восстановления")


if __name__ == "__main__":
    asyncio.run(main())

"""
Fixtures compartilhadas para os testes pytest.

Fornece FakeSerial — um substituto de serial.Serial que:
  - guarda tudo que foi escrito (.written: list[bytes])
  - retorna respostas pré-programadas para read_all()
  - permite simular um diálogo completo de comandos AT sem hardware real

Uso:
    def test_xxx(make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n", "OK\r\n"])
        dev.open()
        dev.send_at("AT+P2P=...")
        assert b"AT+P2P=" in fake.written[-1]
"""
from __future__ import annotations

import os
import sys
from collections import deque
from pathlib import Path

import pytest

# Adiciona a pasta LoRa_RAK_GUI/ ao sys.path para os testes encontrarem os módulos
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class FakeSerial:
    """Substituto de serial.Serial para testes sem hardware."""

    def __init__(self, port="COMx", baudrate=115200, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self.written: list[bytes] = []           # tudo que foi escrito (em ordem)
        self._read_queue: deque[bytes] = deque() # respostas a entregar para read_all()
        self._in_waiting_queue: deque[bytes] = deque()  # para in_waiting/read

    # --- API pyserial compatível ---
    @property
    def in_waiting(self) -> int:
        return sum(len(b) for b in self._in_waiting_queue)

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def read_all(self) -> bytes:
        """Retorna a próxima resposta pré-programada (1 por chamada).
        Cada queue_response() = uma resposta para uma read_all().
        Útil para simular flush_rx() seguido de send_at()."""
        if not self._read_queue:
            return b""
        return self._read_queue.popleft()

    def read(self, size: int) -> bytes:
        # Concatena chunks até size
        out = bytearray()
        while size > 0 and self._in_waiting_queue:
            chunk = self._in_waiting_queue.popleft()
            if len(chunk) <= size:
                out.extend(chunk)
                size -= len(chunk)
            else:
                out.extend(chunk[:size])
                self._in_waiting_queue.appendleft(chunk[size:])
                size = 0
        return bytes(out)

    def reset_input_buffer(self):
        """Limpa apenas o buffer de stream (in_waiting/read).
        _read_queue (respostas pré-programadas para read_all) é preservada,
        pois representa o que ainda vai 'chegar do dispositivo' nos próximos reads."""
        self._in_waiting_queue.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False

    # --- API de teste (não existe em pyserial real) ---
    def queue_response(self, data: str | bytes):
        """Adiciona uma resposta que será devolvida no próximo read_all()."""
        if isinstance(data, str):
            data = data.encode()
        self._read_queue.append(data)

    def queue_stream(self, data: str | bytes):
        """Adiciona dados para serem entregues via in_waiting/read (simula RX contínuo)."""
        if isinstance(data, str):
            data = data.encode()
        self._in_waiting_queue.append(data)

    def last_command(self) -> str:
        """Retorna o último comando escrito como string (sem \\r\\n)."""
        if not self.written:
            return ""
        return self.written[-1].decode(errors="ignore").rstrip("\r\n")

    def all_commands(self) -> list[str]:
        """Lista de todos os comandos enviados em ordem."""
        return [b.decode(errors="ignore").rstrip("\r\n") for b in self.written]


@pytest.fixture
def fake_serial():
    """Instância vazia de FakeSerial."""
    return FakeSerial()


@pytest.fixture
def make_device(monkeypatch):
    """
    Factory que cria um driver concreto com sua porta serial monkeypatched
    para usar FakeSerial. Retorna (device, fake_serial).

    Exemplo:
        def test_x(make_device):
            from drivers.rak3172 import RAK3172
            dev, fake = make_device(RAK3172, responses=["OK\\r\\n"])
            dev.open()
            dev.send_at("AT")
            assert fake.last_command() == "AT"
    """
    created: list[FakeSerial] = []

    def _factory(driver_class, *, responses: list[str] | None = None,
                 port: str = "COMx", baudrate: int | None = None):
        if baudrate is None:
            baudrate = driver_class.default_baudrate

        fake = FakeSerial(port=port, baudrate=baudrate)
        if responses:
            for r in responses:
                fake.queue_response(r)

        # Substitui serial.Serial dentro de drivers.base por fábrica que retorna fake
        import drivers.base
        monkeypatch.setattr(drivers.base.serial, "Serial", lambda *a, **kw: fake)

        # Substitui time.sleep para não tornar os testes lentos
        monkeypatch.setattr(drivers.base.time, "sleep", lambda *a, **kw: None)

        device = driver_class(port, baudrate)
        created.append(fake)
        return device, fake

    yield _factory

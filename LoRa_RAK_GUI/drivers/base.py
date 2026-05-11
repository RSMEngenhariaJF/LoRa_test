"""
Classe base abstrata para drivers de módulos LoRa.

Define o contrato comum (open/close/send_at/read_until) e métodos abstratos que cada
driver concreto deve implementar (configure_radio, tx_payload, etc.).

Concrete drivers ficam em arquivos próprios:
  - drivers/rak3172.py
  - drivers/kg200z.py
  - drivers/smart_sx1262.py

E são registrados no dicionário DEVICES em drivers/__init__.py.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

import serial


class LoRaDevice(ABC):
    """Base abstrata para drivers de módulo LoRa via UART AT.

    Subclasses devem definir:
      - name: str
      - default_baudrate: int
      - capabilities: set[str]   # subconjunto de {TX, RX, CW, SCAN, INCREMENTAL, LORAWAN}
      - ping_cmd: str            # comando para "Testar dispositivo"
      - LW_CMDS: dict[str,str]   # templates de comandos LoRaWAN (opcional)
      - LW_MODE_ENTER/EXIT: str  # se precisar trocar P2P<->LoRaWAN (ex. RAK3172)

    E implementar os métodos abstratos.
    """

    name: str = "Generic"
    default_baudrate: int = 115200
    capabilities: set[str] = set()
    ping_cmd: str = "AT"
    LW_CMDS: dict[str, str] = {}
    LW_MODE_ENTER: str | None = None
    LW_MODE_EXIT: str | None = None

    # ---- ciclo de vida ----
    def __init__(self, port: str, baudrate: int, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        # Callback de debug: chamado como debug_cb("TX"/"RX", payload_str, elapsed_ms_or_None)
        self.debug_cb: object = None
        # Public Network Mode (PNM / syncword)
        self.public_network: bool = False
        self._current_pnm: bool | None = None

    def open(self):
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        time.sleep(1.5)
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def flush_rx(self):
        if self.is_open():
            try:
                _ = self.ser.read_all()
            except Exception:
                pass

    # ---- IO base ----
    def send_at(self, cmd: str, wait: float = 0.4) -> str:
        if not self.is_open():
            raise RuntimeError("Porta não aberta")
        t0 = time.time()
        cb = self.debug_cb
        if cb:
            try: cb("TX", cmd.strip(), None)
            except Exception: pass
        with self.lock:
            self.ser.write((cmd.strip() + "\r\n").encode())
            time.sleep(wait)
            rsp = self.ser.read_all().decode(errors="ignore")
        elapsed_ms = (time.time() - t0) * 1000.0
        if cb:
            try: cb("RX", rsp, elapsed_ms)
            except Exception: pass
        return rsp

    def ping(self, wait: float = 1.0) -> tuple[bool, str]:
        """Envia ping_cmd e verifica resposta 'OK'."""
        try:
            self.flush_rx()
            rsp = self.send_at(self.ping_cmd, wait=wait)
            return ("OK" in rsp.upper()), rsp
        except Exception as e:
            return False, str(e)

    def read_until(self, timeout_s: float):
        if not self.is_open():
            raise RuntimeError("Porta não aberta")
        end = time.time() + timeout_s
        buff = ""
        while time.time() < end:
            with self.lock:
                in_w = self.ser.in_waiting if self.ser else 0
                if in_w:
                    buff += self.ser.read(in_w).decode(errors="ignore")
            while "\r\n" in buff:
                line, buff = buff.split("\r\n", 1)
                line = line.strip()
                if line:
                    yield line
            if not buff:
                time.sleep(0.05)

    # ---- LoRaWAN end-device ----
    def lw_supports(self, key: str) -> bool:
        return key in self.LW_CMDS

    def lw_cmd(self, key: str, value=None, wait: float = 0.4) -> str:
        if not self.lw_supports(key):
            raise NotImplementedError(f"{self.name} não suporta o comando LoRaWAN '{key}'")
        tmpl = self.LW_CMDS[key]
        cmd = tmpl.format(value=value) if "{value}" in tmpl else tmpl
        return self.send_at(cmd, wait=wait)

    def lw_enter_mode(self) -> str | None:
        return self.send_at(self.LW_MODE_ENTER, wait=0.6) if self.LW_MODE_ENTER else None

    def lw_exit_mode(self) -> str | None:
        return self.send_at(self.LW_MODE_EXIT, wait=0.6) if self.LW_MODE_EXIT else None

    # ---- Public Network Mode ----
    def set_public_network(self, enable: bool) -> str | None:
        """Override no driver para enviar AT+PNM ou AT+SYNCWORD."""
        return None

    def apply_public_network(self) -> str | None:
        """Aplica self.public_network se diferente do último valor."""
        if self._current_pnm == self.public_network:
            return None
        rsp = self.set_public_network(self.public_network)
        self._current_pnm = self.public_network
        return rsp

    # ---- abstratos por dispositivo ----
    @abstractmethod
    def configure_radio(self, freq_hz: int, sf: int, bw_khz: int, cr_label: str,
                        preamble: int, power: int) -> str: ...

    @abstractmethod
    def tx_payload(self, payload: bytes, msg_text: str | None = None) -> str: ...

    @abstractmethod
    def rx_continuous_start(self) -> str: ...

    @abstractmethod
    def rx_stop(self) -> str: ...

    @abstractmethod
    def cw_start(self, freq_hz: int, power: int, sf: int, bw_khz: int) -> str: ...

    @abstractmethod
    def cw_stop(self) -> str: ...

    @abstractmethod
    def parse_rx_line(self, line: str) -> tuple[str | None, float | None, float | None]: ...

"""
Sistema de logging da sessão.

- SessionLogger: gera 2 arquivos paralelos (sessao_*.txt amigável + debug_*.txt raw AT)
  e mantém lista de resultados estruturados (PacketResult) para exportar como CSV.
- PacketResult: linha de resultado de uma operação TX/RX/SCAN.

Uso:
    logger = SessionLogger(log_queue=app.queue)
    logger.open_txt(device_name="RAK3172")
    device.debug_cb = logger.debug_log  # plugar callback para AT raw
    logger.log("[INFO] Conectado")
    logger.add_result(PacketResult(...))
    logger.save_csv("resultados.csv")
    logger.close_txt()
"""
from __future__ import annotations

import csv
import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime

from constants import LOGS_DIR


# ===========================================================
#   Resultado de um pacote (linha do CSV)
# ===========================================================
@dataclass
class PacketResult:
    timestamp: str
    device: str
    mode: str
    seq: int
    payload_tx: str
    payload_rx: str
    match: int
    attempts: int
    timeout_s: float
    retries: int
    rssi: str
    snr: str
    rtt_ms: str
    freq_tx_hz: str
    freq_rx_hz: str


# ===========================================================
#   SessionLogger
# ===========================================================
@dataclass
class SessionLogger:
    """
    Gera 2 arquivos de log em paralelo:
      - sessao_YYYYMMDD_HHMMSS.txt — amigável (eventos + decisões + erros)
      - debug_YYYYMMDD_HHMMSS.txt  — raw AT (TX/RX + ΔT em ms), alimentado por debug_cb do driver

    Também mantém lista de PacketResult para exportar CSV via save_csv().
    """
    log_queue: queue.Queue
    txt_path: str = ""
    txt_file: object = None
    debug_path: str = ""
    debug_file: object = None
    results: list[PacketResult] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def open_txt(self, device_name: str = ""):
        os.makedirs(LOGS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.txt_path = os.path.join(LOGS_DIR, f"sessao_{ts}.txt")
        self.txt_file = open(self.txt_path, "a", encoding="utf-8", buffering=1)
        header = (
            f"=== Sessão LoRa iniciada em "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (dispositivo: {device_name}) ===\n"
        )
        self.txt_file.write(header)
        self.txt_file.flush()

        self.debug_path = os.path.join(LOGS_DIR, f"debug_{ts}.txt")
        self.debug_file = open(self.debug_path, "a", encoding="utf-8", buffering=1)
        self.debug_file.write(
            f"=== Debug AT iniciado em {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
            f"(dispositivo: {device_name}) ===\n"
        )

    def close_txt(self):
        if self.txt_file:
            try:
                self.txt_file.write(
                    f"=== Sessão encerrada em "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
                self.txt_file.close()
            except Exception:
                pass
            self.txt_file = None
        if self.debug_file:
            try:
                self.debug_file.write(
                    f"=== Debug encerrado em "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                )
                self.debug_file.close()
            except Exception:
                pass
            self.debug_file = None

    def log(self, msg: str):
        """Loga linha amigável no console (queue) + arquivo TXT."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_queue.put(("log", line))
        with self.lock:
            if self.txt_file:
                try:
                    self.txt_file.write(line + "\n")
                except Exception:
                    pass

    def debug_log(self, direction: str, payload: str, elapsed_ms: float | None):
        """Callback usado por LoRaDevice.send_at — registra todo I/O AT raw."""
        if not self.debug_file:
            return
        now = datetime.now()
        ts = now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"
        safe = payload.replace("\r", "\\r").replace("\n", "\\n")
        line = f"[{ts}] {direction}{'>' if direction == 'TX' else '<'} {safe}"
        if elapsed_ms is not None:
            line += f"   ΔT={elapsed_ms:.0f}ms"
        with self.lock:
            try:
                self.debug_file.write(line + "\n")
            except Exception:
                pass

    def add_result(self, r: PacketResult):
        with self.lock:
            self.results.append(r)

    def save_csv(self, path: str):
        fields = [
            "timestamp", "device", "mode", "seq",
            "payload_tx", "payload_rx", "match",
            "attempts", "timeout_s", "retries",
            "rssi", "snr", "rtt_ms",
            "freq_tx_hz", "freq_rx_hz",
        ]
        with self.lock:
            rows = list(self.results)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
            w.writeheader()
            for r in rows:
                w.writerow(r.__dict__)

"""
LoRa Multi-Device - Interface Gráfica de Testes
================================================
Aplicação Tkinter para testar módulos LoRa via UART (comandos AT).

Dispositivos suportados:
  - RAK3172 (Wisduo, baud padrão 115200)         — referência: doc proprietária
  - Quectel KG200Z (baud padrão 9600)            — doc/Quectel_KG200Z_AT_Commands_Manual_*.pdf
  - SMART SMW-SX1262M0 (baud padrão 9600)        — doc/SMART_LoRa_AT_Command_*.pdf

Modos disponíveis (variam conforme dispositivo via "capabilities"):
  - TX único           : envia payload (HEX/texto), mede RSSI/SNR/RTT da resposta
  - RX contínuo        : escuta e registra pacotes recebidos
  - Pacote incremental : sequência crescente 16 bits (análise de PDR) — RAK3172
  - Scanner            : cicla por 3 frequências, escutando dwell s em cada
  - Portadora CW       : transmite portadora contínua (teste de espectro)
  - Console AT         : envio manual de qualquer comando AT (universal)

Logs:
  - TXT auto-salvo em logs/sessao_YYYYMMDD_HHMMSS.txt (cada linha com timestamp)
  - CSV de resultados estruturados via botão "Salvar CSV"
"""

from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import serial
from serial.tools import list_ports

# Módulo LoRaWAN (gateway/NS simulado)
try:
    from lorawan import (
        BANDS, JoinRequest, build_join_accept, derive_session_keys,
        DataFrame, build_downlink, hex_to_bytes, bytes_to_hex,
        random_bytes, parse_mhdr, MTYPE_JOIN_REQUEST,
        MTYPE_UNCONF_DATA_UP, MTYPE_CONF_DATA_UP,
    )
    LORAWAN_AVAILABLE = True
except ImportError as _e:
    LORAWAN_AVAILABLE = False
    _LORAWAN_IMPORT_ERROR = str(_e)


DEVICES_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devices.json")


LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


# ===========================================================
#   CONSTANTES
# ===========================================================
CR_LABELS = ["4/5", "4/6", "4/7", "4/8"]


def mhz_to_hz(value) -> int:
    if isinstance(value, str):
        value = value.replace(",", ".").strip()
    return int(round(float(value) * 1_000_000))


def hz_to_mhz(hz: int) -> str:
    return f"{hz / 1_000_000:g}"


# ===========================================================
#   DRIVERS DE DISPOSITIVO
# ===========================================================
class LoRaDevice(ABC):
    """Classe base. Subclasses implementam os comandos AT específicos do módulo."""

    name: str = "Generic"
    default_baudrate: int = 115200
    # Capabilities suportadas. Possíveis: TX, RX, CW, SCAN, INCREMENTAL
    capabilities: set[str] = set()
    # Comando de verificação (ping) — varia por dispositivo (ver datasheets)
    ping_cmd: str = "AT"

    def __init__(self, port: str, baudrate: int, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        # Callback de debug: chamado como debug_cb("TX"/"RX", payload_str, elapsed_ms_or_None)
        self.debug_cb: object = None

    # ---- conexão / IO base (comum a todos) ----
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
        """Verifica se o módulo responde ao comando AT específico.
        Retorna (ok, resposta_bruta). Cada subclasse pode sobrescrever ping_cmd."""
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

    # ---- métodos abstratos por dispositivo ----
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


# ----------------------------- RAK3172 -----------------------------
class RAK3172(LoRaDevice):
    name = "RAK3172"
    default_baudrate = 115200
    capabilities = {"TX", "RX", "CW", "SCAN", "INCREMENTAL"}

    _CR_MAP = {"4/5": 0, "4/6": 1, "4/7": 2, "4/8": 3}

    def configure_radio(self, freq_hz, sf, bw_khz, cr_label, preamble, power):
        self.send_at("AT+NWM=0")
        cr = self._CR_MAP[cr_label]
        return self.send_at(f"AT+P2P={freq_hz}:{sf}:{bw_khz}:{cr}:{preamble}:{power}")

    def tx_payload(self, payload, msg_text=None):
        return self.send_at(f"AT+PSEND={payload.hex().upper()}")

    def rx_continuous_start(self):
        return self.send_at("AT+PRECV=65534", wait=0.2)

    def rx_stop(self):
        return self.send_at("AT+PRECV=0", wait=0.2)

    def precv_timeout(self, ms: int) -> str:
        return self.send_at(f"AT+PRECV={int(ms)}", wait=0.1)

    def cw_start(self, freq_hz, power, sf, bw_khz):
        # Configura via AT+P2P e dispara AT+TTONE
        self.send_at("AT+NWM=0")
        self.send_at(f"AT+P2P={freq_hz}:{sf}:{bw_khz}:0:10:{power}")
        return self.send_at("AT+TTONE", wait=0.2)

    def cw_stop(self):
        return self.send_at("AT", wait=0.2)

    def parse_rx_line(self, line):
        """Espera +EVT:RXP2P:<rssi>:<snr>:<payload_hex>."""
        parts = line.split(":")
        if "RXP2P" not in line.upper():
            return None, None, None
        try:
            idx = next(i for i, p in enumerate(parts) if p.upper() == "RXP2P")
            rssi = float(parts[idx + 1])
            snr = float(parts[idx + 2])
            payload_hex = parts[idx + 3].strip().upper()
            return payload_hex, rssi, snr
        except Exception:
            return None, None, None


# ----------------------------- Quectel KG200Z -----------------------------
class KG200Z(LoRaDevice):
    name = "Quectel KG200Z"
    default_baudrate = 9600
    capabilities = {"TX", "RX", "CW", "SCAN"}
    ping_cmd = "ATQ"  # KG200Z usa ATQ (não AT) para verificar comunicação

    _CR_MAP = {"4/5": 1, "4/6": 2, "4/7": 3, "4/8": 4}
    # BW LoRa: 0=7.8125, 1=15.625, 2=31.25, 3=62.5, 4=125, 5=250, 6=500
    _BW_MAP = {7: 0, 15: 1, 31: 2, 62: 3, 125: 4, 250: 5, 500: 6}

    def _bw_index(self, bw_khz: int) -> int:
        return self._BW_MAP.get(int(bw_khz), 4)

    def configure_radio(self, freq_hz, sf, bw_khz, cr_label, preamble, power):
        self.send_at("AT+QP2P=1")  # habilita modo P2P
        cr = self._CR_MAP[cr_label]
        bw_idx = self._bw_index(bw_khz)
        # AT+QTCONF=freq:pow:bw:sf:cr:lna:pa:mod:paylen:freqdev:lowdropt:BT
        # mod=1 (LoRa), lna=0, pa=0, paylen=16, freqdev=25000, lowdropt=2(auto), BT=3
        cmd = f"AT+QTCONF={freq_hz}:{power}:{bw_idx}:{sf}:{cr}:0:0:1:16:25000:2:3"
        return self.send_at(cmd)

    def tx_payload(self, payload, msg_text=None):
        # KG200Z aceita string ASCII via AT+QTDA
        # Usa msg_text quando disponível; caso contrário, manda hex como string
        data = msg_text if msg_text else payload.hex().upper()
        if len(data) > 256:
            data = data[:256]
        self.send_at(f"AT+QTDA={data}")
        return self.send_at("AT+QTTX=1", wait=0.5)

    def rx_continuous_start(self):
        # KG200Z não tem RX contínuo nativo — usa AT+QTRX com contagem alta
        return self.send_at("AT+QTRX=65535", wait=0.2)

    def rx_stop(self):
        return self.send_at("AT+QTOFF", wait=0.2)

    def cw_start(self, freq_hz, power, sf, bw_khz):
        cr = 1  # 4/5
        bw_idx = self._bw_index(bw_khz)
        self.send_at(f"AT+QTCONF={freq_hz}:{power}:{bw_idx}:{sf}:{cr}:0:0:1:16:25000:2:3")
        return self.send_at("AT+QTTONE", wait=0.2)

    def cw_stop(self):
        return self.send_at("AT+QTOFF", wait=0.2)

    def parse_rx_line(self, line):
        """KG200Z: 'RX data len=4, buf: ABCD' e 'RssiValue=-13 dBm, SnrValue=4dB'.
        Mantém estado simples — extrai o que conseguir desta linha."""
        up = line.upper()
        rssi = snr = None
        payload = None

        if "RX DATA" in up and "BUF:" in up:
            try:
                payload = line.split("buf:", 1)[1].strip()
            except Exception:
                pass

        if "RSSIVALUE" in up:
            try:
                v = up.split("RSSIVALUE", 1)[1].split("=", 1)[1]
                v = "".join(c for c in v if (c.isdigit() or c in "+-."))
                rssi = float(v)
            except Exception:
                pass
        if "SNRVALUE" in up:
            try:
                v = up.split("SNRVALUE", 1)[1].split("=", 1)[1]
                v = "".join(c for c in v if (c.isdigit() or c in "+-."))
                snr = float(v)
            except Exception:
                pass
        return payload, rssi, snr


# ----------------------------- SMART SMW-SX1262M0 -----------------------------
class SMARTSx1262(LoRaDevice):
    name = "SMART SMW-SX1262M0"
    default_baudrate = 9600
    capabilities = {"TX", "RX", "CW", "SCAN"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_freq_khz: int = 904000
        self._last_power: int = 14

    def configure_radio(self, freq_hz, sf, bw_khz, cr_label, preamble, power):
        # AT+TCONF=freq_kHz:power:bw_kHz:sf:cr_label:lna:pa
        freq_khz = int(round(freq_hz / 1000))
        self._last_freq_khz = freq_khz
        self._last_power = power
        return self.send_at(f"AT+TCONF={freq_khz}:{power}:{bw_khz}:{sf}:{cr_label}:0:0")

    def tx_payload(self, payload, msg_text=None):
        # AT+TXLRA=freq_kHz:cont_mode(0/1):text
        text = msg_text if msg_text else payload.hex().upper()
        if len(text) > 64:
            text = text[:64]
        return self.send_at(f"AT+TXLRA={self._last_freq_khz}:0:{text}")

    def rx_continuous_start(self):
        # AT+RXLRA=freq_kHz:1 (contínuo)
        return self.send_at(f"AT+RXLRA={self._last_freq_khz}:1", wait=0.2)

    def rx_stop(self):
        return self.send_at("AT+TOFF", wait=0.2)

    def cw_start(self, freq_hz, power, sf, bw_khz):
        freq_khz = int(round(freq_hz / 1000))
        self._last_freq_khz = freq_khz
        self._last_power = power
        # Configura antes (TCONF) e dispara TXTONE
        self.send_at(f"AT+TCONF={freq_khz}:{power}:{bw_khz}:{sf}:4/5:0:0")
        return self.send_at(f"AT+TXTONE={freq_khz}", wait=0.2)

    def cw_stop(self):
        return self.send_at("AT+TOFF", wait=0.2)

    def parse_rx_line(self, line):
        """SMART: 'RSSI=-9 dBm SNR=6 dBm Rx Text-> Hello World'."""
        up = line.upper()
        rssi = snr = None
        payload = None

        if "RSSI=" in up:
            try:
                v = up.split("RSSI=", 1)[1].split()[0]
                v = "".join(c for c in v if (c.isdigit() or c in "+-."))
                rssi = float(v)
            except Exception:
                pass
        if "SNR=" in up:
            try:
                v = up.split("SNR=", 1)[1].split()[0]
                v = "".join(c for c in v if (c.isdigit() or c in "+-."))
                snr = float(v)
            except Exception:
                pass
        if "TEXT->" in up or "RX TEXT" in up:
            try:
                payload = line.split("->", 1)[1].strip()
            except Exception:
                pass
        return payload, rssi, snr


# Registro central de dispositivos
DEVICES: dict[str, type[LoRaDevice]] = {
    "RAK3172": RAK3172,
    "Quectel KG200Z": KG200Z,
    "SMART SMW-SX1262M0": SMARTSx1262,
}


# ===========================================================
#   RESULTADO E LOG DE SESSÃO
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


@dataclass
class SessionLogger:
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

        # Log de debug paralelo (toda comunicação AT raw)
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

    def debug_log(self, direction: str, payload: str, elapsed_ms: float | None):
        """Callback usado por LoRaDevice.send_at — registra todo I/O AT raw."""
        if not self.debug_file:
            return
        now = datetime.now()
        ts = now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"
        # Para RX, limita exibição inline; quebras de linha aparecem como literal
        safe = payload.replace("\r", "\\r").replace("\n", "\\n")
        line = f"[{ts}] {direction}{'>' if direction == 'TX' else '<'} {safe}"
        if elapsed_ms is not None:
            line += f"   ΔT={elapsed_ms:.0f}ms"
        with self.lock:
            try:
                self.debug_file.write(line + "\n")
            except Exception:
                pass

    def log(self, msg: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_queue.put(("log", line))
        with self.lock:
            if self.txt_file:
                try:
                    self.txt_file.write(line + "\n")
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


# ===========================================================
#   APLICAÇÃO
# ===========================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LoRa Multi-Device — Testes via UART")
        self.geometry("1280x900")
        self.minsize(1000, 700)

        self.queue: queue.Queue = queue.Queue()
        self.device: LoRaDevice | None = None
        self.is_connected = False

        self.logger = SessionLogger(log_queue=self.queue)

        # Estado
        self.seq_counter = 0
        self.rx_running = False
        self.scan_running = False
        self.cw_running = False
        self.incr_running = False
        # Gateway LoRaWAN
        self.gw_running = False
        self.gw_thread: threading.Thread | None = None
        self.devices_db: list[dict] = []  # lista de dicts {name, dev_eui, join_eui, app_key, session}
        self._next_dev_addr = 0x01000001  # contador simples para alocar DevAddr
        self._load_devices_json()

        self._build_ui()
        self._on_device_change()  # ajusta defaults
        self.after(100, self._process_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------
    #   UI
    # -----------------------------------------------------------
    def _build_ui(self):
        # === Barra superior: dispositivo + porta ===
        top = ttk.LabelFrame(self, text="Dispositivo e Conexão UART")
        top.pack(fill="x", padx=10, pady=5)

        ttk.Label(top, text="Dispositivo:").grid(row=0, column=0, padx=4, pady=4, sticky="e")
        self.cb_device = ttk.Combobox(top, width=24, values=list(DEVICES.keys()), state="readonly")
        self.cb_device.set("RAK3172")
        self.cb_device.grid(row=0, column=1, padx=4, pady=4)
        self.cb_device.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())

        ttk.Label(top, text="Porta:").grid(row=0, column=2, padx=4, pady=4, sticky="e")
        self.cb_port = ttk.Combobox(top, width=24, values=[])
        self.cb_port.grid(row=0, column=3, padx=4, pady=4)
        ttk.Button(top, text="Atualizar", command=self._refresh_ports).grid(row=0, column=4, padx=4, pady=4)

        ttk.Label(top, text="Baud:").grid(row=0, column=5, padx=4, pady=4, sticky="e")
        self.e_baud = ttk.Entry(top, width=10)
        self.e_baud.insert(0, "115200")
        self.e_baud.grid(row=0, column=6, padx=4, pady=4)

        self.b_connect = ttk.Button(top, text="Abrir UART", command=self._connect)
        self.b_connect.grid(row=0, column=7, padx=10, pady=4)
        self.b_test = ttk.Button(top, text="Testar dispositivo", command=self._test_device, state="disabled")
        self.b_test.grid(row=0, column=8, padx=4, pady=4)
        self.b_disconnect = ttk.Button(top, text="Desconectar", command=self._disconnect, state="disabled")
        self.b_disconnect.grid(row=0, column=9, padx=4, pady=4)

        # Status separado: porta UART e dispositivo
        sf_status = ttk.Frame(top)
        sf_status.grid(row=1, column=0, columnspan=10, padx=4, pady=2, sticky="w")
        ttk.Label(sf_status, text="Porta UART:").pack(side="left", padx=(0, 4))
        self.lbl_uart_status = ttk.Label(sf_status, text="fechada", foreground="red")
        self.lbl_uart_status.pack(side="left", padx=(0, 15))
        ttk.Label(sf_status, text="Dispositivo:").pack(side="left", padx=(0, 4))
        self.lbl_dev_status = ttk.Label(sf_status, text="não testado", foreground="gray")
        self.lbl_dev_status.pack(side="left", padx=(0, 15))

        self.lbl_caps = ttk.Label(top, text="", foreground="blue")
        self.lbl_caps.grid(row=2, column=0, columnspan=10, padx=4, pady=2, sticky="w")

        # === PanedWindow vertical: notebook em cima, log embaixo (divisor arrastável) ===
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=5)

        # --- Painel superior: Notebook (abas) ---
        nb_frame = ttk.Frame(paned)
        paned.add(nb_frame, weight=3)
        self.nb = ttk.Notebook(nb_frame)
        self.nb.pack(fill="both", expand=True)

        self._build_tab_tx()
        self._build_tab_rx()
        self._build_tab_incr()
        self._build_tab_scanner()
        self._build_tab_cw()
        self._build_tab_devices()
        self._build_tab_gateway()
        self._build_tab_console()

        # --- Painel inferior: Console + botões de log ---
        bottom = ttk.LabelFrame(paned, text="Log da Sessão (arraste a borda superior para redimensionar)")
        paned.add(bottom, weight=2)

        btnbar = ttk.Frame(bottom)
        btnbar.pack(fill="x", padx=4, pady=2)
        ttk.Button(btnbar, text="Limpar log", command=self._clear_log).pack(side="left", padx=2)
        ttk.Button(btnbar, text="Salvar CSV resultados…", command=self._save_csv).pack(side="left", padx=2)
        ttk.Button(btnbar, text="Abrir log debug", command=self._open_debug_log).pack(side="left", padx=2)
        ttk.Button(btnbar, text="Abrir pasta de logs", command=self._open_logs_folder).pack(side="left", padx=2)
        self.b_detach_log = ttk.Button(btnbar, text="Destacar log ⇗", command=self._toggle_detach_log)
        self.b_detach_log.pack(side="left", padx=2)
        self.lbl_logfile = ttk.Label(btnbar, text="Logs: (serão criados ao conectar)")
        self.lbl_logfile.pack(side="right", padx=4)

        # Text com scrollbar vertical
        log_container = ttk.Frame(bottom)
        log_container.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt_log = tk.Text(log_container, wrap="word", height=14)
        log_scroll = ttk.Scrollbar(log_container, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=log_scroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        # Lista de widgets de log ativos (inline + destacado se aberto)
        self._log_widgets: list[tk.Text] = [self.txt_log]
        self._log_window: tk.Toplevel | None = None

        self._refresh_ports()

    def _build_tab_tx(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="TX único")
        self.tab_tx = tab

        ttk.Label(tab, text="Mensagem (texto ou 0xHEX):").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.e_msg = ttk.Entry(tab, width=80)
        self.e_msg.insert(0, "0x0100000050840000000000000000FF11400216")
        self.e_msg.grid(row=0, column=1, columnspan=10, padx=4, pady=4, sticky="w")

        ttk.Label(tab, text="Timeout RX (s):").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.e_tx_timeout = ttk.Entry(tab, width=8)
        self.e_tx_timeout.insert(0, "5")
        self.e_tx_timeout.grid(row=1, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(tab, text="Reenvios:").grid(row=1, column=2, sticky="e", padx=4, pady=4)
        self.e_tx_retries = ttk.Entry(tab, width=8)
        self.e_tx_retries.insert(0, "2")
        self.e_tx_retries.grid(row=1, column=3, sticky="w", padx=4, pady=4)

        f_tx = ttk.LabelFrame(tab, text="Configuração TX")
        f_tx.grid(row=2, column=0, columnspan=8, padx=4, pady=4, sticky="ew")
        self.tx_entries = self._make_p2p_inputs(
            f_tx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=True
        )

        f_rx = ttk.LabelFrame(tab, text="Configuração RX")
        f_rx.grid(row=3, column=0, columnspan=8, padx=4, pady=4, sticky="ew")
        self.rx_entries = self._make_p2p_inputs(
            f_rx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=False
        )

        self.b_tx_send = ttk.Button(tab, text="Enviar pacote", command=self._send_tx_once, state="disabled")
        self.b_tx_send.grid(row=4, column=0, columnspan=2, padx=4, pady=8, sticky="w")

    def _build_tab_rx(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="RX contínuo")
        self.tab_rx = tab

        f_rx = ttk.LabelFrame(tab, text="Configuração RX")
        f_rx.pack(fill="x", padx=4, pady=4)
        self.rx_cont_entries = self._make_p2p_inputs(
            f_rx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=True
        )

        bf = ttk.Frame(tab)
        bf.pack(fill="x", padx=4, pady=4)
        self.b_rx_start = ttk.Button(bf, text="Iniciar RX contínuo", command=self._start_rx, state="disabled")
        self.b_rx_start.pack(side="left", padx=4)
        self.b_rx_stop = ttk.Button(bf, text="Parar RX", command=self._stop_rx, state="disabled")
        self.b_rx_stop.pack(side="left", padx=4)

    def _build_tab_incr(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Pacote incremental 16 bits")
        self.tab_incr = tab

        ttk.Label(tab, text="Envia palavras de 16 bits incrementais (0x0000, 0x0001, ...).").pack(anchor="w", padx=4, pady=4)

        cfg = ttk.Frame(tab)
        cfg.pack(fill="x", padx=4, pady=4)

        ttk.Label(cfg, text="Intervalo (s):").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.e_incr_interval = ttk.Entry(cfg, width=8)
        self.e_incr_interval.insert(0, "5")
        self.e_incr_interval.grid(row=0, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(cfg, text="Quantidade (0=infinito):").grid(row=0, column=2, sticky="e", padx=4, pady=2)
        self.e_incr_count = ttk.Entry(cfg, width=8)
        self.e_incr_count.insert(0, "0")
        self.e_incr_count.grid(row=0, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(cfg, text="Timeout RX (s):").grid(row=0, column=4, sticky="e", padx=4, pady=2)
        self.e_incr_timeout = ttk.Entry(cfg, width=8)
        self.e_incr_timeout.insert(0, "3")
        self.e_incr_timeout.grid(row=0, column=5, sticky="w", padx=4, pady=2)

        f_tx = ttk.LabelFrame(tab, text="Configuração TX")
        f_tx.pack(fill="x", padx=4, pady=4)
        self.incr_tx_entries = self._make_p2p_inputs(
            f_tx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=True
        )

        f_rx = ttk.LabelFrame(tab, text="Configuração RX")
        f_rx.pack(fill="x", padx=4, pady=4)
        self.incr_rx_entries = self._make_p2p_inputs(
            f_rx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=False
        )

        bf = ttk.Frame(tab)
        bf.pack(fill="x", padx=4, pady=4)
        self.b_incr_start = ttk.Button(bf, text="Iniciar incremental", command=self._start_incr, state="disabled")
        self.b_incr_start.pack(side="left", padx=4)
        self.b_incr_stop = ttk.Button(bf, text="Parar", command=self._stop_incr, state="disabled")
        self.b_incr_stop.pack(side="left", padx=4)

        self.lbl_incr_seq = ttk.Label(bf, text="Próximo seq: 0x0000")
        self.lbl_incr_seq.pack(side="left", padx=15)
        ttk.Button(bf, text="Resetar contador", command=self._reset_incr).pack(side="left", padx=4)

    def _build_tab_scanner(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Scanner (3 freq)")
        self.tab_scan = tab

        ttk.Label(tab, text="Cicla por 3 frequências, escutando dwell s em cada.").pack(anchor="w", padx=4, pady=4)

        cfg = ttk.Frame(tab)
        cfg.pack(fill="x", padx=4, pady=4)

        ttk.Label(cfg, text="Dwell (s):").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.e_scan_dwell = ttk.Entry(cfg, width=8); self.e_scan_dwell.insert(0, "5")
        self.e_scan_dwell.grid(row=0, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(cfg, text="SF:").grid(row=0, column=2, sticky="e", padx=4, pady=2)
        self.e_scan_sf = ttk.Entry(cfg, width=6); self.e_scan_sf.insert(0, "11")
        self.e_scan_sf.grid(row=0, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(cfg, text="BW (kHz):").grid(row=0, column=4, sticky="e", padx=4, pady=2)
        self.e_scan_bw = ttk.Entry(cfg, width=6); self.e_scan_bw.insert(0, "500")
        self.e_scan_bw.grid(row=0, column=5, sticky="w", padx=4, pady=2)

        ttk.Label(cfg, text="CR:").grid(row=0, column=6, sticky="e", padx=4, pady=2)
        self.e_scan_cr = ttk.Combobox(cfg, width=5, values=CR_LABELS, state="readonly"); self.e_scan_cr.set("4/5")
        self.e_scan_cr.grid(row=0, column=7, sticky="w", padx=4, pady=2)

        ttk.Label(cfg, text="Preamble:").grid(row=0, column=8, sticky="e", padx=4, pady=2)
        self.e_scan_pre = ttk.Entry(cfg, width=6); self.e_scan_pre.insert(0, "10")
        self.e_scan_pre.grid(row=0, column=9, sticky="w", padx=4, pady=2)

        ttk.Label(cfg, text="Power (dBm):").grid(row=0, column=10, sticky="e", padx=4, pady=2)
        self.e_scan_pwr = ttk.Entry(cfg, width=6); self.e_scan_pwr.insert(0, "14")
        self.e_scan_pwr.grid(row=0, column=11, sticky="w", padx=4, pady=2)

        f_freq = ttk.LabelFrame(tab, text="Frequências (MHz)")
        f_freq.pack(fill="x", padx=4, pady=4)
        ttk.Label(f_freq, text="Freq 1:").grid(row=0, column=0, padx=4, pady=2)
        self.e_scan_f1 = ttk.Entry(f_freq, width=10); self.e_scan_f1.insert(0, "903")
        self.e_scan_f1.grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(f_freq, text="Freq 2:").grid(row=0, column=2, padx=4, pady=2)
        self.e_scan_f2 = ttk.Entry(f_freq, width=10); self.e_scan_f2.insert(0, "904")
        self.e_scan_f2.grid(row=0, column=3, padx=4, pady=2)
        ttk.Label(f_freq, text="Freq 3:").grid(row=0, column=4, padx=4, pady=2)
        self.e_scan_f3 = ttk.Entry(f_freq, width=10); self.e_scan_f3.insert(0, "915")
        self.e_scan_f3.grid(row=0, column=5, padx=4, pady=2)

        bf = ttk.Frame(tab); bf.pack(fill="x", padx=4, pady=4)
        self.b_scan_start = ttk.Button(bf, text="Iniciar Scanner", command=self._start_scan, state="disabled")
        self.b_scan_start.pack(side="left", padx=4)
        self.b_scan_stop = ttk.Button(bf, text="Parar Scanner", command=self._stop_scan, state="disabled")
        self.b_scan_stop.pack(side="left", padx=4)

    def _build_tab_cw(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Portadora CW")
        self.tab_cw = tab

        ttk.Label(
            tab,
            text="Transmite portadora contínua (Continuous Wave) para teste de espectro / medida de potência.",
            justify="left",
        ).pack(anchor="w", padx=4, pady=4)

        cfg = ttk.LabelFrame(tab, text="Configuração da Portadora")
        cfg.pack(fill="x", padx=4, pady=4)

        ttk.Label(cfg, text="Frequência (MHz):").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.e_cw_freq = ttk.Entry(cfg, width=10); self.e_cw_freq.insert(0, "904")
        self.e_cw_freq.grid(row=0, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(cfg, text="Power (dBm):").grid(row=0, column=2, sticky="e", padx=4, pady=4)
        self.e_cw_pwr = ttk.Entry(cfg, width=8); self.e_cw_pwr.insert(0, "14")
        self.e_cw_pwr.grid(row=0, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(cfg, text="SF:").grid(row=0, column=4, sticky="e", padx=4, pady=4)
        self.e_cw_sf = ttk.Entry(cfg, width=6); self.e_cw_sf.insert(0, "11")
        self.e_cw_sf.grid(row=0, column=5, sticky="w", padx=4, pady=4)

        ttk.Label(cfg, text="BW (kHz):").grid(row=0, column=6, sticky="e", padx=4, pady=4)
        self.e_cw_bw = ttk.Entry(cfg, width=6); self.e_cw_bw.insert(0, "500")
        self.e_cw_bw.grid(row=0, column=7, sticky="w", padx=4, pady=4)

        bf = ttk.Frame(tab); bf.pack(fill="x", padx=4, pady=4)
        self.b_cw_start = ttk.Button(bf, text="Ligar portadora", command=self._start_cw, state="disabled")
        self.b_cw_start.pack(side="left", padx=4)
        self.b_cw_stop = ttk.Button(bf, text="Desligar portadora", command=self._stop_cw, state="disabled")
        self.b_cw_stop.pack(side="left", padx=4)
        self.lbl_cw_state = ttk.Label(bf, text="CW: desligada", foreground="gray")
        self.lbl_cw_state.pack(side="left", padx=15)

    def _build_tab_devices(self):
        """Aba para gerenciar cadastro de end-devices LoRaWAN (config fixa, separada do gateway)."""
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Dispositivos LoRaWAN")
        self.tab_devices = tab

        if not LORAWAN_AVAILABLE:
            msg = ("Módulo LoRaWAN não disponível. Instale a dependência 'cryptography':\n\n"
                   "    pip install cryptography")
            ttk.Label(tab, text=msg, justify="left", foreground="red").pack(padx=10, pady=10)
            return

        ttk.Label(
            tab,
            text=("Cadastro de end-devices LoRaWAN (OTAA). Esses dispositivos são reconhecidos pelo "
                  "Gateway na aba 'Gateway LoRaWAN'.\n"
                  "Persistência: LoRa_RAK_GUI/devices.json"),
            justify="left",
        ).pack(anchor="w", padx=8, pady=8)

        # --- Tabela ---
        f_dev = ttk.LabelFrame(tab, text="Dispositivos cadastrados")
        f_dev.pack(fill="both", expand=True, padx=8, pady=4)

        # Container interno usa GRID (Treeview + 2 scrollbars). f_dev usa pack para este container.
        tv_container = ttk.Frame(f_dev)
        tv_container.pack(fill="both", expand=True, padx=4, pady=4)

        cols = ("name", "dev_eui", "join_eui", "app_key", "dev_addr", "fcnt_up", "fcnt_dn", "status")
        self.tv_devices = ttk.Treeview(tv_container, columns=cols, show="headings", height=14)
        headings = {
            "name": ("Nome", 120),
            "dev_eui": ("DevEUI", 150),
            "join_eui": ("JoinEUI", 150),
            "app_key": ("AppKey", 270),
            "dev_addr": ("DevAddr", 90),
            "fcnt_up": ("FCntUp", 70),
            "fcnt_dn": ("FCntDn", 70),
            "status": ("Status", 110),
        }
        for c, (txt, w) in headings.items():
            self.tv_devices.heading(c, text=txt)
            self.tv_devices.column(c, width=w, anchor="w")

        scr_y = ttk.Scrollbar(tv_container, orient="vertical", command=self.tv_devices.yview)
        scr_x = ttk.Scrollbar(tv_container, orient="horizontal", command=self.tv_devices.xview)
        self.tv_devices.configure(yscrollcommand=scr_y.set, xscrollcommand=scr_x.set)
        self.tv_devices.grid(row=0, column=0, sticky="nsew")
        scr_y.grid(row=0, column=1, sticky="ns")
        scr_x.grid(row=1, column=0, sticky="ew")
        tv_container.grid_rowconfigure(0, weight=1)
        tv_container.grid_columnconfigure(0, weight=1)

        # Duplo-clique edita
        self.tv_devices.bind("<Double-1>", lambda _e: self._gw_edit_device())

        # --- Botões CRUD ---
        bf = ttk.Frame(tab); bf.pack(fill="x", padx=8, pady=8)
        ttk.Button(bf, text="Adicionar...", command=self._gw_add_device).pack(side="left", padx=2)
        ttk.Button(bf, text="Editar...", command=self._gw_edit_device).pack(side="left", padx=2)
        ttk.Button(bf, text="Remover", command=self._gw_remove_device).pack(side="left", padx=2)
        ttk.Button(bf, text="Limpar sessão", command=self._gw_clear_session).pack(side="left", padx=2)
        ttk.Separator(bf, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bf, text="Salvar JSON", command=self._save_devices_json).pack(side="left", padx=2)
        ttk.Button(bf, text="Recarregar JSON", command=self._reload_devices_json).pack(side="left", padx=2)

        ttk.Button(
            bf, text="← Ir para Gateway LoRaWAN",
            command=lambda: self.nb.select(self.tab_gw),
        ).pack(side="right", padx=2)

        self._refresh_devices_tree()

    def _build_tab_gateway(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Gateway LoRaWAN")
        self.tab_gw = tab

        if not LORAWAN_AVAILABLE:
            msg = ("Módulo LoRaWAN não disponível. Instale a dependência 'cryptography':\n\n"
                   "    pip install cryptography\n\n"
                   f"Erro: {_LORAWAN_IMPORT_ERROR}")
            ttk.Label(tab, text=msg, justify="left", foreground="red").pack(padx=10, pady=10)
            return

        ttk.Label(
            tab,
            text=("Gateway LoRaWAN single-channel + Network Server simulado em Python.\n"
                  "O módulo conectado entra em P2P/RX cru; este código implementa Join + uplinks (OTAA)."),
            justify="left",
        ).pack(anchor="w", padx=4, pady=4)

        # --- Configuração da banda ---
        f_band = ttk.LabelFrame(tab, text="Banda LoRaWAN")
        f_band.pack(fill="x", padx=4, pady=4)

        ttk.Label(f_band, text="Banda:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.cb_gw_band = ttk.Combobox(f_band, width=28, values=list(BANDS.keys()), state="readonly")
        self.cb_gw_band.set("AU915 (Brasil sub-band 8)")
        self.cb_gw_band.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        self.cb_gw_band.bind("<<ComboboxSelected>>", lambda _e: self._apply_band_defaults())

        ttk.Label(f_band, text="Freq Join (MHz):").grid(row=0, column=2, sticky="e", padx=4, pady=2)
        self.e_gw_freq = ttk.Entry(f_band, width=10); self.e_gw_freq.insert(0, "916.8")
        self.e_gw_freq.grid(row=0, column=3, sticky="w", padx=4, pady=2)

        ttk.Label(f_band, text="SF:").grid(row=0, column=4, sticky="e", padx=4, pady=2)
        self.e_gw_sf = ttk.Entry(f_band, width=5); self.e_gw_sf.insert(0, "10")
        self.e_gw_sf.grid(row=0, column=5, sticky="w", padx=4, pady=2)

        ttk.Label(f_band, text="BW (kHz):").grid(row=0, column=6, sticky="e", padx=4, pady=2)
        self.e_gw_bw = ttk.Entry(f_band, width=6); self.e_gw_bw.insert(0, "500")
        self.e_gw_bw.grid(row=0, column=7, sticky="w", padx=4, pady=2)

        ttk.Label(f_band, text="Power TX (dBm):").grid(row=0, column=8, sticky="e", padx=4, pady=2)
        self.e_gw_pwr = ttk.Entry(f_band, width=5); self.e_gw_pwr.insert(0, "14")
        self.e_gw_pwr.grid(row=0, column=9, sticky="w", padx=4, pady=2)

        ttk.Label(f_band, text="RX Delay (s):").grid(row=0, column=10, sticky="e", padx=4, pady=2)
        self.e_gw_rxdelay = ttk.Entry(f_band, width=5); self.e_gw_rxdelay.insert(0, "5")
        self.e_gw_rxdelay.grid(row=0, column=11, sticky="w", padx=4, pady=2)

        self._apply_band_defaults()

        # --- Resumo de devices cadastrados (gestão fica na aba 'Dispositivos LoRaWAN') ---
        f_summary = ttk.LabelFrame(tab, text="End-Devices cadastrados")
        f_summary.pack(fill="x", padx=4, pady=4)
        self.lbl_devices_summary = ttk.Label(f_summary, text="(0 dispositivos)", foreground="gray")
        self.lbl_devices_summary.pack(side="left", padx=8, pady=6)
        ttk.Button(
            f_summary, text="Abrir aba 'Dispositivos LoRaWAN' →",
            command=lambda: self.nb.select(self.tab_devices),
        ).pack(side="right", padx=8, pady=4)

        # --- Controle do gateway ---
        bf = ttk.Frame(tab); bf.pack(fill="x", padx=4, pady=4)
        self.b_gw_start = ttk.Button(bf, text="Iniciar Gateway", command=self._start_gateway, state="disabled")
        self.b_gw_start.pack(side="left", padx=4)
        self.b_gw_stop = ttk.Button(bf, text="Parar Gateway", command=self._stop_gateway, state="disabled")
        self.b_gw_stop.pack(side="left", padx=4)
        self.lbl_gw_state = ttk.Label(bf, text="Gateway: parado", foreground="gray")
        self.lbl_gw_state.pack(side="left", padx=15)

        # --- Console de eventos LoRaWAN ---
        f_evt = ttk.LabelFrame(tab, text="Eventos LoRaWAN (frames decodificados)")
        f_evt.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt_gw_console = tk.Text(f_evt, wrap="word", height=12)
        self.txt_gw_console.pack(fill="both", expand=True, padx=4, pady=4)

    def _apply_band_defaults(self):
        band = self.cb_gw_band.get()
        cfg = BANDS.get(band)
        if not cfg: return
        for entry, val in [
            (self.e_gw_freq, str(cfg["join_freq_mhz"])),
            (self.e_gw_sf, str(cfg["default_sf"])),
            (self.e_gw_bw, str(cfg["default_bw_khz"])),
        ]:
            entry.delete(0, "end"); entry.insert(0, val)

    def _build_tab_console(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="Console AT")
        self.tab_console = tab

        ttk.Label(
            tab,
            text=("Envia comandos AT manualmente ao dispositivo conectado.\n"
                  "Útil para configurações específicas que não estão nas outras abas."),
            justify="left",
        ).pack(anchor="w", padx=4, pady=4)

        # Linha de envio
        sf = ttk.Frame(tab); sf.pack(fill="x", padx=4, pady=4)
        ttk.Label(sf, text="Comando AT:").pack(side="left", padx=4)
        self.e_at_cmd = ttk.Entry(sf, width=70)
        self.e_at_cmd.pack(side="left", fill="x", expand=True, padx=4)
        self.e_at_cmd.bind("<Return>", lambda _e: self._send_at_manual())
        self.e_at_cmd.bind("<Up>", lambda _e: self._at_history(-1))
        self.e_at_cmd.bind("<Down>", lambda _e: self._at_history(+1))

        ttk.Label(sf, text="Espera (s):").pack(side="left", padx=4)
        self.e_at_wait = ttk.Entry(sf, width=6); self.e_at_wait.insert(0, "0.4")
        self.e_at_wait.pack(side="left", padx=4)

        self.b_at_send = ttk.Button(sf, text="Enviar", command=self._send_at_manual, state="disabled")
        self.b_at_send.pack(side="left", padx=4)

        # Sugestões rápidas
        sugg = ttk.LabelFrame(tab, text="Comandos rápidos (clique para preencher)")
        sugg.pack(fill="x", padx=4, pady=4)
        self.frame_quick = ttk.Frame(sugg)
        self.frame_quick.pack(fill="x", padx=4, pady=4)

        # Histórico
        hist = ttk.LabelFrame(tab, text="Resposta do dispositivo (última)")
        hist.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt_at_resp = tk.Text(hist, wrap="word", height=10)
        self.txt_at_resp.pack(fill="both", expand=True, padx=4, pady=4)

        self._at_hist: list[str] = []
        self._at_hist_idx = 0

    def _make_p2p_inputs(self, parent, defaults, with_power: bool) -> dict:
        """Cria entradas: Freq (MHz), SF, BW (kHz), CR (combo 4/5..4/8), Preamble, [Power]."""
        entries: dict = {}
        ttk.Label(parent, text="Freq (MHz):").grid(row=0, column=0, sticky="e", padx=3, pady=4)
        e = ttk.Entry(parent, width=10); e.insert(0, defaults[0])
        e.grid(row=0, column=1, sticky="w", padx=3, pady=4); entries["freq"] = e

        ttk.Label(parent, text="SF:").grid(row=0, column=2, sticky="e", padx=3, pady=4)
        e = ttk.Entry(parent, width=5); e.insert(0, defaults[1])
        e.grid(row=0, column=3, sticky="w", padx=3, pady=4); entries["sf"] = e

        ttk.Label(parent, text="BW (kHz):").grid(row=0, column=4, sticky="e", padx=3, pady=4)
        e = ttk.Entry(parent, width=6); e.insert(0, defaults[2])
        e.grid(row=0, column=5, sticky="w", padx=3, pady=4); entries["bw"] = e

        ttk.Label(parent, text="CR:").grid(row=0, column=6, sticky="e", padx=3, pady=4)
        cb = ttk.Combobox(parent, width=5, values=CR_LABELS, state="readonly"); cb.set(defaults[3])
        cb.grid(row=0, column=7, sticky="w", padx=3, pady=4); entries["cr"] = cb

        ttk.Label(parent, text="Preamble:").grid(row=0, column=8, sticky="e", padx=3, pady=4)
        e = ttk.Entry(parent, width=6); e.insert(0, defaults[4])
        e.grid(row=0, column=9, sticky="w", padx=3, pady=4); entries["preamble"] = e

        if with_power:
            ttk.Label(parent, text="Power (dBm):").grid(row=0, column=10, sticky="e", padx=3, pady=4)
            e = ttk.Entry(parent, width=6); e.insert(0, defaults[5])
            e.grid(row=0, column=11, sticky="w", padx=3, pady=4); entries["power"] = e
        return entries

    # -----------------------------------------------------------
    #   Helpers gerais
    # -----------------------------------------------------------
    def _refresh_ports(self):
        ports = []
        for p in list_ports.comports():
            label = f"{p.device} - {p.description}" if p.description else p.device
            ports.append(label)
        self.cb_port["values"] = ports
        if ports and not self.cb_port.get():
            self.cb_port.set(ports[0])

    def _selected_port(self) -> str:
        raw = self.cb_port.get().strip()
        return raw.split(" - ")[0] if " - " in raw else raw

    def _selected_device_class(self) -> type[LoRaDevice]:
        return DEVICES[self.cb_device.get()]

    def _read_p2p_inputs(self, entries: dict, default_power: int | None = None) -> dict:
        cr_label = entries["cr"].get().strip()
        if cr_label not in CR_LABELS:
            raise ValueError(f"CR inválido: {cr_label}")
        cfg = dict(
            freq=mhz_to_hz(entries["freq"].get()),
            sf=int(entries["sf"].get().strip()),
            bw=int(entries["bw"].get().strip()),
            cr_label=cr_label,
            preamble=int(entries["preamble"].get().strip()),
        )
        if "power" in entries:
            cfg["power"] = int(entries["power"].get().strip())
        elif default_power is not None:
            cfg["power"] = default_power
        return cfg

    def _process_queue(self):
        try:
            while True:
                kind, val = self.queue.get_nowait()
                if kind == "log":
                    # Replica em todos os widgets de log (inline + janela destacada se houver)
                    for w in self._log_widgets:
                        try:
                            w.insert("end", val + "\n")
                            w.see("end")
                        except tk.TclError:
                            pass  # widget destruído
                elif kind == "gw_log":
                    if hasattr(self, "txt_gw_console"):
                        self.txt_gw_console.insert("end", val + "\n")
                        self.txt_gw_console.see("end")
                elif kind == "at_resp":
                    self.txt_at_resp.delete("1.0", "end")
                    self.txt_at_resp.insert("end", val)
                elif kind == "ui":
                    val()
        except queue.Empty:
            pass
        self.after(100, self._process_queue)

    def _ui(self, fn):
        self.queue.put(("ui", fn))

    def _clear_log(self):
        for w in self._log_widgets:
            try:
                w.delete("1.0", "end")
            except tk.TclError:
                pass

    def _toggle_detach_log(self):
        """Abre ou fecha uma janela flutuante espelhando o log (pode ir para outro monitor)."""
        if self._log_window is not None:
            # Já destacado → reanexa (fecha janela)
            self._log_window.destroy()
            return  # WM_DELETE_WINDOW callback cuida do resto

        # Cria janela destacada
        win = tk.Toplevel(self)
        win.title("Log da Sessão — janela destacada")
        win.geometry("900x600")

        # Text widget + scrollbar
        container = ttk.Frame(win)
        container.pack(side="bottom", fill="both", expand=True, padx=4, pady=4)
        txt = tk.Text(container, wrap="word")
        scr = ttk.Scrollbar(container, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scr.set)
        txt.pack(side="left", fill="both", expand=True)
        scr.pack(side="right", fill="y")

        # Copia conteúdo atual do log inline
        current = self.txt_log.get("1.0", "end-1c")
        if current:
            txt.insert("1.0", current)
            txt.see("end")

        # Registra na lista de widgets que recebem log em tempo real
        self._log_widgets.append(txt)
        self._log_window = win
        self.b_detach_log.config(text="Reanexar log ⇙")

        # Callback de fechamento (compartilhado por X e botão Reanexar)
        def on_close():
            try:
                self._log_widgets.remove(txt)
            except ValueError:
                pass
            self._log_window = None
            try:
                self.b_detach_log.config(text="Destacar log ⇗")
            except tk.TclError:
                pass
            try:
                win.destroy()
            except tk.TclError:
                pass
        win.protocol("WM_DELETE_WINDOW", on_close)

        # Barra superior com ações (após on_close estar definido)
        bar = ttk.Frame(win)
        bar.pack(side="top", fill="x", padx=4, pady=2)
        ttk.Button(bar, text="Limpar", command=self._clear_log).pack(side="left", padx=2)
        ttk.Button(bar, text="Reanexar", command=on_close).pack(side="left", padx=2)
        ttk.Label(bar, text="(arraste para outro monitor se desejar)").pack(side="right", padx=4)

    def _open_logs_folder(self):
        os.makedirs(LOGS_DIR, exist_ok=True)
        try:
            os.startfile(LOGS_DIR)
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível abrir a pasta: {e}")

    def _open_debug_log(self):
        path = self.logger.debug_path
        if not path or not os.path.exists(path):
            messagebox.showinfo("Info", "Nenhum log de debug ainda. Abra a porta UART primeiro.")
            return
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível abrir o log: {e}")

    # -----------------------------------------------------------
    #   Mudança de dispositivo
    # -----------------------------------------------------------
    def _on_device_change(self):
        if self.is_connected:
            messagebox.showwarning("Aviso", "Desconecte antes de trocar de dispositivo.")
            self.cb_device.set(self.device.name if self.device else "RAK3172")
            return
        cls = self._selected_device_class()
        # baudrate padrão
        self.e_baud.delete(0, "end")
        self.e_baud.insert(0, str(cls.default_baudrate))
        # capabilities
        caps = ", ".join(sorted(cls.capabilities)) if cls.capabilities else "(nenhuma)"
        self.lbl_caps.config(text=f"Capabilities {cls.name}: {caps}")
        # habilita/desabilita abas
        self._update_tab_states(cls.capabilities)
        # comandos rápidos
        self._populate_quick_commands(cls)

    def _update_tab_states(self, caps: set[str]):
        # Lista (aba, capability necessária)
        mapping = [
            (self.tab_tx, "TX"),
            (self.tab_rx, "RX"),
            (self.tab_incr, "INCREMENTAL"),
            (self.tab_scan, "SCAN"),
            (self.tab_cw, "CW"),
        ]
        for tab, cap in mapping:
            try:
                state = "normal" if cap in caps else "disabled"
                self.nb.tab(tab, state=state)
            except Exception:
                pass

    def _populate_quick_commands(self, cls: type[LoRaDevice]):
        for w in self.frame_quick.winfo_children():
            w.destroy()
        suggestions = {
            "RAK3172": ["AT", "AT+VER=?", "AT+NWM=?", "AT+P2P=?", "AT+PRECV=0"],
            "Quectel KG200Z": ["ATQ", "AT+QVER=?", "AT+QP2P=?", "AT+QTCONF=?", "AT+QTOFF"],
            "SMART SMW-SX1262M0": ["AT", "AT+VER=?", "AT+ID=?", "AT+TCONF=?", "AT+TOFF"],
        }
        for cmd in suggestions.get(cls.name, ["AT"]):
            b = ttk.Button(self.frame_quick, text=cmd, width=18,
                           command=lambda c=cmd: self._set_at_cmd(c))
            b.pack(side="left", padx=2, pady=2)

    def _set_at_cmd(self, cmd: str):
        self.e_at_cmd.delete(0, "end")
        self.e_at_cmd.insert(0, cmd)

    # -----------------------------------------------------------
    #   Conexão
    # -----------------------------------------------------------
    def _connect(self):
        """Apenas abre a porta UART. NÃO testa comunicação com o dispositivo."""
        if self.is_connected:
            return
        port = self._selected_port()
        if not port:
            messagebox.showerror("Erro", "Selecione uma porta UART.")
            return
        try:
            baud = int(self.e_baud.get().strip())
        except ValueError:
            messagebox.showerror("Erro", "Baudrate inválido.")
            return

        cls = self._selected_device_class()
        try:
            self.device = cls(port, baud)
            self.device.open()
            self.logger.open_txt(device_name=cls.name)
            # Pluga o callback de debug para gravar todo I/O AT raw
            self.device.debug_cb = self.logger.debug_log
            self.lbl_logfile.config(
                text=f"Logs: {os.path.basename(self.logger.txt_path)}  +  "
                     f"{os.path.basename(self.logger.debug_path)}"
            )
            self.is_connected = True
            self.lbl_uart_status.config(
                text=f"aberta ({port} @ {baud} bps)", foreground="blue")
            self.lbl_dev_status.config(text="não testado", foreground="gray")
            self.b_connect.config(state="disabled")
            self.b_test.config(state="normal")
            self.b_disconnect.config(state="normal")
            self.cb_device.config(state="disabled")
            self._toggle_action_buttons(True)
            self.logger.log(f"[INFO] Porta UART aberta: {port} @ {baud} bps. Driver: {cls.name}.")
            self.logger.log(
                f"[INFO] Use 'Testar dispositivo' para verificar comunicação "
                f"(comando: {cls.ping_cmd})."
            )
        except Exception as e:
            if self.device:
                try: self.device.close()
                except Exception: pass
                self.device = None
            self.logger.log(f"[ERRO] Falha ao abrir porta UART: {e}")
            messagebox.showerror("Erro", f"Falha ao abrir porta UART: {e}")

    def _test_device(self):
        """Envia o comando de ping específico do dispositivo e verifica resposta."""
        if not self.is_connected or not self.device:
            messagebox.showwarning("Aviso", "Abra a porta UART primeiro.")
            return
        if self.rx_running or self.scan_running or self.incr_running or self.cw_running:
            messagebox.showwarning("Aviso", "Pare a operação em andamento antes de testar.")
            return

        def worker():
            cmd = self.device.ping_cmd
            self.logger.log(f">>> [Teste dispositivo] Enviando: {cmd}")
            ok, rsp = self.device.ping(wait=1.0)
            rsp_clean = rsp.strip() if rsp else "(sem resposta)"
            self.logger.log(f"<<< {rsp_clean}")
            if ok:
                self._ui(lambda: self.lbl_dev_status.config(
                    text=f"OK (respondeu a {cmd})", foreground="green"))
                self.logger.log(f"[INFO] Dispositivo {self.device.name} respondeu OK.")
            else:
                self._ui(lambda: self.lbl_dev_status.config(
                    text=f"sem resposta a {cmd}", foreground="red"))
                self.logger.log(
                    f"[AVISO] Dispositivo não respondeu OK ao comando '{cmd}'. "
                    f"Verifique baudrate, fiação UART (TX/RX cruzados) e alimentação."
                )

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect(self):
        self.rx_running = False
        self.scan_running = False
        self.incr_running = False
        self.gw_running = False
        if self.cw_running:
            try:
                if self.device: self.device.cw_stop()
            except Exception: pass
            self.cw_running = False
        time.sleep(0.3)

        if self.device:
            try: self.device.close()
            except Exception: pass
            self.device = None

        self.is_connected = False
        self.lbl_uart_status.config(text="fechada", foreground="red")
        self.lbl_dev_status.config(text="não testado", foreground="gray")
        self.b_connect.config(state="normal")
        self.b_test.config(state="disabled")
        self.b_disconnect.config(state="disabled")
        self.cb_device.config(state="readonly")
        self._toggle_action_buttons(False)
        self.logger.log("[INFO] Desconectado.")
        self.logger.close_txt()
        self.lbl_logfile.config(text="Log TXT: (será criado ao conectar)")

    def _toggle_action_buttons(self, enable: bool):
        caps = self.device.capabilities if self.device else set()
        def st(cap):
            return "normal" if (enable and cap in caps) else "disabled"
        self.b_tx_send.config(state=st("TX"))
        self.b_rx_start.config(state=st("RX"))
        self.b_incr_start.config(state=st("INCREMENTAL"))
        self.b_scan_start.config(state=st("SCAN"))
        self.b_cw_start.config(state=st("CW"))
        self.b_at_send.config(state="normal" if enable else "disabled")
        # Gateway depende só de RX (todos os 3 chips suportam)
        if hasattr(self, "b_gw_start"):
            self.b_gw_start.config(state="normal" if (enable and "RX" in caps) else "disabled")
        if not enable:
            self.b_rx_stop.config(state="disabled")
            self.b_incr_stop.config(state="disabled")
            self.b_scan_stop.config(state="disabled")
            self.b_cw_stop.config(state="disabled")
            if hasattr(self, "b_gw_stop"):
                self.b_gw_stop.config(state="disabled")

    def _check_busy(self) -> bool:
        if (self.rx_running or self.scan_running or self.incr_running
                or self.cw_running or self.gw_running):
            messagebox.showwarning("Aviso", "Pare a operação em andamento antes de iniciar outra.")
            return True
        return False

    # -----------------------------------------------------------
    #   TX único
    # -----------------------------------------------------------
    def _send_tx_once(self):
        if not self.is_connected or not self.device: return
        if self._check_busy(): return
        msg = self.e_msg.get().strip()
        try:
            payload, msg_text = self._encode_payload(msg) if msg else (
                self.seq_counter.to_bytes(2, "big"), None
            )
        except ValueError as e:
            messagebox.showerror("Erro", str(e)); return

        try:
            timeout = float(self.e_tx_timeout.get().strip())
            retries = int(self.e_tx_retries.get().strip())
            cfg_tx = self._read_p2p_inputs(self.tx_entries)
            cfg_rx = self._read_p2p_inputs(self.rx_entries, default_power=cfg_tx["power"])
        except ValueError as e:
            messagebox.showerror("Erro", f"Parâmetro inválido: {e}"); return

        seq = self.seq_counter
        self.seq_counter += 1
        t = threading.Thread(
            target=self._tx_worker,
            args=(seq, payload, msg_text, timeout, retries, cfg_tx, cfg_rx),
            daemon=True,
        )
        t.start()

    @staticmethod
    def _encode_payload(msg: str) -> tuple[bytes, str | None]:
        """Retorna (bytes, texto_original_ou_None)."""
        if msg.lower().startswith("0x"):
            try:
                return bytes.fromhex(msg[2:].strip()), None
            except ValueError:
                raise ValueError("Mensagem HEX inválida.")
        return msg.encode("utf-8"), msg

    def _tx_worker(self, seq, payload, msg_text, timeout, retries, cfg_tx, cfg_rx):
        payload_hex = payload.hex().upper()
        self.logger.log(f"--- TX seq={seq} HEX={payload_hex} ({self.device.name}) ---")
        attempts = 0
        rx_payload = None
        rssi = snr = rtt_ms = None

        try:
            self.device.flush_rx()
            for attempt in range(retries + 1):
                attempts += 1
                tag = "primeiro envio" if attempts == 1 else f"REENVIO #{attempts - 1}"

                self.logger.log(f"  [{tag}] Config TX: {cfg_tx}")
                self.device.configure_radio(
                    cfg_tx["freq"], cfg_tx["sf"], cfg_tx["bw"], cfg_tx["cr_label"],
                    cfg_tx["preamble"], cfg_tx["power"],
                )

                t0 = time.time()
                self.logger.log(f"  [{tag}] Transmitindo payload")
                self.device.tx_payload(payload, msg_text=msg_text)

                self.logger.log(f"  [{tag}] Config RX: {cfg_rx}")
                self.device.configure_radio(
                    cfg_rx["freq"], cfg_rx["sf"], cfg_rx["bw"], cfg_rx["cr_label"],
                    cfg_rx["preamble"], cfg_tx["power"],
                )
                # tenta abrir RX com timeout (RAK tem método dedicado)
                if hasattr(self.device, "precv_timeout"):
                    self.device.precv_timeout(int(timeout * 1000))
                else:
                    try: self.device.rx_continuous_start()
                    except Exception: pass

                got = False
                for line in self.device.read_until(timeout):
                    self.logger.log(f"  RX>> {line}")
                    ph, rxr, rxs = self.device.parse_rx_line(line)
                    if rxr is not None: rssi = rxr
                    if rxs is not None: snr = rxs
                    if ph:
                        rx_payload = ph
                        rtt_ms = (time.time() - t0) * 1000.0
                        got = True
                        break

                # parar RX se necessário
                try: self.device.rx_stop()
                except Exception: pass

                if got: break
                self.logger.log(f"  Timeout em [{tag}].")
        except Exception as e:
            self.logger.log(f"[ERRO TX] {e}")

        # match: comparação tolerante (HEX direto ou repr ASCII)
        match = False
        if rx_payload:
            up = rx_payload.upper().replace(":", "").strip()
            match = (up == payload_hex) or (rx_payload.strip() == (msg_text or ""))

        self._record_result(
            mode="TX", seq=seq,
            payload_tx=msg_text or payload_hex,
            payload_rx=rx_payload or "",
            match=int(match),
            attempts=attempts, timeout_s=timeout, retries=retries,
            rssi=rssi, snr=snr, rtt_ms=rtt_ms,
            freq_tx_hz=cfg_tx.get("freq"), freq_rx_hz=cfg_rx.get("freq"),
        )
        self.logger.log(
            f"<< Resultado TX seq={seq}: match={match} rx={rx_payload} "
            f"rssi={rssi} snr={snr} rtt={rtt_ms} tentativas={attempts}"
        )
        self.logger.log("-" * 60)

    # -----------------------------------------------------------
    #   RX contínuo
    # -----------------------------------------------------------
    def _start_rx(self):
        if not self.is_connected or not self.device: return
        if self._check_busy(): return
        try:
            cfg = self._read_p2p_inputs(self.rx_cont_entries)
        except ValueError as e:
            messagebox.showerror("Erro", f"Parâmetro inválido: {e}"); return

        self.rx_running = True
        self.b_rx_start.config(state="disabled")
        self.b_rx_stop.config(state="normal")
        threading.Thread(target=self._rx_worker, args=(cfg,), daemon=True).start()

    def _stop_rx(self):
        if self.rx_running:
            self.rx_running = False
            self.logger.log("[INFO] Solicitando parada do RX contínuo...")

    def _rx_worker(self, cfg):
        self.logger.log(f"== RX contínuo iniciado cfg={cfg} ==")
        try:
            self.device.flush_rx()
            self.device.configure_radio(
                cfg["freq"], cfg["sf"], cfg["bw"], cfg["cr_label"],
                cfg["preamble"], cfg["power"],
            )
            self.device.rx_continuous_start()
            while self.rx_running:
                for line in self.device.read_until(1.0):
                    if not self.rx_running: break
                    self.logger.log(f"RX>> {line}")
                    ph, rssi, snr = self.device.parse_rx_line(line)
                    if ph:
                        self._record_result(
                            mode="RX", seq=-1,
                            payload_tx="", payload_rx=ph,
                            match=0, attempts=1, timeout_s=0.0, retries=0,
                            rssi=rssi, snr=snr, rtt_ms=None,
                            freq_tx_hz=None, freq_rx_hz=cfg.get("freq"),
                        )
                        self.logger.log(f"<< Pacote: {ph} RSSI={rssi} SNR={snr}")
                        self.logger.log("-" * 60)
            try: self.device.rx_stop()
            except Exception: pass
        except Exception as e:
            self.logger.log(f"[ERRO RX] {e}")
        finally:
            self.rx_running = False
            self._ui(lambda: self.b_rx_start.config(state="normal"))
            self._ui(lambda: self.b_rx_stop.config(state="disabled"))
            self.logger.log("== RX contínuo encerrado ==")

    # -----------------------------------------------------------
    #   Pacote incremental
    # -----------------------------------------------------------
    def _reset_incr(self):
        self.seq_counter = 0
        self._update_incr_label()

    def _update_incr_label(self):
        self.lbl_incr_seq.config(text=f"Próximo seq: 0x{self.seq_counter & 0xFFFF:04X}")

    def _start_incr(self):
        if not self.is_connected or not self.device: return
        if self._check_busy(): return
        try:
            interval = float(self.e_incr_interval.get().strip())
            count = int(self.e_incr_count.get().strip())
            timeout = float(self.e_incr_timeout.get().strip())
            cfg_tx = self._read_p2p_inputs(self.incr_tx_entries)
            cfg_rx = self._read_p2p_inputs(self.incr_rx_entries, default_power=cfg_tx["power"])
        except ValueError as e:
            messagebox.showerror("Erro", f"Parâmetro inválido: {e}"); return

        self.incr_running = True
        self.b_incr_start.config(state="disabled")
        self.b_incr_stop.config(state="normal")
        threading.Thread(
            target=self._incr_worker, args=(interval, count, timeout, cfg_tx, cfg_rx), daemon=True
        ).start()

    def _stop_incr(self):
        if self.incr_running:
            self.incr_running = False
            self.logger.log("[INFO] Solicitando parada do envio incremental...")

    def _incr_worker(self, interval, count, timeout, cfg_tx, cfg_rx):
        self.logger.log(f"== Incremental iniciado: intervalo={interval}s qtd={count or '∞'} ==")
        sent = 0
        try:
            while self.incr_running and (count == 0 or sent < count):
                seq = self.seq_counter & 0xFFFF
                payload = seq.to_bytes(2, "big")
                self._tx_worker(seq, payload, None, timeout, 0, cfg_tx, cfg_rx)
                self.seq_counter = (self.seq_counter + 1) & 0xFFFF
                self._ui(self._update_incr_label)
                sent += 1
                t_end = time.time() + interval
                while time.time() < t_end and self.incr_running:
                    time.sleep(0.1)
        except Exception as e:
            self.logger.log(f"[ERRO INCR] {e}")
        finally:
            self.incr_running = False
            self._ui(lambda: self.b_incr_start.config(state="normal"))
            self._ui(lambda: self.b_incr_stop.config(state="disabled"))
            self.logger.log(f"== Incremental encerrado: {sent} pacote(s) enviado(s) ==")

    # -----------------------------------------------------------
    #   Scanner
    # -----------------------------------------------------------
    def _start_scan(self):
        if not self.is_connected or not self.device: return
        if self._check_busy(): return
        try:
            cr_label = self.e_scan_cr.get().strip()
            if cr_label not in CR_LABELS: raise ValueError(f"CR inválido: {cr_label}")
            dwell = float(self.e_scan_dwell.get().strip())
            base = dict(
                sf=int(self.e_scan_sf.get().strip()),
                bw=int(self.e_scan_bw.get().strip()),
                cr_label=cr_label,
                preamble=int(self.e_scan_pre.get().strip()),
                power=int(self.e_scan_pwr.get().strip()),
            )
            freqs = [
                mhz_to_hz(self.e_scan_f1.get()),
                mhz_to_hz(self.e_scan_f2.get()),
                mhz_to_hz(self.e_scan_f3.get()),
            ]
        except ValueError as e:
            messagebox.showerror("Erro", f"Parâmetro inválido: {e}"); return

        self.scan_running = True
        self.b_scan_start.config(state="disabled")
        self.b_scan_stop.config(state="normal")
        threading.Thread(target=self._scan_worker, args=(freqs, dwell, base), daemon=True).start()

    def _stop_scan(self):
        if self.scan_running:
            self.scan_running = False
            self.logger.log("[INFO] Solicitando parada do Scanner...")

    def _scan_worker(self, freqs, dwell, base):
        self.logger.log(f"== Scanner iniciado: freqs={[hz_to_mhz(f) for f in freqs]}MHz dwell={dwell}s ==")
        try:
            self.device.flush_rx()
            idx = 0
            while self.scan_running:
                freq = freqs[idx % len(freqs)]; idx += 1
                self.logger.log(f"--- Scan FREQ={hz_to_mhz(freq)} MHz ---")
                self.device.configure_radio(
                    freq, base["sf"], base["bw"], base["cr_label"],
                    base["preamble"], base["power"],
                )
                self.device.rx_continuous_start()
                t_end = time.time() + dwell
                while time.time() < t_end and self.scan_running:
                    for line in self.device.read_until(min(1.0, max(0.05, t_end - time.time()))):
                        if not self.scan_running: break
                        self.logger.log(f"  RX>> {line}")
                        ph, rssi, snr = self.device.parse_rx_line(line)
                        if ph:
                            self._record_result(
                                mode="SCAN", seq=-1,
                                payload_tx="", payload_rx=ph,
                                match=0, attempts=1, timeout_s=dwell, retries=0,
                                rssi=rssi, snr=snr, rtt_ms=None,
                                freq_tx_hz=None, freq_rx_hz=freq,
                            )
                            self.logger.log(f"  << SCAN freq={hz_to_mhz(freq)}MHz {ph} RSSI={rssi} SNR={snr}")
                try: self.device.rx_stop()
                except Exception: pass
        except Exception as e:
            self.logger.log(f"[ERRO SCAN] {e}")
        finally:
            self.scan_running = False
            self._ui(lambda: self.b_scan_start.config(state="normal"))
            self._ui(lambda: self.b_scan_stop.config(state="disabled"))
            self.logger.log("== Scanner encerrado ==")

    # -----------------------------------------------------------
    #   CW
    # -----------------------------------------------------------
    def _start_cw(self):
        if not self.is_connected or not self.device: return
        if self._check_busy(): return
        try:
            freq_hz = mhz_to_hz(self.e_cw_freq.get())
            power = int(self.e_cw_pwr.get().strip())
            sf = int(self.e_cw_sf.get().strip())
            bw = int(self.e_cw_bw.get().strip())
        except ValueError as e:
            messagebox.showerror("Erro", f"Parâmetro inválido: {e}"); return

        try:
            self.logger.log(f"== Ligando CW: freq={hz_to_mhz(freq_hz)} MHz power={power} dBm ({self.device.name}) ==")
            rsp = self.device.cw_start(freq_hz, power, sf, bw)
            self.logger.log(f"CW>> {rsp.strip()}")
            self.cw_running = True
            self.lbl_cw_state.config(
                text=f"CW: LIGADA ({hz_to_mhz(freq_hz)} MHz, {power} dBm)", foreground="red")
            self.b_cw_start.config(state="disabled")
            self.b_cw_stop.config(state="normal")
        except Exception as e:
            self.logger.log(f"[ERRO CW] {e}")
            messagebox.showerror("Erro", f"Falha ao ligar CW: {e}")

    def _stop_cw(self):
        if not self.device or not self.cw_running: return
        try:
            self.logger.log("== Desligando CW ==")
            rsp = self.device.cw_stop()
            self.logger.log(f"CW>> {rsp.strip()}")
        except Exception as e:
            self.logger.log(f"[ERRO CW STOP] {e}")
        finally:
            self.cw_running = False
            self.lbl_cw_state.config(text="CW: desligada", foreground="gray")
            self.b_cw_start.config(state="normal")
            self.b_cw_stop.config(state="disabled")

    # -----------------------------------------------------------
    #   Console AT manual
    # -----------------------------------------------------------
    def _send_at_manual(self):
        if not self.is_connected or not self.device:
            messagebox.showwarning("Aviso", "Conecte ao dispositivo primeiro.")
            return
        cmd = self.e_at_cmd.get().strip()
        if not cmd: return
        try:
            wait = float(self.e_at_wait.get().strip())
        except ValueError:
            messagebox.showerror("Erro", "Espera inválida."); return

        # adiciona ao histórico
        if not self._at_hist or self._at_hist[-1] != cmd:
            self._at_hist.append(cmd)
        self._at_hist_idx = len(self._at_hist)

        def worker():
            try:
                self.logger.log(f">>> [Manual AT] {cmd}")
                rsp = self.device.send_at(cmd, wait=wait)
                self.logger.log(f"<<< {rsp.strip()}")
                self.queue.put(("at_resp", f"Comando: {cmd}\n\n{rsp.strip()}"))
            except Exception as e:
                self.logger.log(f"[ERRO AT manual] {e}")
                self.queue.put(("at_resp", f"ERRO: {e}"))

        threading.Thread(target=worker, daemon=True).start()
        self.e_at_cmd.delete(0, "end")

    def _at_history(self, direction: int):
        if not self._at_hist: return "break"
        self._at_hist_idx = max(0, min(len(self._at_hist), self._at_hist_idx + direction))
        self.e_at_cmd.delete(0, "end")
        if self._at_hist_idx < len(self._at_hist):
            self.e_at_cmd.insert(0, self._at_hist[self._at_hist_idx])
        return "break"

    # -----------------------------------------------------------
    #   Gateway LoRaWAN — devices.json
    # -----------------------------------------------------------
    def _load_devices_json(self):
        if not os.path.exists(DEVICES_JSON_PATH):
            return
        try:
            with open(DEVICES_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.devices_db = data.get("devices", [])
        except Exception as e:
            print(f"[WARN] Falha ao carregar devices.json: {e}")
            self.devices_db = []

    def _save_devices_json(self):
        try:
            with open(DEVICES_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump({"devices": self.devices_db}, f, indent=2, ensure_ascii=False)
            self.logger.log(f"[GW] devices.json salvo: {DEVICES_JSON_PATH}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao salvar JSON: {e}")

    def _reload_devices_json(self):
        self._load_devices_json()
        self._refresh_devices_tree()
        self.logger.log(f"[GW] devices.json recarregado ({len(self.devices_db)} dispositivos)")

    def _refresh_devices_tree(self):
        if hasattr(self, "tv_devices"):
            for item in self.tv_devices.get_children():
                self.tv_devices.delete(item)
            for d in self.devices_db:
                sess = d.get("session") or {}
                self.tv_devices.insert("", "end", values=(
                    d.get("name", ""), d.get("dev_eui", ""), d.get("join_eui", ""),
                    d.get("app_key", ""), sess.get("dev_addr", ""),
                    sess.get("fcnt_up", ""), sess.get("fcnt_down", ""),
                    "joined" if sess else "não-joined",
                ))
        # Atualiza resumo na aba Gateway
        if hasattr(self, "lbl_devices_summary"):
            n = len(self.devices_db)
            joined = sum(1 for d in self.devices_db if d.get("session"))
            if n == 0:
                txt, fg = "(0 dispositivos cadastrados)", "red"
            else:
                txt = f"{n} dispositivo(s) cadastrado(s) — {joined} com sessão ativa"
                fg = "green" if joined else "blue"
            self.lbl_devices_summary.config(text=txt, foreground=fg)

    def _gw_add_device(self):
        self._gw_device_dialog(None)

    def _gw_edit_device(self):
        sel = self.tv_devices.selection()
        if not sel:
            messagebox.showinfo("Info", "Selecione um dispositivo na tabela."); return
        idx = self.tv_devices.index(sel[0])
        self._gw_device_dialog(idx)

    def _gw_remove_device(self):
        sel = self.tv_devices.selection()
        if not sel: return
        idx = self.tv_devices.index(sel[0])
        if not messagebox.askyesno("Remover", f"Remover {self.devices_db[idx].get('name', '?')}?"):
            return
        del self.devices_db[idx]
        self._refresh_devices_tree()

    def _gw_clear_session(self):
        sel = self.tv_devices.selection()
        if not sel: return
        idx = self.tv_devices.index(sel[0])
        self.devices_db[idx]["session"] = None
        self._refresh_devices_tree()
        self.logger.log(f"[GW] Sessão limpa para {self.devices_db[idx].get('name', '?')}")

    def _gw_device_dialog(self, idx: int | None):
        """Diálogo modal para adicionar/editar device."""
        dlg = tk.Toplevel(self); dlg.title("End-Device LoRaWAN"); dlg.transient(self); dlg.grab_set()
        ttk.Label(dlg, text="Nome:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        e_name = ttk.Entry(dlg, width=30); e_name.grid(row=0, column=1, padx=4, pady=4)
        ttk.Label(dlg, text="DevEUI (16 hex):").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        e_deui = ttk.Entry(dlg, width=30); e_deui.grid(row=1, column=1, padx=4, pady=4)
        ttk.Label(dlg, text="JoinEUI / AppEUI (16 hex):").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        e_jeui = ttk.Entry(dlg, width=30); e_jeui.grid(row=2, column=1, padx=4, pady=4)
        ttk.Label(dlg, text="AppKey (32 hex):").grid(row=3, column=0, sticky="e", padx=4, pady=4)
        e_akey = ttk.Entry(dlg, width=40); e_akey.grid(row=3, column=1, padx=4, pady=4)

        if idx is not None:
            d = self.devices_db[idx]
            e_name.insert(0, d.get("name", ""))
            e_deui.insert(0, d.get("dev_eui", ""))
            e_jeui.insert(0, d.get("join_eui", ""))
            e_akey.insert(0, d.get("app_key", ""))
        else:
            e_jeui.insert(0, "0000000000000000")

        def on_ok():
            try:
                # valida formato
                if len(hex_to_bytes(e_deui.get())) != 8: raise ValueError("DevEUI deve ter 8 bytes (16 hex)")
                if len(hex_to_bytes(e_jeui.get())) != 8: raise ValueError("JoinEUI deve ter 8 bytes (16 hex)")
                if len(hex_to_bytes(e_akey.get())) != 16: raise ValueError("AppKey deve ter 16 bytes (32 hex)")
            except Exception as e:
                messagebox.showerror("Erro", str(e)); return
            entry = {
                "name": e_name.get().strip() or "(sem nome)",
                "dev_eui": e_deui.get().strip().upper().replace(":", "").replace(" ", ""),
                "join_eui": e_jeui.get().strip().upper().replace(":", "").replace(" ", ""),
                "app_key": e_akey.get().strip().upper().replace(":", "").replace(" ", ""),
                "session": self.devices_db[idx].get("session") if idx is not None else None,
            }
            if idx is None:
                self.devices_db.append(entry)
            else:
                self.devices_db[idx] = entry
            self._refresh_devices_tree()
            dlg.destroy()

        ttk.Button(dlg, text="OK", command=on_ok).grid(row=4, column=0, padx=4, pady=8, sticky="e")
        ttk.Button(dlg, text="Cancelar", command=dlg.destroy).grid(row=4, column=1, padx=4, pady=8, sticky="w")

    # -----------------------------------------------------------
    #   Gateway LoRaWAN — worker
    # -----------------------------------------------------------
    def _gw_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.queue.put(("gw_log", line))
        self.logger.log(f"[GW] {msg}")

    def _start_gateway(self):
        if not self.is_connected or not self.device:
            messagebox.showwarning("Aviso", "Abra a UART primeiro."); return
        if self._check_busy():
            return
        if not self.devices_db:
            messagebox.showinfo("Info", "Cadastre pelo menos um end-device antes."); return
        try:
            freq_hz = mhz_to_hz(self.e_gw_freq.get())
            sf = int(self.e_gw_sf.get().strip())
            bw = int(self.e_gw_bw.get().strip())
            power = int(self.e_gw_pwr.get().strip())
            rx_delay = int(self.e_gw_rxdelay.get().strip())
        except ValueError as e:
            messagebox.showerror("Erro", f"Parâmetro inválido: {e}"); return

        band_cfg = BANDS.get(self.cb_gw_band.get(), {})
        net_id = band_cfg.get("net_id", 0)

        self.gw_running = True
        self.b_gw_start.config(state="disabled")
        self.b_gw_stop.config(state="normal")
        self.lbl_gw_state.config(text="Gateway: rodando", foreground="green")
        self.gw_thread = threading.Thread(
            target=self._gw_worker,
            args=(freq_hz, sf, bw, power, rx_delay, net_id),
            daemon=True,
        )
        self.gw_thread.start()

    def _stop_gateway(self):
        if self.gw_running:
            self.gw_running = False
            self._gw_log("solicitando parada...")

    def _gw_worker(self, freq_hz, sf, bw, power, rx_delay, net_id):
        """Loop principal do gateway: configura módulo em P2P/RX, escuta frames, processa."""
        self._gw_log(f"iniciado em {hz_to_mhz(freq_hz)} MHz SF{sf} BW{bw}kHz")
        try:
            self.device.flush_rx()
            # P2P "puro" — sem header LoRaWAN do firmware; recebemos bytes crus
            self.device.configure_radio(freq_hz, sf, bw, "4/5", 8, power)
            self.device.rx_continuous_start()

            while self.gw_running:
                for line in self.device.read_until(1.0):
                    if not self.gw_running: break
                    payload_hex, rssi, snr = self.device.parse_rx_line(line)
                    if not payload_hex:
                        continue
                    try:
                        raw = bytes.fromhex(payload_hex.replace(":", "").replace(" ", ""))
                    except ValueError:
                        self._gw_log(f"frame inválido (hex): {payload_hex}")
                        continue
                    if not raw:
                        continue
                    self._process_lorawan_frame(raw, rssi, snr, freq_hz, sf, bw, power, rx_delay, net_id)

            try: self.device.rx_stop()
            except Exception: pass
        except Exception as e:
            self._gw_log(f"erro: {e}")
        finally:
            self.gw_running = False
            self._ui(lambda: self.b_gw_start.config(state="normal"))
            self._ui(lambda: self.b_gw_stop.config(state="disabled"))
            self._ui(lambda: self.lbl_gw_state.config(text="Gateway: parado", foreground="gray"))
            self._gw_log("encerrado")

    def _process_lorawan_frame(self, raw, rssi, snr, freq_hz, sf, bw, power, rx_delay, net_id):
        mtype = parse_mhdr(raw[0])
        self._gw_log(f"frame recebido ({len(raw)} bytes, MType={mtype}, RSSI={rssi}, SNR={snr})")

        if mtype == MTYPE_JOIN_REQUEST:
            self._handle_join_request(raw, freq_hz, sf, bw, power, rx_delay, net_id)
        elif mtype in (MTYPE_UNCONF_DATA_UP, MTYPE_CONF_DATA_UP):
            self._handle_uplink(raw)
        else:
            self._gw_log(f"  ignorado (não é uplink/join). raw=0x{raw.hex().upper()}")

    def _handle_join_request(self, raw, freq_hz, sf, bw, power, rx_delay, net_id):
        try:
            jr = JoinRequest.parse(raw)
        except Exception as e:
            self._gw_log(f"  Join Request inválido: {e}"); return

        dev_eui_hex = bytes_to_hex(jr.dev_eui)
        join_eui_hex = bytes_to_hex(jr.join_eui)
        dev_nonce_hex = bytes_to_hex(jr.dev_nonce)
        self._gw_log(f"  JOIN REQUEST: DevEUI={dev_eui_hex} JoinEUI={join_eui_hex} "
                     f"DevNonce={dev_nonce_hex}")

        # Procura device cadastrado
        dev = self._find_device(dev_eui_hex)
        if not dev:
            self._gw_log(f"  REJEITADO: DevEUI {dev_eui_hex} não cadastrado")
            return
        try:
            app_key = hex_to_bytes(dev["app_key"])
        except Exception as e:
            self._gw_log(f"  AppKey inválida: {e}"); return

        if not jr.validate_mic(app_key):
            self._gw_log(f"  REJEITADO: MIC inválido para {dev['name']}")
            return

        self._gw_log(f"  MIC OK. Aceitando Join de '{dev['name']}'")

        # Aloca DevAddr e gera AppNonce
        dev_addr = self._next_dev_addr & 0xFFFFFFFF
        self._next_dev_addr += 1
        app_nonce = random_bytes(3)

        # Deriva chaves
        nwk_skey, app_skey = derive_session_keys(app_key, app_nonce, net_id, jr.dev_nonce[::-1])
        # ^ note: dev_nonce armazenado em BE; spec usa LE para derivação

        # Constrói Join Accept
        try:
            ja = build_join_accept(
                app_key=app_key,
                app_nonce=app_nonce,
                net_id=net_id,
                dev_addr=dev_addr,
                dl_settings=0,
                rx_delay=rx_delay,
            )
        except Exception as e:
            self._gw_log(f"  Falha ao construir Join Accept: {e}"); return

        # Atualiza sessão no DB
        dev["session"] = {
            "dev_addr": f"{dev_addr:08X}",
            "nwk_skey": bytes_to_hex(nwk_skey),
            "app_skey": bytes_to_hex(app_skey),
            "fcnt_up": 0,
            "fcnt_down": 0,
            "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._ui(self._refresh_devices_tree)
        self._gw_log(f"  Join Accept (cifrado) = 0x{ja.hex().upper()}")
        self._gw_log(f"  Sessão: DevAddr={dev_addr:08X} NwkSKey={bytes_to_hex(nwk_skey)[:16]}... "
                     f"AppSKey={bytes_to_hex(app_skey)[:16]}...")

        # Envia Join Accept após RX_DELAY1 (que para Join é JOIN_ACCEPT_DELAY1, default 5s)
        # Aqui simplificamos: envia imediatamente. Em produção, agendar precisão exata.
        self._gw_log(f"  Aguardando ~{rx_delay}s antes de TX (janela RX1 simulada)...")
        time.sleep(max(0.5, rx_delay - 0.5))  # margem para o end-device estar pronto
        try:
            # Re-configura RX (já estava em RX) — para enviar, alguns chips precisam ir para TX
            self.device.rx_stop()
            time.sleep(0.05)
            self.device.configure_radio(freq_hz, sf, bw, "4/5", 8, power)
            self.device.tx_payload(ja, msg_text=None)
            self._gw_log("  Join Accept transmitido")
            time.sleep(0.2)
            # Volta para RX
            self.device.rx_continuous_start()
        except Exception as e:
            self._gw_log(f"  Falha ao transmitir Join Accept: {e}")

    def _handle_uplink(self, raw):
        try:
            df = DataFrame.parse(raw)
        except Exception as e:
            self._gw_log(f"  Data frame inválido: {e}"); return

        dev_addr_hex = f"{df.dev_addr:08X}"
        # Procura sessão pelo DevAddr
        dev = self._find_session_by_dev_addr(dev_addr_hex)
        if not dev:
            self._gw_log(f"  UPLINK ignorado: DevAddr {dev_addr_hex} sem sessão (faça Join primeiro)")
            return
        sess = dev["session"]
        try:
            nwk_skey = hex_to_bytes(sess["nwk_skey"])
            app_skey = hex_to_bytes(sess["app_skey"])
        except Exception as e:
            self._gw_log(f"  Chaves de sessão inválidas: {e}"); return

        if not df.validate_mic(nwk_skey, fcnt32=df.fcnt):
            self._gw_log(f"  UPLINK MIC inválido para {dev['name']} (FCnt={df.fcnt})")
            return

        try:
            clear = df.decrypt_payload(app_skey, nwk_skey, fcnt32=df.fcnt)
        except Exception as e:
            self._gw_log(f"  Falha ao decifrar payload: {e}"); return

        sess["fcnt_up"] = df.fcnt
        self._ui(self._refresh_devices_tree)
        self._gw_log(
            f"  UPLINK de '{dev['name']}' DevAddr={dev_addr_hex} "
            f"FCnt={df.fcnt} FPort={df.fport} "
            f"payload={clear.hex().upper() if clear else '(vazio)'}"
        )

    def _find_device(self, dev_eui_hex: str) -> dict | None:
        target = dev_eui_hex.upper().replace(":", "").replace(" ", "")
        for d in self.devices_db:
            if d.get("dev_eui", "").upper().replace(":", "") == target:
                return d
        return None

    def _find_session_by_dev_addr(self, dev_addr_hex: str) -> dict | None:
        target = dev_addr_hex.upper()
        for d in self.devices_db:
            sess = d.get("session") or {}
            if sess.get("dev_addr", "").upper() == target:
                return d
        return None

    # -----------------------------------------------------------
    #   Resultados / CSV
    # -----------------------------------------------------------
    def _record_result(self, mode, seq, payload_tx, payload_rx, match, attempts,
                       timeout_s, retries, rssi, snr, rtt_ms, freq_tx_hz, freq_rx_hz):
        r = PacketResult(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            device=self.device.name if self.device else "",
            mode=mode, seq=seq,
            payload_tx=payload_tx,
            payload_rx=payload_rx or "",
            match=int(match),
            attempts=attempts, timeout_s=timeout_s, retries=retries,
            rssi="" if rssi is None else str(rssi),
            snr="" if snr is None else str(snr),
            rtt_ms="" if rtt_ms is None else f"{rtt_ms:.1f}",
            freq_tx_hz="" if freq_tx_hz is None else str(freq_tx_hz),
            freq_rx_hz="" if freq_rx_hz is None else str(freq_rx_hz),
        )
        self.logger.add_result(r)

    def _save_csv(self):
        if not self.logger.results:
            messagebox.showwarning("Aviso", "Não há resultados para salvar."); return
        os.makedirs(LOGS_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default = os.path.join(LOGS_DIR, f"resultados_{ts}.csv")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialdir=LOGS_DIR,
            initialfile=os.path.basename(default), filetypes=[("CSV", "*.csv")],
        )
        if not path: return
        try:
            self.logger.save_csv(path)
            self.logger.log(f"[INFO] CSV salvo em: {path}")
            messagebox.showinfo("OK", f"CSV salvo:\n{path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao salvar CSV: {e}")

    # -----------------------------------------------------------
    #   Encerramento
    # -----------------------------------------------------------
    def _on_close(self):
        try:
            if self.is_connected: self._disconnect()
        except Exception:
            pass
        # Fecha janela de log destacada se aberta
        if self._log_window is not None:
            try: self._log_window.destroy()
            except Exception: pass
        self.destroy()


# ===========================================================
if __name__ == "__main__":
    app = App()
    app.mainloop()

from __future__ import annotations

import serial
import time
import threading
import queue
import csv
from dataclasses import dataclass, field
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# --- LOCALIZAÇÃO (IP GEO) --------------------------------------------------

def get_current_location() -> tuple[float | None, float | None]:
    """
    Tenta obter latitude/longitude aproximadas via IP.
    Se falhar, retorna (None, None).
    Necessita do pacote 'requests' instalado.
    """
    try:
        import requests
        resp = requests.get("http://ip-api.com/json/", timeout=3)
        data = resp.json()
        if data.get("status") == "success":
            return float(data.get("lat")), float(data.get("lon"))
    except Exception:
        pass
    return None, None


# --- DRIVER RAK3172 --------------------------------------------------------


class RAK3172:
    """
    Driver básico para o módulo RAK3172 via comandos AT.
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None

    def open(self):
        if self.ser is None or not self.ser.is_open:
            self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
            time.sleep(2)  # estabilizar porta / módulo

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def send_at(self, command: str, wait: float = 0.5, show: bool = False) -> str:
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Porta serial não está aberta.")

        cmd = (command + "\r\n").encode()
        self.ser.write(cmd)
        time.sleep(wait)
        response = self.ser.read_all().decode(errors="ignore")

        if show:
            print(f"-> {command}")
            print(f"<- {response.strip()}\n")

        return response

    def configure_p2p(self, freq_hz: int, sf: int, bw_khz: int,
                      cr: int, preamble: int, power_dbm: int):
        self.send_at("AT+NWM=0")  # modo P2P
        cmd_p2p = f"AT+P2P={freq_hz}:{sf}:{bw_khz}:{cr}:{preamble}:{power_dbm}"
        self.send_at(cmd_p2p)

    def send_p2p(self, payload: bytes, show: bool = False) -> str:
        hex_str = payload.hex().upper()
        cmd = f"AT+PSEND={hex_str}"
        return self.send_at(cmd, wait=0.5, show=show)

    def read_lines_until(self, timeout: float):
        """
        Gera linhas recebidas da serial por até 'timeout' segundos.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Porta serial não está aberta.")

        end_time = time.time() + timeout
        buffer = ""
        while time.time() < end_time:
            if self.ser.in_waiting:
                data = self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                buffer += data
                while "\r\n" in buffer:
                    line, buffer = buffer.split("\r\n", 1)
                    line = line.strip()
                    if line:
                        yield line
            else:
                time.sleep(0.05)


# --- ESTRUTURAS DE DOCUMENTAÇÃO DO TESTE -----------------------------------


@dataclass
class P2PTestResult:
    seq_value: int
    orig_word_hex: str
    recv_word_hex: str | None
    match: bool
    attempts: int
    timeout_s: float
    rssi: float | None
    snr: float | None
    rtt_ms: float | None
    lat: float | None
    lon: float | None
    timestamp: str


@dataclass
class P2PTestMetadata:
    test_name: str
    start_time: str
    port: str
    baudrate: int
    freq_tx: int
    sf_tx: int
    bw_tx: int
    cr_tx: int
    preamble_tx: int
    power_tx: int
    freq_rx: int
    sf_rx: int
    bw_rx: int
    cr_rx: int
    preamble_rx: int
    results: list[P2PTestResult] = field(default_factory=list)

    def add_result(self, result: P2PTestResult):
        self.results.append(result)

    def save_to_csv(self, filename: str):
        fieldnames = [
            "test_name", "start_time", "port", "baudrate",
            "freq_tx", "sf_tx", "bw_tx", "cr_tx", "preamble_tx", "power_tx",
            "freq_rx", "sf_rx", "bw_rx", "cr_rx", "preamble_rx",
            "seq_value", "orig_word_hex", "recv_word_hex", "match",
            "attempts", "timeout_s", "rssi", "snr", "rtt_ms",
            "lat", "lon", "timestamp"
        ]
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
            writer.writeheader()
            for r in self.results:
                writer.writerow({
                    "test_name": self.test_name,
                    "start_time": self.start_time,
                    "port": self.port,
                    "baudrate": self.baudrate,
                    "freq_tx": self.freq_tx,
                    "sf_tx": self.sf_tx,
                    "bw_tx": self.bw_tx,
                    "cr_tx": self.cr_tx,
                    "preamble_tx": self.preamble_tx,
                    "power_tx": self.power_tx,
                    "freq_rx": self.freq_rx,
                    "sf_rx": self.sf_rx,
                    "bw_rx": self.bw_rx,
                    "cr_rx": self.cr_rx,
                    "preamble_rx": self.preamble_rx,
                    "seq_value": r.seq_value,
                    "orig_word_hex": r.orig_word_hex,
                    "recv_word_hex": r.recv_word_hex or "",
                    "match": int(r.match),
                    "attempts": r.attempts,
                    "timeout_s": r.timeout_s,
                    "rssi": r.rssi if r.rssi is not None else "",
                    "snr": r.snr if r.snr is not None else "",
                    "rtt_ms": f"{r.rtt_ms:.1f}" if r.rtt_ms is not None else "",
                    "lat": r.lat if r.lat is not None else "",
                    "lon": r.lon if r.lon is not None else "",
                    "timestamp": r.timestamp
                })


# --- MOTOR DO ENVIO DE UM ÚNICO PACOTE ------------------------------------


class P2PDocSinglePacketEngine:
    """
    Faz o envio de UM pacote (16 bits) com tentativas de reenvio.
    Antes de cada tentativa:
      - configura a RAK para TX com os parâmetros de TX atuais;
      - envia;
      - reconfigura para RX com os parâmetros de RX atuais;
      - aguarda resposta.
    """

    def __init__(
        self,
        device: RAK3172,
        meta: P2PTestMetadata,
        seq_value: int,
        max_retries: int,
        timeout_s: float,
        log_queue: queue.Queue,
        tx_cfg: dict,
        rx_cfg: dict,
    ):
        self.device = device
        self.meta = meta
        self.seq_value = seq_value & 0xFFFF
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.log_queue = log_queue
        self.tx_cfg = tx_cfg
        self.rx_cfg = rx_cfg

    def log(self, msg: str):
        self.log_queue.put(("log", msg))

    def parse_rx_line(self, line: str):
        """
        Tenta extrair RSSI, SNR e payload em hex a partir de uma linha do módulo.
        Pode precisar de ajuste conforme o formato exato do firmware.
        """
        line_up = line.upper()
        rssi = None
        snr = None
        payload_hex = None

        parts = line.strip().split(":")

        # Tentativa 1: formato tipo +EVT:RXP2P:-45:10:5:ABCD
        if "RXP2P" in line_up and len(parts) >= 5:
            try:
                possible_payload = parts[-1].strip()
                if all(c in "0123456789ABCDEF" for c in possible_payload.upper()):
                    payload_hex = possible_payload.upper()
                # RSSI / SNR em campos anteriores (heurística)
                rssi = float(parts[-4])
                snr = float(parts[-3])
            except Exception:
                pass

        # Tentativa 2: tokens com RSSI / SNR
        if rssi is None or snr is None:
            for token in parts:
                t = token.upper()
                if "RSSI" in t and rssi is None:
                    for sep in ["=", " "]:
                        if sep in token:
                            try:
                                v = token.split(sep)[-1]
                                v = "".join(ch for ch in v if (ch.isdigit() or ch in "+-."))
                                rssi = float(v)
                            except Exception:
                                pass
                if "SNR" in t and snr is None:
                    for sep in ["=", " "]:
                        if sep in token:
                            try:
                                v = token.split(sep)[-1]
                                v = "".join(ch for ch in v if (ch.isdigit() or ch in "+-."))
                                snr = float(v)
                            except Exception:
                                pass

        # Tentativa 3: último token hex razoável
        if payload_hex is None:
            tokens = line.strip().split()
            for tok in reversed(tokens):
                t = tok.upper()
                if len(t) >= 4 and all(c in "0123456789ABCDEF" for c in t):
                    payload_hex = t
                    break

        return rssi, snr, payload_hex

    def run(self):
        orig_word_hex = f"{self.seq_value:04X}"
        payload = self.seq_value.to_bytes(2, byteorder="big")
        lat, lon = get_current_location()

        self.log(
            f"Enviando seq {self.seq_value} (0x{orig_word_hex}) | "
            f"tentativas máx = {self.max_retries + 1} | timeout = {self.timeout_s:.1f}s | "
            f"lat={lat} lon={lon}"
        )
        self.log(
            f"Config TX: freq={self.tx_cfg['freq']} SF={self.tx_cfg['sf']} "
            f"BW={self.tx_cfg['bw']} CR={self.tx_cfg['cr']} preamble={self.tx_cfg['preamble']} "
            f"power={self.tx_cfg['power']} "
            f"| Config RX: freq={self.rx_cfg['freq']} SF={self.rx_cfg['sf']} "
            f"BW={self.rx_cfg['bw']} CR={self.rx_cfg['cr']} preamble={self.rx_cfg['preamble']}"
        )

        attempts_used = 0
        recv_word_hex = None
        match = False
        rssi = None
        snr = None
        rtt_ms = None

        max_attempts = self.max_retries + 1

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            self.log(f"  Tentativa {attempt}/{max_attempts}...")

            # 1) Configura para TRANSMITIR (TX)
            try:
                self.device.configure_p2p(
                    self.tx_cfg["freq"],
                    self.tx_cfg["sf"],
                    self.tx_cfg["bw"],
                    self.tx_cfg["cr"],
                    self.tx_cfg["preamble"],
                    self.tx_cfg["power"],
                )
            except Exception as e:
                self.log(f"  ERRO ao configurar TX: {e}")
                break

            # 2) Envia payload
            t0 = time.time()
            try:
                self.device.send_p2p(payload, show=False)
            except Exception as e:
                self.log(f"  ERRO ao enviar na tentativa {attempt}: {e}")
                break

            # 3) Configura para RECEBER (RX) com os parâmetros de RX atuais
            try:
                # Usa power de TX (campo obrigatório no AT+P2P, mas irrelevante para RX)
                self.device.configure_p2p(
                    self.rx_cfg["freq"],
                    self.rx_cfg["sf"],
                    self.rx_cfg["bw"],
                    self.rx_cfg["cr"],
                    self.rx_cfg["preamble"],
                    self.tx_cfg["power"],
                )
            except Exception as e:
                self.log(f"  ERRO ao configurar RX: {e}")
                break

            # 4) Espera resposta nessa tentativa
            got_response = False
            for line in self.device.read_lines_until(self.timeout_s):
                self.log(f"    Linha recebida: {line}")
                rssi, snr, payload_hex = self.parse_rx_line(line)
                if payload_hex:
                    # Considera word = últimos 4 dígitos hex
                    recv_word_hex = payload_hex[-4:].upper()
                    rtt_ms = (time.time() - t0) * 1000.0
                    match = (recv_word_hex == orig_word_hex)
                    status = "OK" if match else "ERRO"
                    self.log(
                        f"    Recebido payload={payload_hex} "
                        f"(word=0x{recv_word_hex}) | match={status} | "
                        f"RSSI={rssi} | SNR={snr} | RTT={rtt_ms:.1f} ms"
                    )
                    got_response = True
                    break

            if got_response:
                # Se recebeu algo, não faz mais reenvio
                break
            else:
                self.log(
                    f"    Timeout sem resposta na tentativa {attempt} "
                    f"(timeout={self.timeout_s:.1f}s)."
                )
                if attempt < max_attempts:
                    self.log("    Reenviando (nova configuração TX/RX será aplicada)...")
                else:
                    self.log("    Limite de tentativas atingido, encerrando.")

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        result = P2PTestResult(
            seq_value=self.seq_value,
            orig_word_hex=orig_word_hex,
            recv_word_hex=recv_word_hex,
            match=match,
            attempts=attempts_used,
            timeout_s=self.timeout_s,
            rssi=rssi,
            snr=snr,
            rtt_ms=rtt_ms,
            lat=lat,
            lon=lon,
            timestamp=ts,
        )

        self.meta.add_result(result)
        self.log(
            f"Resultado final seq {self.seq_value}: "
            f"match={match}, recv_word={recv_word_hex}, tentativas={attempts_used}, "
            f"lat={lat}, lon={lon}, timestamp={ts}"
        )


# --- GUI -------------------------------------------------------------------


class TestApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Teste P2P RAK3172 - Pacote 16 bits Incremental")
        self.geometry("950x650")

        self.log_queue: queue.Queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.meta: P2PTestMetadata | None = None
        self.device: RAK3172 | None = None
        self.connected: bool = False

        self.current_seq = 0  # valor 16-bit que será enviado

        self.create_widgets()
        self.after(100, self.process_log_queue)

    # --- GUI building ------------------------------------------------------

    def create_widgets(self):
        cfg = ttk.LabelFrame(self, text="Configuração do Teste")
        cfg.pack(fill="x", padx=10, pady=5)

        # Linha 0: porta / baud / timeout / retries
        ttk.Label(cfg, text="Porta serial:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.entry_port = ttk.Entry(cfg, width=10)
        self.entry_port.insert(0, "COM4")
        self.entry_port.grid(row=0, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="Baudrate:").grid(row=0, column=2, sticky="e", padx=5, pady=2)
        self.entry_baud = ttk.Entry(cfg, width=10)
        self.entry_baud.insert(0, "115200")
        self.entry_baud.grid(row=0, column=3, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="Timeout resp. (s):").grid(row=0, column=4, sticky="e", padx=5, pady=2)
        self.entry_timeout = ttk.Entry(cfg, width=8)
        self.entry_timeout.insert(0, "5")
        self.entry_timeout.grid(row=0, column=5, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="Reenvios (extras):").grid(row=0, column=6, sticky="e", padx=5, pady=2)
        self.entry_retries = ttk.Entry(cfg, width=8)
        self.entry_retries.insert(0, "2")
        self.entry_retries.grid(row=0, column=7, sticky="w", padx=5, pady=2)

        # Linha 1: parâmetros TX
        ttk.Label(cfg, text="Freq TX (Hz):").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        self.entry_freq_tx = ttk.Entry(cfg, width=12)
        self.entry_freq_tx.insert(0, "915200000")
        self.entry_freq_tx.grid(row=1, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="SF TX:").grid(row=1, column=2, sticky="e", padx=5, pady=2)
        self.entry_sf_tx = ttk.Entry(cfg, width=5)
        self.entry_sf_tx.insert(0, "9")
        self.entry_sf_tx.grid(row=1, column=3, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="BW TX (kHz):").grid(row=1, column=4, sticky="e", padx=5, pady=2)
        self.entry_bw_tx = ttk.Entry(cfg, width=6)
        self.entry_bw_tx.insert(0, "125")
        self.entry_bw_tx.grid(row=1, column=5, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="CR TX:").grid(row=1, column=6, sticky="e", padx=5, pady=2)
        self.entry_cr_tx = ttk.Entry(cfg, width=5)
        self.entry_cr_tx.insert(0, "0")
        self.entry_cr_tx.grid(row=1, column=7, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="Preamble TX:").grid(row=1, column=8, sticky="e", padx=5, pady=2)
        self.entry_preamble_tx = ttk.Entry(cfg, width=6)
        self.entry_preamble_tx.insert(0, "10")
        self.entry_preamble_tx.grid(row=1, column=9, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="Power TX (dBm):").grid(row=1, column=10, sticky="e", padx=5, pady=2)
        self.entry_power_tx = ttk.Entry(cfg, width=6)
        self.entry_power_tx.insert(0, "14")
        self.entry_power_tx.grid(row=1, column=11, sticky="w", padx=5, pady=2)

        # Linha 2: parâmetros RX (só documentação + configuração de RX)
        ttk.Label(cfg, text="Freq RX (Hz):").grid(row=2, column=0, sticky="e", padx=5, pady=2)
        self.entry_freq_rx = ttk.Entry(cfg, width=12)
        self.entry_freq_rx.insert(0, "915200000")
        self.entry_freq_rx.grid(row=2, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="SF RX:").grid(row=2, column=2, sticky="e", padx=5, pady=2)
        self.entry_sf_rx = ttk.Entry(cfg, width=5)
        self.entry_sf_rx.insert(0, "9")
        self.entry_sf_rx.grid(row=2, column=3, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="BW RX (kHz):").grid(row=2, column=4, sticky="e", padx=5, pady=2)
        self.entry_bw_rx = ttk.Entry(cfg, width=6)
        self.entry_bw_rx.insert(0, "125")
        self.entry_bw_rx.grid(row=2, column=5, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="CR RX:").grid(row=2, column=6, sticky="e", padx=5, pady=2)
        self.entry_cr_rx = ttk.Entry(cfg, width=5)
        self.entry_cr_rx.insert(0, "0")
        self.entry_cr_rx.grid(row=2, column=7, sticky="w", padx=5, pady=2)

        ttk.Label(cfg, text="Preamble RX:").grid(row=2, column=8, sticky="e", padx=5, pady=2)
        self.entry_preamble_rx = ttk.Entry(cfg, width=6)
        self.entry_preamble_rx.insert(0, "10")
        self.entry_preamble_rx.grid(row=2, column=9, sticky="w", padx=5, pady=2)

        # Label do próximo pacote
        self.lbl_next_seq = ttk.Label(cfg, text="")
        self.lbl_next_seq.grid(row=3, column=0, columnspan=12, sticky="w", padx=5, pady=5)
        self.update_seq_label()

        # Botões principais
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=5)

        self.btn_connect = ttk.Button(btn_frame, text="Conectar", command=self.connect_device)
        self.btn_connect.pack(side="left", padx=5)

        self.btn_disconnect = ttk.Button(btn_frame, text="Desconectar", command=self.disconnect_device, state="disabled")
        self.btn_disconnect.pack(side="left", padx=5)

        self.btn_send = ttk.Button(btn_frame, text="Enviar pacote", command=self.send_packet, state="disabled")
        self.btn_send.pack(side="left", padx=5)

        self.btn_save = ttk.Button(btn_frame, text="Salvar CSV", command=self.save_csv, state="disabled")
        self.btn_save.pack(side="right", padx=5)

        # Área de log
        log_frame = ttk.LabelFrame(self, text="Log")
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.text_log = tk.Text(log_frame, wrap="word")
        self.text_log.pack(fill="both", expand=True, padx=5, pady=5)

    # --- Helpers -----------------------------------------------------------

    def update_seq_label(self):
        word_hex = f"{self.current_seq & 0xFFFF:04X}"
        self.lbl_next_seq.config(
            text=f"Próximo pacote (16 bits): seq = {self.current_seq & 0xFFFF} (0x{word_hex})"
        )

    def append_log(self, msg: str):
        self.text_log.insert("end", msg + "\n")
        self.text_log.see("end")

    def process_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self.append_log(payload)
        except queue.Empty:
            pass
        self.after(100, self.process_log_queue)

    # --- Conexão -----------------------------------------------------------

    def connect_device(self):
        if self.connected:
            messagebox.showinfo("Info", "Já está conectado.")
            return

        try:
            port = self.entry_port.get().strip()
            baudrate = int(self.entry_baud.get().strip())
        except ValueError as e:
            messagebox.showerror("Erro", f"Baudrate inválido: {e}")
            return

        try:
            self.device = RAK3172(port, baudrate=baudrate, timeout=1.0)
            self.device.open()
            self.append_log(f"Conectado à porta {port} @ {baudrate} bps. Testando AT...")
            resp = self.device.send_at("AT", wait=0.5, show=False)
            self.append_log(f"Resposta AT: {resp.strip()}")
            if "OK" not in resp.upper():
                messagebox.showwarning("Aviso", "Dispositivo não respondeu 'OK' ao comando AT.")
            self.connected = True
            self.btn_connect.config(state="disabled")
            self.btn_disconnect.config(state="normal")
            self.btn_send.config(state="normal")
        except Exception as e:
            if self.device:
                self.device.close()
                self.device = None
            messagebox.showerror("Erro", f"Falha ao conectar: {e}")
            self.append_log(f"Erro ao conectar: {e}")

    def disconnect_device(self):
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
        self.device = None
        self.connected = False
        self.append_log("Dispositivo desconectado.")
        self.btn_connect.config(state="normal")
        self.btn_disconnect.config(state="disabled")
        self.btn_send.config(state="disabled")

    # --- Ações -------------------------------------------------------------

    def send_packet(self):
        # Evita duas threads ao mesmo tempo
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Aviso", "Um envio já está em andamento.")
            return

        if not self.connected or not self.device or not self.device.ser or not self.device.ser.is_open:
            messagebox.showwarning("Aviso", "Conecte ao dispositivo primeiro.")
            return

        try:
            port = self.entry_port.get().strip()
            baudrate = int(self.entry_baud.get().strip())
            timeout_s = float(self.entry_timeout.get().strip())
            retries = int(self.entry_retries.get().strip())

            freq_tx = int(self.entry_freq_tx.get().strip())
            sf_tx = int(self.entry_sf_tx.get().strip())
            bw_tx = int(self.entry_bw_tx.get().strip())
            cr_tx = int(self.entry_cr_tx.get().strip())
            preamble_tx = int(self.entry_preamble_tx.get().strip())
            power_tx = int(self.entry_power_tx.get().strip())

            freq_rx = int(self.entry_freq_rx.get().strip())
            sf_rx = int(self.entry_sf_rx.get().strip())
            bw_rx = int(self.entry_bw_rx.get().strip())
            cr_rx = int(self.entry_cr_rx.get().strip())
            preamble_rx = int(self.entry_preamble_rx.get().strip())

        except ValueError as e:
            messagebox.showerror("Erro", f"Valor inválido em algum campo: {e}")
            return

        # Se ainda não temos meta (primeiro envio), cria
        if self.meta is None:
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.meta = P2PTestMetadata(
                test_name="P2P_16bits_incremental",
                start_time=start_time,
                port=port,
                baudrate=baudrate,
                freq_tx=freq_tx,
                sf_tx=sf_tx,
                bw_tx=bw_tx,
                cr_tx=cr_tx,
                preamble_tx=preamble_tx,
                power_tx=power_tx,
                freq_rx=freq_rx,
                sf_rx=sf_rx,
                bw_rx=bw_rx,
                cr_rx=cr_rx,
                preamble_rx=preamble_rx,
            )
        else:
            # Atualiza meta para refletir configurações atuais
            self.meta.port = port
            self.meta.baudrate = baudrate
            self.meta.freq_tx = freq_tx
            self.meta.sf_tx = sf_tx
            self.meta.bw_tx = bw_tx
            self.meta.cr_tx = cr_tx
            self.meta.preamble_tx = preamble_tx
            self.meta.power_tx = power_tx
            self.meta.freq_rx = freq_rx
            self.meta.sf_rx = sf_rx
            self.meta.bw_rx = bw_rx
            self.meta.cr_rx = cr_rx
            self.meta.preamble_rx = preamble_rx

        # Monta configs TX/RX que serão usadas pelo engine
        tx_cfg = {
            "freq": freq_tx,
            "sf": sf_tx,
            "bw": bw_tx,
            "cr": cr_tx,
            "preamble": preamble_tx,
            "power": power_tx,
        }
        rx_cfg = {
            "freq": freq_rx,
            "sf": sf_rx,
            "bw": bw_rx,
            "cr": cr_rx,
            "preamble": preamble_rx,
        }

        seq_to_send = self.current_seq & 0xFFFF

        engine = P2PDocSinglePacketEngine(
            device=self.device,
            meta=self.meta,
            seq_value=seq_to_send,
            max_retries=retries,
            timeout_s=timeout_s,
            log_queue=self.log_queue,
            tx_cfg=tx_cfg,
            rx_cfg=rx_cfg,
        )

        def worker():
            try:
                engine.run()
            finally:
                # Não fecha a porta aqui: fica conectada para o próximo envio
                self.after(0, self.on_send_finished)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

        self.btn_send.config(state="disabled")
        self.btn_save.config(state="disabled")

    def on_send_finished(self):
        # Incrementa sequência pra próxima vez
        self.current_seq = (self.current_seq + 1) & 0xFFFF
        self.update_seq_label()
        if self.connected:
            self.btn_send.config(state="normal")

        if self.meta and self.meta.results:
            self.btn_save.config(state="normal")

    def save_csv(self):
        if not self.meta or not self.meta.results:
            messagebox.showwarning("Aviso", "Não há resultados para salvar.")
            return

        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            title="Salvar resultados em CSV",
        )
        if not filename:
            return

        try:
            self.meta.save_to_csv(filename)
            messagebox.showinfo("Sucesso", f"Resultados salvos em:\n{filename}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha ao salvar CSV: {e}")


if __name__ == "__main__":
    app = TestApp()
    app.mainloop()

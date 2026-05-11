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


# ===========================================================
#   DRIVER RAK3172
# ===========================================================
class RAK3172:
    def __init__(self, port, baudrate=115200, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None

    def open(self):
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        time.sleep(1.5)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def flush_rx(self):
        if self.ser and self.ser.is_open:
            try:
                _ = self.ser.read_all()
            except Exception:
                pass

    def send_at(self, cmd: str, wait: float = 0.4) -> str:
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Porta não aberta")
        cmd_tx = (cmd.strip() + "\r\n").encode()
        self.ser.write(cmd_tx)
        time.sleep(wait)
        return self.ser.read_all().decode(errors="ignore")

    def configure_p2p(self, freq, sf, bw, cr, preamble, power):
        # NWM=0 => P2P
        self.send_at("AT+NWM=0")
        cmd = f"AT+P2P={freq}:{sf}:{bw}:{cr}:{preamble}:{power}"
        return self.send_at(cmd)

    def send_payload(self, payload: bytes):
        cmd = f"AT+PSEND={payload.hex().upper()}"
        return self.send_at(cmd)

    def read_until(self, timeout_s: float):
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Porta não aberta")

        end = time.time() + timeout_s
        buff = ""

        while time.time() < end:
            if self.ser.in_waiting:
                buff += self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                while "\r\n" in buff:
                    line, buff = buff.split("\r\n", 1)
                    line = line.strip()
                    if line:
                        yield line
            else:
                time.sleep(0.05)


# ===========================================================
#   DOCUMENTAÇÃO DOS RESULTADOS
# ===========================================================
@dataclass
class PacketResult:
    # Para TX: seq >= 0; para RX contínuo: seq = -1
    seq: int
    payload_tx_hex: str
    payload_rx_hex: str | None
    match: bool
    attempts: int
    rssi: float | None
    snr: float | None
    rtt_ms: float | None
    timeout: float
    retries: int
    timestamp: str
    mode: str  # "TX" ou "RX"
    freq_tx_hz: int | None
    freq_rx_hz: int | None


@dataclass
class TestLog:
    name: str
    start_time: str
    port: str
    baudrate: int
    results: list[PacketResult] = field(default_factory=list)

    def add(self, r: PacketResult):
        self.results.append(r)

    def save_csv(self, filename: str):
        fields = [
            "timestamp", "mode", "seq",
            "payload_tx_hex", "payload_rx_hex", "match",
            "attempts", "timeout", "retries",
            "rssi", "snr", "rtt_ms",
            "freq_tx_hz", "freq_rx_hz",
            "port", "baudrate",
        ]

        with open(filename, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=";")
            w.writeheader()

            for r in self.results:
                w.writerow({
                    "timestamp": r.timestamp,
                    "mode": r.mode,
                    "seq": r.seq,
                    "payload_tx_hex": r.payload_tx_hex,
                    "payload_rx_hex": r.payload_rx_hex or "",
                    "match": int(r.match),
                    "attempts": r.attempts,
                    "timeout": r.timeout,
                    "retries": r.retries,
                    "rssi": r.rssi if r.rssi is not None else "",
                    "snr": r.snr if r.snr is not None else "",
                    "rtt_ms": f"{r.rtt_ms:.1f}" if r.rtt_ms is not None else "",
                    "freq_tx_hz": r.freq_tx_hz if r.freq_tx_hz is not None else "",
                    "freq_rx_hz": r.freq_rx_hz if r.freq_rx_hz is not None else "",
                    "port": self.port,
                    "baudrate": self.baudrate,
                })


# ===========================================================
#   EXECUÇÃO DE UM ÚNICO PACOTE (TX + RX)
#   Log diferencia primeiro envio x reenvio
# ===========================================================
class P2PExecution:
    def __init__(self, device: RAK3172, testlog: TestLog, seq_value: int, payload: bytes,
                 timeout: float, retries: int, cfg_tx: dict, cfg_rx: dict, logqueue: queue.Queue):
        self.device = device
        self.testlog = testlog
        self.seq_value = seq_value & 0xFFFF
        self.payload = payload
        self.timeout = timeout
        self.retries = retries
        self.cfg_tx = cfg_tx
        self.cfg_rx = cfg_rx
        self.log = logqueue

    def emit(self, msg: str):
        self.log.put(("log", msg))

    @staticmethod
    def parse_line(line: str):
        """
        Esperado algo como:
        +EVT:RXP2P:-40:6:20000000419C0000000101000100

        Procuramos 'RXP2P' e assumimos:
        RXP2P:<rssi>:<snr>:<payload_hex>
        """
        parts = line.split(":")
        rssi = snr = None
        payload_hex = None

        if "RXP2P" in line.upper():
            try:
                idx = next(i for i, p in enumerate(parts) if p.upper() == "RXP2P")
                rssi = float(parts[idx + 1])
                snr = float(parts[idx + 2])
                payload_hex = parts[idx + 3].strip().upper()
            except Exception:
                pass

        return payload_hex, rssi, snr

    def run(self):
        payload_hex = self.payload.hex().upper()

        attempts = 0
        rx_hex = None
        rssi = snr = None
        rtt_ms = None

        # (melhoria) limpa qualquer lixo no RX antes de começar
        self.device.flush_rx()

        for attempt in range(self.retries + 1):
            attempts += 1

            tentativa_txt = "primeiro envio" if attempts == 1 else f"REENVIO #{attempts - 1}"

            self.emit(f"Config TX: {self.cfg_tx} ({tentativa_txt})")
            self.device.configure_p2p(**self.cfg_tx)

            t0 = time.time()
            self.emit(
                f"Enviando pacote TX seq={self.seq_value} HEX={payload_hex} "
                f"(tentativa {attempts} - {tentativa_txt})"
            )
            self.device.send_payload(self.payload)

            # configura RX (pode ser freq diferente)
            self.emit(f"Config RX: {self.cfg_rx} (após {tentativa_txt})")
            rx_cfg_mod = self.cfg_rx.copy()
            rx_cfg_mod["power"] = self.cfg_tx["power"]  # o AT+P2P exige power também
            self.device.configure_p2p(**rx_cfg_mod)

            # inicia recepção com timeout
            try:
                self.device.send_at(f"AT+PRECV={int(self.timeout * 1000)}", wait=0.1)
            except Exception:
                pass

            got = False
            for line in self.device.read_until(self.timeout):
                self.emit(f"RX>> {line}")
                ph, rxrssi, rxsnr = self.parse_line(line)
                if ph:
                    rx_hex = ph
                    rssi = rxrssi
                    snr = rxsnr
                    rtt_ms = (time.time() - t0) * 1000.0
                    got = True
                    break

            if got:
                if attempts == 1:
                    self.emit(f"Pacote seq={self.seq_value} recebido no primeiro envio.")
                else:
                    self.emit(
                        f"Pacote seq={self.seq_value} recebido após reenvio "
                        f"(total de tentativas: {attempts})."
                    )
                break
            else:
                if attempts == 1:
                    self.emit(f"Timeout no primeiro envio (tentativa {attempts}).")
                else:
                    self.emit(f"Timeout no reenvio #{attempts - 1} (tentativa {attempts}).")

        match = (rx_hex == payload_hex) if rx_hex is not None else False

        result = PacketResult(
            seq=self.seq_value,
            payload_tx_hex=payload_hex,
            payload_rx_hex=rx_hex,
            match=match,
            attempts=attempts,
            rssi=rssi,
            snr=snr,
            rtt_ms=rtt_ms,
            timeout=self.timeout,
            retries=self.retries,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            mode="TX",
            freq_tx_hz=int(self.cfg_tx.get("freq")) if self.cfg_tx.get("freq") is not None else None,
            freq_rx_hz=int(self.cfg_rx.get("freq")) if self.cfg_rx.get("freq") is not None else None,
        )
        self.testlog.add(result)

        if rx_hex is not None:
            resumo = "sem reenvio" if attempts == 1 else f"com reenvio (tentativas={attempts})"
        else:
            resumo = "SEM RESPOSTA (esgotadas as tentativas)"

        self.emit(
            f"<< Resultado TX: RX_hex={rx_hex}, match={match}, rssi={rssi}, snr={snr}, "
            f"rtt={rtt_ms}, tentativas={attempts}, f_tx={result.freq_tx_hz}, f_rx={result.freq_rx_hz} -> {resumo}"
        )
        self.emit("-" * 60)


# ===========================================================
#   GUI
# ===========================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Teste LoRa RAK3172")
        self.geometry("950x650")

        self.queue = queue.Queue()
        self.device: RAK3172 | None = None
        self.is_connected = False

        self.seq = 0
        self.testlog: TestLog | None = None

        # RX contínuo
        self.rx_running = False
        self.rx_thread: threading.Thread | None = None

        self.init_ui()
        self.after(100, self.process_queue)

        # fecha bonitinho
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self):
        try:
            self.rx_running = False
        except Exception:
            pass
        try:
            if self.device:
                self.device.close()
        except Exception:
            pass
        self.destroy()

    # ---------------- UI -----------------
    def init_ui(self):
        frame = ttk.LabelFrame(self, text="Configuração")
        frame.pack(fill="x", padx=10, pady=5)

        # PORTA / BAUD / TIMEOUT / REENVIOS
        ttk.Label(frame, text="Porta:").grid(row=0, column=0, padx=3, pady=2, sticky="e")
        self.e_port = ttk.Entry(frame, width=10)
        self.e_port.insert(0, "COM6")
        self.e_port.grid(row=0, column=1, padx=3, pady=2)

        ttk.Label(frame, text="Baud:").grid(row=0, column=2, padx=3, pady=2, sticky="e")
        self.e_baud = ttk.Entry(frame, width=10)
        self.e_baud.insert(0, "115200")
        self.e_baud.grid(row=0, column=3, padx=3, pady=2)

        ttk.Label(frame, text="Timeout (s):").grid(row=0, column=4, padx=3, pady=2, sticky="e")
        self.e_timeout = ttk.Entry(frame, width=6)
        self.e_timeout.insert(0, "5")
        self.e_timeout.grid(row=0, column=5, padx=3, pady=2)

        ttk.Label(frame, text="Reenvios:").grid(row=0, column=6, padx=3, pady=2, sticky="e")
        self.e_retry = ttk.Entry(frame, width=6)
        self.e_retry.insert(0, "2")
        self.e_retry.grid(row=0, column=7, padx=3, pady=2)

        # MENSAGEM MANUAL
        ttk.Label(frame, text="Mensagem manual (texto ou 0xHEX):").grid(row=1, column=0, padx=3, pady=2, sticky="e")
        self.e_msg = ttk.Entry(frame, width=70)
        self.e_msg.insert(0, "0x0100000050840000000000000000FF11400216")
        self.e_msg.grid(row=1, column=1, columnspan=10, padx=3, pady=2, sticky="w")

        # PARÂMETROS TX
        ttk.Label(frame, text="Freq TX (Hz):").grid(row=2, column=0, padx=3, pady=2, sticky="e")
        self.e_f_tx = ttk.Entry(frame, width=12)
        self.e_f_tx.insert(0, "904000000")
        self.e_f_tx.grid(row=2, column=1, padx=3, pady=2)

        ttk.Label(frame, text="SF TX:").grid(row=2, column=2, padx=3, pady=2, sticky="e")
        self.e_sf_tx = ttk.Entry(frame, width=5)
        self.e_sf_tx.insert(0, "11")
        self.e_sf_tx.grid(row=2, column=3, padx=3, pady=2)

        ttk.Label(frame, text="BW TX (kHz):").grid(row=2, column=4, padx=3, pady=2, sticky="e")
        self.e_bw_tx = ttk.Entry(frame, width=5)
        self.e_bw_tx.insert(0, "500")
        self.e_bw_tx.grid(row=2, column=5, padx=3, pady=2)

        ttk.Label(frame, text="CR TX:").grid(row=2, column=6, padx=3, pady=2, sticky="e")
        self.e_cr_tx = ttk.Entry(frame, width=5)
        self.e_cr_tx.insert(0, "0")
        self.e_cr_tx.grid(row=2, column=7, padx=3, pady=2)

        ttk.Label(frame, text="Preamble TX:").grid(row=2, column=8, padx=3, pady=2, sticky="e")
        self.e_pre_tx = ttk.Entry(frame, width=5)
        self.e_pre_tx.insert(0, "10")
        self.e_pre_tx.grid(row=2, column=9, padx=3, pady=2)

        ttk.Label(frame, text="Power TX (dBm):").grid(row=2, column=10, padx=3, pady=2, sticky="e")
        self.e_pwr_tx = ttk.Entry(frame, width=5)
        self.e_pwr_tx.insert(0, "14")
        self.e_pwr_tx.grid(row=2, column=11, padx=3, pady=2)

        # PARÂMETROS RX
        ttk.Label(frame, text="Freq RX (Hz):").grid(row=3, column=0, padx=3, pady=2, sticky="e")
        self.e_f_rx = ttk.Entry(frame, width=12)
        self.e_f_rx.insert(0, "904000000")
        self.e_f_rx.grid(row=3, column=1, padx=3, pady=2)

        ttk.Label(frame, text="SF RX:").grid(row=3, column=2, padx=3, pady=2, sticky="e")
        self.e_sf_rx = ttk.Entry(frame, width=5)
        self.e_sf_rx.insert(0, "11")
        self.e_sf_rx.grid(row=3, column=3, padx=3, pady=2)

        ttk.Label(frame, text="BW RX (kHz):").grid(row=3, column=4, padx=3, pady=2, sticky="e")
        self.e_bw_rx = ttk.Entry(frame, width=5)
        self.e_bw_rx.insert(0, "500")
        self.e_bw_rx.grid(row=3, column=5, padx=3, pady=2)

        ttk.Label(frame, text="CR RX:").grid(row=3, column=6, padx=3, pady=2, sticky="e")
        self.e_cr_rx = ttk.Entry(frame, width=5)
        self.e_cr_rx.insert(0, "0")
        self.e_cr_rx.grid(row=3, column=7, padx=3, pady=2)

        ttk.Label(frame, text="Preamble RX:").grid(row=3, column=8, padx=3, pady=2, sticky="e")
        self.e_pre_rx = ttk.Entry(frame, width=5)
        self.e_pre_rx.insert(0, "10")
        self.e_pre_rx.grid(row=3, column=9, padx=3, pady=2)

        # BOTÕES
        bf = ttk.Frame(self)
        bf.pack(fill="x", pady=5, padx=10)

        self.b_connect = ttk.Button(bf, text="Conectar", command=self.connect)
        self.b_connect.pack(side="left", padx=5)

        self.b_send = ttk.Button(bf, text="Enviar pacote (TX)", command=self.send_packet, state="disabled")
        self.b_send.pack(side="left", padx=5)

        self.b_rx_start = ttk.Button(bf, text="Iniciar RX contínuo", command=self.start_rx, state="disabled")
        self.b_rx_start.pack(side="left", padx=5)

        self.b_rx_stop = ttk.Button(bf, text="Parar RX", command=self.stop_rx, state="disabled")
        self.b_rx_stop.pack(side="left", padx=5)

        self.b_save = ttk.Button(bf, text="Salvar CSV", command=self.save_csv, state="disabled")
        self.b_save.pack(side="right", padx=5)

        # LOG
        logbox = ttk.LabelFrame(self, text="Log")
        logbox.pack(fill="both", expand=True, padx=10, pady=5)
        self.text = tk.Text(logbox)
        self.text.pack(fill="both", expand=True)

    # ---------------- LOG / QUEUE -----------------
    def log(self, msg: str):
        self.text.insert("end", msg + "\n")
        self.text.see("end")

    def process_queue(self):
        try:
            while True:
                typ, val = self.queue.get_nowait()
                if typ == "log":
                    self.log(val)
        except queue.Empty:
            pass
        self.after(100, self.process_queue)

    # ---------------- CONEXÃO -----------------
    def connect(self):
        try:
            port = self.e_port.get().strip()
            baud = int(self.e_baud.get().strip())

            self.device = RAK3172(port, baud)
            self.device.open()

            rsp = self.device.send_at("AT")
            self.log(f"AT>> {rsp.strip()}")

            self.is_connected = True
            self.testlog = TestLog(
                name="TesteLoRa",
                start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                port=port,
                baudrate=baud,
            )

            self.b_send.config(state="normal")
            self.b_rx_start.config(state="normal")
            self.log("== Conectado! ==")

        except Exception as e:
            messagebox.showerror("Falha", str(e))

    # ---------------- TX ÚNICO -----------------
    def send_packet(self):
        if not self.device or not self.is_connected:
            return

        if self.rx_running:
            messagebox.showwarning("Aviso", "Pare o RX contínuo antes de enviar pacotes TX.")
            return

        try:
            timeout = float(self.e_timeout.get().strip())
            retries = int(self.e_retry.get().strip())
        except ValueError:
            messagebox.showerror("Erro", "Timeout ou Reenvios inválidos.")
            return

        msg = self.e_msg.get().strip()
        if msg:
            if msg.lower().startswith("0x"):
                hexstr = msg[2:].strip()
                try:
                    payload = bytes.fromhex(hexstr)
                except ValueError:
                    messagebox.showerror("Erro", "Mensagem manual HEX inválida.")
                    return
            else:
                payload = msg.encode("utf-8")
        else:
            word = self.seq & 0xFFFF
            payload = word.to_bytes(2, "big")

        try:
            cfg_tx = dict(
                freq=int(self.e_f_tx.get().strip()),
                sf=int(self.e_sf_tx.get().strip()),
                bw=int(self.e_bw_tx.get().strip()),
                cr=int(self.e_cr_tx.get().strip()),
                preamble=int(self.e_pre_tx.get().strip()),
                power=int(self.e_pwr_tx.get().strip()),
            )
            cfg_rx = dict(
                freq=int(self.e_f_rx.get().strip()),
                sf=int(self.e_sf_rx.get().strip()),
                bw=int(self.e_bw_rx.get().strip()),
                cr=int(self.e_cr_rx.get().strip()),
                preamble=int(self.e_pre_rx.get().strip()),
            )
        except ValueError:
            messagebox.showerror("Erro", "Parâmetro TX/RX inválido.")
            return

        if not self.testlog:
            messagebox.showerror("Erro", "Log não inicializado (conecte novamente).")
            return

        exec_tx = P2PExecution(
            self.device,
            self.testlog,
            self.seq,
            payload,
            timeout,
            retries,
            cfg_tx,
            cfg_rx,
            self.queue,
        )

        t = threading.Thread(target=exec_tx.run, daemon=True)
        t.start()

        self.seq += 1
        self.b_save.config(state="normal")

    # ---------------- RX CONTÍNUO -----------------
    @staticmethod
    def parse_line(line: str):
        parts = line.split(":")
        rssi = snr = None
        payload_hex = None

        if "RXP2P" in line.upper():
            try:
                idx = next(i for i, p in enumerate(parts) if p.upper() == "RXP2P")
                rssi = float(parts[idx + 1])
                snr = float(parts[idx + 2])
                payload_hex = parts[idx + 3].strip().upper()
            except Exception:
                pass

        return payload_hex, rssi, snr

    def start_rx(self):
        if not self.device or not self.is_connected:
            return
        if self.rx_running:
            return

        try:
            cfg_rx = dict(
                freq=int(self.e_f_rx.get().strip()),
                sf=int(self.e_sf_rx.get().strip()),
                bw=int(self.e_bw_rx.get().strip()),
                cr=int(self.e_cr_rx.get().strip()),
                preamble=int(self.e_pre_rx.get().strip()),
            )
            power_tx = int(self.e_pwr_tx.get().strip())  # AT+P2P exige power
        except ValueError:
            messagebox.showerror("Erro", "Parâmetros de RX inválidos.")
            return

        if not self.testlog:
            messagebox.showerror("Erro", "Log não inicializado (conecte novamente).")
            return

        self.rx_running = True
        self.b_rx_start.config(state="disabled")
        self.b_send.config(state="disabled")
        self.b_rx_stop.config(state="normal")
        self.b_save.config(state="normal")

        def rx_loop():
            self.queue.put(("log", f"== Iniciando RX contínuo com cfg_rx={cfg_rx} =="))

            try:
                cfg = cfg_rx.copy()
                cfg["power"] = power_tx

                self.device.flush_rx()
                self.device.configure_p2p(**cfg)
                self.device.send_at("AT+PRECV=65534", wait=0.2)

                while self.rx_running:
                    for line in self.device.read_until(1.0):
                        if not self.rx_running:
                            break
                        self.queue.put(("log", f"RX>> {line}"))
                        payload_hex, rssi, snr = self.parse_line(line)
                        if payload_hex:
                            result = PacketResult(
                                seq=-1,
                                payload_tx_hex="",
                                payload_rx_hex=payload_hex,
                                match=False,
                                attempts=1,
                                rssi=rssi,
                                snr=snr,
                                rtt_ms=None,
                                timeout=0.0,
                                retries=0,
                                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                mode="RX",
                                freq_tx_hz=None,
                                freq_rx_hz=int(cfg_rx.get("freq")) if cfg_rx.get("freq") is not None else None,
                            )
                            self.testlog.add(result)
                            self.queue.put(("log", f"<< Pacote RX salvo: HEX={payload_hex}, RSSI={rssi}, SNR={snr}, f_rx={result.freq_rx_hz}"))
                            self.queue.put(("log", "-" * 60))

            except Exception as e:
                self.queue.put(("log", f"Erro no RX contínuo: {e}"))
            finally:
                self.rx_running = False
                self.after(0, self._rx_stopped_ui)

        self.rx_thread = threading.Thread(target=rx_loop, daemon=True)
        self.rx_thread.start()

    def _rx_stopped_ui(self):
        self.b_rx_start.config(state="normal")
        self.b_rx_stop.config(state="disabled")
        self.b_send.config(state="normal")

    def stop_rx(self):
        if self.rx_running:
            self.rx_running = False
            self.log("== Parando RX contínuo... ==")

    # ---------------- CSV -----------------
    def save_csv(self):
        if not self.testlog or not self.testlog.results:
            messagebox.showwarning("Aviso", "Não há resultados para salvar.")
            return

        fname = filedialog.asksaveasfilename(defaultextension=".csv")
        if fname:
            try:
                self.testlog.save_csv(fname)
                messagebox.showinfo("OK", "CSV salvo com sucesso!")
            except Exception as e:
                messagebox.showerror("Erro", str(e))


if __name__ == "__main__":
    App().mainloop()

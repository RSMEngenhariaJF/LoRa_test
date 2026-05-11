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

#0x0100000050840000000000000000FF11400216
# ===========================================================
#   LOCALIZAÇÃO VIA IP
# ===========================================================
def get_current_location():
    try:
        import requests
        r = requests.get("http://ip-api.com/json/", timeout=3)
        j = r.json()
        if j.get("status") == "success":
            return float(j.get("lat")), float(j.get("lon"))
    except Exception:
        pass
    return None, None


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
            self.ser.close()
            self.ser = None

    def send_at(self, cmd, wait=0.4):
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Porta não aberta")
        cmd_tx = (cmd + "\r\n").encode()
        self.ser.write(cmd_tx)
        time.sleep(wait)
        return self.ser.read_all().decode(errors="ignore")

    def configure_p2p(self, freq, sf, bw, cr, preamble, power):
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
                    yield line.strip()
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
    lat: float | None
    lon: float | None
    mode: str  # "TX" ou "RX"


@dataclass
class TestLog:
    name: str
    start_time: str
    port: str
    baudrate: int
    results: list[PacketResult] = field(default_factory=list)

    def add(self, r: PacketResult):
        self.results.append(r)

    def save_csv(self, filename):
        fields = [
            "timestamp", "mode", "seq",
            "payload_tx_hex", "payload_rx_hex", "match",
            "attempts", "timeout", "retries",
            "rssi", "snr", "rtt_ms",
            "lat", "lon",
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
                    "lat": r.lat if r.lat is not None else "",
                    "lon": r.lon if r.lon is not None else "",
                    "port": self.port,
                    "baudrate": self.baudrate
                })


# ===========================================================
#   EXECUÇÃO DE UM ÚNICO PACOTE (TX + RX)
# ===========================================================
class P2PExecution:
    def __init__(self, device, testlog: TestLog, seq_value, payload,
                 timeout, retries, cfg_tx, cfg_rx, logqueue):
        self.device = device
        self.testlog = testlog
        self.seq_value = seq_value & 0xFFFF
        self.payload = payload
        self.timeout = timeout
        self.retries = retries
        self.cfg_tx = cfg_tx
        self.cfg_rx = cfg_rx
        self.log = logqueue

    def emit(self, msg):
        self.log.put(("log", msg))

    @staticmethod
    def parse_line(line: str):
        parts = line.split(":")
        rssi = snr = None
        payload_hex = None

        # Exemplo típico:
        # +EVT:RXP2P:-45:12:08:ABCD1122
        if len(parts) >= 5 and "RXP2P" in line.upper():
            try:
                payload_hex = parts[-1].strip().upper()
                rssi = float(parts[-4])
                snr = float(parts[-3])
            except Exception:
                pass

        return payload_hex, rssi, snr

    def run(self):
        payload_hex = self.payload.hex().upper()
        lat, lon = get_current_location()

        attempts = 0
        rx_hex = None
        match = False
        rssi = snr = None
        rtt_ms = None

        for attempt in range(self.retries + 1):
            attempts += 1

            self.emit(f"Config TX: {self.cfg_tx}")
            self.device.configure_p2p(**self.cfg_tx)

            t0 = time.time()
            self.emit(f"Enviando pacote TX seq={self.seq_value} HEX={payload_hex} (tentativa {attempts})")
            self.device.send_payload(self.payload)

            self.emit(f"Config RX: {self.cfg_rx}")
            rx_cfg_mod = self.cfg_rx.copy()
            rx_cfg_mod["power"] = self.cfg_tx["power"]
            self.device.configure_p2p(**rx_cfg_mod)

            # Aqui poderíamos usar AT+PRECV com timeout centrado no self.timeout
            # Ex: timeout em ms → int(self.timeout * 1000)
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
                    match = (rx_hex == payload_hex)
                    rssi = rxrssi
                    snr = rxsnr
                    rtt_ms = (time.time() - t0) * 1000.0
                    got = True
                    break

            if got:
                break
            else:
                self.emit(f"Timeout na tentativa {attempts}")

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
            lat=lat,
            lon=lon,
            mode="TX",
        )
        self.testlog.add(result)
        self.emit(f"<< Resultado TX: match={match}, rssi={rssi}, snr={snr}, rtt={rtt_ms}")


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

        # FAIXA DE CONTROLE / BOTÕES
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
    def log(self, msg):
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

        # payload
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

        if len(parts) >= 5 and "RXP2P" in line.upper():
            try:
                payload_hex = parts[-1].strip().upper()
                rssi = float(parts[-4])
                snr = float(parts[-3])
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
            power_tx = int(self.e_pwr_tx.get().strip())  # só para preencher o campo power
        except ValueError:
            messagebox.showerror("Erro", "Parâmetros de RX inválidos.")
            return

        self.rx_running = True
        self.b_rx_start.config(state="disabled")
        self.b_send.config(state="disabled")
        self.b_rx_stop.config(state="normal")

        def rx_loop():
            self.queue.put(("log", f"== Iniciando RX contínuo com cfg_rx={cfg_rx} =="))
            try:
                cfg = cfg_rx.copy()
                cfg["power"] = power_tx

                # Equivalente ao seu script:
                # AT+NWM=0
                # AT+P2P=904000000:11:500:0:10:14
                # AT+PRECV=65534
                self.device.configure_p2p(**cfg)
                self.device.send_at("AT+PRECV=65534", wait=0.2)

                while self.rx_running:
                    for line in self.device.read_until(1.0):
                        if not self.rx_running:
                            break
                        self.queue.put(("log", f"RX>> {line}"))
                        payload_hex, rssi, snr = self.parse_line(line)
                        if payload_hex:
                            lat, lon = get_current_location()
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
                                lat=lat,
                                lon=lon,
                                mode="RX",
                            )
                            if self.testlog:
                                self.testlog.add(result)
                            self.queue.put(("log", f"<< Pacote RX salvo: HEX={payload_hex}, RSSI={rssi}, SNR={snr}"))
                            self.queue.put(("log", "-" * 60))
            except Exception as e:
                self.queue.put(("log", f"Erro no RX contínuo: {e}"))
            finally:
                self.rx_running = False
                self.after(0, self._rx_stopped_ui)

        self.rx_thread = threading.Thread(target=rx_loop, daemon=True)
        self.rx_thread.start()
        self.b_save.config(state="normal")

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

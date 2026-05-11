from __future__ import annotations

import csv
import json
import re
import time
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import serial
from serial.tools import list_ports


# ============================================================
# Utilitários
# ============================================================
HEX_PAIR_RE = re.compile(r"^[0-9A-Fa-f]{2}$")

def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def safe_int(s: str, default: int | None = None) -> int | None:
    try:
        return int(str(s).strip())
    except Exception:
        return default

def safe_float(s: str, default: float | None = None) -> float | None:
    try:
        return float(str(s).strip())
    except Exception:
        return default

def hex_compact_to_colon(hex_compact: str) -> str:
    """
    "0102A0" -> "01:02:A0"
    """
    h = hex_compact.strip()
    if h.startswith(("0x", "0X")):
        h = h[2:]
    h = re.sub(r"[^0-9A-Fa-f]", "", h)  # remove qualquer lixo
    if len(h) % 2 == 1:
        # se vier ímpar, completa à esquerda com 0 (raro)
        h = "0" + h
    pairs = [h[i:i+2].upper() for i in range(0, len(h), 2)]
    return ":".join(pairs)

def norm_hex_payload(s: str) -> str:
    """
    Normaliza payload HEX em:
      - 0x0102... (compacto)
      - 01:02:...
      - com espaços/virgulas/etc
    para sempre "AA:BB:CC:..."
    """
    if not s:
        return ""
    raw = s.strip()

    # formato 0x.... ou hex contínuo longo
    if raw.startswith(("0x", "0X")):
        return hex_compact_to_colon(raw)

    # se parece hex contínuo (sem :)
    only_hex = re.sub(r"[^0-9A-Fa-f]", "", raw)
    if len(only_hex) >= 2 and len(only_hex) == len(raw) and (len(only_hex) % 2 == 0):
        return hex_compact_to_colon(raw)

    # formato com separadores
    raw = raw.replace(",", ":").replace(" ", ":")
    parts = [p for p in raw.split(":") if p != ""]
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if len(p) == 0:
            continue
        # aceita 1 ou 2 dígitos, pad com zero
        if re.fullmatch(r"[0-9A-Fa-f]{1,2}", p):
            out.append(p.upper().zfill(2))
        else:
            # fallback: tenta tratar como compacto
            return hex_compact_to_colon(raw)
    return ":".join(out)

def looks_like_hex_payload(s: str) -> bool:
    n = norm_hex_payload(s)
    if not n:
        return False
    parts = [p for p in n.split(":") if p]
    return all(HEX_PAIR_RE.match(p) for p in parts)


# ============================================================
# Config de cada frequência
# ============================================================
@dataclass
class FreqProfile:
    freq_khz: int = 915200
    power_dbm: int = 12
    bw_khz: int = 125
    sf: int = 9
    cr: str = "4/8"
    lna_state: int = 0
    pa_boost: int = 0


# ============================================================
# Driver AT (serial)
# ============================================================
class ATLink:
    def __init__(self, port: str, baudrate: int = 115200, timeout_s: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout_s = timeout_s
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()

    def open(self):
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout_s)
        time.sleep(0.2)
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

    def _write_line(self, line: str):
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Porta serial não está aberta")
        data = (line.strip() + "\r\n").encode("utf-8", errors="ignore")
        self.ser.write(data)
        self.ser.flush()

    def _read_line(self) -> str:
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Porta serial não está aberta")
        b = self.ser.readline()
        if not b:
            return ""
        try:
            return b.decode("utf-8", errors="ignore").rstrip("\r\n")
        except Exception:
            return str(b)

    def send_cmd_collect(self, cmd: str, deadline_s: float = 2.0) -> list[str]:
        end = time.time() + max(0.1, deadline_s)
        lines: list[str] = []
        with self.lock:
            self._write_line(cmd)
            while time.time() < end:
                line = self._read_line()
                if line == "":
                    continue
                lines.append(line)

                u = line.strip().upper()
                if u in {"OK", "AT_ERROR", "AT_PARAM_ERROR", "AT_BUSY_ERROR", "AT_RX_ERROR", "AT_TEST_PARAM_OVERFLOW"}:
                    break
        return lines


# ============================================================
# Worker (varredura + buffer de log)
# ============================================================
@dataclass
class LogRow:
    timestamp: str
    freq_khz: int
    direction: str          # "RX" / "TX"
    rssi_dbm: float | None
    snr_db: float | None
    payload: str
    match: bool

class ScannerWorker:
    def __init__(
        self,
        at: ATLink,
        profiles: list[FreqProfile],
        dwell_s: float,
        expected_mode: str,      # "HEX" ou "TEXT"
        expected_payload: str,
        ui_log_fn,
        on_new_logrow_fn,        # callback para atualizar contador, etc
    ):
        self.at = at
        self.profiles = profiles
        self.dwell_s = max(0.2, float(dwell_s))
        self.expected_mode = expected_mode.upper().strip()
        self.expected_payload = expected_payload.strip()

        self.ui_log_fn = ui_log_fn
        self.on_new_logrow_fn = on_new_logrow_fn

        self.stop_evt = threading.Event()
        self.thread: threading.Thread | None = None

        # log em memória (salva só quando usuário pedir)
        self.log_rows: list[LogRow] = []
        self.log_lock = threading.Lock()

        # expected normalizado
        if self.expected_mode == "HEX":
            self.expected_norm = norm_hex_payload(self.expected_payload)
        else:
            self.expected_norm = self.expected_payload

    def start(self):
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        try:
            self.at.send_cmd_collect("AT+TOFF", deadline_s=1.5)
        except Exception:
            pass

    def clear_log(self):
        with self.log_lock:
            self.log_rows.clear()

    def export_log_csv(self, path: str):
        with self.log_lock:
            rows = list(self.log_rows)

        with open(path, "w", newline="", encoding="utf-8") as fp:
            w = csv.writer(fp)
            w.writerow(["timestamp", "freq_khz", "dir", "rssi_dbm", "snr_db", "payload", "match"])
            for r in rows:
                w.writerow([r.timestamp, r.freq_khz, r.direction, r.rssi_dbm, r.snr_db, r.payload, int(r.match)])

    def _ui(self, msg: str):
        try:
            self.ui_log_fn(msg)
        except Exception:
            pass

    def _push_log(self, row: LogRow):
        with self.log_lock:
            self.log_rows.append(row)
            n = len(self.log_rows)
        try:
            self.on_new_logrow_fn(n)
        except Exception:
            pass

    def _set_tconf(self, p: FreqProfile) -> bool:
        cmd = f"AT+TCONF={p.freq_khz}:{p.power_dbm}:{p.bw_khz}:{p.sf}:{p.cr}:{p.lna_state}:{p.pa_boost}"
        lines = self.at.send_cmd_collect(cmd, deadline_s=2.0)
        ok = any(l.strip().upper() == "OK" for l in lines)
        if not ok:
            self._ui(f"[TCONF FAIL] {cmd} -> {lines}")
        return ok

    def _parse_rx_lines(self, lines: list[str]) -> tuple[float | None, float | None, str]:
        joined = " ".join(lines)
        rssi = None
        snr = None
        payload = ""

        m_rssi = re.search(r"RSSI\s*=\s*([-]?\d+(?:\.\d+)?)", joined, flags=re.IGNORECASE)
        if m_rssi:
            rssi = safe_float(m_rssi.group(1))

        m_snr = re.search(r"SNR\s*=\s*([-]?\d+(?:\.\d+)?)", joined, flags=re.IGNORECASE)
        if m_snr:
            snr = safe_float(m_snr.group(1))

        m_pay = re.search(r"Rx\s*hex\s*->\s*(.+)$", joined, flags=re.IGNORECASE)
        if m_pay:
            payload = m_pay.group(1).strip()
        else:
            m_pay2 = re.search(r"Text\s*->\s*(.+)$", joined, flags=re.IGNORECASE)
            if m_pay2:
                payload = m_pay2.group(1).strip()

        # Se vier payload como "0x...." ou compacto, normaliza (modo HEX usa isso)
        return rssi, snr, payload

    def _payload_match(self, payload: str) -> tuple[bool, str]:
        if self.expected_mode == "HEX":
            p_norm = norm_hex_payload(payload)
            return (p_norm == self.expected_norm), p_norm
        else:
            return (payload.strip() == self.expected_norm), payload.strip()

    def _tx_echo(self, freq_khz: int, payload_norm: str):
        """
        Para HEX: usa AT+TXLRAH=<freq>:0:<AA:BB:...>
        Para TEXT: converte para bytes UTF-8 e manda como HEX.
        """
        if self.expected_mode == "HEX":
            if not looks_like_hex_payload(payload_norm):
                self._ui(f"[TX SKIP] payload não parece HEX válido: {payload_norm}")
                return
            cmd = f"AT+TXLRAH={freq_khz}:0:{payload_norm}"
        else:
            b = payload_norm.encode("utf-8", errors="ignore")[:32]
            hexs = ":".join(f"{x:02X}" for x in b)
            cmd = f"AT+TXLRAH={freq_khz}:0:{hexs}"

        lines = self.at.send_cmd_collect(cmd, deadline_s=2.0)
        ok = any(l.strip().upper() == "OK" for l in lines)
        self._ui(f"[TX] {cmd} -> {'OK' if ok else lines}")

    def _run(self):
        idx = 0
        self._ui("[INFO] Scanner iniciado.")

        while not self.stop_evt.is_set():
            p = self.profiles[idx % len(self.profiles)]
            idx += 1

            self._ui(f"\n=== FREQ {p.freq_khz} kHz | dwell={self.dwell_s:.1f}s ===")

            if not self._set_tconf(p):
                time.sleep(0.2)
                continue

            t_end = time.time() + self.dwell_s
            while time.time() < t_end and not self.stop_evt.is_set():
                rx_cmd = f"AT+RXLRAH={p.freq_khz}:0"
                lines = self.at.send_cmd_collect(rx_cmd, deadline_s=min(2.5, self.dwell_s))
                if not lines:
                    continue

                status = (lines[-1].strip().upper() if lines else "")
                if status in {"AT_ERROR", "AT_PARAM_ERROR", "AT_BUSY_ERROR"}:
                    self._ui(f"[RX ERR] {rx_cmd} -> {lines}")
                    time.sleep(0.1)
                    continue

                rssi, snr, payload = self._parse_rx_lines(lines)
                match, payload_norm = self._payload_match(payload)

                self._ui(f"[RX] freq={p.freq_khz} rssi={rssi} snr={snr} payload='{payload_norm}' match={match}")

                self._push_log(LogRow(
                    timestamp=now_iso(),
                    freq_khz=p.freq_khz,
                    direction="RX",
                    rssi_dbm=rssi,
                    snr_db=snr,
                    payload=payload_norm,
                    match=match
                ))

                if match:
                    self._tx_echo(p.freq_khz, payload_norm)
                    self._push_log(LogRow(
                        timestamp=now_iso(),
                        freq_khz=p.freq_khz,
                        direction="TX",
                        rssi_dbm=None,
                        snr_db=None,
                        payload=payload_norm,
                        match=True
                    ))

                time.sleep(0.05)

        self._ui("[INFO] Scanner finalizado.")


# ============================================================
# GUI
# ============================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LoRa P2P Scanner (3 frequências) - SMW-SX1262M0")
        self.geometry("1020x740")

        self.worker: ScannerWorker | None = None
        self.at: ATLink | None = None

        self.log_count_var = tk.StringVar(value="0")

        self._build_ui()
        self._refresh_ports()

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="COM:").grid(row=0, column=0, sticky="w")
        self.cb_port = ttk.Combobox(top, width=18, values=[])
        self.cb_port.grid(row=0, column=1, sticky="w", padx=5)
        ttk.Button(top, text="Atualizar", command=self._refresh_ports).grid(row=0, column=2, padx=5)

        ttk.Label(top, text="Baud:").grid(row=0, column=3, sticky="w", padx=(20, 0))
        self.en_baud = ttk.Entry(top, width=10)
        self.en_baud.insert(0, "115200")
        self.en_baud.grid(row=0, column=4, sticky="w", padx=5)

        ttk.Label(top, text="Timeout (s):").grid(row=0, column=5, sticky="w", padx=(20, 0))
        self.en_timeout = ttk.Entry(top, width=8)
        self.en_timeout.insert(0, "1.0")
        self.en_timeout.grid(row=0, column=6, sticky="w", padx=5)

        ttk.Label(top, text="Expected mode:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cb_mode = ttk.Combobox(top, width=10, values=["HEX", "TEXT"], state="readonly")
        self.cb_mode.set("HEX")
        self.cb_mode.grid(row=1, column=1, sticky="w", padx=5, pady=(8, 0))

        ttk.Label(top, text="Expected payload:").grid(row=1, column=3, sticky="w", padx=(20, 0), pady=(8, 0))
        self.en_expected = ttk.Entry(top, width=60)
        self.en_expected.insert(0, "0x0100000050840000000000000000FF11400216")
        self.en_expected.grid(row=1, column=4, columnspan=3, sticky="w", padx=5, pady=(8, 0))

        ttk.Label(top, text="Dwell por freq (s):").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.en_dwell = ttk.Entry(top, width=10)
        self.en_dwell.insert(0, "5.0")
        self.en_dwell.grid(row=2, column=1, sticky="w", padx=5, pady=(8, 0))

        # Contador de log em memória
        ttk.Label(top, text="Log em memória:").grid(row=2, column=3, sticky="w", padx=(20, 0), pady=(8, 0))
        ttk.Label(top, textvariable=self.log_count_var).grid(row=2, column=4, sticky="w", pady=(8, 0))

        # Perfis
        prof = ttk.LabelFrame(self, text="Perfis (3 frequências) - AT+TCONF")
        prof.pack(fill="x", padx=10, pady=10)

        headers = ["freq_khz", "power_dbm", "bw_khz", "sf", "cr(4/5..4/8)", "lna", "pa_boost"]
        for c, h in enumerate(headers):
            ttk.Label(prof, text=h).grid(row=0, column=c, padx=5, pady=2)

        self.prof_entries: list[list[ttk.Entry]] = []
        defaults = [
            (913000, 20, 500, 11, "4/5", 0, 0),
            (915000, 20, 500, 11, "4/5", 0, 0),
            (928000, 20, 500, 11, "4/5", 0, 0),
        ]
        for r in range(3):
            row_entries = []
            for c in range(7):
                e = ttk.Entry(prof, width=14)
                e.insert(0, str(defaults[r][c]))
                e.grid(row=r + 1, column=c, padx=5, pady=2)
                row_entries.append(e)
            self.prof_entries.append(row_entries)

        # Botões principais
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=5)

        self.bt_start = ttk.Button(btns, text="Start", command=self._start)
        self.bt_start.pack(side="left", padx=5)

        self.bt_stop = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.bt_stop.pack(side="left", padx=5)

        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)

        self.bt_save_log = ttk.Button(btns, text="Salvar log…", command=self._save_log, state="disabled")
        self.bt_save_log.pack(side="left", padx=5)

        self.bt_clear_log = ttk.Button(btns, text="Limpar log", command=self._clear_log, state="disabled")
        self.bt_clear_log.pack(side="left", padx=5)

        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)

        self.bt_save_cfg = ttk.Button(btns, text="Salvar configuração…", command=self._save_cfg)
        self.bt_save_cfg.pack(side="left", padx=5)

        self.bt_load_cfg = ttk.Button(btns, text="Carregar configuração…", command=self._load_cfg)
        self.bt_load_cfg.pack(side="left", padx=5)

        # Console
        out = ttk.LabelFrame(self, text="Console")
        out.pack(fill="both", expand=True, padx=10, pady=10)

        self.txt = scrolledtext.ScrolledText(out, wrap=tk.WORD, height=22)
        self.txt.pack(fill="both", expand=True, padx=8, pady=8)

    def _refresh_ports(self):
        ports = [p.device for p in list_ports.comports()]
        self.cb_port["values"] = ports
        if ports and not self.cb_port.get():
            self.cb_port.set(ports[0])

    def _ui_log(self, msg: str):
        def _append():
            self.txt.insert(tk.END, msg + "\n")
            self.txt.see(tk.END)
        self.after(0, _append)

    def _on_new_logrow(self, n: int):
        def _upd():
            self.log_count_var.set(str(n))
        self.after(0, _upd)

    def _read_profiles(self) -> list[FreqProfile]:
        profs: list[FreqProfile] = []
        for row in self.prof_entries:
            freq = safe_int(row[0].get(), 915200) or 915200
            power = safe_int(row[1].get(), 12) or 12
            bw = safe_int(row[2].get(), 125) or 125
            sf = safe_int(row[3].get(), 9) or 9
            cr = row[4].get().strip() or "4/8"
            lna = safe_int(row[5].get(), 0) or 0
            pa = safe_int(row[6].get(), 0) or 0
            profs.append(FreqProfile(freq, power, bw, sf, cr, lna, pa))
        return profs

    def _apply_profiles_to_ui(self, profiles: list[FreqProfile]):
        for r in range(min(3, len(profiles))):
            p = profiles[r]
            vals = [p.freq_khz, p.power_dbm, p.bw_khz, p.sf, p.cr, p.lna_state, p.pa_boost]
            for c in range(7):
                e = self.prof_entries[r][c]
                e.delete(0, tk.END)
                e.insert(0, str(vals[c]))

    # -------------------------
    # Start / Stop
    # -------------------------
    def _start(self):
        if self.worker is not None:
            return

        port = self.cb_port.get().strip()
        if not port:
            messagebox.showerror("Erro", "Selecione uma porta COM.")
            return

        baud = safe_int(self.en_baud.get(), 115200) or 115200
        tout = safe_float(self.en_timeout.get(), 1.0) or 1.0
        dwell = safe_float(self.en_dwell.get(), 5.0) or 5.0

        expected_mode = self.cb_mode.get().strip() or "HEX"
        expected_payload = self.en_expected.get().strip()

        profiles = self._read_profiles()

        try:
            self.at = ATLink(port, baudrate=baud, timeout_s=tout)
            self.at.open()
        except Exception as e:
            self.at = None
            messagebox.showerror("Erro", f"Falha abrindo serial: {e}")
            return

        self.worker = ScannerWorker(
            at=self.at,
            profiles=profiles,
            dwell_s=dwell,
            expected_mode=expected_mode,
            expected_payload=expected_payload,
            ui_log_fn=self._ui_log,
            on_new_logrow_fn=self._on_new_logrow
        )

        self.worker.start()

        self.bt_start.config(state="disabled")
        self.bt_stop.config(state="normal")
        self.bt_save_log.config(state="normal")
        self.bt_clear_log.config(state="normal")

        self._ui_log(f"[INFO] Serial aberta: {port} @ {baud} bps")
        self._ui_log(f"[INFO] Expected ({expected_mode}) = {expected_payload}")
        if expected_mode.upper() == "HEX":
            self._ui_log(f"[INFO] Expected norm = {norm_hex_payload(expected_payload)}")

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.worker = None

        if self.at:
            try:
                self.at.close()
            except Exception:
                pass
            self.at = None

        self.bt_start.config(state="normal")
        self.bt_stop.config(state="disabled")
        self._ui_log("[INFO] Stop pressionado. Porta fechada.")

    # -------------------------
    # Log: salvar / limpar
    # -------------------------
    def _save_log(self):
        if not self.worker:
            messagebox.showwarning("Aviso", "Inicie o scanner para gerar log em memória.")
            return
        if int(self.log_count_var.get() or "0") == 0:
            messagebox.showwarning("Aviso", "Log vazio.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return

        try:
            self.worker.export_log_csv(path)
            self._ui_log(f"[INFO] Log salvo em: {path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha salvando log: {e}")

    def _clear_log(self):
        if not self.worker:
            self.log_count_var.set("0")
            return
        self.worker.clear_log()
        self.log_count_var.set("0")
        self._ui_log("[INFO] Log em memória limpo.")

    # -------------------------
    # Config: salvar / carregar
    # -------------------------
    def _collect_config(self) -> dict[str, Any]:
        cfg = {
            "port": self.cb_port.get().strip(),
            "baud": safe_int(self.en_baud.get(), 115200) or 115200,
            "timeout_s": safe_float(self.en_timeout.get(), 1.0) or 1.0,
            "dwell_s": safe_float(self.en_dwell.get(), 5.0) or 5.0,
            "expected_mode": self.cb_mode.get().strip() or "HEX",
            "expected_payload": self.en_expected.get().strip(),
            "profiles": [asdict(p) for p in self._read_profiles()],
        }
        return cfg

    def _apply_config(self, cfg: dict[str, Any]):
        port = str(cfg.get("port", "")).strip()
        baud = cfg.get("baud", 115200)
        timeout_s = cfg.get("timeout_s", 1.0)
        dwell_s = cfg.get("dwell_s", 5.0)
        expected_mode = str(cfg.get("expected_mode", "HEX")).strip().upper()
        expected_payload = str(cfg.get("expected_payload", "")).strip()

        if port:
            self.cb_port.set(port)
        self.en_baud.delete(0, tk.END); self.en_baud.insert(0, str(baud))
        self.en_timeout.delete(0, tk.END); self.en_timeout.insert(0, str(timeout_s))
        self.en_dwell.delete(0, tk.END); self.en_dwell.insert(0, str(dwell_s))

        if expected_mode not in ("HEX", "TEXT"):
            expected_mode = "HEX"
        self.cb_mode.set(expected_mode)
        self.en_expected.delete(0, tk.END); self.en_expected.insert(0, expected_payload)

        prof_list = cfg.get("profiles", [])
        profiles: list[FreqProfile] = []
        for item in prof_list[:3]:
            try:
                profiles.append(FreqProfile(
                    freq_khz=int(item.get("freq_khz", 915200)),
                    power_dbm=int(item.get("power_dbm", 12)),
                    bw_khz=int(item.get("bw_khz", 125)),
                    sf=int(item.get("sf", 9)),
                    cr=str(item.get("cr", "4/8")),
                    lna_state=int(item.get("lna_state", 0)),
                    pa_boost=int(item.get("pa_boost", 0)),
                ))
            except Exception:
                pass
        if profiles:
            while len(profiles) < 3:
                profiles.append(FreqProfile())
            self._apply_profiles_to_ui(profiles)

    def _save_cfg(self):
        cfg = self._collect_config()
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(cfg, fp, indent=2, ensure_ascii=False)
            self._ui_log(f"[INFO] Configuração salva em: {path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha salvando configuração: {e}")

    def _load_cfg(self):
        if self.worker is not None:
            messagebox.showwarning("Aviso", "Pare o scanner (Stop) antes de carregar configuração.")
            return

        path = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            self._apply_config(cfg)
            self._ui_log(f"[INFO] Configuração carregada: {path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Falha carregando configuração: {e}")

    def on_close(self):
        try:
            self._stop()
        finally:
            self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()

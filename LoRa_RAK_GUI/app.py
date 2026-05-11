"""
Classe App — janela principal Tkinter da aplicação.

Toda a UI (menubar, abas, handlers de evento, workers) vive aqui.
O entry point é lora_rak_gui.py (apenas instancia App e roda mainloop).

Dependências internas:
  - constants: metadados da app, CR_LABELS, mhz_to_hz/hz_to_mhz, caminhos
  - logger:    SessionLogger, PacketResult
  - drivers:   DEVICES, LoRaDevice (camada de driver por chip)
  - lorawan:   protocolo LoRaWAN para o Gateway simulado (opcional)
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from serial.tools import list_ports

from constants import (
    APP_NAME, APP_VERSION, APP_AUTHOR, APP_YEAR, APP_GITHUB, APP_DESCRIPTION,
    LOGS_DIR, DEVICES_JSON_PATH, CR_LABELS, mhz_to_hz, hz_to_mhz,
)
from logger import SessionLogger, PacketResult
from drivers import DEVICES, LoRaDevice

# Módulo LoRaWAN é opcional (depende de 'cryptography')
try:
    from lorawan import (
        BANDS, JoinRequest, build_join_accept, derive_session_keys,
        DataFrame, build_downlink, hex_to_bytes, bytes_to_hex,
        random_bytes, parse_mhdr, MTYPE_JOIN_REQUEST,
        MTYPE_UNCONF_DATA_UP, MTYPE_CONF_DATA_UP,
    )
    LORAWAN_AVAILABLE = True
    _LORAWAN_IMPORT_ERROR = ""
except ImportError as _e:
    LORAWAN_AVAILABLE = False
    _LORAWAN_IMPORT_ERROR = str(_e)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} — Testes via UART")
        self.geometry("1280x900")
        self.minsize(1000, 700)

        self.queue: queue.Queue = queue.Queue()
        self.device: LoRaDevice | None = None
        self.is_connected = False

        self.logger = SessionLogger(log_queue=self.queue)

        # Estado de operações
        self.seq_counter = 0
        self.rx_running = False
        self.scan_running = False
        self.cw_running = False
        self.incr_running = False
        # Gateway LoRaWAN
        self.gw_running = False
        self.gw_thread: threading.Thread | None = None
        self.devices_db: list[dict] = []
        self._next_dev_addr = 0x01000001
        self._load_devices_json()

        self._build_ui()
        self._on_device_change()  # ajusta defaults
        self.after(100, self._process_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -----------------------------------------------------------
    #   UI principal
    # -----------------------------------------------------------
    def _build_ui(self):
        # === Menubar (Ferramentas + Sobre) ===
        self._build_menubar()

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

        ttk.Label(top, text="Modo:").grid(row=0, column=7, padx=4, pady=4, sticky="e")
        self.cb_pnm = ttk.Combobox(
            top, width=18, state="readonly",
            values=["Privado (P2P, 0x12)", "Público (LoRaWAN, 0x34)"],
        )
        self.cb_pnm.set("Privado (P2P, 0x12)")
        self.cb_pnm.grid(row=0, column=8, padx=4, pady=4)
        self.cb_pnm.bind("<<ComboboxSelected>>", lambda _e: self._on_pnm_change())

        self.b_connect = ttk.Button(top, text="Abrir UART", command=self._connect)
        self.b_connect.grid(row=0, column=9, padx=10, pady=4)
        self.b_test = ttk.Button(top, text="Testar dispositivo", command=self._test_device, state="disabled")
        self.b_test.grid(row=0, column=10, padx=4, pady=4)
        self.b_disconnect = ttk.Button(top, text="Desconectar", command=self._disconnect, state="disabled")
        self.b_disconnect.grid(row=0, column=11, padx=4, pady=4)

        sf_status = ttk.Frame(top)
        sf_status.grid(row=1, column=0, columnspan=12, padx=4, pady=2, sticky="w")
        ttk.Label(sf_status, text="Porta UART:").pack(side="left", padx=(0, 4))
        self.lbl_uart_status = ttk.Label(sf_status, text="fechada", foreground="red")
        self.lbl_uart_status.pack(side="left", padx=(0, 15))
        ttk.Label(sf_status, text="Dispositivo:").pack(side="left", padx=(0, 4))
        self.lbl_dev_status = ttk.Label(sf_status, text="não testado", foreground="gray")
        self.lbl_dev_status.pack(side="left", padx=(0, 15))

        self.lbl_caps = ttk.Label(top, text="", foreground="blue")
        self.lbl_caps.grid(row=2, column=0, columnspan=12, padx=4, pady=2, sticky="w")

        # === PanedWindow vertical: notebook em cima, log embaixo ===
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=5)

        nb_frame = ttk.Frame(paned)
        paned.add(nb_frame, weight=3)
        self.nb = ttk.Notebook(nb_frame)
        self.nb.pack(fill="both", expand=True)

        self._build_tab_tx()
        self._build_tab_rx()
        self._build_tab_incr()
        self._build_tab_scanner()
        self._build_tab_cw()
        self._build_tab_end_device()
        self._build_tab_devices()
        self._build_tab_gateway()
        self._build_tab_console()
        self._populate_tools_menu()

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

        log_container = ttk.Frame(bottom)
        log_container.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt_log = tk.Text(log_container, wrap="word", height=14)
        log_scroll = ttk.Scrollbar(log_container, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=log_scroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self._log_widgets: list[tk.Text] = [self.txt_log]
        self._log_window: tk.Toplevel | None = None

        self._refresh_ports()

    # -----------------------------------------------------------
    #   Menubar
    # -----------------------------------------------------------
    def _build_menubar(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Abrir pasta de logs", command=self._open_logs_folder)
        file_menu.add_command(label="Abrir log debug atual", command=self._open_debug_log)
        file_menu.add_separator()
        file_menu.add_command(label="Sair", accelerator="Alt+F4", command=self._on_close)
        menubar.add_cascade(label="Arquivo", menu=file_menu)

        self._tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Ferramentas", menu=self._tools_menu)

        about_menu = tk.Menu(menubar, tearoff=0)
        about_menu.add_command(label=f"Sobre {APP_NAME}...", command=self._show_about)
        menubar.add_cascade(label="Sobre", menu=about_menu)

        self.config(menu=menubar)

    def _populate_tools_menu(self):
        items = [
            ("TX único",              self.tab_tx),
            ("RX contínuo",           self.tab_rx),
            ("Pacote incremental",    self.tab_incr),
            ("Scanner (3 freq)",      self.tab_scan),
            ("Portadora CW",          self.tab_cw),
            None,
            ("End-Device LoRaWAN",    self.tab_end_device),
            ("Dispositivos LoRaWAN",  self.tab_devices),
            ("Gateway LoRaWAN",       self.tab_gw),
            None,
            ("Console AT manual",     self.tab_console),
        ]
        for item in items:
            if item is None:
                self._tools_menu.add_separator()
                continue
            label, tab = item
            self._tools_menu.add_command(
                label=label,
                command=lambda t=tab: self.nb.select(t),
            )

    def _show_about(self):
        dlg = tk.Toplevel(self)
        dlg.title(f"Sobre — {APP_NAME}")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        ttk.Label(dlg, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(pady=(20, 4), padx=30)
        ttk.Label(dlg, text=f"Versão {APP_VERSION}", foreground="gray").pack(padx=30)
        ttk.Separator(dlg, orient="horizontal").pack(fill="x", padx=30, pady=15)

        info = ttk.Frame(dlg)
        info.pack(fill="x", padx=30)

        def row(label: str, value: str, mono: bool = False):
            f = ttk.Frame(info); f.pack(fill="x", pady=3)
            ttk.Label(f, text=label, width=15, anchor="w",
                      font=("Segoe UI", 9, "bold")).pack(side="left")
            font = ("Consolas", 9) if mono else ("Segoe UI", 9)
            ttk.Label(f, text=value, anchor="w", font=font).pack(side="left", fill="x", expand=True)

        row("Autor:",        APP_AUTHOR)
        row("Ano:",          APP_YEAR)
        row("Dispositivos:", "RAK3172, Quectel KG200Z, SMART SMW-SX1262M0")
        row("Modos:",        "P2P, End-Device LoRaWAN, Gateway LoRaWAN (OTAA)")
        row("Dependências:", "Python 3.10+, pyserial, cryptography")
        row("Repositório:",  APP_GITHUB, mono=True)

        ttk.Separator(dlg, orient="horizontal").pack(fill="x", padx=30, pady=15)

        ttk.Label(
            dlg, text=APP_DESCRIPTION,
            justify="left", wraplength=480,
        ).pack(padx=30, pady=(0, 10))

        ttk.Button(dlg, text="OK", command=dlg.destroy, width=12).pack(pady=15)

        dlg.update_idletasks()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        x = self.winfo_x() + (self.winfo_width() // 2) - (w // 2)
        y = self.winfo_y() + (self.winfo_height() // 2) - (h // 2)
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")

        dlg.bind("<Return>", lambda _e: dlg.destroy())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    # -----------------------------------------------------------
    #   Abas P2P (TX/RX/Incremental/Scanner/CW)
    # -----------------------------------------------------------
    def _build_tab_tx(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="TX único")
        self.tab_tx = tab

        ttk.Label(tab, text="Mensagem (texto ou 0xHEX):").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.e_msg = ttk.Entry(tab, width=80)
        self.e_msg.insert(0, "0x0100000050840000000000000000FF11400216")
        self.e_msg.grid(row=0, column=1, columnspan=10, padx=4, pady=4, sticky="w")

        ttk.Label(tab, text="Timeout RX (s):").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.e_tx_timeout = ttk.Entry(tab, width=8); self.e_tx_timeout.insert(0, "5")
        self.e_tx_timeout.grid(row=1, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(tab, text="Reenvios:").grid(row=1, column=2, sticky="e", padx=4, pady=4)
        self.e_tx_retries = ttk.Entry(tab, width=8); self.e_tx_retries.insert(0, "2")
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
        tab = ttk.Frame(self.nb); self.nb.add(tab, text="RX contínuo"); self.tab_rx = tab
        f_rx = ttk.LabelFrame(tab, text="Configuração RX"); f_rx.pack(fill="x", padx=4, pady=4)
        self.rx_cont_entries = self._make_p2p_inputs(
            f_rx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=True
        )
        bf = ttk.Frame(tab); bf.pack(fill="x", padx=4, pady=4)
        self.b_rx_start = ttk.Button(bf, text="Iniciar RX contínuo", command=self._start_rx, state="disabled")
        self.b_rx_start.pack(side="left", padx=4)
        self.b_rx_stop = ttk.Button(bf, text="Parar RX", command=self._stop_rx, state="disabled")
        self.b_rx_stop.pack(side="left", padx=4)

    def _build_tab_incr(self):
        tab = ttk.Frame(self.nb); self.nb.add(tab, text="Pacote incremental 16 bits"); self.tab_incr = tab
        ttk.Label(tab, text="Envia palavras de 16 bits incrementais (0x0000, 0x0001, ...).").pack(anchor="w", padx=4, pady=4)
        cfg = ttk.Frame(tab); cfg.pack(fill="x", padx=4, pady=4)
        ttk.Label(cfg, text="Intervalo (s):").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        self.e_incr_interval = ttk.Entry(cfg, width=8); self.e_incr_interval.insert(0, "5")
        self.e_incr_interval.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(cfg, text="Quantidade (0=infinito):").grid(row=0, column=2, sticky="e", padx=4, pady=2)
        self.e_incr_count = ttk.Entry(cfg, width=8); self.e_incr_count.insert(0, "0")
        self.e_incr_count.grid(row=0, column=3, sticky="w", padx=4, pady=2)
        ttk.Label(cfg, text="Timeout RX (s):").grid(row=0, column=4, sticky="e", padx=4, pady=2)
        self.e_incr_timeout = ttk.Entry(cfg, width=8); self.e_incr_timeout.insert(0, "3")
        self.e_incr_timeout.grid(row=0, column=5, sticky="w", padx=4, pady=2)

        f_tx = ttk.LabelFrame(tab, text="Configuração TX"); f_tx.pack(fill="x", padx=4, pady=4)
        self.incr_tx_entries = self._make_p2p_inputs(
            f_tx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=True
        )
        f_rx = ttk.LabelFrame(tab, text="Configuração RX"); f_rx.pack(fill="x", padx=4, pady=4)
        self.incr_rx_entries = self._make_p2p_inputs(
            f_rx, defaults=("904", "11", "500", "4/5", "10", "14"), with_power=False
        )

        bf = ttk.Frame(tab); bf.pack(fill="x", padx=4, pady=4)
        self.b_incr_start = ttk.Button(bf, text="Iniciar incremental", command=self._start_incr, state="disabled")
        self.b_incr_start.pack(side="left", padx=4)
        self.b_incr_stop = ttk.Button(bf, text="Parar", command=self._stop_incr, state="disabled")
        self.b_incr_stop.pack(side="left", padx=4)
        self.lbl_incr_seq = ttk.Label(bf, text="Próximo seq: 0x0000")
        self.lbl_incr_seq.pack(side="left", padx=15)
        ttk.Button(bf, text="Resetar contador", command=self._reset_incr).pack(side="left", padx=4)

    def _build_tab_scanner(self):
        tab = ttk.Frame(self.nb); self.nb.add(tab, text="Scanner (3 freq)"); self.tab_scan = tab
        ttk.Label(tab, text="Cicla por 3 frequências, escutando dwell s em cada.").pack(anchor="w", padx=4, pady=4)
        cfg = ttk.Frame(tab); cfg.pack(fill="x", padx=4, pady=4)
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

        f_freq = ttk.LabelFrame(tab, text="Frequências (MHz)"); f_freq.pack(fill="x", padx=4, pady=4)
        ttk.Label(f_freq, text="Freq 1:").grid(row=0, column=0, padx=4, pady=2)
        self.e_scan_f1 = ttk.Entry(f_freq, width=10); self.e_scan_f1.insert(0, "903"); self.e_scan_f1.grid(row=0, column=1, padx=4, pady=2)
        ttk.Label(f_freq, text="Freq 2:").grid(row=0, column=2, padx=4, pady=2)
        self.e_scan_f2 = ttk.Entry(f_freq, width=10); self.e_scan_f2.insert(0, "904"); self.e_scan_f2.grid(row=0, column=3, padx=4, pady=2)
        ttk.Label(f_freq, text="Freq 3:").grid(row=0, column=4, padx=4, pady=2)
        self.e_scan_f3 = ttk.Entry(f_freq, width=10); self.e_scan_f3.insert(0, "915"); self.e_scan_f3.grid(row=0, column=5, padx=4, pady=2)

        bf = ttk.Frame(tab); bf.pack(fill="x", padx=4, pady=4)
        self.b_scan_start = ttk.Button(bf, text="Iniciar Scanner", command=self._start_scan, state="disabled")
        self.b_scan_start.pack(side="left", padx=4)
        self.b_scan_stop = ttk.Button(bf, text="Parar Scanner", command=self._stop_scan, state="disabled")
        self.b_scan_stop.pack(side="left", padx=4)

    def _build_tab_cw(self):
        tab = ttk.Frame(self.nb); self.nb.add(tab, text="Portadora CW"); self.tab_cw = tab
        ttk.Label(
            tab,
            text="Transmite portadora contínua (Continuous Wave) para teste de espectro / medida de potência.",
            justify="left",
        ).pack(anchor="w", padx=4, pady=4)
        cfg = ttk.LabelFrame(tab, text="Configuração da Portadora"); cfg.pack(fill="x", padx=4, pady=4)
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

    # -----------------------------------------------------------
    #   Aba End-Device LoRaWAN
    # -----------------------------------------------------------
    def _build_tab_end_device(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="End-Device LoRaWAN")
        self.tab_end_device = tab

        head = ttk.LabelFrame(tab, text="Modo do Dispositivo")
        head.pack(fill="x", padx=4, pady=4)
        ttk.Label(
            head,
            text=(
                "Esta aba configura o módulo conectado como END-DEVICE LoRaWAN.\n"
                "Para RAK3172: clique em 'Trocar para LoRaWAN' (AT+NWM=1) antes de usar. As abas P2P "
                "deixarão de funcionar até 'Voltar para P2P' (AT+NWM=0)."
            ),
            justify="left",
        ).pack(anchor="w", padx=6, pady=2)
        bf_mode = ttk.Frame(head); bf_mode.pack(fill="x", padx=4, pady=4)
        self.b_ed_enter = ttk.Button(bf_mode, text="Trocar para LoRaWAN", command=self._ed_enter_lw_mode, state="disabled")
        self.b_ed_enter.pack(side="left", padx=4)
        self.b_ed_exit = ttk.Button(bf_mode, text="Voltar para P2P", command=self._ed_exit_lw_mode, state="disabled")
        self.b_ed_exit.pack(side="left", padx=4)
        self.lbl_ed_mode = ttk.Label(bf_mode, text="Modo atual: ?", foreground="gray")
        self.lbl_ed_mode.pack(side="left", padx=15)

        ed_pw = ttk.PanedWindow(tab, orient="horizontal")
        ed_pw.pack(fill="both", expand=True, padx=4, pady=4)

        left = ttk.Frame(ed_pw); ed_pw.add(left, weight=3)

        # Modo de Ativação
        f_act = ttk.LabelFrame(left, text="Modo de Ativação"); f_act.pack(fill="x", padx=4, pady=4)
        self.ed_njm = tk.StringVar(value="OTAA")
        ttk.Radiobutton(f_act, text="OTAA (Over-the-Air Activation)",
                        variable=self.ed_njm, value="OTAA",
                        command=self._ed_njm_change).pack(anchor="w", padx=6, pady=2)
        ttk.Radiobutton(f_act, text="ABP (Activation By Personalization)",
                        variable=self.ed_njm, value="ABP",
                        command=self._ed_njm_change).pack(anchor="w", padx=6, pady=2)
        bf_act = ttk.Frame(f_act); bf_act.pack(fill="x", padx=4, pady=4)
        ttk.Button(bf_act, text="Aplicar modo (AT+NJM)",
                   command=lambda: self._ed_send("njm", 1 if self.ed_njm.get() == "OTAA" else 0)).pack(side="left", padx=2)

        # Chaves OTAA
        self.f_otaa = ttk.LabelFrame(left, text="Chaves OTAA"); self.f_otaa.pack(fill="x", padx=4, pady=4)
        self.ed_otaa_entries = self._ed_make_key_row(self.f_otaa, "DevEUI:", "deveui", 18)
        self.ed_otaa_entries.update(self._ed_make_key_row(self.f_otaa, "AppEUI / JoinEUI:", "appeui", 18))
        self.ed_otaa_entries.update(self._ed_make_key_row(self.f_otaa, "AppKey (32 hex):", "appkey", 38))
        ttk.Button(self.f_otaa, text="Ler DevEUI",
                   command=self._ed_read_deveui).pack(anchor="w", padx=6, pady=2)

        # Chaves ABP
        self.f_abp = ttk.LabelFrame(left, text="Chaves ABP"); self.f_abp.pack(fill="x", padx=4, pady=4)
        self.ed_abp_entries = self._ed_make_key_row(self.f_abp, "DevAddr (8 hex):", "devaddr", 14)
        self.ed_abp_entries.update(self._ed_make_key_row(self.f_abp, "NwkSKey (32 hex):", "nwkskey", 38))
        self.ed_abp_entries.update(self._ed_make_key_row(self.f_abp, "AppSKey (32 hex):", "appskey", 38))
        self.ed_abp_entries.update(self._ed_make_key_row(self.f_abp, "NwkID (opcional):", "nwkid", 10))

        # Parâmetros gerais
        f_par = ttk.LabelFrame(left, text="Parâmetros de Rede"); f_par.pack(fill="x", padx=4, pady=4)
        self._ed_param_entries: dict[str, tk.Variable] = {}

        row = 0
        ttk.Label(f_par, text="Class:").grid(row=row, column=0, sticky="e", padx=4, pady=2)
        var_class = tk.StringVar(value="A")
        ttk.Combobox(f_par, width=5, values=["A", "B", "C"], state="readonly", textvariable=var_class).grid(row=row, column=1, sticky="w", padx=4, pady=2)
        self._ed_param_entries["class"] = var_class
        ttk.Button(f_par, text="Set", command=lambda: self._ed_send("class", var_class.get())).grid(row=row, column=2, padx=2)

        ttk.Label(f_par, text="DR:").grid(row=row, column=3, sticky="e", padx=4, pady=2)
        var_dr = tk.StringVar(value="3")
        ttk.Entry(f_par, width=5, textvariable=var_dr).grid(row=row, column=4, sticky="w", padx=4, pady=2)
        self._ed_param_entries["dr"] = var_dr
        ttk.Button(f_par, text="Set", command=lambda: self._ed_send("dr", var_dr.get())).grid(row=row, column=5, padx=2)

        ttk.Label(f_par, text="TXP:").grid(row=row, column=6, sticky="e", padx=4, pady=2)
        var_txp = tk.StringVar(value="0")
        ttk.Entry(f_par, width=5, textvariable=var_txp).grid(row=row, column=7, sticky="w", padx=4, pady=2)
        self._ed_param_entries["txp"] = var_txp
        ttk.Button(f_par, text="Set", command=lambda: self._ed_send("txp", var_txp.get())).grid(row=row, column=8, padx=2)

        row += 1
        var_adr = tk.IntVar(value=1)
        ttk.Checkbutton(f_par, text="ADR (Adaptive Data Rate)", variable=var_adr,
                        command=lambda: self._ed_send("adr", var_adr.get())).grid(row=row, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        self._ed_param_entries["adr"] = var_adr
        var_cfm = tk.IntVar(value=0)
        ttk.Checkbutton(f_par, text="CFM (Confirmed)", variable=var_cfm,
                        command=lambda: self._ed_send("cfm", var_cfm.get())).grid(row=row, column=3, columnspan=3, sticky="w", padx=4, pady=2)
        self._ed_param_entries["cfm"] = var_cfm
        var_dcs = tk.IntVar(value=0)
        ttk.Checkbutton(f_par, text="DCS (Duty Cycle)", variable=var_dcs,
                        command=lambda: self._ed_send("dcs", var_dcs.get())).grid(row=row, column=6, columnspan=3, sticky="w", padx=4, pady=2)
        self._ed_param_entries["dcs"] = var_dcs
        row += 1
        var_pnm = tk.IntVar(value=1)
        ttk.Checkbutton(f_par, text="PNM (Public Network)", variable=var_pnm,
                        command=lambda: self._ed_send_pnm(bool(var_pnm.get()))).grid(row=row, column=0, columnspan=3, sticky="w", padx=4, pady=2)
        self._ed_param_entries["pnm"] = var_pnm

        # Delays
        f_dly = ttk.LabelFrame(left, text="Delays / Janelas RX"); f_dly.pack(fill="x", padx=4, pady=4)
        delays = [
            ("JN1DL (ms):", "jn1dl", "5000"),
            ("JN2DL (ms):", "jn2dl", "6000"),
            ("RX1DL (ms):", "rx1dl", "1000"),
            ("RX2DL (ms):", "rx2dl", "2000"),
            ("RX2FQ (Hz):", "rx2fq", "923300000"),
            ("RX2DR:",      "rx2dr", "8"),
            ("CNTUP:",      "cntup", "0"),
        ]
        for i, (label, key, default) in enumerate(delays):
            r, c = divmod(i, 3)
            ttk.Label(f_dly, text=label).grid(row=r * 2, column=c * 3, sticky="e", padx=4, pady=2)
            var = tk.StringVar(value=default)
            ttk.Entry(f_dly, width=14, textvariable=var).grid(row=r * 2, column=c * 3 + 1, sticky="w", padx=2, pady=2)
            ttk.Button(f_dly, text="Set",
                       command=lambda k=key, v=var: self._ed_send(k, v.get())).grid(row=r * 2, column=c * 3 + 2, padx=2)
            self._ed_param_entries[key] = var

        # Ações
        f_act2 = ttk.LabelFrame(left, text="Ações"); f_act2.pack(fill="x", padx=4, pady=4)
        bf_join = ttk.Frame(f_act2); bf_join.pack(fill="x", padx=4, pady=2)
        ttk.Button(bf_join, text="JOIN", command=self._ed_join).pack(side="left", padx=4)
        ttk.Button(bf_join, text="Consultar NJS", command=self._ed_query_njs).pack(side="left", padx=4)
        self.lbl_ed_njs = ttk.Label(bf_join, text="NJS: ?", foreground="gray"); self.lbl_ed_njs.pack(side="left", padx=15)
        bf_send = ttk.Frame(f_act2); bf_send.pack(fill="x", padx=4, pady=4)
        ttk.Label(bf_send, text="FPort:").pack(side="left", padx=2)
        self.ed_fport = tk.StringVar(value="2")
        ttk.Entry(bf_send, width=5, textvariable=self.ed_fport).pack(side="left", padx=2)
        ttk.Label(bf_send, text="(0=NwkSKey/MAC, 1-223=AppSKey)").pack(side="left", padx=2)
        bf_send2 = ttk.Frame(f_act2); bf_send2.pack(fill="x", padx=4, pady=2)
        ttk.Label(bf_send2, text="Payload (HEX):").pack(side="left", padx=2)
        self.ed_send_payload = tk.StringVar(value="0102")
        ttk.Entry(bf_send2, width=50, textvariable=self.ed_send_payload).pack(side="left", padx=2, fill="x", expand=True)
        self.ed_send_confirmed = tk.IntVar(value=0)
        ttk.Checkbutton(bf_send2, text="ACK", variable=self.ed_send_confirmed).pack(side="left", padx=4)
        ttk.Button(bf_send2, text="Enviar (SEND)", command=self._ed_send_uplink).pack(side="left", padx=4)

        # Console
        right = ttk.LabelFrame(ed_pw, text="Console End-Device (comandos e respostas)")
        ed_pw.add(right, weight=2)
        self.txt_ed_console = tk.Text(right, wrap="word", height=20)
        scr = ttk.Scrollbar(right, orient="vertical", command=self.txt_ed_console.yview)
        self.txt_ed_console.configure(yscrollcommand=scr.set)
        self.txt_ed_console.pack(side="left", fill="both", expand=True)
        scr.pack(side="right", fill="y")

        self._ed_njm_change()

    def _ed_make_key_row(self, parent, label: str, key: str, width: int) -> dict:
        frame = ttk.Frame(parent); frame.pack(fill="x", padx=4, pady=2)
        ttk.Label(frame, text=label, width=20).pack(side="left", padx=2)
        var = tk.StringVar(value="")
        ttk.Entry(frame, width=width, textvariable=var).pack(side="left", padx=2)
        ttk.Button(frame, text="Set",
                   command=lambda k=key, v=var: self._ed_send_hex(k, v.get())).pack(side="left", padx=4)
        return {key: var}

    def _ed_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.queue.put(("ed_log", line))
        self.logger.log(f"[ED] {msg}")

    def _ed_njm_change(self):
        if not hasattr(self, "f_otaa"): return
        is_otaa = self.ed_njm.get() == "OTAA"
        self.f_otaa.configure(text=("Chaves OTAA ◄ (ativo)" if is_otaa else "Chaves OTAA (inativo)"))
        self.f_abp.configure(text=("Chaves ABP ◄ (ativo)" if not is_otaa else "Chaves ABP (inativo)"))

    def _ed_check_ready(self) -> bool:
        if not self.is_connected or not self.device:
            messagebox.showwarning("Aviso", "Abra a UART primeiro."); return False
        if "LORAWAN" not in self.device.capabilities:
            messagebox.showwarning("Aviso", f"{self.device.name} não suporta modo LoRaWAN nesta GUI."); return False
        return True

    def _ed_send(self, key: str, value):
        if not self._ed_check_ready(): return
        if not self.device.lw_supports(key):
            self._ed_log(f"'{key}' não suportado por {self.device.name}"); return
        try:
            rsp = self.device.lw_cmd(key, value)
            self._ed_log(f"{key}={value} → {rsp.strip()}")
        except Exception as e:
            self._ed_log(f"ERRO em {key}: {e}")

    def _ed_send_hex(self, key: str, value: str):
        if not self._ed_check_ready(): return
        v = value.strip().replace(":", "").replace(" ", "").replace("-", "").upper()
        if v.startswith("0X"): v = v[2:]
        if not v:
            messagebox.showwarning("Aviso", f"{key}: valor vazio"); return
        self._ed_send(key, v)

    def _ed_send_pnm(self, public: bool):
        if not self._ed_check_ready(): return
        self.device.public_network = public
        try:
            rsp = self.device.apply_public_network()
            tag = "PÚBLICO (0x34)" if public else "PRIVADO (0x12)"
            if rsp is not None:
                self._ed_log(f"PNM={tag} → {rsp.strip()}")
            else:
                self._ed_log(f"PNM={tag} (driver não expõe comando — no-op)")
            self.cb_pnm.set("Público (LoRaWAN, 0x34)" if public else "Privado (P2P, 0x12)")
        except Exception as e:
            self._ed_log(f"ERRO PNM: {e}")

    def _ed_read_deveui(self):
        if not self._ed_check_ready(): return
        try:
            rsp = self.device.lw_cmd("deveui_get")
            self._ed_log(f"DevEUI=? → {rsp.strip()}")
            import re
            m = re.search(r"([0-9A-Fa-f:]{15,})", rsp)
            if m:
                self.ed_otaa_entries["deveui"].set(m.group(1).replace(":", "").upper())
        except Exception as e:
            self._ed_log(f"ERRO ler DevEUI: {e}")

    def _ed_enter_lw_mode(self):
        if not self._ed_check_ready(): return
        try:
            rsp = self.device.lw_enter_mode()
            if rsp is None:
                self._ed_log(f"{self.device.name} não precisa de comando de troca de modo")
            else:
                self._ed_log(f"Modo LoRaWAN: {rsp.strip()}")
            self.lbl_ed_mode.config(text="Modo atual: LoRaWAN", foreground="green")
            self.b_ed_enter.config(state="disabled")
            self.b_ed_exit.config(state="normal" if self.device.LW_MODE_EXIT else "disabled")
        except Exception as e:
            self._ed_log(f"ERRO entrar em modo LoRaWAN: {e}")

    def _ed_exit_lw_mode(self):
        if not self._ed_check_ready(): return
        try:
            rsp = self.device.lw_exit_mode()
            if rsp is None:
                self._ed_log("não precisa de comando de saída")
            else:
                self._ed_log(f"Modo P2P: {rsp.strip()}")
            self.lbl_ed_mode.config(text="Modo atual: P2P", foreground="blue")
            self.b_ed_enter.config(state="normal" if self.device.LW_MODE_ENTER else "disabled")
            self.b_ed_exit.config(state="disabled")
        except Exception as e:
            self._ed_log(f"ERRO sair de modo LoRaWAN: {e}")

    def _ed_join(self):
        if not self._ed_check_ready(): return
        otaa = self.ed_njm.get() == "OTAA"
        try:
            if self.device.name.startswith("Quectel"):
                rsp = self.device.lw_cmd("join", 1 if otaa else 0, wait=2.0)
            else:
                if self.device.lw_supports("njm"):
                    self.device.lw_cmd("njm", 1 if otaa else 0)
                rsp = self.device.lw_cmd("join", wait=2.0)
            self._ed_log(f"JOIN ({'OTAA' if otaa else 'ABP'}) → {rsp.strip()}")
        except Exception as e:
            self._ed_log(f"ERRO JOIN: {e}")

    def _ed_query_njs(self):
        if not self._ed_check_ready(): return
        try:
            rsp = self.device.lw_cmd("njs")
            txt = rsp.strip()
            self._ed_log(f"NJS → {txt}")
            joined = "1" in txt and "0" not in txt[-3:]
            self.lbl_ed_njs.config(
                text=f"NJS: {'JOINED' if joined else 'NOT JOINED'}",
                foreground="green" if joined else "red",
            )
        except Exception as e:
            self._ed_log(f"ERRO NJS: {e}")

    def _ed_send_uplink(self):
        if not self._ed_check_ready(): return
        try:
            port = int(self.ed_fport.get().strip())
            payload_hex = self.ed_send_payload.get().strip().replace(" ", "").replace(":", "")
            if payload_hex.lower().startswith("0x"): payload_hex = payload_hex[2:]
            ack = self.ed_send_confirmed.get()
        except ValueError as e:
            messagebox.showerror("Erro", f"Parâmetro inválido: {e}"); return

        if self.device.name.startswith("Quectel"):
            value = f"{port}:{ack}:{payload_hex}"; key = "send"
        elif self.device.lw_supports("sendb"):
            value = f"{port}:{payload_hex}"; key = "sendb"
        else:
            value = f"{port}:{payload_hex}"; key = "send"
        try:
            rsp = self.device.lw_cmd(key, value, wait=2.0)
            self._ed_log(f"SEND FPort={port} ACK={ack} payload={payload_hex} → {rsp.strip()}")
        except Exception as e:
            self._ed_log(f"ERRO SEND: {e}")

    # -----------------------------------------------------------
    #   Aba Dispositivos LoRaWAN (CRUD)
    # -----------------------------------------------------------
    def _build_tab_devices(self):
        tab = ttk.Frame(self.nb); self.nb.add(tab, text="Dispositivos LoRaWAN"); self.tab_devices = tab

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

        f_dev = ttk.LabelFrame(tab, text="Dispositivos cadastrados"); f_dev.pack(fill="both", expand=True, padx=8, pady=4)

        tv_container = ttk.Frame(f_dev); tv_container.pack(fill="both", expand=True, padx=4, pady=4)

        cols = ("name", "dev_eui", "join_eui", "app_key", "dev_addr", "fcnt_up", "fcnt_dn", "status")
        self.tv_devices = ttk.Treeview(tv_container, columns=cols, show="headings", height=14)
        headings = {
            "name": ("Nome", 120), "dev_eui": ("DevEUI", 150), "join_eui": ("JoinEUI", 150),
            "app_key": ("AppKey", 270), "dev_addr": ("DevAddr", 90),
            "fcnt_up": ("FCntUp", 70), "fcnt_dn": ("FCntDn", 70), "status": ("Status", 110),
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

        self.tv_devices.bind("<Double-1>", lambda _e: self._gw_edit_device())

        bf = ttk.Frame(tab); bf.pack(fill="x", padx=8, pady=8)
        ttk.Button(bf, text="Adicionar...", command=self._gw_add_device).pack(side="left", padx=2)
        ttk.Button(bf, text="Editar...", command=self._gw_edit_device).pack(side="left", padx=2)
        ttk.Button(bf, text="Remover", command=self._gw_remove_device).pack(side="left", padx=2)
        ttk.Button(bf, text="Limpar sessão", command=self._gw_clear_session).pack(side="left", padx=2)
        ttk.Separator(bf, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bf, text="Salvar JSON", command=self._save_devices_json).pack(side="left", padx=2)
        ttk.Button(bf, text="Recarregar JSON", command=self._reload_devices_json).pack(side="left", padx=2)
        ttk.Button(bf, text="← Ir para Gateway LoRaWAN",
                   command=lambda: self.nb.select(self.tab_gw)).pack(side="right", padx=2)

        self._refresh_devices_tree()

    # -----------------------------------------------------------
    #   Aba Gateway LoRaWAN
    # -----------------------------------------------------------
    def _build_tab_gateway(self):
        tab = ttk.Frame(self.nb); self.nb.add(tab, text="Gateway LoRaWAN"); self.tab_gw = tab

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

        f_band = ttk.LabelFrame(tab, text="Banda LoRaWAN"); f_band.pack(fill="x", padx=4, pady=4)
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

        f_summary = ttk.LabelFrame(tab, text="End-Devices cadastrados"); f_summary.pack(fill="x", padx=4, pady=4)
        self.lbl_devices_summary = ttk.Label(f_summary, text="(0 dispositivos)", foreground="gray")
        self.lbl_devices_summary.pack(side="left", padx=8, pady=6)
        ttk.Button(f_summary, text="Abrir aba 'Dispositivos LoRaWAN' →",
                   command=lambda: self.nb.select(self.tab_devices)).pack(side="right", padx=8, pady=4)

        bf = ttk.Frame(tab); bf.pack(fill="x", padx=4, pady=4)
        self.b_gw_start = ttk.Button(bf, text="Iniciar Gateway", command=self._start_gateway, state="disabled")
        self.b_gw_start.pack(side="left", padx=4)
        self.b_gw_stop = ttk.Button(bf, text="Parar Gateway", command=self._stop_gateway, state="disabled")
        self.b_gw_stop.pack(side="left", padx=4)
        self.lbl_gw_state = ttk.Label(bf, text="Gateway: parado", foreground="gray")
        self.lbl_gw_state.pack(side="left", padx=15)

        f_evt = ttk.LabelFrame(tab, text="Eventos LoRaWAN (frames decodificados)")
        f_evt.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt_gw_console = tk.Text(f_evt, wrap="word", height=12)
        self.txt_gw_console.pack(fill="both", expand=True, padx=4, pady=4)

    def _on_pnm_change(self):
        is_public = self.cb_pnm.get().startswith("Público")
        if not self.device:
            return
        self.device.public_network = is_public
        if self.is_connected:
            try:
                rsp = self.device.apply_public_network()
                tag = "PÚBLICO (LoRaWAN, syncword 0x34)" if is_public else "PRIVADO (P2P, syncword 0x12)"
                if rsp is not None:
                    self.logger.log(f"[PNM] Aplicado: {tag} → {rsp.strip()}")
                else:
                    self.logger.log(f"[PNM] {tag} (dispositivo não expõe comando — no-op)")
            except Exception as e:
                self.logger.log(f"[ERRO PNM] {e}")

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

    # -----------------------------------------------------------
    #   Aba Console AT
    # -----------------------------------------------------------
    def _build_tab_console(self):
        tab = ttk.Frame(self.nb); self.nb.add(tab, text="Console AT"); self.tab_console = tab
        ttk.Label(
            tab,
            text=("Envia comandos AT manualmente ao dispositivo conectado.\n"
                  "Útil para configurações específicas que não estão nas outras abas."),
            justify="left",
        ).pack(anchor="w", padx=4, pady=4)

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

        sugg = ttk.LabelFrame(tab, text="Comandos rápidos (clique para preencher)")
        sugg.pack(fill="x", padx=4, pady=4)
        self.frame_quick = ttk.Frame(sugg); self.frame_quick.pack(fill="x", padx=4, pady=4)

        hist = ttk.LabelFrame(tab, text="Resposta do dispositivo (última)")
        hist.pack(fill="both", expand=True, padx=4, pady=4)
        self.txt_at_resp = tk.Text(hist, wrap="word", height=10)
        self.txt_at_resp.pack(fill="both", expand=True, padx=4, pady=4)

        self._at_hist: list[str] = []
        self._at_hist_idx = 0

    # -----------------------------------------------------------
    #   Helpers de UI
    # -----------------------------------------------------------
    def _make_p2p_inputs(self, parent, defaults, with_power: bool) -> dict:
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
                    for w in self._log_widgets:
                        try:
                            w.insert("end", val + "\n"); w.see("end")
                        except tk.TclError:
                            pass
                elif kind == "gw_log":
                    if hasattr(self, "txt_gw_console"):
                        self.txt_gw_console.insert("end", val + "\n"); self.txt_gw_console.see("end")
                elif kind == "ed_log":
                    if hasattr(self, "txt_ed_console"):
                        self.txt_ed_console.insert("end", val + "\n"); self.txt_ed_console.see("end")
                elif kind == "at_resp":
                    self.txt_at_resp.delete("1.0", "end"); self.txt_at_resp.insert("end", val)
                elif kind == "ui":
                    val()
        except queue.Empty:
            pass
        self.after(100, self._process_queue)

    def _ui(self, fn):
        self.queue.put(("ui", fn))

    def _clear_log(self):
        for w in self._log_widgets:
            try: w.delete("1.0", "end")
            except tk.TclError: pass

    def _toggle_detach_log(self):
        if self._log_window is not None:
            self._log_window.destroy(); return
        win = tk.Toplevel(self)
        win.title("Log da Sessão — janela destacada")
        win.geometry("900x600")

        container = ttk.Frame(win); container.pack(side="bottom", fill="both", expand=True, padx=4, pady=4)
        txt = tk.Text(container, wrap="word")
        scr = ttk.Scrollbar(container, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scr.set)
        txt.pack(side="left", fill="both", expand=True); scr.pack(side="right", fill="y")

        current = self.txt_log.get("1.0", "end-1c")
        if current:
            txt.insert("1.0", current); txt.see("end")

        self._log_widgets.append(txt)
        self._log_window = win
        self.b_detach_log.config(text="Reanexar log ⇙")

        def on_close():
            try: self._log_widgets.remove(txt)
            except ValueError: pass
            self._log_window = None
            try: self.b_detach_log.config(text="Destacar log ⇗")
            except tk.TclError: pass
            try: win.destroy()
            except tk.TclError: pass
        win.protocol("WM_DELETE_WINDOW", on_close)

        bar = ttk.Frame(win); bar.pack(side="top", fill="x", padx=4, pady=2)
        ttk.Button(bar, text="Limpar", command=self._clear_log).pack(side="left", padx=2)
        ttk.Button(bar, text="Reanexar", command=on_close).pack(side="left", padx=2)
        ttk.Label(bar, text="(arraste para outro monitor se desejar)").pack(side="right", padx=4)

    def _open_logs_folder(self):
        os.makedirs(LOGS_DIR, exist_ok=True)
        try: os.startfile(LOGS_DIR)
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível abrir a pasta: {e}")

    def _open_debug_log(self):
        path = self.logger.debug_path
        if not path or not os.path.exists(path):
            messagebox.showinfo("Info", "Nenhum log de debug ainda. Abra a porta UART primeiro."); return
        try: os.startfile(path)
        except Exception as e:
            messagebox.showerror("Erro", f"Não foi possível abrir o log: {e}")

    # -----------------------------------------------------------
    #   Troca de dispositivo
    # -----------------------------------------------------------
    def _on_device_change(self):
        if self.is_connected:
            messagebox.showwarning("Aviso", "Desconecte antes de trocar de dispositivo.")
            self.cb_device.set(self.device.name if self.device else "RAK3172")
            return
        cls = self._selected_device_class()
        self.e_baud.delete(0, "end"); self.e_baud.insert(0, str(cls.default_baudrate))
        caps = ", ".join(sorted(cls.capabilities)) if cls.capabilities else "(nenhuma)"
        self.lbl_caps.config(text=f"Capabilities {cls.name}: {caps}")
        self._update_tab_states(cls.capabilities)
        self._populate_quick_commands(cls)

    def _update_tab_states(self, caps: set[str]):
        mapping = [
            (self.tab_tx, "TX"), (self.tab_rx, "RX"),
            (self.tab_incr, "INCREMENTAL"), (self.tab_scan, "SCAN"), (self.tab_cw, "CW"),
        ]
        for tab, cap in mapping:
            try:
                self.nb.tab(tab, state="normal" if cap in caps else "disabled")
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
            ttk.Button(self.frame_quick, text=cmd, width=18,
                       command=lambda c=cmd: self._set_at_cmd(c)).pack(side="left", padx=2, pady=2)

    def _set_at_cmd(self, cmd: str):
        self.e_at_cmd.delete(0, "end"); self.e_at_cmd.insert(0, cmd)

    # -----------------------------------------------------------
    #   Conexão
    # -----------------------------------------------------------
    def _connect(self):
        if self.is_connected:
            return
        port = self._selected_port()
        if not port:
            messagebox.showerror("Erro", "Selecione uma porta UART."); return
        try:
            baud = int(self.e_baud.get().strip())
        except ValueError:
            messagebox.showerror("Erro", "Baudrate inválido."); return

        cls = self._selected_device_class()
        try:
            self.device = cls(port, baud)
            self.device.open()
            self.logger.open_txt(device_name=cls.name)
            self.device.debug_cb = self.logger.debug_log
            self.device.public_network = self.cb_pnm.get().startswith("Público")
            self.lbl_logfile.config(
                text=f"Logs: {os.path.basename(self.logger.txt_path)}  +  "
                     f"{os.path.basename(self.logger.debug_path)}"
            )
            self.is_connected = True
            self.lbl_uart_status.config(text=f"aberta ({port} @ {baud} bps)", foreground="blue")
            self.lbl_dev_status.config(text="não testado", foreground="gray")
            self.b_connect.config(state="disabled")
            self.b_test.config(state="normal")
            self.b_disconnect.config(state="normal")
            self.cb_device.config(state="disabled")
            self._toggle_action_buttons(True)
            self.logger.log(f"[INFO] Porta UART aberta: {port} @ {baud} bps. Driver: {cls.name}.")
            self.logger.log(f"[INFO] Use 'Testar dispositivo' para verificar (comando: {cls.ping_cmd}).")
        except Exception as e:
            if self.device:
                try: self.device.close()
                except Exception: pass
                self.device = None
            self.logger.log(f"[ERRO] Falha ao abrir porta UART: {e}")
            messagebox.showerror("Erro", f"Falha ao abrir porta UART: {e}")

    def _test_device(self):
        if not self.is_connected or not self.device:
            messagebox.showwarning("Aviso", "Abra a porta UART primeiro."); return
        if self.rx_running or self.scan_running or self.incr_running or self.cw_running:
            messagebox.showwarning("Aviso", "Pare a operação em andamento antes de testar."); return

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
        self.lbl_logfile.config(text="Logs: (serão criados ao conectar)")

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
        if hasattr(self, "b_gw_start"):
            self.b_gw_start.config(state="normal" if (enable and "RX" in caps) else "disabled")
        if hasattr(self, "b_ed_enter"):
            has_lw = enable and "LORAWAN" in caps
            self.b_ed_enter.config(
                state="normal" if (has_lw and self.device and self.device.LW_MODE_ENTER) else "disabled"
            )
            if has_lw and self.device and self.device.LW_MODE_ENTER is None:
                self.lbl_ed_mode.config(text="Modo atual: LoRaWAN (nativo)", foreground="green")
            elif has_lw:
                self.lbl_ed_mode.config(text="Modo atual: P2P (clique em 'Trocar para LoRaWAN')", foreground="orange")
            else:
                self.lbl_ed_mode.config(text="Modo atual: ?", foreground="gray")
        if not enable:
            self.b_rx_stop.config(state="disabled")
            self.b_incr_stop.config(state="disabled")
            self.b_scan_stop.config(state="disabled")
            self.b_cw_stop.config(state="disabled")
            if hasattr(self, "b_gw_stop"):
                self.b_gw_stop.config(state="disabled")
            if hasattr(self, "b_ed_exit"):
                self.b_ed_exit.config(state="disabled")

    def _check_busy(self) -> bool:
        if (self.rx_running or self.scan_running or self.incr_running
                or self.cw_running or self.gw_running):
            messagebox.showwarning("Aviso", "Pare a operação em andamento antes de iniciar outra.")
            return True
        return False

    # -----------------------------------------------------------
    #   TX único + workers
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
        threading.Thread(
            target=self._tx_worker,
            args=(seq, payload, msg_text, timeout, retries, cfg_tx, cfg_rx),
            daemon=True,
        ).start()

    @staticmethod
    def _encode_payload(msg: str) -> tuple[bytes, str | None]:
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
                        got = True; break

                try: self.device.rx_stop()
                except Exception: pass

                if got: break
                self.logger.log(f"  Timeout em [{tag}].")
        except Exception as e:
            self.logger.log(f"[ERRO TX] {e}")

        match = False
        if rx_payload:
            up = rx_payload.upper().replace(":", "").strip()
            match = (up == payload_hex) or (rx_payload.strip() == (msg_text or ""))

        self._record_result(
            mode="TX", seq=seq,
            payload_tx=msg_text or payload_hex, payload_rx=rx_payload or "",
            match=int(match), attempts=attempts, timeout_s=timeout, retries=retries,
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
                            mode="RX", seq=-1, payload_tx="", payload_rx=ph,
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
    #   Incremental
    # -----------------------------------------------------------
    def _reset_incr(self):
        self.seq_counter = 0; self._update_incr_label()

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
        threading.Thread(target=self._incr_worker,
                         args=(interval, count, timeout, cfg_tx, cfg_rx), daemon=True).start()

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
                                mode="SCAN", seq=-1, payload_tx="", payload_rx=ph,
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
    #   Console AT
    # -----------------------------------------------------------
    def _send_at_manual(self):
        if not self.is_connected or not self.device:
            messagebox.showwarning("Aviso", "Conecte ao dispositivo primeiro."); return
        cmd = self.e_at_cmd.get().strip()
        if not cmd: return
        try:
            wait = float(self.e_at_wait.get().strip())
        except ValueError:
            messagebox.showerror("Erro", "Espera inválida."); return

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
    #   Gateway LoRaWAN — CRUD de devices.json
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
            if idx is None: self.devices_db.append(entry)
            else: self.devices_db[idx] = entry
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
        if self._check_busy(): return
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
        self._gw_log(f"iniciado em {hz_to_mhz(freq_hz)} MHz SF{sf} BW{bw}kHz")
        prev_pnm = False
        try:
            self.device.flush_rx()
            prev_pnm = self.device.public_network
            self.device.public_network = True
            self._gw_log("PNM=PÚBLICO (syncword 0x34) para escutar frames LoRaWAN")
            self.device.configure_radio(freq_hz, sf, bw, "4/5", 8, power)
            self.device.rx_continuous_start()

            while self.gw_running:
                for line in self.device.read_until(1.0):
                    if not self.gw_running: break
                    payload_hex, rssi, snr = self.device.parse_rx_line(line)
                    if not payload_hex: continue
                    try:
                        raw = bytes.fromhex(payload_hex.replace(":", "").replace(" ", ""))
                    except ValueError:
                        self._gw_log(f"frame inválido (hex): {payload_hex}"); continue
                    if not raw: continue
                    self._process_lorawan_frame(raw, rssi, snr, freq_hz, sf, bw, power, rx_delay, net_id)

            try: self.device.rx_stop()
            except Exception: pass
        except Exception as e:
            self._gw_log(f"erro: {e}")
        finally:
            try:
                self.device.public_network = prev_pnm
                self._gw_log(f"PNM restaurado para {'PÚBLICO' if prev_pnm else 'PRIVADO'}")
            except Exception:
                pass
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
        self._gw_log(f"  JOIN REQUEST: DevEUI={dev_eui_hex} JoinEUI={join_eui_hex} DevNonce={dev_nonce_hex}")

        dev = self._find_device(dev_eui_hex)
        if not dev:
            self._gw_log(f"  REJEITADO: DevEUI {dev_eui_hex} não cadastrado"); return
        try:
            app_key = hex_to_bytes(dev["app_key"])
        except Exception as e:
            self._gw_log(f"  AppKey inválida: {e}"); return

        if not jr.validate_mic(app_key):
            self._gw_log(f"  REJEITADO: MIC inválido para {dev['name']}"); return

        self._gw_log(f"  MIC OK. Aceitando Join de '{dev['name']}'")

        dev_addr = self._next_dev_addr & 0xFFFFFFFF
        self._next_dev_addr += 1
        app_nonce = random_bytes(3)

        nwk_skey, app_skey = derive_session_keys(app_key, app_nonce, net_id, jr.dev_nonce[::-1])

        try:
            ja = build_join_accept(
                app_key=app_key, app_nonce=app_nonce, net_id=net_id,
                dev_addr=dev_addr, dl_settings=0, rx_delay=rx_delay,
            )
        except Exception as e:
            self._gw_log(f"  Falha ao construir Join Accept: {e}"); return

        dev["session"] = {
            "dev_addr": f"{dev_addr:08X}",
            "nwk_skey": bytes_to_hex(nwk_skey),
            "app_skey": bytes_to_hex(app_skey),
            "fcnt_up": 0, "fcnt_down": 0,
            "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._ui(self._refresh_devices_tree)
        self._gw_log(f"  Join Accept (cifrado) = 0x{ja.hex().upper()}")
        self._gw_log(f"  Sessão: DevAddr={dev_addr:08X} NwkSKey={bytes_to_hex(nwk_skey)[:16]}... "
                     f"AppSKey={bytes_to_hex(app_skey)[:16]}...")

        self._gw_log(f"  Aguardando ~{rx_delay}s antes de TX (janela RX1 simulada)...")
        time.sleep(max(0.5, rx_delay - 0.5))
        try:
            self.device.rx_stop()
            time.sleep(0.05)
            self.device.configure_radio(freq_hz, sf, bw, "4/5", 8, power)
            self.device.tx_payload(ja, msg_text=None)
            self._gw_log("  Join Accept transmitido")
            time.sleep(0.2)
            self.device.rx_continuous_start()
        except Exception as e:
            self._gw_log(f"  Falha ao transmitir Join Accept: {e}")

    def _handle_uplink(self, raw):
        try:
            df = DataFrame.parse(raw)
        except Exception as e:
            self._gw_log(f"  Data frame inválido: {e}"); return

        dev_addr_hex = f"{df.dev_addr:08X}"
        dev = self._find_session_by_dev_addr(dev_addr_hex)
        if not dev:
            self._gw_log(f"  UPLINK ignorado: DevAddr {dev_addr_hex} sem sessão (faça Join primeiro)"); return
        sess = dev["session"]
        try:
            nwk_skey = hex_to_bytes(sess["nwk_skey"])
            app_skey = hex_to_bytes(sess["app_skey"])
        except Exception as e:
            self._gw_log(f"  Chaves de sessão inválidas: {e}"); return

        if not df.validate_mic(nwk_skey, fcnt32=df.fcnt):
            self._gw_log(f"  UPLINK MIC inválido para {dev['name']} (FCnt={df.fcnt})"); return

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
            payload_tx=payload_tx, payload_rx=payload_rx or "",
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
        if self._log_window is not None:
            try: self._log_window.destroy()
            except Exception: pass
        self.destroy()

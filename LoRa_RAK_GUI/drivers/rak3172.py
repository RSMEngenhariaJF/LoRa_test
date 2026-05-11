"""
Driver para o módulo RAK3172 (RAK Wireless / Wisduo).

Modo P2P: AT+NWM=0, AT+P2P=freq:sf:bw:cr:preamble:power
Modo LoRaWAN: AT+NWM=1, AT+CLASS, AT+JOIN, etc.

Doc: https://docs.rakwireless.com/Product-Categories/WisDuo/RAK3172-Module/AT-Command-Manual/
"""
from __future__ import annotations

from drivers.base import LoRaDevice


class RAK3172(LoRaDevice):
    name = "RAK3172"
    default_baudrate = 115200
    capabilities = {"TX", "RX", "CW", "SCAN", "INCREMENTAL", "LORAWAN"}

    _CR_MAP = {"4/5": 0, "4/6": 1, "4/7": 2, "4/8": 3}

    # Modo LoRaWAN: AT+NWM=1 (sai do P2P)
    LW_MODE_ENTER = "AT+NWM=1"
    LW_MODE_EXIT = "AT+NWM=0"

    LW_CMDS = {
        "class":      "AT+CLASS={value}",       # A/B/C
        "njm":        "AT+NJM={value}",         # 0=ABP, 1=OTAA
        "njs":        "AT+NJS=?",
        "join":       "AT+JOIN=1:0:10:8",       # join, no auto-join, 10s, 8 retries
        "adr":        "AT+ADR={value}",         # 0/1
        "cfm":        "AT+CFM={value}",         # 0/1
        "dcs":        "AT+DCS={value}",         # 0/1
        "dr":         "AT+DR={value}",          # 0..15 conforme banda
        "txp":        "AT+TXP={value}",         # 0..15
        "jn1dl":      "AT+JN1DL={value}",       # ms
        "jn2dl":      "AT+JN2DL={value}",
        "rx1dl":      "AT+RX1DL={value}",
        "rx2dl":      "AT+RX2DL={value}",
        "rx2fq":      "AT+RX2FQ={value}",       # Hz
        "rx2dr":      "AT+RX2DR={value}",
        "appeui":     "AT+APPEUI={value}",      # 16 hex sem separadores
        "appkey":     "AT+APPKEY={value}",      # 32 hex
        "deveui":     "AT+DEUI={value}",
        "deveui_get": "AT+DEUI=?",
        "devaddr":    "AT+DEVADDR={value}",     # 8 hex
        "nwkskey":    "AT+NWKSKEY={value}",
        "appskey":    "AT+APPSKEY={value}",
        "send":       "AT+SEND={value}",        # value = "port:hex"
        "cntup":      "AT+CNT={value}",
    }

    # ---- PNM via SYNCWORD ----
    def set_public_network(self, enable: bool) -> str:
        sync = "0x34" if enable else "0x12"
        return self.send_at(f"AT+SYNCWORD={sync}")

    # ---- Configuração de rádio (modo P2P) ----
    def configure_radio(self, freq_hz, sf, bw_khz, cr_label, preamble, power):
        self.send_at("AT+NWM=0")
        self.apply_public_network()
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

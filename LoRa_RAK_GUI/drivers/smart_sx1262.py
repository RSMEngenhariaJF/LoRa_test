"""
Driver para o módulo SMART SMW-SX1262M0 (SMART Modular Technologies).

Particularidades:
  - Frequência em kHz (não Hz como RAK/KG200Z)
  - CR como string '4/5', '4/6', '4/7', '4/8' (passa direto no AT+TCONF)
  - AT+PNM dedicado para Public Network Mode (syncword)
  - AT+TXLRA aceita texto plain (não hex)

Doc: doc/SMART_LoRa_AT_Command_*.pdf
"""
from __future__ import annotations

from drivers.base import LoRaDevice


class SMARTSx1262(LoRaDevice):
    name = "SMART SMW-SX1262M0"
    default_baudrate = 9600
    capabilities = {"TX", "RX", "CW", "SCAN", "LORAWAN"}

    LW_CMDS = {
        "class":      "AT+CLASS={value}",
        "njm":        "AT+NJM={value}",
        "njs":        "AT+NJS=?",
        "join":       "AT+JOIN",
        "adr":        "AT+ADR={value}",
        "cfm":        "AT+CFM={value}",
        "cfs":        "AT+CFS=?",
        "dcs":        "AT+DCS={value}",
        "dr":         "AT+DR={value}",
        "txp":        "AT+TXP={value}",         # 0..10
        "jn1dl":      "AT+JN1DL={value}",
        "jn2dl":      "AT+JN2DL={value}",
        "rx1dl":      "AT+RX1DL={value}",
        "rx2dl":      "AT+RX2DL={value}",
        "rx2fq":      "AT+RX2FQ={value}",
        "rx2dr":      "AT+RX2DR={value}",
        "appeui":     "AT+APPEUI={value}",
        "appkey":     "AT+APPKEY={value}",
        "deveui_get": "AT+DEUI=?",               # SMART não permite escrever DevEUI
        "devaddr":    "AT+DADDR={value}",
        "nwkskey":    "AT+NWKSKEY={value}",
        "appskey":    "AT+APPSKEY={value}",
        "nwkid":      "AT+NWKID={value}",
        "send":       "AT+SEND={value}",         # value = "port:text"
        "sendb":      "AT+SENDB={value}",        # value = "port:hex"
        "cntup":      "AT+CNTUP={value}",
        "pnm":        "AT+PNM={value}",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_freq_khz: int = 904000
        self._last_power: int = 14

    def set_public_network(self, enable: bool) -> str:
        """AT+PNM=1 (público, syncword 0x34) ou =0 (privado, 0x12)."""
        return self.send_at(f"AT+PNM={1 if enable else 0}")

    def configure_radio(self, freq_hz, sf, bw_khz, cr_label, preamble, power):
        # AT+TCONF=freq_kHz:power:bw_kHz:sf:cr_label:lna:pa
        freq_khz = int(round(freq_hz / 1000))
        self._last_freq_khz = freq_khz
        self._last_power = power
        self.apply_public_network()
        return self.send_at(f"AT+TCONF={freq_khz}:{power}:{bw_khz}:{sf}:{cr_label}:0:0")

    def tx_payload(self, payload, msg_text=None):
        # AT+TXLRA=freq_kHz:cont_mode(0/1):text
        text = msg_text if msg_text else payload.hex().upper()
        if len(text) > 64:
            text = text[:64]
        return self.send_at(f"AT+TXLRA={self._last_freq_khz}:0:{text}")

    def rx_continuous_start(self):
        return self.send_at(f"AT+RXLRA={self._last_freq_khz}:1", wait=0.2)

    def rx_stop(self):
        return self.send_at("AT+TOFF", wait=0.2)

    def cw_start(self, freq_hz, power, sf, bw_khz):
        freq_khz = int(round(freq_hz / 1000))
        self._last_freq_khz = freq_khz
        self._last_power = power
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

"""
Driver para o módulo Quectel KG200Z.

Particularidades:
  - Ping é 'ATQ' (não 'AT')
  - Comandos AT prefixados com 'Q' (AT+QCLASS, AT+QSEND, etc.)
  - Modo P2P controlado por AT+QP2P=1 (habilita testes P2P)
  - AT+QSEND embute flag de ACK (port:ack:hex)
  - Modo de Join (OTAA/ABP) é parâmetro do próprio AT+QJOIN

Doc: doc/Quectel_KG200Z_AT_Commands_Manual_*.pdf
"""
from __future__ import annotations

from drivers.base import LoRaDevice


class KG200Z(LoRaDevice):
    name = "Quectel KG200Z"
    default_baudrate = 9600
    capabilities = {"TX", "RX", "CW", "SCAN", "LORAWAN"}
    ping_cmd = "ATQ"

    _CR_MAP = {"4/5": 1, "4/6": 2, "4/7": 3, "4/8": 4}
    # BW LoRa: 0=7.8125, 1=15.625, 2=31.25, 3=62.5, 4=125, 5=250, 6=500 kHz
    _BW_MAP = {7: 0, 15: 1, 31: 2, 62: 3, 125: 4, 250: 5, 500: 6}

    LW_CMDS = {
        "class":      "AT+QCLASS={value}",
        # 'njm' não existe separado — modo embutido no QJOIN (0=ABP, 1=OTAA)
        "njs":        "AT+QSTATUS=?",
        "join":       "AT+QJOIN={value}",       # 0=ABP, 1=OTAA
        "adr":        "AT+QADR={value}",
        # 'cfm' não existe separado — flag de ACK embutido no QSEND
        "dcs":        "AT+QDCS={value}",
        "dr":         "AT+QDR={value}",
        "txp":        "AT+QTXP={value}",
        "jn1dl":      "AT+QJN1DL={value}",
        "jn2dl":      "AT+QJN2DL={value}",
        "rx1dl":      "AT+QRX1DL={value}",
        "rx2dl":      "AT+QRX2DL={value}",
        "rx2fq":      "AT+QRX2FQ={value}",
        "rx2dr":      "AT+QRX2DR={value}",
        "band":       "AT+QBAND={value}",       # 0-9 (AU915=1, US915=8)
        "appeui":     "AT+QAPPEUI={value}",
        "appkey":     "AT+QAPPKEY={value}",
        "deveui":     "AT+QDEUI={value}",
        "deveui_get": "AT+QDEUI=?",
        "devaddr":    "AT+QDADDR={value}",
        "nwkskey":    "AT+QNWKSKEY={value}",
        "appskey":    "AT+QAPPSKEY={value}",
        "send":       "AT+QSEND={value}",       # value = "port:ack:hex"
    }

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
        # KG200Z aceita string ASCII via AT+QTDA. Para HEX, manda como string ASCII.
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
        Extrai o que conseguir desta linha (estado simples por linha)."""
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
                # Pega só até o primeiro separador (espaço, vírgula, dB...) para não
                # concatenar dígitos do SNR que vem em seguida.
                v = up.split("RSSIVALUE", 1)[1].split("=", 1)[1]
                # Pega caracteres válidos só até encontrar o primeiro inválido
                # (ex.: 'D' em 'dBm', ',' antes do SNR, ' ' espaço)
                acc = ""
                started = False
                for c in v:
                    if c.isdigit() or c in "+-.":
                        acc += c
                        started = True
                    elif started:
                        break
                rssi = float(acc)
            except Exception:
                pass
        if "SNRVALUE" in up:
            try:
                v = up.split("SNRVALUE", 1)[1].split("=", 1)[1]
                acc = ""
                started = False
                for c in v:
                    if c.isdigit() or c in "+-.":
                        acc += c
                        started = True
                    elif started:
                        break
                snr = float(acc)
            except Exception:
                pass
        return payload, rssi, snr

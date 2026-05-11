"""
Implementação do protocolo LoRaWAN 1.0.x (lado Gateway + Network Server simulado).

Foco: OTAA — Join Request / Join Accept + uplinks/downlinks com cifra AES-128.
Single-channel: gateway escuta uma frequência por vez.

Referências:
  - LoRaWAN 1.0.3 Specification
  - LoRaWAN Regional Parameters 1.0.3 revA
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC
from cryptography.hazmat.backends import default_backend


# ===========================================================
#   Bandas LoRaWAN — defaults para frequência de Join, SF, BW
# ===========================================================
# Notas:
#   - Em AU915/US915, OTAA Join Request usa canais 0-63 (125 kHz) ou 64-71 (500 kHz).
#     Para single-channel gateway, fixamos 1 canal default da sub-band 8 (BR).
#   - RX1: mesma freq do uplink (com offset por DR); RX2: freq fixa por banda.
BANDS: dict[str, dict] = {
    "AU915 (Brasil sub-band 8)": {
        "join_freq_mhz": 916.8,    # canal 64 (500 kHz) — uplink Join padrão sub-band 8
        "rx2_freq_mhz": 923.3,
        "default_sf": 10,
        "default_bw_khz": 500,
        "net_id": 0x000000,
    },
    "US915 (sub-band 1)": {
        "join_freq_mhz": 902.3,
        "rx2_freq_mhz": 923.3,
        "default_sf": 10,
        "default_bw_khz": 125,
        "net_id": 0x000000,
    },
    "EU868": {
        "join_freq_mhz": 868.1,
        "rx2_freq_mhz": 869.525,
        "default_sf": 12,
        "default_bw_khz": 125,
        "net_id": 0x000000,
    },
    "AS923-AS1": {
        "join_freq_mhz": 923.2,
        "rx2_freq_mhz": 923.2,
        "default_sf": 10,
        "default_bw_khz": 125,
        "net_id": 0x000000,
    },
    "IN865": {
        "join_freq_mhz": 865.0625,
        "rx2_freq_mhz": 866.55,
        "default_sf": 12,
        "default_bw_khz": 125,
        "net_id": 0x000000,
    },
    "KR920": {
        "join_freq_mhz": 922.1,
        "rx2_freq_mhz": 921.9,
        "default_sf": 12,
        "default_bw_khz": 125,
        "net_id": 0x000000,
    },
}


# ===========================================================
#   Tipos MAC (3 bits MType no MHDR)
# ===========================================================
MTYPE_JOIN_REQUEST = 0b000
MTYPE_JOIN_ACCEPT = 0b001
MTYPE_UNCONF_DATA_UP = 0b010
MTYPE_UNCONF_DATA_DN = 0b011
MTYPE_CONF_DATA_UP = 0b100
MTYPE_CONF_DATA_DN = 0b101


def mhdr(mtype: int) -> int:
    """Constrói o byte MHDR (MType nos 3 bits altos, major=000 nos 2 baixos)."""
    return (mtype & 0b111) << 5


def parse_mhdr(byte: int) -> int:
    """Extrai MType do byte MHDR."""
    return (byte >> 5) & 0b111


# ===========================================================
#   Primitivas AES-128
# ===========================================================
def aes128_cmac(key: bytes, msg: bytes) -> bytes:
    """AES-128 CMAC — usado para MIC de todos os frames LoRaWAN."""
    if len(key) != 16:
        raise ValueError(f"AppKey/SKey deve ter 16 bytes, recebeu {len(key)}")
    c = CMAC(algorithms.AES(key), backend=default_backend())
    c.update(msg)
    return c.finalize()


def aes128_ecb_encrypt(key: bytes, plain: bytes) -> bytes:
    """AES-128 ECB encrypt (1 bloco ou múltiplos)."""
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(plain) + enc.finalize()


def aes128_ecb_decrypt(key: bytes, cipher_bytes: bytes) -> bytes:
    """AES-128 ECB decrypt — usado para CIFRAR Join Accept (servidor)."""
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    dec = cipher.decryptor()
    return dec.update(cipher_bytes) + dec.finalize()


def random_bytes(n: int) -> bytes:
    return os.urandom(n)


# ===========================================================
#   Derivação de chaves de sessão (LoRaWAN 1.0.x)
# ===========================================================
def derive_session_keys(app_key: bytes, app_nonce: bytes, net_id: int, dev_nonce: bytes) -> tuple[bytes, bytes]:
    """
    Retorna (NwkSKey, AppSKey).
    NwkSKey = aes128_encrypt(AppKey, 0x01 | AppNonce | NetID | DevNonce | pad16)
    AppSKey = aes128_encrypt(AppKey, 0x02 | AppNonce | NetID | DevNonce | pad16)
    """
    if len(app_nonce) != 3:
        raise ValueError("AppNonce deve ter 3 bytes")
    if len(dev_nonce) != 2:
        raise ValueError("DevNonce deve ter 2 bytes")
    net_id_bytes = net_id.to_bytes(3, "little")
    pad = bytes(7)
    block_nwk = bytes([0x01]) + app_nonce + net_id_bytes + dev_nonce + pad
    block_app = bytes([0x02]) + app_nonce + net_id_bytes + dev_nonce + pad
    nwk_skey = aes128_ecb_encrypt(app_key, block_nwk)
    app_skey = aes128_ecb_encrypt(app_key, block_app)
    return nwk_skey, app_skey


# ===========================================================
#   Join Request
# ===========================================================
@dataclass
class JoinRequest:
    """Parser/validador de Join Request frame.

    Layout (23 bytes):
      MHDR(1) | JoinEUI(8, LE) | DevEUI(8, LE) | DevNonce(2, LE) | MIC(4)
    """
    raw: bytes
    join_eui: bytes      # 8 bytes BE (depois de reverter LE da serial)
    dev_eui: bytes       # 8 bytes BE
    dev_nonce: bytes     # 2 bytes BE
    mic: bytes           # 4 bytes

    @classmethod
    def parse(cls, raw: bytes) -> "JoinRequest":
        if len(raw) != 23:
            raise ValueError(f"Join Request deve ter 23 bytes, recebeu {len(raw)}")
        if parse_mhdr(raw[0]) != MTYPE_JOIN_REQUEST:
            raise ValueError(f"MHDR não é Join Request: 0x{raw[0]:02X}")
        # LoRaWAN transmite EUIs em little-endian; armazenamos em BE para exibição
        join_eui = raw[1:9][::-1]
        dev_eui = raw[9:17][::-1]
        dev_nonce = raw[17:19][::-1]
        mic = raw[19:23]
        return cls(raw=raw, join_eui=join_eui, dev_eui=dev_eui, dev_nonce=dev_nonce, mic=mic)

    def validate_mic(self, app_key: bytes) -> bool:
        """MIC = aes128_cmac(AppKey, MHDR | JoinEUI(LE) | DevEUI(LE) | DevNonce(LE))[0:4]."""
        computed = aes128_cmac(app_key, self.raw[:-4])[:4]
        return computed == self.mic


# ===========================================================
#   Join Accept
# ===========================================================
def build_join_accept(
    app_key: bytes,
    app_nonce: bytes,         # 3 bytes
    net_id: int,
    dev_addr: int,            # 4 bytes (será serializado em LE)
    dl_settings: int = 0,     # RX1DRoffset(3) | RX2DataRate(4)
    rx_delay: int = 1,        # delay em segundos (1 = janela RX1 em 5s? não — rx_delay=1 significa 1s default)
    cf_list: bytes | None = None,
) -> bytes:
    """
    Constrói um Join Accept frame cifrado, pronto para transmitir.

    Layout do plaintext (sem MHDR):
      AppNonce(3, LE) | NetID(3, LE) | DevAddr(4, LE) | DLSettings(1) | RxDelay(1) | [CFList(16)] | MIC(4)

    O servidor cifra o plaintext usando AES-128 ECB DECRYPT com AppKey.
    Frame final = MHDR | encrypted

    NOTA sobre rx_delay: campo de 4 bits no payload (0..15). 0 e 1 significam 1s.
    Para LoRaWAN 1.0 a janela RX1 abre rx_delay segundos após o fim do uplink.

    IMPORTANTE: app_nonce é usado "como está" tanto no frame quanto na derivação
    de chaves (derive_session_keys). Não invertemos — endianness do AppNonce é
    apenas convenção do fio; o que importa é que servidor e end-device usem a
    MESMA sequência de bytes em ambos os lugares.
    """
    if len(app_nonce) != 3:
        raise ValueError("AppNonce deve ter 3 bytes")
    cf_list = cf_list or b""
    if cf_list and len(cf_list) != 16:
        raise ValueError("CFList deve ter 0 ou 16 bytes")

    mhdr_byte = bytes([mhdr(MTYPE_JOIN_ACCEPT)])
    body = (
        app_nonce                          # 3 bytes — mesma ordem usada em derive_session_keys
        + net_id.to_bytes(3, "little")     # NetID LE
        + dev_addr.to_bytes(4, "little")   # DevAddr LE
        + bytes([dl_settings, rx_delay])
        + cf_list
    )
    # MIC sobre (MHDR | body) usando AppKey
    mic = aes128_cmac(app_key, mhdr_byte + body)[:4]
    plaintext_to_cipher = body + mic
    # ECB sem padding: tamanho precisa ser múltiplo de 16
    if len(plaintext_to_cipher) % 16 != 0:
        raise ValueError(f"Payload Join Accept não múltiplo de 16: {len(plaintext_to_cipher)}")
    encrypted = aes128_ecb_decrypt(app_key, plaintext_to_cipher)
    return mhdr_byte + encrypted


# ===========================================================
#   Data Frame (Uplink/Downlink)
# ===========================================================
@dataclass
class DataFrame:
    """Parser/builder de Data Up/Down frame.

    Layout:
      MHDR(1) | DevAddr(4, LE) | FCtrl(1) | FCnt(2, LE) | FOpts(0-15) | [FPort(1) | FRMPayload(N)] | MIC(4)
    """
    raw: bytes
    mtype: int
    dev_addr: int
    fctrl: int
    fcnt: int
    fopts: bytes
    fport: int | None
    frm_payload: bytes
    mic: bytes
    dir_uplink: bool       # True se uplink, False se downlink

    @classmethod
    def parse(cls, raw: bytes) -> "DataFrame":
        if len(raw) < 12:
            raise ValueError(f"Data frame curto demais: {len(raw)} bytes")
        mt = parse_mhdr(raw[0])
        if mt not in (MTYPE_UNCONF_DATA_UP, MTYPE_CONF_DATA_UP,
                      MTYPE_UNCONF_DATA_DN, MTYPE_CONF_DATA_DN):
            raise ValueError(f"MType não é data frame: {mt}")
        dir_up = mt in (MTYPE_UNCONF_DATA_UP, MTYPE_CONF_DATA_UP)
        dev_addr = int.from_bytes(raw[1:5], "little")
        fctrl = raw[5]
        fcnt = int.from_bytes(raw[6:8], "little")
        fopts_len = fctrl & 0x0F
        if 8 + fopts_len > len(raw) - 4:
            raise ValueError("FOptsLen excede tamanho do frame")
        fopts = raw[8:8 + fopts_len]
        rest = raw[8 + fopts_len:-4]
        if rest:
            fport = rest[0]
            frm_payload = rest[1:]
        else:
            fport = None
            frm_payload = b""
        mic = raw[-4:]
        return cls(
            raw=raw, mtype=mt, dev_addr=dev_addr, fctrl=fctrl, fcnt=fcnt,
            fopts=fopts, fport=fport, frm_payload=frm_payload, mic=mic,
            dir_uplink=dir_up,
        )

    def validate_mic(self, nwk_skey: bytes, fcnt32: int | None = None) -> bool:
        """
        MIC = aes128_cmac(NwkSKey, B0 | msg_sem_MIC)[0:4]
        B0 = 0x49 | 0x00x4 | Dir(1) | DevAddr(4, LE) | FCnt32(4, LE) | 0x00 | len(1)
        """
        if fcnt32 is None:
            fcnt32 = self.fcnt
        direction = 0 if self.dir_uplink else 1
        msg_no_mic = self.raw[:-4]
        b0 = (
            bytes([0x49, 0, 0, 0, 0, direction])
            + self.dev_addr.to_bytes(4, "little")
            + fcnt32.to_bytes(4, "little")
            + bytes([0, len(msg_no_mic)])
        )
        computed = aes128_cmac(nwk_skey, b0 + msg_no_mic)[:4]
        return computed == self.mic

    def decrypt_payload(self, app_skey: bytes, nwk_skey: bytes, fcnt32: int | None = None) -> bytes:
        """Cifra FRMPayload com AES-128 CTR. Retorna o payload claro."""
        if not self.frm_payload:
            return b""
        if fcnt32 is None:
            fcnt32 = self.fcnt
        key = nwk_skey if (self.fport == 0) else app_skey
        return _aes128_ctr_payload(key, self.dev_addr, fcnt32, self.dir_uplink, self.frm_payload)


def _aes128_ctr_payload(key: bytes, dev_addr: int, fcnt32: int, dir_uplink: bool, payload: bytes) -> bytes:
    """Cifra/decifra FRMPayload segundo LoRaWAN 1.0.x (operação simétrica)."""
    direction = 0 if dir_uplink else 1
    out = bytearray()
    n_blocks = (len(payload) + 15) // 16
    for i in range(1, n_blocks + 1):
        a_i = (
            bytes([0x01, 0, 0, 0, 0, direction])
            + dev_addr.to_bytes(4, "little")
            + fcnt32.to_bytes(4, "little")
            + bytes([0, i])
        )
        s_i = aes128_ecb_encrypt(key, a_i)
        start = (i - 1) * 16
        end = min(start + 16, len(payload))
        for j in range(start, end):
            out.append(payload[j] ^ s_i[j - start])
    return bytes(out)


def build_downlink(
    nwk_skey: bytes,
    app_skey: bytes,
    dev_addr: int,
    fcnt_down: int,
    fport: int,
    payload: bytes,
    confirmed: bool = False,
    fpending: bool = False,
    ack: bool = False,
) -> bytes:
    """Constrói um downlink completo (cifrado + MIC). Pronto para AT+PSEND."""
    mtype = MTYPE_CONF_DATA_DN if confirmed else MTYPE_UNCONF_DATA_DN
    mhdr_byte = bytes([mhdr(mtype)])
    fctrl = 0
    if ack: fctrl |= 0x20
    if fpending: fctrl |= 0x10
    # FOptsLen = 0 (sem opções MAC neste build mínimo)
    header = (
        dev_addr.to_bytes(4, "little")
        + bytes([fctrl])
        + (fcnt_down & 0xFFFF).to_bytes(2, "little")
    )
    encrypted = _aes128_ctr_payload(app_skey, dev_addr, fcnt_down, False, payload) if payload else b""
    body = header + bytes([fport]) + encrypted if payload else header
    msg_no_mic = mhdr_byte + body
    b0 = (
        bytes([0x49, 0, 0, 0, 0, 1])
        + dev_addr.to_bytes(4, "little")
        + fcnt_down.to_bytes(4, "little")
        + bytes([0, len(msg_no_mic)])
    )
    mic = aes128_cmac(nwk_skey, b0 + msg_no_mic)[:4]
    return msg_no_mic + mic


# ===========================================================
#   Helpers para devices.json
# ===========================================================
def hex_to_bytes(s: str) -> bytes:
    """Aceita 'AA:BB:CC' ou 'AABBCC' ou '0xAABBCC'."""
    s = s.strip().replace(":", "").replace(" ", "").replace("-", "")
    if s.lower().startswith("0x"):
        s = s[2:]
    return bytes.fromhex(s)


def bytes_to_hex(b: bytes, sep: str = "") -> str:
    return sep.join(f"{x:02X}" for x in b)

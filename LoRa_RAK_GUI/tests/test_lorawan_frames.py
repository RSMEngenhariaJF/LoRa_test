"""
Testes dos frames LoRaWAN — Join Request, Join Accept e DataFrame.

Constrói frames sinteticamente e valida:
  - Parse de Join Request + validação de MIC
  - Build de Join Accept (formato correto, cifrado, MIC válido)
  - Round-trip: server gera JA, end-device "decifra" e recupera o plaintext
  - DataFrame uplink: parse, MIC validation, decifra de payload
"""
import pytest

pytest.importorskip("cryptography", reason="lib 'cryptography' não instalada")

from lorawan import (
    aes128_cmac, aes128_ecb_encrypt,
    JoinRequest, build_join_accept, derive_session_keys,
    DataFrame, build_downlink, _aes128_ctr_payload,
    mhdr, parse_mhdr,
    MTYPE_JOIN_REQUEST, MTYPE_JOIN_ACCEPT,
    MTYPE_UNCONF_DATA_UP, MTYPE_CONF_DATA_UP,
    MTYPE_UNCONF_DATA_DN,
)

pytestmark = pytest.mark.crypto


# ===========================================================
#   Helpers para construir frames sintéticos
# ===========================================================
def make_join_request(app_key: bytes, join_eui_be: bytes, dev_eui_be: bytes, dev_nonce_be: bytes) -> bytes:
    """Constrói um Join Request válido (com MIC correto)."""
    mhdr_byte = bytes([mhdr(MTYPE_JOIN_REQUEST)])
    body = mhdr_byte + join_eui_be[::-1] + dev_eui_be[::-1] + dev_nonce_be[::-1]
    mic = aes128_cmac(app_key, body)[:4]
    return body + mic


# ===========================================================
#   Join Request
# ===========================================================
class TestJoinRequest:
    APP_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
    JOIN_EUI = bytes.fromhex("1122334455667788")
    DEV_EUI = bytes.fromhex("AABBCCDDEEFF0011")
    DEV_NONCE = bytes.fromhex("1234")

    def _frame(self):
        return make_join_request(self.APP_KEY, self.JOIN_EUI, self.DEV_EUI, self.DEV_NONCE)

    def test_tamanho_correto(self):
        assert len(self._frame()) == 23

    def test_parse_extrai_eui_corretos(self):
        jr = JoinRequest.parse(self._frame())
        assert jr.join_eui == self.JOIN_EUI
        assert jr.dev_eui == self.DEV_EUI
        assert jr.dev_nonce == self.DEV_NONCE

    def test_mic_valido_aceito(self):
        jr = JoinRequest.parse(self._frame())
        assert jr.validate_mic(self.APP_KEY)

    def test_mic_invalido_rejeitado_com_chave_errada(self):
        jr = JoinRequest.parse(self._frame())
        wrong_key = bytes(16)
        assert not jr.validate_mic(wrong_key)

    def test_mic_invalido_se_frame_corrompido(self):
        frame = bytearray(self._frame())
        frame[5] ^= 0xFF  # corrompe um byte do JoinEUI
        jr = JoinRequest.parse(bytes(frame))
        assert not jr.validate_mic(self.APP_KEY)

    def test_frame_tamanho_errado_lanca(self):
        with pytest.raises(ValueError):
            JoinRequest.parse(b"\x00" * 10)
        with pytest.raises(ValueError):
            JoinRequest.parse(b"\x00" * 30)

    def test_mtype_errado_lanca(self):
        # MHDR de Data Up, não Join Request
        raw = bytes([0x40]) + b"\x00" * 22
        with pytest.raises(ValueError):
            JoinRequest.parse(raw)


# ===========================================================
#   Join Accept
# ===========================================================
class TestJoinAccept:
    APP_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
    APP_NONCE = bytes.fromhex("AABBCC")
    NET_ID = 0x000001
    DEV_ADDR = 0x01ABCDEF

    def test_tamanho_sem_cflist(self):
        """JA sem CFList = MHDR(1) + body cifrado(16) = 17 bytes."""
        ja = build_join_accept(self.APP_KEY, self.APP_NONCE, self.NET_ID, self.DEV_ADDR)
        assert len(ja) == 17

    def test_mhdr_correto(self):
        ja = build_join_accept(self.APP_KEY, self.APP_NONCE, self.NET_ID, self.DEV_ADDR)
        assert parse_mhdr(ja[0]) == MTYPE_JOIN_ACCEPT
        assert ja[0] == 0x20

    def test_end_device_consegue_decifrar(self):
        """Server cifra com ECB decrypt; end-device usa ECB encrypt para decifrar (LoRaWAN spec)."""
        ja = build_join_accept(self.APP_KEY, self.APP_NONCE, self.NET_ID,
                                self.DEV_ADDR, dl_settings=0, rx_delay=5)
        # Plaintext recuperado pelo end-device
        plain = aes128_ecb_encrypt(self.APP_KEY, ja[1:])  # ignora MHDR
        # Verifica conteúdo: AppNonce | NetID | DevAddr | DLSettings | RxDelay | MIC
        assert plain[0:3] == self.APP_NONCE
        assert int.from_bytes(plain[3:6], "little") == self.NET_ID
        assert int.from_bytes(plain[6:10], "little") == self.DEV_ADDR
        assert plain[10] == 0  # DLSettings
        assert plain[11] == 5  # RxDelay
        # MIC ocupa os 4 bytes finais
        body_no_mic = bytes([ja[0]]) + plain[:-4]
        expected_mic = aes128_cmac(self.APP_KEY, body_no_mic)[:4]
        assert plain[-4:] == expected_mic

    def test_app_nonce_invalido(self):
        with pytest.raises(ValueError):
            build_join_accept(self.APP_KEY, b"AB", self.NET_ID, self.DEV_ADDR)


# ===========================================================
#   Round-trip Join: Server <-> End-Device gera mesmas chaves
# ===========================================================
class TestJoinRoundtrip:
    """Simula o fluxo completo OTAA e verifica que as chaves derivadas
    pelo lado servidor são idênticas às que o end-device derivaria."""

    APP_KEY = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
    NET_ID = 0x000013

    def test_server_e_device_derivam_mesmas_chaves(self):
        # End-device cria Join Request
        join_eui = bytes.fromhex("0000000000000001")
        dev_eui = bytes.fromhex("1111222233334444")
        dev_nonce = bytes.fromhex("ABCD")
        jr_raw = make_join_request(self.APP_KEY, join_eui, dev_eui, dev_nonce)

        # Server recebe e valida
        jr = JoinRequest.parse(jr_raw)
        assert jr.validate_mic(self.APP_KEY)

        # Server gera AppNonce e deriva chaves usando DevNonce (LE) recebido
        app_nonce = bytes.fromhex("DEADBE")
        # jr.dev_nonce está armazenado em BE; spec usa LE
        dev_nonce_le = jr.dev_nonce[::-1]
        nwk_server, app_server = derive_session_keys(
            self.APP_KEY, app_nonce, self.NET_ID, dev_nonce_le
        )

        # End-device deriva localmente com o mesmo DevNonce (que ele próprio gerou em LE)
        dev_nonce_le_device = dev_nonce[::-1]  # como o device originalmente serializou
        nwk_device, app_device = derive_session_keys(
            self.APP_KEY, app_nonce, self.NET_ID, dev_nonce_le_device
        )

        assert nwk_server == nwk_device, "Server e device geram NwkSKey diferentes — bug grave"
        assert app_server == app_device, "Server e device geram AppSKey diferentes — bug grave"


# ===========================================================
#   DataFrame (uplink)
# ===========================================================
class TestDataFrameUplink:
    """Constrói um uplink válido sinteticamente e testa parse/MIC/decifra."""

    def _build_uplink(self, nwk_skey, app_skey, dev_addr, fcnt, fport, payload, confirmed=False):
        """Constrói um uplink completo (cifrado + MIC)."""
        mtype = MTYPE_CONF_DATA_UP if confirmed else MTYPE_UNCONF_DATA_UP
        mhdr_byte = bytes([mhdr(mtype)])
        header = (
            dev_addr.to_bytes(4, "little")
            + bytes([0])  # FCtrl = 0
            + (fcnt & 0xFFFF).to_bytes(2, "little")
        )
        encrypted = _aes128_ctr_payload(app_skey, dev_addr, fcnt, True, payload) if payload else b""
        body = header + bytes([fport]) + encrypted
        msg_no_mic = mhdr_byte + body
        b0 = (
            bytes([0x49, 0, 0, 0, 0, 0])  # uplink (Dir=0)
            + dev_addr.to_bytes(4, "little")
            + fcnt.to_bytes(4, "little")
            + bytes([0, len(msg_no_mic)])
        )
        mic = aes128_cmac(nwk_skey, b0 + msg_no_mic)[:4]
        return msg_no_mic + mic

    def test_parse_uplink_valido(self):
        nwk = bytes(range(16))
        app = bytes(range(16, 32))
        dev_addr = 0x12345678
        fcnt = 5
        payload = b"\x01\x02\x03"

        raw = self._build_uplink(nwk, app, dev_addr, fcnt, fport=2, payload=payload)
        df = DataFrame.parse(raw)

        assert df.dev_addr == dev_addr
        assert df.fcnt == fcnt
        assert df.fport == 2
        assert df.dir_uplink is True

    def test_mic_valido(self):
        nwk = bytes(range(16))
        app = bytes(range(16, 32))
        raw = self._build_uplink(nwk, app, 0x12345678, 5, 2, b"\x01\x02")
        df = DataFrame.parse(raw)
        assert df.validate_mic(nwk, fcnt32=5)

    def test_mic_invalido_chave_errada(self):
        nwk = bytes(range(16))
        app = bytes(range(16, 32))
        raw = self._build_uplink(nwk, app, 0x12345678, 5, 2, b"\x01\x02")
        df = DataFrame.parse(raw)
        wrong = bytes(16)
        assert not df.validate_mic(wrong, fcnt32=5)

    def test_payload_decifrado_corretamente(self):
        nwk = bytes(range(16))
        app = bytes(range(16, 32))
        payload_clear = b"Hello LoRaWAN!"
        raw = self._build_uplink(nwk, app, 0x12345678, 5, 2, payload_clear)
        df = DataFrame.parse(raw)
        decifrado = df.decrypt_payload(app, nwk, fcnt32=5)
        assert decifrado == payload_clear

    def test_payload_fport_zero_usa_nwkskey(self):
        """FPort=0 → cifrado com NwkSKey (não AppSKey)."""
        nwk = bytes(range(16))
        app = bytes(range(16, 32))
        payload_clear = b"\xAB\xCD"
        # Build com nwk fazendo papel de "chave de cifra"
        raw = self._build_uplink(nwk, nwk, 0x12345678, 5, 0, payload_clear)
        # Refaz manualmente porque o helper acima usa app_skey
        # Vamos só validar que decrypt usa nwk quando fport=0
        df = DataFrame.parse(raw)
        # Se fport=0, decrypt usa nwk_skey internamente
        # (a função _aes128_ctr_payload é simétrica, então isso funciona)
        decifrado = df.decrypt_payload(app, nwk, fcnt32=5)
        # Como cifrei com nwk e estou pedindo decrypt em fport=0 (que usa nwk), deve bater
        assert decifrado == payload_clear


# ===========================================================
#   MType helpers
# ===========================================================
class TestMType:
    def test_mhdr_join_request(self):
        assert mhdr(MTYPE_JOIN_REQUEST) == 0x00

    def test_mhdr_join_accept(self):
        assert mhdr(MTYPE_JOIN_ACCEPT) == 0x20

    def test_mhdr_unconf_uplink(self):
        assert mhdr(MTYPE_UNCONF_DATA_UP) == 0x40

    def test_mhdr_unconf_downlink(self):
        assert mhdr(MTYPE_UNCONF_DATA_DN) == 0x60

    def test_parse_mhdr_inverte(self):
        for mt in [MTYPE_JOIN_REQUEST, MTYPE_JOIN_ACCEPT,
                   MTYPE_UNCONF_DATA_UP, MTYPE_CONF_DATA_UP]:
            assert parse_mhdr(mhdr(mt)) == mt

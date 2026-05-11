"""
Testes das primitivas crypto do módulo lorawan.

Validações:
  - AES-CMAC contra vetor de teste NIST oficial
  - AES-128 ECB encrypt/decrypt (round-trip)
  - Derivação de chaves de sessão (NwkSKey != AppSKey)
  - Helpers hex_to_bytes / bytes_to_hex

Marcados com @pytest.mark.crypto — skipam se 'cryptography' não estiver instalado.
"""
import pytest

# Skip todos os testes deste arquivo se cryptography não estiver disponível
crypto = pytest.importorskip("cryptography", reason="lib 'cryptography' não instalada")

from lorawan import (
    aes128_cmac, aes128_ecb_encrypt, aes128_ecb_decrypt,
    derive_session_keys, random_bytes,
    hex_to_bytes, bytes_to_hex,
)

pytestmark = pytest.mark.crypto


# ===========================================================
#   AES-CMAC
# ===========================================================
class TestAES_CMAC:
    """
    Vetor de teste oficial NIST SP 800-38B, Example 1 (AES-128):
      Key: 2B7E151628AED2A6ABF7158809CF4F3C
      Msg: <empty>
      Tag: BB1D6929E95937287FA37D129B756746
    """
    KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")

    def test_vetor_nist_msg_vazia(self):
        tag = aes128_cmac(self.KEY, b"")
        assert tag.hex().upper() == "BB1D6929E95937287FA37D129B756746"

    def test_vetor_nist_msg_16_bytes(self):
        """NIST SP 800-38B Example 2: msg de 16 bytes."""
        msg = bytes.fromhex("6BC1BEE22E409F96E93D7E117393172A")
        tag = aes128_cmac(self.KEY, msg)
        assert tag.hex().upper() == "070A16B46B4D4144F79BDD9DD04A287C"

    def test_chave_invalida_lanca_erro(self):
        with pytest.raises(ValueError):
            aes128_cmac(b"chave_curta", b"abc")

    def test_mesma_msg_mesma_tag(self):
        """CMAC é determinístico."""
        tag1 = aes128_cmac(self.KEY, b"hello")
        tag2 = aes128_cmac(self.KEY, b"hello")
        assert tag1 == tag2

    def test_msg_diferente_tag_diferente(self):
        tag1 = aes128_cmac(self.KEY, b"hello")
        tag2 = aes128_cmac(self.KEY, b"hellO")
        assert tag1 != tag2


# ===========================================================
#   AES-128 ECB
# ===========================================================
class TestAES_ECB:
    KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")

    def test_roundtrip_um_bloco(self):
        plain = b"X" * 16
        cipher = aes128_ecb_encrypt(self.KEY, plain)
        assert len(cipher) == 16
        back = aes128_ecb_decrypt(self.KEY, cipher)
        assert back == plain

    def test_vetor_nist_aes_ecb(self):
        """FIPS 197 Appendix C.1 — AES-128 ECB."""
        plain = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
        key = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
        cipher = aes128_ecb_encrypt(key, plain)
        assert cipher.hex().upper() == "69C4E0D86A7B0430D8CDB78070B4C55A"

    def test_decrypt_e_inverso_de_encrypt(self):
        """Importante para LoRaWAN: server usa ECB decrypt para cifrar Join Accept."""
        plain = b"A" * 16
        # Servidor "cifra" usando decrypt
        server_cipher = aes128_ecb_decrypt(self.KEY, plain)
        # End-device "decifra" usando encrypt
        recovered = aes128_ecb_encrypt(self.KEY, server_cipher)
        assert recovered == plain


# ===========================================================
#   Derivação de chaves de sessão
# ===========================================================
class TestDeriveSessionKeys:
    APP_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
    APP_NONCE = bytes.fromhex("AABBCC")
    NET_ID = 0x000001
    DEV_NONCE = bytes.fromhex("0102")

    def test_chaves_tem_16_bytes(self):
        nwk, app = derive_session_keys(self.APP_KEY, self.APP_NONCE, self.NET_ID, self.DEV_NONCE)
        assert len(nwk) == 16
        assert len(app) == 16

    def test_nwk_e_app_skey_diferentes(self):
        """NwkSKey usa prefixo 0x01, AppSKey usa 0x02 → devem ser diferentes."""
        nwk, app = derive_session_keys(self.APP_KEY, self.APP_NONCE, self.NET_ID, self.DEV_NONCE)
        assert nwk != app

    def test_deriva_deterministicamente(self):
        nwk1, app1 = derive_session_keys(self.APP_KEY, self.APP_NONCE, self.NET_ID, self.DEV_NONCE)
        nwk2, app2 = derive_session_keys(self.APP_KEY, self.APP_NONCE, self.NET_ID, self.DEV_NONCE)
        assert nwk1 == nwk2
        assert app1 == app2

    def test_dev_nonce_diferente_chave_diferente(self):
        nwk1, _ = derive_session_keys(self.APP_KEY, self.APP_NONCE, self.NET_ID, b"\x01\x02")
        nwk2, _ = derive_session_keys(self.APP_KEY, self.APP_NONCE, self.NET_ID, b"\x03\x04")
        assert nwk1 != nwk2

    def test_app_nonce_invalido(self):
        with pytest.raises(ValueError):
            derive_session_keys(self.APP_KEY, b"AB", self.NET_ID, self.DEV_NONCE)  # 2 bytes em vez de 3

    def test_dev_nonce_invalido(self):
        with pytest.raises(ValueError):
            derive_session_keys(self.APP_KEY, self.APP_NONCE, self.NET_ID, b"X")  # 1 byte em vez de 2


# ===========================================================
#   Random bytes
# ===========================================================
class TestRandomBytes:
    def test_tamanho_correto(self):
        for n in [1, 3, 16, 32]:
            assert len(random_bytes(n)) == n

    def test_aleatorio_diferente_a_cada_chamada(self):
        # 16 bytes têm 2^128 possibilidades — colisão é astronômicamente improvável
        a = random_bytes(16)
        b = random_bytes(16)
        assert a != b


# ===========================================================
#   hex_to_bytes / bytes_to_hex
# ===========================================================
class TestHexHelpers:
    def test_hex_to_bytes_compacto(self):
        assert hex_to_bytes("AABBCC") == b"\xaa\xbb\xcc"

    def test_hex_to_bytes_com_dois_pontos(self):
        assert hex_to_bytes("AA:BB:CC") == b"\xaa\xbb\xcc"

    def test_hex_to_bytes_prefixo_0x(self):
        assert hex_to_bytes("0xAABBCC") == b"\xaa\xbb\xcc"

    def test_hex_to_bytes_aceita_espacos(self):
        assert hex_to_bytes("AA BB CC") == b"\xaa\xbb\xcc"

    def test_bytes_to_hex_sem_separador(self):
        assert bytes_to_hex(b"\xaa\xbb\xcc") == "AABBCC"

    def test_bytes_to_hex_com_separador(self):
        assert bytes_to_hex(b"\xaa\xbb\xcc", sep=":") == "AA:BB:CC"

    def test_roundtrip(self):
        original = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        assert hex_to_bytes(bytes_to_hex(original)) == original

"""
Testes de integração — fluxos end-to-end com FakeSerial.

Simula cenários reais cruzando vários módulos:
  - Sessão completa: open_txt + send_at + debug_cb + CSV
  - Gateway processa Join Request fake e gera Join Accept válido
"""
import os
import queue

import pytest

from drivers import RAK3172
from logger import SessionLogger, PacketResult


# ===========================================================
#   Sessão completa: drivers + logger
# ===========================================================
class TestSessionFlow:
    """
    Cenário: usuário abre porta, envia AT, sessão registra
    em TXT + debug.txt, exporta CSV.
    """

    def test_fluxo_completo(self, make_device, monkeypatch, tmp_path):
        # Redireciona LOGS_DIR
        import logger as logger_mod
        monkeypatch.setattr(logger_mod, "LOGS_DIR", str(tmp_path))

        # Cria driver com FakeSerial
        dev, fake = make_device(RAK3172, responses=["OK\r\n", "OK\r\n"])

        # Cria SessionLogger e pluga callback de debug
        q = queue.Queue()
        sess = SessionLogger(log_queue=q)
        sess.open_txt(device_name=dev.name)
        dev.debug_cb = sess.debug_log

        # Conecta e faz operações
        dev.open()
        dev.send_at("AT")
        dev.send_at("AT+VER=?")

        # Adiciona resultado fake
        sess.add_result(PacketResult(
            timestamp="2026-05-11 10:00", device=dev.name, mode="TX",
            seq=0, payload_tx="01", payload_rx="01", match=1, attempts=1,
            timeout_s=5.0, retries=0, rssi="-50", snr="8", rtt_ms="120",
            freq_tx_hz="904000000", freq_rx_hz="904000000",
        ))

        # Exporta CSV
        csv_path = tmp_path / "results.csv"
        sess.save_csv(str(csv_path))

        sess.close_txt()

        # Verificações
        assert os.path.exists(sess.txt_path)
        assert os.path.exists(sess.debug_path)
        assert csv_path.exists()

        # debug.txt deve conter os comandos enviados
        with open(sess.debug_path, encoding="utf-8") as f:
            debug = f.read()
        assert "AT+VER=?" in debug
        assert "TX>" in debug
        assert "RX<" in debug


# ===========================================================
#   Gateway recebe Join Request e gera Join Accept
# ===========================================================
class TestGatewayJoinFlow:
    """Simula fluxo OTAA: end-device envia JR, gateway valida + responde JA."""

    def test_join_completo(self):
        crypto = pytest.importorskip("cryptography")
        from lorawan import (
            aes128_cmac, JoinRequest, build_join_accept,
            derive_session_keys, mhdr, MTYPE_JOIN_REQUEST,
        )

        # Setup do device cadastrado no servidor
        app_key = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
        dev_eui = bytes.fromhex("AABBCCDDEEFF0011")
        join_eui = bytes.fromhex("0000000000000000")
        dev_nonce = bytes.fromhex("0001")

        # 1. End-device monta Join Request
        body = bytes([mhdr(MTYPE_JOIN_REQUEST)]) + join_eui[::-1] + dev_eui[::-1] + dev_nonce[::-1]
        mic = aes128_cmac(app_key, body)[:4]
        jr_frame = body + mic

        # 2. Gateway parseia e valida
        jr = JoinRequest.parse(jr_frame)
        assert jr.dev_eui == dev_eui
        assert jr.validate_mic(app_key)

        # 3. Gateway aloca DevAddr, gera AppNonce, deriva chaves
        dev_addr = 0x01ABCDEF
        app_nonce = bytes.fromhex("123456")
        net_id = 0x000013
        nwk_skey, app_skey = derive_session_keys(
            app_key, app_nonce, net_id, jr.dev_nonce[::-1]
        )
        assert len(nwk_skey) == 16
        assert len(app_skey) == 16
        assert nwk_skey != app_skey

        # 4. Gateway constrói Join Accept
        ja = build_join_accept(
            app_key=app_key, app_nonce=app_nonce, net_id=net_id,
            dev_addr=dev_addr, dl_settings=0, rx_delay=5,
        )
        assert len(ja) == 17

        # 5. End-device "decifra" o JA (usa AES ECB encrypt)
        from lorawan import aes128_ecb_encrypt
        plain = aes128_ecb_encrypt(app_key, ja[1:])
        assert plain[0:3] == app_nonce  # AppNonce no início
        assert int.from_bytes(plain[6:10], "little") == dev_addr  # DevAddr

        # 6. End-device deriva chaves localmente — devem bater
        nwk_dev, app_dev = derive_session_keys(
            app_key, app_nonce, net_id, dev_nonce[::-1]
        )
        assert nwk_dev == nwk_skey, "Server e device derivam NwkSKey diferentes!"
        assert app_dev == app_skey, "Server e device derivam AppSKey diferentes!"


# ===========================================================
#   Smoke test: importa app.py e instancia (sem mostrar janela)
# ===========================================================
@pytest.mark.slow
class TestAppSmoke:
    """Não instancia janela Tk; só verifica que imports e estrutura compilam."""

    def test_app_importa_sem_erro(self):
        """Não roda a App (Tk precisa de display), mas verifica que módulo carrega."""
        # Só verifica que o módulo importa — não instancia App
        import app
        assert hasattr(app, "App")
        assert hasattr(app, "LORAWAN_AVAILABLE")

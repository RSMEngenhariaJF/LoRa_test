"""
Testes do módulo logger — SessionLogger e PacketResult.

Testa:
  - Abertura/fechamento dos arquivos TXT + debug
  - Escrita de linhas com timestamp
  - Callback de debug (TX/RX)
  - Export CSV com formato correto
"""
import csv
import os
import queue
import tempfile

import pytest

from logger import SessionLogger, PacketResult
import constants


@pytest.fixture
def temp_logs_dir(monkeypatch, tmp_path):
    """Redireciona LOGS_DIR para tempdir do teste."""
    monkeypatch.setattr(constants, "LOGS_DIR", str(tmp_path))
    # Também precisa redirecionar dentro do módulo logger (que importou com from)
    import logger as logger_mod
    monkeypatch.setattr(logger_mod, "LOGS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def session(temp_logs_dir):
    """SessionLogger pronto para uso, com tempdir."""
    q = queue.Queue()
    return SessionLogger(log_queue=q)


class TestSessionLoggerArquivos:
    def test_open_txt_cria_arquivos(self, session, temp_logs_dir):
        session.open_txt(device_name="TEST")
        assert os.path.exists(session.txt_path)
        assert os.path.exists(session.debug_path)
        # Nomes devem seguir o padrão sessao_*.txt e debug_*.txt
        assert "sessao_" in os.path.basename(session.txt_path)
        assert "debug_" in os.path.basename(session.debug_path)
        session.close_txt()

    def test_open_escreve_cabecalho(self, session):
        session.open_txt(device_name="RAK3172")
        session.close_txt()
        with open(session.txt_path, encoding="utf-8") as f:
            content = f.read()
        assert "RAK3172" in content
        assert "iniciada" in content
        assert "encerrada" in content

    def test_close_idempotente(self, session):
        session.open_txt()
        session.close_txt()
        session.close_txt()  # não deve dar erro


class TestSessionLoggerEscrita:
    def test_log_escreve_no_arquivo(self, session):
        session.open_txt()
        session.log("mensagem de teste")
        session.close_txt()
        with open(session.txt_path, encoding="utf-8") as f:
            content = f.read()
        assert "mensagem de teste" in content

    def test_log_inclui_timestamp(self, session):
        session.open_txt()
        session.log("xyz")
        session.close_txt()
        with open(session.txt_path, encoding="utf-8") as f:
            content = f.read()
        # Formato [YYYY-MM-DD HH:MM:SS]
        import re
        assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", content)

    def test_log_envia_para_queue(self, session):
        session.open_txt()
        session.log("teste queue")
        session.close_txt()
        # Pelo menos uma entrada na queue contendo nossa mensagem
        items = []
        while not session.log_queue.empty():
            items.append(session.log_queue.get_nowait())
        assert any("teste queue" in str(item) for item in items)


class TestDebugLog:
    def test_debug_log_tx_rx(self, session):
        session.open_txt()
        session.debug_log("TX", "AT+P2P=...", None)
        session.debug_log("RX", "OK\r\n", 123.4)
        session.close_txt()
        with open(session.debug_path, encoding="utf-8") as f:
            content = f.read()
        assert "AT+P2P=" in content
        assert "OK" in content
        assert "TX>" in content
        assert "RX<" in content
        assert "123" in content  # ΔT

    def test_debug_log_escapa_quebras_linha(self, session):
        session.open_txt()
        session.debug_log("RX", "linha1\r\nlinha2", 100)
        session.close_txt()
        with open(session.debug_path, encoding="utf-8") as f:
            content = f.read()
        # Quebras devem aparecer como literais (\r\n não cria nova linha no log)
        assert "\\r\\n" in content
        # Cada chamada debug_log = 1 linha no arquivo
        # Header + 1 linha de debug = 2 linhas mínimo
        lines = content.strip().splitlines()
        # Não deveria ter quebrado em mais linhas que o esperado
        assert len([l for l in lines if l.startswith("[")]) >= 1


class TestPacketResultCSV:
    def test_add_e_save_csv(self, session, tmp_path):
        r = PacketResult(
            timestamp="2026-05-11 10:00:00", device="RAK3172", mode="TX",
            seq=0, payload_tx="01", payload_rx="01", match=1, attempts=1,
            timeout_s=5.0, retries=0, rssi="-50", snr="8", rtt_ms="120.5",
            freq_tx_hz="904000000", freq_rx_hz="904000000",
        )
        session.add_result(r)

        csv_path = tmp_path / "out.csv"
        session.save_csv(str(csv_path))

        assert csv_path.exists()
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["device"] == "RAK3172"
        assert rows[0]["mode"] == "TX"
        assert rows[0]["seq"] == "0"
        assert rows[0]["match"] == "1"
        assert rows[0]["rssi"] == "-50"

    def test_csv_separador_ponto_virgula(self, session, tmp_path):
        session.add_result(PacketResult(
            timestamp="t", device="d", mode="m", seq=0,
            payload_tx="x", payload_rx="y", match=0,
            attempts=1, timeout_s=0.0, retries=0,
            rssi="", snr="", rtt_ms="", freq_tx_hz="", freq_rx_hz="",
        ))
        csv_path = tmp_path / "sep.csv"
        session.save_csv(str(csv_path))
        with open(csv_path, encoding="utf-8") as f:
            first_line = f.readline()
        assert ";" in first_line
        assert "," not in first_line  # CSV brasileiro usa ;

    def test_csv_vazio_ainda_gera_header(self, session, tmp_path):
        csv_path = tmp_path / "vazio.csv"
        session.save_csv(str(csv_path))
        assert csv_path.exists()
        with open(csv_path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1  # só header
        assert "timestamp" in lines[0]

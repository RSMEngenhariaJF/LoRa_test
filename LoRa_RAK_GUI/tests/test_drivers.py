"""
Testes dos drivers de dispositivo (RAK3172, KG200Z, SMART SX1262).

Usa FakeSerial (conftest.py) para simular a porta UART. Valida:
  - Atributos de classe (name, default_baudrate, capabilities, ping_cmd)
  - Templates LW_CMDS (sintaxe AT correta por chip)
  - configure_radio gera os comandos AT esperados
  - parse_rx_line extrai RSSI/SNR/payload conforme formato de cada chip
  - PNM (Public Network Mode) chama o comando correto
  - Modo LoRaWAN enter/exit (apenas RAK3172)
"""
import pytest

from drivers import DEVICES, LoRaDevice
from drivers.rak3172 import RAK3172
from drivers.kg200z import KG200Z
from drivers.smart_sx1262 import SMARTSx1262


# ===========================================================
#   Testes que se aplicam a TODOS os drivers
# ===========================================================
ALL_DRIVERS = [RAK3172, KG200Z, SMARTSx1262]


class TestRegistroDevices:
    def test_devices_dict_tem_3_dispositivos(self):
        assert len(DEVICES) == 3

    def test_chaves_sao_strings_humanas(self):
        assert "RAK3172" in DEVICES
        assert "Quectel KG200Z" in DEVICES
        assert "SMART SMW-SX1262M0" in DEVICES

    def test_todos_herdam_de_LoRaDevice(self):
        for cls in DEVICES.values():
            assert issubclass(cls, LoRaDevice)


class TestAtributosClasse:
    @pytest.mark.parametrize("cls", ALL_DRIVERS)
    def test_tem_name(self, cls):
        assert isinstance(cls.name, str) and len(cls.name) > 0

    @pytest.mark.parametrize("cls", ALL_DRIVERS)
    def test_tem_baudrate_padrao_razoavel(self, cls):
        assert cls.default_baudrate in (9600, 19200, 38400, 57600, 115200)

    @pytest.mark.parametrize("cls", ALL_DRIVERS)
    def test_capabilities_validas(self, cls):
        VALID_CAPS = {"TX", "RX", "CW", "SCAN", "INCREMENTAL", "LORAWAN"}
        assert cls.capabilities, f"{cls.name} sem capabilities"
        assert cls.capabilities.issubset(VALID_CAPS), \
            f"{cls.name} tem capability desconhecida: {cls.capabilities - VALID_CAPS}"

    @pytest.mark.parametrize("cls", ALL_DRIVERS)
    def test_ping_cmd_definido(self, cls):
        assert cls.ping_cmd in ("AT", "ATQ"), f"{cls.name}: ping_cmd '{cls.ping_cmd}'"

    @pytest.mark.parametrize("cls", ALL_DRIVERS)
    def test_lw_cmds_nao_vazio(self, cls):
        assert len(cls.LW_CMDS) > 0, f"{cls.name} não tem comandos LoRaWAN"


class TestPingPorDispositivo:
    """KG200Z usa ATQ (não AT) — verificação crítica para diagnóstico."""

    def test_rak3172_usa_AT(self):
        assert RAK3172.ping_cmd == "AT"

    def test_kg200z_usa_ATQ(self):
        assert KG200Z.ping_cmd == "ATQ"

    def test_smart_usa_AT(self):
        assert SMARTSx1262.ping_cmd == "AT"


# ===========================================================
#   Testes funcionais (com FakeSerial)
# ===========================================================
class TestRAK3172:
    def test_configure_radio_envia_NWM_P2P(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n", "OK\r\n", "OK\r\n"])
        dev.open()
        dev.configure_radio(freq_hz=904_000_000, sf=11, bw_khz=500,
                            cr_label="4/5", preamble=10, power=14)
        cmds = fake.all_commands()
        # Deve enviar AT+NWM=0 primeiro, depois AT+P2P=...
        assert any("AT+NWM=0" in c for c in cmds)
        p2p = [c for c in cmds if "AT+P2P=" in c]
        assert len(p2p) == 1
        # CR "4/5" → 0 para RAK3172
        assert "904000000:11:500:0:10:14" in p2p[0]

    def test_pnm_via_syncword(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n"])
        dev.open()
        dev.set_public_network(True)
        assert "AT+SYNCWORD=0x34" in fake.last_command()

        fake.queue_response("OK\r\n")
        dev.set_public_network(False)
        assert "AT+SYNCWORD=0x12" in fake.last_command()

    def test_tx_payload_usa_PSEND_hex(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n"])
        dev.open()
        dev.tx_payload(b"\x01\x02\x03")
        assert "AT+PSEND=010203" in fake.last_command()

    def test_rx_continuous_usa_precv_65534(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n"])
        dev.open()
        dev.rx_continuous_start()
        assert fake.last_command() == "AT+PRECV=65534"

    def test_parse_rx_line_formato_correto(self):
        dev = RAK3172("COMx", 115200)
        # Formato +EVT:RXP2P:rssi:snr:hex
        payload, rssi, snr = dev.parse_rx_line("+EVT:RXP2P:-45:8:01020304")
        assert payload == "01020304"
        assert rssi == -45.0
        assert snr == 8.0

    def test_parse_rx_line_ignora_linha_sem_rxp2p(self):
        dev = RAK3172("COMx", 115200)
        payload, rssi, snr = dev.parse_rx_line("OK")
        assert payload is None
        assert rssi is None and snr is None

    def test_modo_lorawan_enter_exit(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n", "OK\r\n"])
        dev.open()
        dev.lw_enter_mode()
        assert fake.last_command() == "AT+NWM=1"
        dev.lw_exit_mode()
        assert fake.last_command() == "AT+NWM=0"


class TestKG200Z:
    def test_capabilities_inclui_lorawan(self):
        assert "LORAWAN" in KG200Z.capabilities

    def test_configure_radio_usa_QTCONF(self, make_device):
        dev, fake = make_device(KG200Z, responses=["OK\r\n", "OK\r\n"])
        dev.open()
        dev.configure_radio(freq_hz=904_000_000, sf=11, bw_khz=500,
                            cr_label="4/5", preamble=10, power=14)
        cmds = fake.all_commands()
        # KG200Z usa AT+QP2P=1 e AT+QTCONF
        assert any("AT+QP2P=1" in c for c in cmds)
        qtconf = [c for c in cmds if "AT+QTCONF=" in c]
        assert len(qtconf) == 1
        # CR 4/5 → 1 para KG200Z; BW 500 → idx 6; SF 11
        # Format: freq:pow:bw:sf:cr:lna:pa:mod:paylen:freqdev:lowdropt:BT
        assert "904000000:14:6:11:1:" in qtconf[0]

    def test_tx_payload_usa_QTDA_e_QTTX(self, make_device):
        dev, fake = make_device(KG200Z, responses=["OK\r\n", "OK\r\n"])
        dev.open()
        dev.tx_payload(b"\x01\x02\x03", msg_text="ABCD")
        cmds = fake.all_commands()
        assert any("AT+QTDA=ABCD" in c for c in cmds)
        assert any("AT+QTTX=1" in c for c in cmds)

    def test_pnm_sem_comando_dedicado_no_op(self, make_device):
        """KG200Z não tem AT+PNM nem AT+SYNCWORD — set_public_network retorna None."""
        dev, _ = make_device(KG200Z, responses=[])
        dev.open()
        result = dev.set_public_network(True)
        assert result is None

    def test_parse_rx_line_extrai_buf(self):
        dev = KG200Z("COMx", 9600)
        payload, _rssi, _snr = dev.parse_rx_line("99s436:RX data len=4, buf: ABCD")
        assert payload == "ABCD"

    def test_parse_rx_line_extrai_rssi_snr(self):
        dev = KG200Z("COMx", 9600)
        _p, rssi, snr = dev.parse_rx_line("99s438:RssiValue=-13 dBm, SnrValue=4dB")
        assert rssi == -13.0
        assert snr == 4.0


class TestSMARTSx1262:
    def test_capabilities_inclui_lorawan(self):
        assert "LORAWAN" in SMARTSx1262.capabilities

    def test_configure_radio_usa_TCONF_khz(self, make_device):
        """SMART usa freq em kHz (não Hz)."""
        dev, fake = make_device(SMARTSx1262, responses=["OK\r\n", "OK\r\n"])
        dev.open()
        dev.configure_radio(freq_hz=904_000_000, sf=11, bw_khz=500,
                            cr_label="4/5", preamble=10, power=14)
        cmds = fake.all_commands()
        tconf = [c for c in cmds if "AT+TCONF=" in c]
        assert len(tconf) == 1
        # SMART usa: freq_kHz:power:bw_kHz:sf:cr_label:lna:pa
        # 904 MHz → 904000 kHz; CR como string "4/5"
        assert "904000:14:500:11:4/5:" in tconf[0]

    def test_pnm_usa_PNM(self, make_device):
        dev, fake = make_device(SMARTSx1262, responses=["OK\r\n"])
        dev.open()
        dev.set_public_network(True)
        assert "AT+PNM=1" in fake.last_command()

        fake.queue_response("OK\r\n")
        dev.set_public_network(False)
        assert "AT+PNM=0" in fake.last_command()

    def test_tx_payload_usa_TXLRA(self, make_device):
        dev, fake = make_device(SMARTSx1262, responses=["OK\r\n", "OK\r\n"])
        dev.open()
        dev.configure_radio(freq_hz=904_000_000, sf=11, bw_khz=500,
                            cr_label="4/5", preamble=10, power=14)
        fake.queue_response("OK\r\n")
        dev.tx_payload(b"", msg_text="Hello")
        last = fake.last_command()
        assert "AT+TXLRA=904000:0:Hello" in last

    def test_parse_rx_line_extrai_rssi_snr_text(self):
        dev = SMARTSx1262("COMx", 9600)
        payload, rssi, snr = dev.parse_rx_line("RSSI=-9 dBm SNR=6 dBm Rx Text-> Hello World")
        assert rssi == -9.0
        assert snr == 6.0
        assert payload == "Hello World"


# ===========================================================
#   Testes de cache PNM (apply_public_network)
# ===========================================================
class TestApplyPublicNetworkCache:
    def test_apply_so_envia_se_mudou(self, make_device):
        """Segunda chamada com mesmo valor não deve enviar comando AT (cache)."""
        dev, fake = make_device(RAK3172, responses=["OK\r\n"])
        dev.open()
        dev.public_network = True
        rsp1 = dev.apply_public_network()
        assert rsp1 is not None
        cmds_before = len(fake.written)

        rsp2 = dev.apply_public_network()  # mesmo valor
        cmds_after = len(fake.written)
        assert rsp2 is None
        assert cmds_after == cmds_before, "cache falhou — comando reenviado desnecessariamente"

    def test_apply_envia_quando_muda(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n", "OK\r\n"])
        dev.open()
        dev.public_network = True
        dev.apply_public_network()
        dev.public_network = False
        rsp = dev.apply_public_network()
        assert rsp is not None  # mudou, então enviou


# ===========================================================
#   Testes do mapa LW_CMDS (comandos LoRaWAN)
# ===========================================================
class TestLWCommands:
    def test_lw_supports_basico(self):
        dev = RAK3172("COMx", 115200)
        assert dev.lw_supports("class")
        assert dev.lw_supports("join")
        assert not dev.lw_supports("comando_inexistente")

    def test_lw_cmd_gera_string_formatada(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n"])
        dev.open()
        dev.lw_cmd("class", "A")
        assert fake.last_command() == "AT+CLASS=A"

    def test_lw_cmd_sem_value(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n"])
        dev.open()
        dev.lw_cmd("njs")
        assert fake.last_command() == "AT+NJS=?"

    def test_lw_cmd_nao_suportado_lanca(self, make_device):
        dev, _ = make_device(RAK3172, responses=[])
        dev.open()
        with pytest.raises(NotImplementedError):
            dev.lw_cmd("comando_inexistente", "x")

    @pytest.mark.parametrize("cls", ALL_DRIVERS)
    def test_todos_drivers_suportam_class(self, cls):
        assert "class" in cls.LW_CMDS, f"{cls.name} sem comando 'class'"

    @pytest.mark.parametrize("cls", ALL_DRIVERS)
    def test_todos_drivers_suportam_join(self, cls):
        assert "join" in cls.LW_CMDS, f"{cls.name} sem comando 'join'"


# ===========================================================
#   Testes de send_at (debug callback, lock)
# ===========================================================
class TestSendAT:
    def test_send_at_chama_debug_cb(self, make_device):
        dev, fake = make_device(RAK3172, responses=["OK\r\n"])
        dev.open()

        events = []
        dev.debug_cb = lambda direction, payload, ms: events.append((direction, payload, ms))

        dev.send_at("AT")
        assert len(events) == 2  # TX + RX
        assert events[0][0] == "TX"
        assert events[0][1] == "AT"
        assert events[1][0] == "RX"
        assert events[1][2] is not None and events[1][2] >= 0  # ms

    def test_send_at_sem_porta_aberta_lanca(self):
        dev = RAK3172("COMx", 115200)
        # Não chamou open()
        with pytest.raises(RuntimeError):
            dev.send_at("AT")


# ===========================================================
#   Testes do ping
# ===========================================================
class TestPing:
    """ping() faz flush_rx() (1ª read_all) + send_at(ping_cmd) (2ª read_all).
    Por isso queue 2 respostas: vazia para o flush, e a real para o AT."""

    def test_ping_ok(self, make_device):
        dev, fake = make_device(RAK3172, responses=["", "OK\r\n"])
        dev.open()
        ok, rsp = dev.ping()
        assert ok is True
        assert "OK" in rsp

    def test_ping_sem_resposta(self, make_device):
        dev, fake = make_device(RAK3172, responses=["", ""])
        dev.open()
        ok, _ = dev.ping(wait=0.01)
        assert ok is False

    def test_ping_resposta_sem_OK(self, make_device):
        dev, fake = make_device(RAK3172, responses=["", "ERROR\r\n"])
        dev.open()
        ok, _ = dev.ping(wait=0.01)
        assert ok is False

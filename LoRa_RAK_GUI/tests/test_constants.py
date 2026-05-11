"""
Testes do módulo constants — metadados, conversões MHz<->Hz, etc.
"""
import pytest

from constants import (
    APP_NAME, APP_VERSION, APP_AUTHOR, CR_LABELS,
    mhz_to_hz, hz_to_mhz, LOGS_DIR, DEVICES_JSON_PATH,
)


class TestMetadados:
    def test_app_name_definido(self):
        assert APP_NAME and isinstance(APP_NAME, str)

    def test_versao_segue_semver(self):
        parts = APP_VERSION.split(".")
        assert len(parts) >= 2, "versão deve ter pelo menos MAJOR.MINOR"
        for p in parts:
            assert p.isdigit(), f"parte da versão não é numérica: {p}"

    def test_autor_definido(self):
        assert APP_AUTHOR and len(APP_AUTHOR) > 0


class TestCRLabels:
    def test_cr_labels_corretos(self):
        assert CR_LABELS == ["4/5", "4/6", "4/7", "4/8"]


class TestMhzHz:
    def test_mhz_para_hz_inteiro(self):
        assert mhz_to_hz("904") == 904_000_000
        assert mhz_to_hz("868") == 868_000_000
        assert mhz_to_hz("915") == 915_000_000

    def test_mhz_para_hz_decimal(self):
        assert mhz_to_hz("904.5") == 904_500_000
        assert mhz_to_hz("916.8") == 916_800_000

    def test_mhz_aceita_virgula_decimal(self):
        """A entrada pode vir com vírgula (locale pt-BR)."""
        assert mhz_to_hz("904,5") == 904_500_000

    def test_mhz_aceita_float(self):
        assert mhz_to_hz(904.5) == 904_500_000

    def test_mhz_invalido_lanca_excecao(self):
        with pytest.raises(ValueError):
            mhz_to_hz("abc")

    def test_hz_para_mhz_string(self):
        assert hz_to_mhz(904_000_000) == "904"
        assert hz_to_mhz(916_800_000) == "916.8"

    def test_roundtrip_mhz_hz(self):
        for mhz_str in ["903", "904.5", "915", "868.1"]:
            hz = mhz_to_hz(mhz_str)
            back = float(hz_to_mhz(hz))
            assert abs(back - float(mhz_str.replace(",", "."))) < 0.001


class TestCaminhos:
    def test_logs_dir_aponta_para_pasta_logs(self):
        assert LOGS_DIR.endswith("logs")
        assert "LoRa_RAK_GUI" in LOGS_DIR

    def test_devices_json_path_aponta_para_arquivo(self):
        assert DEVICES_JSON_PATH.endswith("devices.json")

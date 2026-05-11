"""
Constantes globais e helpers básicos do projeto.

Centraliza:
  - Metadados da aplicação (nome, versão, autor) — atualize aqui ao lançar nova versão
  - Caminhos comuns (logs/, devices.json)
  - Constantes LoRa compartilhadas (CR_LABELS)
  - Funções de conversão de unidade (MHz <-> Hz)
"""
from __future__ import annotations

import os


# ===========================================================
#   Metadados da aplicação (usados no dialog Sobre)
# ===========================================================
APP_NAME = "LoRa Multi-Device GUI"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Rafael Macedo"
APP_YEAR = "2026"
APP_GITHUB = "github.com/RSMEngenhariaJF/LoRa_test"
APP_DESCRIPTION = (
    "Aplicação para testes de módulos LoRa via UART com comandos AT.\n"
    "Inclui modos P2P (TX/RX/Scanner/CW), configuração de end-device LoRaWAN, "
    "e Gateway LoRaWAN com Network Server simulado (OTAA)."
)


# ===========================================================
#   Caminhos
# ===========================================================
# Diretório base = pasta deste arquivo (LoRa_RAK_GUI/)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
DEVICES_JSON_PATH = os.path.join(BASE_DIR, "devices.json")


# ===========================================================
#   Coding Rate LoRa (datasheet RAK3172 / spec)
# ===========================================================
CR_LABELS = ["4/5", "4/6", "4/7", "4/8"]


# ===========================================================
#   Conversões MHz <-> Hz
# ===========================================================
def mhz_to_hz(value) -> int:
    """Converte string/float em MHz para inteiro Hz. Aceita vírgula como decimal."""
    if isinstance(value, str):
        value = value.replace(",", ".").strip()
    return int(round(float(value) * 1_000_000))


def hz_to_mhz(hz: int) -> str:
    """Formata Hz como MHz (ex.: 904000000 -> '904')."""
    return f"{hz / 1_000_000:g}"

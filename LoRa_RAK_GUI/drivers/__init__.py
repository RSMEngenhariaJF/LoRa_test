"""
Pacote de drivers de módulos LoRa.

Para adicionar um novo módulo:
  1. Crie drivers/seu_modulo.py com classe herdando de LoRaDevice
  2. Importe-a aqui
  3. Registre em DEVICES com o nome amigável

Uso típico:
    from drivers import DEVICES, LoRaDevice
    cls = DEVICES["RAK3172"]
    device = cls(port="COM6", baudrate=115200)
"""
from drivers.base import LoRaDevice
from drivers.rak3172 import RAK3172
from drivers.kg200z import KG200Z
from drivers.smart_sx1262 import SMARTSx1262


# Registro central — chave é o nome exibido no Combobox da UI
DEVICES: dict[str, type[LoRaDevice]] = {
    "RAK3172": RAK3172,
    "Quectel KG200Z": KG200Z,
    "SMART SMW-SX1262M0": SMARTSx1262,
}

__all__ = ["LoRaDevice", "RAK3172", "KG200Z", "SMARTSx1262", "DEVICES"]

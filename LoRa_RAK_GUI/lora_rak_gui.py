"""
Entry point — LoRa Multi-Device GUI

Execução:
    python LoRa_RAK_GUI/lora_rak_gui.py

Estrutura modular:
    constants.py         — metadados, caminhos, CR_LABELS, MHz<->Hz
    logger.py            — SessionLogger + PacketResult
    drivers/             — pacote com LoRaDevice base + um arquivo por chip
        base.py          — classe abstrata
        rak3172.py
        kg200z.py
        smart_sx1262.py
        __init__.py      — exporta DEVICES (registro central)
    lorawan.py           — protocolo LoRaWAN 1.0.x (Gateway + NS simulado)
    app.py               — classe App (Tkinter) com toda a UI
    lora_rak_gui.py      — este arquivo (apenas inicia a App)
"""
from __future__ import annotations

from app import App


if __name__ == "__main__":
    App().mainloop()

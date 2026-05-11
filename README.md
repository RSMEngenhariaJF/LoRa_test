# LoRa Multi-Device — Interface Gráfica de Testes

Aplicação Tkinter para testar módulos LoRa via UART com comandos AT. Suporta múltiplos dispositivos com sintaxes AT diferentes através de uma camada de driver e adapta a interface conforme as capacidades de cada chip.

---

## ✨ Funcionalidades

- **Detecção automática de portas UART** (combobox com COM disponíveis + descrição do dispositivo)
- **Múltiplos dispositivos** com troca dinâmica e adaptação da UI:
  - RAK3172 (Wisduo) — comandos `AT+P2P`, `AT+PSEND`, `AT+PRECV`, `AT+TTONE`
  - Quectel KG200Z — comandos `AT+QP2P`, `AT+QTCONF`, `AT+QTTX`, `AT+QTRX`, `AT+QTTONE`
  - SMART SMW-SX1262M0 — comandos `AT+TCONF`, `AT+TXLRA`, `AT+RXLRA`, `AT+TXTONE`
- **Modos de teste** (habilitados conforme capacidade do dispositivo selecionado):
  - **TX único** — envia mensagem manual (texto ou `0xHEX`) com retentativas e mede RSSI/SNR/RTT
  - **RX contínuo** — escuta passiva e registra todos os pacotes recebidos
  - **Pacote incremental 16 bits** — sequência crescente para análise de PDR (taxa de entrega)
  - **Scanner** — cicla por 3 frequências, escutando dwell segundos em cada
  - **Portadora CW** — transmite portadora contínua para teste de espectro / medida de potência
- **Gateway LoRaWAN + Network Server simulado** (OTAA):
  - Implementação do protocolo LoRaWAN 1.0.x em Python (AES-CMAC, AES-ECB, AES-CTR)
  - Recebe Join Request → valida MIC → gera Join Accept cifrado → deriva NwkSKey/AppSKey
  - Decifra uplinks com AppSKey, valida MIC com NwkSKey
  - Bandas: AU915 (Brasil), US915, EU868, AS923, IN865, KR920
  - Cadastro de end-devices persistido em `devices.json` (DevEUI / JoinEUI / AppKey)
  - **Single-channel** — limitação física do SX1262 (escuta 1 freq/SF por vez)
- **Console AT manual** — envio de qualquer comando AT, com histórico navegável (↑/↓) e botões rápidos contextuais por dispositivo
- **Layout com divisor arrastável** + opção de destacar o log em janela flutuante (drag para outro monitor)
- **Logs com timestamp**:
  - `logs/sessao_*.txt` — log amigável (eventos + decisões)
  - `logs/debug_*.txt` — log de debug em paralelo com toda comunicação AT raw (TX/RX + ΔT em ms)
  - CSV de resultados estruturados via botão "Salvar CSV"
- **Conexão em duas etapas** (importante para diagnóstico):
  - **Abrir UART** — só abre a porta serial no SO
  - **Testar dispositivo** — envia o comando de ping específico do módulo (`AT` para RAK/SMART, `ATQ` para KG200Z) e valida resposta `OK`

---

## 📂 Estrutura do repositório

```
.
├── LoRa_RAK_GUI/                  ← Aplicação principal
│   ├── lora_rak_gui.py            ← GUI Tkinter + drivers de dispositivo
│   ├── lorawan.py                 ← Protocolo LoRaWAN 1.0.x (Gateway + NS simulado)
│   ├── requirements.txt           ← pyserial, cryptography
│   ├── devices.example.json       ← Exemplo de cadastro de end-devices
│   ├── devices.json               ← (criado em runtime — IGNORADO pelo git; contém AppKeys)
│   └── logs/                      ← Logs gerados em runtime (sessao_*.txt + debug_*.txt + CSVs)
├── doc/                           ← Datasheets de comandos AT
│   ├── Quectel_KG200Z_AT_Commands_Manual_*.pdf
│   └── SMART_LoRa_AT_Command_*.pdf
├── arquivo_versoes_antigas/       ← Versões anteriores preservadas (referência histórica)
│   ├── scripts/                   ← 8 scripts evolutivos do projeto original
│   └── dados/                     ← Logs/CSVs antigos
├── .gitignore
└── README.md
```

---

## 🚀 Instalação

**Pré-requisitos:** Python 3.10+ (para suporte a `tipo | None` nas anotações).

```powershell
# Clone o repositório
git clone <URL_DO_REPO>
cd "transmissao P2P RAK"

# Instale a dependência (apenas pyserial; tkinter já vem com Python no Windows)
pip install -r LoRa_RAK_GUI/requirements.txt
```

---

## ▶️ Como rodar

```powershell
python LoRa_RAK_GUI/lora_rak_gui.py
```

### Fluxo de uso

1. **Selecione o dispositivo** no combobox superior (RAK3172, KG200Z ou SMART SX1262)
2. O baudrate padrão é ajustado automaticamente (115200 para RAK, 9600 para os outros)
3. **Selecione a porta UART** (clique em "Atualizar" se conectou o cabo agora)
4. Clique em **Abrir UART** — abre só a porta serial
5. Clique em **Testar dispositivo** — envia o comando de ping correto e valida resposta
6. Vá para a aba do modo desejado, ajuste parâmetros (frequência em MHz, CR como dropdown 4/5..4/8) e clique em iniciar
7. Logs aparecem no console inferior e são salvos automaticamente em `LoRa_RAK_GUI/logs/`

---

## 🔧 Diferenças entre dispositivos

A GUI cuida da tradução interna, mas para referência:

| Aspecto | RAK3172 | KG200Z | SMART SX1262 |
|---|---|---|---|
| Baudrate padrão | 115200 | 9600 | 9600 |
| Ping (verificação) | `AT` | `ATQ` | `AT` |
| Modo P2P | `AT+NWM=0` + `AT+P2P=...` | `AT+QP2P=1` + `AT+QTCONF=...` | `AT+TCONF=...` |
| TX | `AT+PSEND=<hex>` | `AT+QTDA=<txt>` + `AT+QTTX=1` | `AT+TXLRA=freq:0:txt` |
| RX contínuo | `AT+PRECV=65534` | `AT+QTRX=65535` | `AT+RXLRA=freq:1` |
| Parar RX/TX | `AT+PRECV=0` | `AT+QTOFF` | `AT+TOFF` |
| Portadora CW | `AT+TTONE` | `AT+QTTONE` | `AT+TXTONE=freq` |
| Frequência (interno) | Hz | Hz | kHz |
| CR (interno) | 0..3 | 1..4 | string `"4/5"`..`"4/8"` |

A GUI sempre exibe **frequência em MHz** e **CR como label `4/5`..`4/8`**; os drivers convertem para o formato exigido por cada chip.

---

## 📡 Gateway LoRaWAN (OTAA)

A aba "Gateway LoRaWAN" implementa um Network Server simulado em Python. O módulo conectado entra em P2P/RX puro e recebe frames LoRaWAN crus; toda a camada MAC (Join Request/Accept, derivação de chaves, decifra) acontece em Python via `lorawan.py`.

### Fluxo
1. Vá para a aba **Dispositivos LoRaWAN** e cadastre seus end-devices (DevEUI + JoinEUI/AppEUI + AppKey)
2. Volte para **Gateway LoRaWAN**, escolha a banda (defaults aplicados automaticamente) e clique em "Iniciar Gateway"
3. Quando um end-device envia Join Request na frequência configurada, o gateway:
   - Valida o MIC com a AppKey cadastrada
   - Aloca um DevAddr, gera AppNonce
   - Deriva NwkSKey e AppSKey conforme spec LoRaWAN 1.0.x
   - Constrói Join Accept cifrado (AES-128 ECB decrypt) e transmite após RX_DELAY
4. Uplinks subsequentes têm o payload decifrado e exibidos no Console LoRaWAN

### Limitações conhecidas
- **Single-channel only** — fixa 1 freq/SF por vez. Configure o end-device para usar essa freq específica
- **Timing RX1 sensível** — comandos AT têm latência ~100-400ms; ajuste `RX Delay` se necessário
- **OTAA apenas** (Fase 1) — ABP fica para versão futura
- **Downlinks programáveis ainda não estão na UI** — a função `build_downlink()` existe em `lorawan.py` mas precisa ser exposta

### Segurança
`devices.json` contém AppKeys e está no `.gitignore`. Para compartilhar o formato sem expor chaves reais, use `devices.example.json`.

## 📝 Logs

### TXT (sessão completa, auto-salvo)

Cada sessão gera um arquivo `LoRa_RAK_GUI/logs/sessao_YYYYMMDD_HHMMSS.txt` que registra **em tempo real** todas as linhas do console com timestamp:

```
[2025-05-08 16:32:14] [INFO] Porta UART aberta: COM6 @ 115200 bps. Driver: RAK3172.
[2025-05-08 16:32:18] >>> [Teste dispositivo] Enviando: AT
[2025-05-08 16:32:19] <<< OK
[2025-05-08 16:32:25] --- TX seq=0 HEX=01020304 (RAK3172) ---
[2025-05-08 16:32:25]   [primeiro envio] Config TX: {'freq': 904000000, 'sf': 11, ...}
[2025-05-08 16:32:26]   RX>> +EVT:RXP2P:-65:8:01020304
[2025-05-08 16:32:26] << Resultado TX seq=0: match=True rx=01020304 rssi=-65.0 snr=8.0 rtt=987.3 tentativas=1
```

### CSV (resultados estruturados, salvo manualmente)

Botão "Salvar CSV resultados…" gera um CSV com separador `;` e colunas:

```
timestamp;device;mode;seq;payload_tx;payload_rx;match;attempts;timeout_s;retries;rssi;snr;rtt_ms;freq_tx_hz;freq_rx_hz
```

Pronto para análise no Excel, Pandas, etc.

---

## 📚 Referências

Os datasheets dos comandos AT estão em `doc/`:

- **Quectel KG200Z** — `doc/Quectel_KG200Z_AT_Commands_Manual_V1.0.0_Preliminary_20240329.pdf`
- **SMART SMW-SX1262M0** — `doc/SMART_LoRa_AT_Command_v1.0_en_v2.14.pdf`
- **RAK3172** — datasheet do fabricante (RAK Wireless): https://docs.rakwireless.com/

---

## 🗂️ Histórico

A pasta `arquivo_versoes_antigas/scripts/` contém 8 scripts da evolução anterior do projeto (versões CLI e GUIs experimentais). Foram mantidos como referência mas a aplicação atual é `LoRa_RAK_GUI/lora_rak_gui.py`, que consolida e generaliza todas as funcionalidades.

---

## 🤝 Contribuindo

Pull requests e issues são bem-vindos. Para adicionar suporte a um novo módulo LoRa:

1. Crie uma classe nova em `lora_rak_gui.py` herdando de `LoRaDevice`
2. Implemente os métodos abstratos (`configure_radio`, `tx_payload`, `rx_continuous_start`, `rx_stop`, `cw_start`, `cw_stop`, `parse_rx_line`)
3. Defina `name`, `default_baudrate`, `capabilities` e `ping_cmd`
4. Registre no dicionário `DEVICES` no fim do arquivo

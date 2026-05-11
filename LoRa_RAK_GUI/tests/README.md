# Metodologia de Testes — LoRa Multi-Device GUI

Este documento descreve a estratégia de testes do projeto: o que é testado, como executar, e como validar manualmente com hardware real.

---

## 🔺 Pirâmide de testes

```
                  ┌─────────────────┐
                  │ Validação manual│   ← com hardware real (não automatizado)
                  │   (hardware)    │
                  ├─────────────────┤
                  │   Integração    │   ← fluxos cruzando módulos (FakeSerial)
                  │  (FakeSerial)   │
                  ├─────────────────┤
                  │                 │
                  │    Unitários    │   ← bottom: maior número, mais rápido
                  │                 │
                  └─────────────────┘
```

**Por que essa estrutura?**
- A **base larga** de unitários roda em segundos, pega regressões cedo
- **Integração** valida que os módulos se conversam corretamente (drivers ↔ logger ↔ lorawan)
- **Validação manual** cobre o que mocks não conseguem: latências reais, firmware quirks, ondas EM

---

## 📦 Estrutura dos testes

```
tests/
├── __init__.py
├── conftest.py                    ← fixtures (FakeSerial, make_device)
├── test_constants.py              ← APP_NAME, mhz_to_hz, CR_LABELS
├── test_logger.py                 ← SessionLogger, PacketResult, CSV
├── test_lorawan_crypto.py         ← AES-CMAC, ECB, derivação de chaves
├── test_lorawan_frames.py         ← Join Request/Accept, DataFrame, MIC
├── test_drivers.py                ← RAK3172, KG200Z, SMART (FakeSerial)
├── test_integration.py            ← fluxos completos end-to-end
└── README.md                      ← este arquivo
```

---

## ▶️ Como executar

### Instalação

```powershell
pip install -r requirements-dev.txt
```

Inclui `pytest`, `pytest-cov` e tudo do `requirements.txt`.

### Rodar todos os testes

```powershell
cd LoRa_RAK_GUI
pytest
```

Saída esperada (resumida):
```
tests/test_constants.py ............         [ 15%]
tests/test_logger.py .........               [ 30%]
tests/test_lorawan_crypto.py ............    [ 55%]
tests/test_lorawan_frames.py ............    [ 80%]
tests/test_drivers.py ...................    [ 95%]
tests/test_integration.py ...                [100%]
============= 78 passed in 1.23s =============
```

### Rodar por categoria

```powershell
pytest tests/test_constants.py          # só helpers
pytest tests/test_drivers.py            # só drivers
pytest -m crypto                        # só testes que usam cryptography
pytest -m "not crypto"                  # ignora testes crypto (se lib não instalada)
pytest -m slow                          # só lentos
pytest -k "RAK3172"                     # só testes que mencionam RAK3172
```

### Cobertura de código

```powershell
pytest --cov=. --cov-report=html
start htmlcov/index.html        # abre relatório no navegador
```

Cobertura-alvo: **80%+** para módulos não-UI (constants, logger, lorawan, drivers).

### Modo verboso (debug)

```powershell
pytest -vv -s                           # mostra prints internos
pytest --pdb                            # entra no debugger em falhas
pytest --tb=long                        # tracebacks completos
```

---

## 🧪 O que é testado (cobertura conceitual)

### `test_constants.py` — Helpers e metadados
- ✅ APP_NAME, APP_VERSION definidos e válidos (semver)
- ✅ CR_LABELS == `["4/5", "4/6", "4/7", "4/8"]` (datasheet RAK3172)
- ✅ `mhz_to_hz`: integer, decimal, vírgula (locale pt-BR), erro em valor inválido
- ✅ `hz_to_mhz`: formato correto + round-trip preserva valor

### `test_logger.py` — SessionLogger e CSV
- ✅ Abre `sessao_*.txt` e `debug_*.txt` em paralelo
- ✅ Escreve cabeçalho com nome do dispositivo
- ✅ Cada `log()` adiciona timestamp `[YYYY-MM-DD HH:MM:SS]`
- ✅ `debug_log` registra TX/RX + ΔT em ms
- ✅ Escapa `\r\n` no debug (não cria linha extra)
- ✅ Export CSV com separador `;` (locale brasileiro)
- ✅ Resultado vazio ainda gera header

### `test_lorawan_crypto.py` — Primitivas crypto (CRÍTICO)
- ✅ **Vetor NIST SP 800-38B** para AES-CMAC (msg vazia e 16 bytes)
- ✅ **Vetor FIPS 197** para AES-128 ECB
- ✅ Round-trip ECB encrypt/decrypt
- ✅ Server ECB-decrypt seguido de device ECB-encrypt recupera plaintext (convenção LoRaWAN)
- ✅ `derive_session_keys`: NwkSKey ≠ AppSKey, determinístico, DevNonce diferente → chave diferente
- ✅ `random_bytes`: tamanho correto, não-determinístico
- ✅ `hex_to_bytes` / `bytes_to_hex`: aceita `:`, ` `, `0x` prefix

### `test_lorawan_frames.py` — Protocolo LoRaWAN (CRÍTICO)
- ✅ Join Request: parse extrai EUIs em BE corretamente
- ✅ Join Request: MIC válido aceito, MIC inválido rejeitado
- ✅ Frame corrompido (1 bit) → MIC inválido
- ✅ Join Accept: tamanho 17 bytes (sem CFList), MHDR = 0x20
- ✅ End-device recupera plaintext via ECB encrypt (convenção LoRaWAN)
- ✅ **Round-trip Join**: server e device derivam mesmas chaves (regressão crítica)
- ✅ DataFrame uplink: parse, MIC válido, FPort, decifra payload
- ✅ FPort=0 usa NwkSKey; FPort≥1 usa AppSKey
- ✅ MType helpers (mhdr, parse_mhdr) consistentes

### `test_drivers.py` — Drivers de dispositivo
- ✅ Registro `DEVICES` tem 3 dispositivos, todos herdam de LoRaDevice
- ✅ Atributos de classe (name, baudrate, capabilities, ping_cmd) válidos
- ✅ **KG200Z usa `ATQ`, RAK/SMART usam `AT`** (regressão importante)
- ✅ **RAK3172**: configure_radio envia `AT+NWM=0` + `AT+P2P=...`, CR mapeado para 0..3
- ✅ **RAK3172**: PNM via `AT+SYNCWORD=0x12/0x34`
- ✅ **KG200Z**: configure_radio envia `AT+QP2P=1` + `AT+QTCONF`, CR 1..4
- ✅ **KG200Z**: PNM no-op (não tem comando exposto)
- ✅ **SMART**: configure_radio usa `AT+TCONF` com kHz (não Hz), CR como string `"4/5"`
- ✅ **SMART**: PNM via `AT+PNM=0/1`
- ✅ Cache do PNM evita reenviar comando idêntico
- ✅ `lw_cmd` preenche `{value}` no template, lança em comando não suportado
- ✅ `send_at` aciona `debug_cb` com (TX, RX, ΔT)
- ✅ `ping`: OK / ERROR / sem resposta

### `test_integration.py` — Fluxos cruzando módulos
- ✅ Sessão completa: driver + logger + CSV em um único fluxo
- ✅ **Gateway processa Join Request fake e gera Join Accept válido** que o end-device consegue decifrar
- ✅ Server e end-device derivam mesmas chaves de sessão

---

## 🛠️ FakeSerial — mock de hardware

Em `conftest.py`, `FakeSerial` simula `serial.Serial`:

```python
def test_meu_driver(make_device):
    dev, fake = make_device(RAK3172, responses=["OK\r\n", "OK\r\n"])
    dev.open()
    dev.send_at("AT+P2P=...")
    assert "AT+P2P=" in fake.last_command()
    assert fake.all_commands() == ["AT+P2P=..."]
```

**API útil**:
- `fake.queue_response(str)` — pré-programa resposta para próximo `read_all()`
- `fake.queue_stream(str)` — alimenta `in_waiting/read` (simula RX contínuo)
- `fake.written` — lista de bytes enviados (em ordem)
- `fake.last_command()` — string do último comando (sem `\r\n`)
- `fake.all_commands()` — todos os comandos enviados

`make_device` também faz `monkeypatch` em `time.sleep` para não tornar os testes lentos.

---

## 🔬 Validação manual com hardware

Os testes automatizados **não substituem** validação com hardware real. Esta seção descreve o **plano de aceitação** antes de considerar uma release pronta.

### Pré-requisitos
- 2x módulos (qualquer combinação RAK3172/KG200Z/SMART) conectados via USB
- Multímetro/analisador de espectro (opcional, para CW)
- Lápis e papel para anotar resultados 🙂

### Checklist por dispositivo

#### RAK3172
| Teste | Critério de aceitação |
|---|---|
| Abrir UART | "aberta" em azul, log mostra baudrate correto |
| Testar dispositivo | "OK (respondeu a AT)" em verde |
| TX único (texto curto) | OK retornado, sem erro de timeout |
| TX único + RX no segundo módulo | RSSI/SNR aparecem, payload bate |
| RX contínuo | recebe pelo menos 5 pacotes consecutivos sem erro |
| Pacote incremental (10 pacotes, 1s) | contador avança, sem erros |
| Scanner 3 freq | cicla, log mostra "Scan FREQ=..." em cada |
| Portadora CW | `lbl_cw_state` vermelho "LIGADA", confirma no analisador |
| Trocar para LoRaWAN | `AT+NWM=1` envia OK, label fica "LoRaWAN" verde |
| Voltar para P2P | `AT+NWM=0` envia OK, label volta para P2P |
| Console AT manual | comando livre + Enter funciona, histórico ↑/↓ |
| Log debug paralelo | `logs/debug_*.txt` criado, contém TX/RX/ΔT |

#### Quectel KG200Z
| Teste | Critério |
|---|---|
| Testar dispositivo | usa `ATQ` (não `AT`) e responde OK |
| Baudrate ajusta para 9600 ao selecionar | sim |
| Trocar para LoRaWAN | label "LoRaWAN (nativo)" (não precisa de comando) |
| TX/RX P2P | funcionar com QTCONF/QTDA/QTTX |

#### SMART SMW-SX1262M0
| Teste | Critério |
|---|---|
| Testar dispositivo | responde `AT` com OK |
| Baudrate ajusta para 9600 | sim |
| PNM checkbox aplicar | envia `AT+PNM=1` ou `AT+PNM=0` corretamente |
| TX único | usa `AT+TXLRA=` com kHz, não Hz |

### Cenário Gateway LoRaWAN (OTAA)
1. **Dispositivo 1** (gateway): RAK3172 em modo P2P, abrir aba "Gateway LoRaWAN"
2. **Dispositivo 2** (end-device): outro RAK3172, ou um device LoRaWAN real
3. Cadastrar o end-device em "Dispositivos LoRaWAN" (DevEUI, JoinEUI, AppKey)
4. Configurar banda AU915 sub-band 8 (Brasil)
5. Clicar "Iniciar Gateway" → log deve mostrar "PNM=PÚBLICO"
6. Disparar Join Request no end-device
7. **Esperado**: Console LoRaWAN do gateway mostra:
   - `JOIN REQUEST: DevEUI=... JoinEUI=...`
   - `MIC OK. Aceitando Join de '...'`
   - `Join Accept (cifrado) = 0x...`
   - Sessão aparece na tabela de devices
8. Uplinks subsequentes devem ser decifrados:
   - `UPLINK de '...' FCnt=N FPort=2 payload=<hex>`

⚠️ **Limitação known issue**: single-channel — o end-device precisa estar fixo na freq do gateway.

---

## 🚨 Critérios de regressão

Antes de **fazer merge** em `main`:

1. **Todos os testes unitários passam**: `pytest` retorna 0 falhas
2. **Cobertura ≥ 80%** nos módulos `constants`, `logger`, `lorawan`, `drivers`
3. **Smoke test manual**: abrir GUI, conectar a um dispositivo real, fazer 1 TX, verificar logs
4. **Sem warning novo** introduzido (rodar com `pytest -W error`)

Antes de **release** (tag):

1. Tudo acima +
2. **Validação manual completa** com pelo menos 1 dispositivo de cada tipo
3. **Testes em região correta** (AU915 para Brasil)
4. README atualizado com mudanças na seção apropriada

---

## 🐛 Adicionando testes para um novo bug

Sempre que um bug for descoberto:

1. **Antes de corrigir**, escreva um teste que reproduza o bug (deve falhar)
2. Corrija o código
3. Confirme que o teste agora passa
4. Mantenha o teste no repositório — regressão futura será capturada

Exemplo: o bug do AppNonce endianness no Join Accept (corrigido em commit anterior) está agora coberto pelo `TestJoinRoundtrip.test_server_e_device_derivam_mesmas_chaves`.

---

## 📚 Recursos

- [pytest docs](https://docs.pytest.org/)
- [LoRaWAN 1.0.3 Specification](https://lora-alliance.org/resource_hub/lorawan-specification-v1-0-3/)
- [LoRaWAN Regional Parameters 1.0.3 revA](https://lora-alliance.org/resource_hub/lorawan-regional-parameters-v1-0-3reva/)
- [NIST SP 800-38B (CMAC test vectors)](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-38B.pdf)
- [FIPS 197 (AES test vectors)](https://nvlpubs.nist.gov/nistpubs/FIPS/NIST.FIPS.197.pdf)

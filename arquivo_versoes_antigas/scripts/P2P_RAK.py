import serial
import time

# Configurações da porta serial
PORTA_SERIAL = 'COM6'  # Altere para a porta correta (ex: /dev/ttyUSB0 no Linux)
BAUDRATE = 115200
TIMEOUT = 2

# Dados para envio
PACOTE = b'\x01\x02\x03\x04\x05'  # 5 bytes
INTERVALO_ENVIO = 30  # 5 minutos = 300 segundos

def enviar_comando(ser, comando, espera=1):
    """Envia um comando AT e lê a resposta"""
    ser.write((comando + '\r\n').encode())
    time.sleep(espera)
    resposta = ser.read_all().decode(errors='ignore')
    print(f'Comando: {comando}\nResposta: {resposta.strip()}\n')
    return resposta

def configurar_p2p(ser):
    """Configura o RAK3172 para modo P2P"""
    comandos = [
        'AT+NWM=0',                    # Define modo LoRa P2P
        'AT+P2P=903000000:11:500:0:10:0',  # Freq, SF, BW, CR, Preambulo, TX Power
    ]
    for cmd in comandos:
        enviar_comando(ser, cmd)
        time.sleep(2)

def enviar_pacote_p2p(ser, dados):
    """Envia dados no modo P2P"""
    hex_str = dados.hex().upper()
    comando = f'AT+PSEND={hex_str}'
    enviar_comando(ser, comando)

def main():
    n=0
    try:
        ser = serial.Serial(PORTA_SERIAL, BAUDRATE, timeout=TIMEOUT)
        time.sleep(2)  # Aguarda estabilização
        print("Conectado à porta serial.")

        configurar_p2p(ser)

        while True:
            print(f"Enviando pacote {n}...")
            n=n+1
            enviar_pacote_p2p(ser, PACOTE)
            print(f"Aguardando {INTERVALO_ENVIO} segundos...\n")
            time.sleep(INTERVALO_ENVIO)

    except serial.SerialException as e:
        print(f"Erro na porta serial: {e}")
    except KeyboardInterrupt:
        print("Interrompido pelo usuário.")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()
            print("Porta serial fechada.")

if __name__ == '__main__':
    main()

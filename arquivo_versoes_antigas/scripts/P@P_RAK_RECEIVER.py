import serial
import time
from datetime import datetime

PORTA_SERIAL = 'COM6'  # Altere conforme necessário
BAUDRATE = 115200
TIMEOUT = 1
LOG_ARQUIVO = 'log_pacotes.txt'

def enviar_comando(ser, comando, espera=1):
    """Envia comando AT e retorna resposta"""
    ser.write((comando + '\r\n').encode())
    time.sleep(espera)
    resposta = ser.read_all().decode(errors='ignore')
    print(f'Comando: {comando}\nResposta: {resposta.strip()}\n')
    return resposta

def configurar_p2p_receptor(ser):
    """Configura o módulo RAK3172 para escutar no modo P2P"""
    enviar_comando(ser, 'AT+NWM=0')  # Modo P2P

    

    enviar_comando(ser, 'AT+P2P=904000000:11:500:0:10:14')  # Parâmetros LoRa
    enviar_comando(ser, 'AT+PRECV=65534')  # RX contínuo

def escutar_pacotes(ser):
    """Lê pacotes recebidos via LoRa P2P"""
    print("Escutando pacotes... Pressione Ctrl+C para parar.\n")
    buffer = ""

    with open(LOG_ARQUIVO, 'a', encoding='utf-8') as arquivo:
        try:
            while True:
                if ser.in_waiting:
                    dados = ser.read(ser.in_waiting).decode(errors='ignore')
                    buffer += dados

                    # Verifica se recebeu uma linha completa
                    while '\r\n' in buffer:
                        linha, buffer = buffer.split('\r\n', 1)
                        if linha.strip():
                            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            log_linha = f'[{timestamp}] Pacote recebido: {linha.strip()}'
                            print(log_linha)
                            arquivo.write(log_linha + '\n')
                            arquivo.flush()  # Garante que seja salvo imediatamente
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nEncerrando escuta.")
        finally:
            if ser.is_open:
                ser.close()
                print("Porta serial fechada.")

def main():
    try:
        ser = serial.Serial(PORTA_SERIAL, BAUDRATE, timeout=TIMEOUT)
        time.sleep(2)
        print("Conectado à porta serial.")

        configurar_p2p_receptor(ser)
        escutar_pacotes(ser)

    except serial.SerialException as e:
        print(f"Erro na porta serial: {e}")
    except KeyboardInterrupt:
        print("Interrompido pelo usuário.")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()

if __name__ == '__main__':
    main()


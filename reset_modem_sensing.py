#!/usr/bin/python2.7

import socket
import subprocess
import time
import sys
import re
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler

HOST = '8.8.8.8'
HOSTVPN = "10.8.0.1"
#HOST=HOSTVPN
PORT = 80
RETRY_INTERVAL = 30        # Tiempo en segundos entre reintentos
ACTION_INTERVALS = [120, 240, 360]  # Intervalos de tiempo en segundos (2, 4, 6 minutos) para realizar acciones

####################
logger = logging.getLogger("ipc conn check" )
logger.setLevel(logging.INFO)
handler = RotatingFileHandler('check_connectivity.log',maxBytes=10000000, backupCount=2)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(formatter)
logger.addHandler(handler)
logger.addHandler(consoleHandler)
####################

failure_start_time = None
action_done = [False, False, False]

def check_connectivity_via_ping(host, count=2):
    successful_pings=0
    try:
        if sys.platform.startswith('win'):
            param = '-n'
        else:
            param = '-c'

        process = subprocess.Popen(
            ['ping', host, param, str(count)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()
        if stderr:
            logger.info( "Errores:")
            logger.info( stderr)

        if sys.platform.startswith('win'):
            successful_pings = len(re.findall(r'Reply from', stdout))
        else:
            successful_pings = len(re.findall(r'bytes from', stdout))

    except OSError as e:
        logger.info( "Ocurrio un error al ejecutar el comando 'ping':", e)
    except Exception as e:
        logger.info( "Ocurrio un error:", e)

    return successful_pings

def check_connectivity_via_socket(host, port):
    try:
        # Intentar conectar al servidor
        with socket.create_connection((host, port), timeout=10):
            return True
    except (socket.timeout, ConnectionRefusedError, socket.error):
        return False

def horario_permite_rebootear():
    ahora = datetime.now()

    inicio_rango = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_rango = ahora.replace(hour=8, minute=0, second=0, microsecond=0)

    #if ahora < fin_rango:
    #    if ahora >= inicio_rango:
    #        return False

    return True

def run_command(command):
    try:
        output = subprocess.check_output(command, stderr=subprocess.STDOUT)
        return (True, output)
    except subprocess.CalledProcessError as e:
        return (False, e.output)
    except Exception as e:
        return (False, str(e))

def action_soft_reset():
    logger.info("OpenVpn , ModemManager restart")
    run_command(['systemctl', 'stop', 'openvpn'])
    time.sleep(2)
    run_command(['systemctl', 'restart', 'ModemManager'])
    time.sleep(2)
    run_command(['systemctl', 'start', 'openvpn'])

def action_modem_hard_reset():

    logger.info("Modem hard reset")

    run_command(['raspi-gpio', 'set', '10', 'pd'])  # raspi-gpio set 10 pd
    time.sleep(0.5)
    run_command(['raspi-gpio', 'set', '10', 'op', 'dl'])  # raspi-gpio set 10 op dl
    time.sleep(5)
    run_command(['raspi-gpio', 'set', '10', 'dh'])  # raspi-gpio set 10 dh
    time.sleep(5)
    run_command(['raspi-gpio', 'set', '10', 'dl'])  # raspi-gpio set 10 dl
    time.sleep(5)
    action_soft_reset()


def action_reboot():
    run_command(['reboot'])

def perform_action(action_id):

    global  failure_start_time, action_done
    if action_id == 1:
        logger.info("Realizando accion 1 (5 minutos)...")

        action_soft_reset()
        # subprocess.run(['sudo', 'systemctl', 'restart', 'networking'])
    elif action_id == 2:
        logger.info("Realizando accion 2 (10 minutos)...")

        action_modem_hard_reset()
        time.sleep(60)
        if not check_connectivity_via_ping(HOST):
            action_soft_reset()

    elif action_id == 3:
        logger.info("Realizando accion 3 (15 minutos)...")

        if horario_permite_rebootear():
            logger.info("rebooteando")
            time.sleep(1)
            action_reboot()

        #reinicio las acciones
        failure_start_time = None
        action_done = [False, False, False]

def main():
    global  failure_start_time,action_done

    while True:
        if not check_connectivity_via_ping(HOST):
            RETRY_INTERVAL=15
            logger.info("No Pong Error")
            if failure_start_time is None:
                failure_start_time = time.time()
            else:
                elapsed_time = time.time() - failure_start_time
                for i, interval in enumerate(ACTION_INTERVALS):
                    if not action_done[i] and elapsed_time >= interval:
                        perform_action(i + 1)
                        action_done[i] = True
        else:
            RETRY_INTERVAL=30
            logger.info("Pong")
            failure_start_time = None
            action_done = [False, False, False]

        time.sleep(RETRY_INTERVAL)

#configurar como servicio con restart automatico
if __name__ == "__main__":
    main()

    #print(run_command(['gpioset', '-m time', '-s', '1', '3', '3=1']))
    #print(check_connectivity_via_ping("18.211.55.123",2)>0)

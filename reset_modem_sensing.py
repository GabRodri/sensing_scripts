#!/usr/bin/python2.7

import os
import socket
import subprocess
import time
import sys
import re
import traceback
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler

HOST = '8.8.8.8'
HOSTVPN = "10.8.0.1"
#HOST=HOSTVPN
PORT = 80
RETRY_INTERVAL_OK = 30     # Tiempo en segundos entre chequeos cuando hay conexion
RETRY_INTERVAL_FALLA = 15  # Tiempo en segundos entre chequeos cuando no hay conexion

CONEXION_NM = "LTE"        # Nombre de la conexion en NetworkManager
VENDOR_QUECTEL = "2c7c"    # idVendor del modem en el bus USB (Quectel EC25-AUX)

GPIO_MODEM = "10"          # GPIO del HAT conectado al modem
GPIO_PULSO_S = 0.3         # Ancho del pulso. RESET_N del EC25: 150-460 ms
                           # (si el pin fuera PWRKEY habria que subirlo a 1.0)

####################
logger = logging.getLogger("sensing conn check" )
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
action_done = []

def check_connectivity_via_wwan(interface="wwan0"):
    try:
        # Ejecuta "ip addr show wwan0"
        process = subprocess.Popen(
            ["ip", "addr", "show", interface],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            logger.info("La interfaz %s no existe" % interface)
            return False

        # Verificar si esta "UP"
        # if "state UP" not in stdout:
        #     logger.info("La interfaz %s existe pero esta inactiva" % interface)
        #     return False

        # Buscar direccion IP (inet)
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", stdout)
        if match:
            ip = match.group(1)
            logger.info("La interfaz %s esta activa - IP: %s" % (interface, ip))
            return True
        else:
            logger.info("La interfaz %s esta activa pero sin IP asignada " % interface)
            return False

    except Exception as e:
        logger.info("Error al verificar la interfaz %s: %s" % (interface, str(e)))
        logger.info(traceback.format_exc())
        return False


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
        logger.info( "Ocurrio un error al ejecutar el comando 'ping': %s" % str(e))
    except Exception as e:
        logger.info( "Ocurrio un error: %s" % str(e))

    return successful_pings

def check_connectivity_via_socket(host, port):
    sock = None
    try:
        # Intentar conectar al servidor
        sock = socket.create_connection((host, port), timeout=10)
        return True
    except (socket.timeout, socket.error):
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

def horario_permite_rebootear():
    ahora = datetime.now()

    inicio_rango = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    fin_rango = ahora.replace(hour=8, minute=0, second=0, microsecond=0)

    #if ahora < fin_rango:
    #    if ahora >= inicio_rango:
    #        return False

    return True

def resumir_salida(salida, limite=200):
    """Colapsa la salida de un comando a una sola linea para el log."""
    if not salida:
        return ""
    texto = " | ".join(l.strip() for l in str(salida).splitlines() if l.strip())
    if not texto:
        return ""
    if len(texto) > limite:
        texto = texto[:limite] + "..."
    return " -> %s" % texto

def run_command(command, descripcion=None):
    """Ejecuta un comando y DEJA CONSTANCIA del resultado en el log.

    Antes el retorno se descartaba en los 5 llamados, asi que un fallo de
    systemctl / raspi-gpio / reboot no dejaba ningun rastro.
    """
    etiqueta = descripcion if descripcion else " ".join(command)
    try:
        output = subprocess.check_output(command, stderr=subprocess.STDOUT)
        logger.info("OK    [%s]%s" % (etiqueta, resumir_salida(output)))
        return (True, output)
    except subprocess.CalledProcessError as e:
        logger.info("FALLO [%s] rc=%s%s" % (etiqueta, e.returncode, resumir_salida(e.output)))
        return (False, e.output)
    except Exception as e:
        logger.info("FALLO [%s] excepcion: %s" % (etiqueta, str(e)))
        return (False, str(e))

def escribir_sysfs(ruta, valor):
    """Escribe en sysfs dejando constancia del resultado."""
    f = None
    try:
        f = open(ruta, "w")
        f.write(valor)
        logger.info("OK    [sysfs %s <- %s]" % (ruta, valor))
        return True
    except Exception as e:
        logger.info("FALLO [sysfs %s <- %s] %s" % (ruta, valor, str(e)))
        return False
    finally:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass

def indice_modem():
    """Devuelve el indice actual del modem segun ModemManager.

    Hay que resolverlo en cada uso: despues de un reset el indice se
    incrementa (por eso config_sensing_bug.py usaba -m 0 y despues -m 1).
    """
    ok, salida = run_command(['mmcli', '-L'], "mmcli -L")
    if not ok:
        return None
    match = re.search(r"/Modem/(\d+)", str(salida))
    if match:
        return match.group(1)
    logger.info("ModemManager no lista ningun modem")
    return None

def buscar_puerto_usb_modem():
    """Devuelve el ID de puerto USB del modem (ej '1-1.3') o None."""
    base = "/sys/bus/usb/devices"
    try:
        for nombre in sorted(os.listdir(base)):
            if ":" in nombre:          # son interfaces, no dispositivos
                continue
            ruta = os.path.join(base, nombre, "idVendor")
            if not os.path.exists(ruta):
                continue
            f = open(ruta)
            try:
                vendor = f.read().strip().lower()
            finally:
                f.close()
            if vendor == VENDOR_QUECTEL:
                return nombre
    except Exception as e:
        logger.info("Error buscando el modem en el bus USB: %s" % str(e))
    return None

def esperar_modem_usb(presente, timeout):
    """Espera a que el modem aparezca (presente=True) o desaparezca del bus USB.

    Devuelve True si se cumplio dentro del timeout. Sirve para comprobar si un
    reset por hardware llego de verdad al modulo: un reset real lo hace
    desaparecer del bus unos segundos y despues reenumerar.
    """
    fin = time.time() + timeout
    while time.time() < fin:
        if (buscar_puerto_usb_modem() is not None) == presente:
            return True
        time.sleep(0.5)
    return False

def levantar_conexion():
    run_command(['nmcli', 'connection', 'up', CONEXION_NM])

# ---------------------------------------------------------------------------
# Escalera de acciones, de menor a mayor agresividad
# ---------------------------------------------------------------------------

def action_bearer_reset():
    """Nivel 1: solo el bearer de datos, sin tocar el modem."""
    run_command(['nmcli', 'connection', 'down', CONEXION_NM])
    time.sleep(2)
    levantar_conexion()

def action_soft_reset():
    """Nivel 2: restart de ModemManager. Ahora si levanta el bearer despues.

    Los coches de Cutcsa no usan OpenVPN, asi que el stop/start de la VPN
    quedo fuera de la escalera.
    """
    run_command(['systemctl', 'restart', 'ModemManager'])
    time.sleep(10)
    levantar_conexion()

def action_modem_disable_enable():
    """Nivel 3: disable/enable del modem (equivale a CFUN=4 / CFUN=1)."""
    idx = indice_modem()
    if idx is None:
        return
    run_command(['mmcli', '-m', idx, '--disable'])
    time.sleep(5)
    run_command(['mmcli', '-m', idx, '--enable'])
    time.sleep(10)
    levantar_conexion()

def action_modem_reset_mm():
    """Nivel 4: reset interno del modulo. Equivale al AT+CFUN=1,1 de
    config_sensing_bug.py, pero por la API de ModemManager, asi que no
    necesita que ModemManager corra en modo --debug."""
    idx = indice_modem()
    if idx is None:
        return
    run_command(['mmcli', '-m', idx, '--reset'])
    time.sleep(30)
    run_command(['systemctl', 'restart', 'ModemManager'])
    time.sleep(10)
    levantar_conexion()

def action_modem_reset_usb():
    """Nivel 5: re-enumeracion del USB.

    Es lo mas parecido por software al corte de alimentacion manual, que es
    lo unico que saco al modem del cuelgue del 15/08 (272 reboots no lo
    lograron). Un reboot en caliente no le corta la energia al modulo.
    """
    puerto = buscar_puerto_usb_modem()
    if puerto is None:
        logger.info("Modem no encontrado en el bus USB (vendor %s) - se omite el reset USB" % VENDOR_QUECTEL)
        return
    logger.info("Reset USB del modem en el puerto %s" % puerto)
    run_command(['systemctl', 'stop', 'ModemManager'])
    time.sleep(2)
    escribir_sysfs('/sys/bus/usb/drivers/usb/unbind', puerto)
    time.sleep(10)
    escribir_sysfs('/sys/bus/usb/drivers/usb/bind', puerto)
    time.sleep(15)
    run_command(['systemctl', 'start', 'ModemManager'])
    time.sleep(20)
    levantar_conexion()

def action_modem_hard_reset():
    """Nivel 6: pulso por GPIO al modem, con verificacion.

    Timing corregido al spec del EC25: RESET_N pide 150-460 ms. Los 5 s que
    habia antes se pasaban diez veces del maximo.

    PENDIENTE: confirmar contra el esquematico del HAT a que pin del modulo
    llega el GPIO 10. La evidencia dice que hoy no llega a ninguno: en la
    caida del 15/08 esta accion corrio 272 veces y wwan0 no desaparecio ni
    una sola vez. Por eso ahora se comprueba si el modulo se cae del bus
    USB despues del pulso, que es lo unico que prueba que el reset llego.
    """
    puerto = buscar_puerto_usb_modem()
    logger.info("Pulso de %.2f s en el GPIO %s (modem en el bus USB: %s)" % (
        GPIO_PULSO_S, GPIO_MODEM, puerto))

    run_command(['raspi-gpio', 'set', GPIO_MODEM, 'pd'])
    time.sleep(0.5)
    run_command(['raspi-gpio', 'set', GPIO_MODEM, 'op', 'dl'])
    time.sleep(0.5)
    run_command(['raspi-gpio', 'set', GPIO_MODEM, 'dh'])
    time.sleep(GPIO_PULSO_S)
    run_command(['raspi-gpio', 'set', GPIO_MODEM, 'dl'])

    if esperar_modem_usb(False, 30):
        logger.info("GPIO %s: el modem se reseteo, desaparecio del bus USB" % GPIO_MODEM)
        if esperar_modem_usb(True, 60):
            logger.info("GPIO %s: el modem reenumero en %s" % (
                GPIO_MODEM, buscar_puerto_usb_modem()))
        else:
            logger.info("GPIO %s: el modem NO volvio a enumerar en 60 s" % GPIO_MODEM)
    else:
        logger.info("GPIO %s: el modem NO desaparecio del bus USB en 30 s - "
                    "el pulso no esta llegando al modulo" % GPIO_MODEM)

    action_soft_reset()

def action_reboot():
    """Nivel 7, ultimo recurso.

    Bajado desde el nivel 3: un reboot no le corta la alimentacion al modem,
    y en la caida del 15/08 corrio 272 veces sin recuperar la conexion.
    """
    if not horario_permite_rebootear():
        logger.info("Reboot omitido por horario")
        return
    logger.info("Rebooteando (ultimo recurso)")
    time.sleep(1)
    run_command(['reboot'])

# (segundos sin conexion, nombre para el log, funcion)
ACCIONES = [
    (120,  "bearer nmcli down/up",        action_bearer_reset),
    (240,  "soft reset ModemManager",     action_soft_reset),
    (360,  "modem disable/enable",        action_modem_disable_enable),
    (540,  "modem reset (CFUN=1,1)",      action_modem_reset_mm),
    (780,  "reset USB del modem",         action_modem_reset_usb),
    (1080, "hard reset por GPIO",         action_modem_hard_reset),
    (1500, "reboot (ultimo recurso)",     action_reboot),
]

def main():
    global failure_start_time, action_done

    action_done = [False] * len(ACCIONES)
    logger.info("=== Watchdog iniciado (PID %s) - %d niveles de escalera ===" % (
        os.getpid(), len(ACCIONES)))

    while True:
        if not check_connectivity_via_wwan():
            retry = RETRY_INTERVAL_FALLA
            logger.info("No Pong Error")
            if failure_start_time is None:
                failure_start_time = time.time()
            else:
                elapsed_time = time.time() - failure_start_time
                for i, (umbral, nombre, funcion) in enumerate(ACCIONES):
                    if not action_done[i] and elapsed_time >= umbral:
                        logger.info("--- Accion %d/%d: %s (a los %d s de falla) ---" % (
                            i + 1, len(ACCIONES), nombre, elapsed_time))
                        try:
                            funcion()
                        except Exception as e:
                            logger.info("Excepcion en la accion %d (%s): %s" % (
                                i + 1, nombre, str(e)))
                            logger.info(traceback.format_exc())
                        action_done[i] = True

                        if i == len(ACCIONES) - 1:
                            # se agoto la escalera, se reinicia el ciclo
                            logger.info("Escalera agotada - se reinicia el ciclo")
                            failure_start_time = None
                            action_done = [False] * len(ACCIONES)

                        # una sola accion por vuelta: hay que volver a medir
                        # la conectividad antes de escalar al proximo nivel
                        break
        else:
            retry = RETRY_INTERVAL_OK
            logger.info("Pong")
            failure_start_time = None
            action_done = [False] * len(ACCIONES)

        time.sleep(retry)

#configurar como servicio con restart automatico
if __name__ == "__main__":
    main()

    #print(run_command(['gpioset', '-m time', '-s', '1', '3', '3=1']))
    #print(check_connectivity_via_ping("18.211.55.123",2)>0)

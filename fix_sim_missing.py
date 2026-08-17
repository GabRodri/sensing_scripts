#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Corrige el 'sim-missing' del Quectel EC25 ajustando AT+QSIMVOL.

Sintoma: ModemManager reporta

    state: failed
    failed reason: sim-missing

con la SIM fisicamente insertada. Por AT se ve AT+CPIN? -> +CME ERROR: 10,
AT+QCCID -> +CME ERROR: 13 y AT+QINISTAT -> 0: el modulo le habla a la
tarjeta y no recibe respuesta electrica. La causa suele ser la tension de la
interfaz de SIM, no el hardware.

Verificado el 2026-08-17 en sensingOCPPCome, que llevaba tiempo caido y no se
arreglaba ni cambiando la SIM (se probaron dos). Con AT+QSIMVOL=1 paso de
'failed' a 'connected'.

SEGURIDAD: si el modem no esta exactamente en 'failed/sim-missing', el script
no toca nada y sale. Nunca hay que correr un CFUN=1,1 sobre un modem sano de
un coche en la calle: si algo sale mal no hay forma de llegar al equipo.
Cuando si esta en sim-missing, el equipo ya esta caido y no hay nada que
perder. Si ningun valor funciona, restaura el original antes de salir.

Uso:
    sudo python3 fix_sim_missing.py            # solo diagnostica, no cambia nada
    sudo python3 fix_sim_missing.py --apply    # intenta el workaround

Codigos de salida:
    0  el modem esta sano, o quedo arreglado
    1  es sim-missing y ningun valor de QSIMVOL lo arreglo -> banco
    2  falta sudo
    3  ModemManager no lista ningun modem
    4  no se encontro el puerto AT
   10  es sim-missing pero se corrio sin --apply
"""

import argparse
import os
import re
import subprocess
import sys
import time

PUERTOS_AT = ["/dev/ttyUSB2", "/dev/ttyUSB3"]
VALORES_QSIMVOL = ["1", "2"]     # rango soportado por el EC25: (0-2)
ESPERA_RESET = 50                # segundos que tarda el modulo tras CFUN=1,1


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def sh(cmd, timeout=60):
    try:
        salida = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout)
        return 0, salida.decode(errors="replace")
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output.decode(errors="replace")
    except Exception as e:
        return -1, str(e)


def at(puerto, comando, espera=1.5):
    """Manda un comando AT y devuelve la respuesta en una linea."""
    try:
        subprocess.call(["stty", "-F", puerto, "115200", "raw", "-echo"],
                        stderr=subprocess.DEVNULL)
        fd = os.open(puerto, os.O_RDWR | os.O_NONBLOCK)
    except Exception as e:
        return "ERROR abriendo %s: %s" % (puerto, e)
    try:
        os.write(fd, (comando + "\r").encode())
        time.sleep(espera)
        buf = b""
        try:
            while True:
                d = os.read(fd, 4096)
                if not d:
                    break
                buf += d
        except Exception:
            pass
        return b" ".join(buf.split()).decode(errors="replace")
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def puerto_at():
    """Devuelve el primer puerto AT que conteste OK."""
    for p in PUERTOS_AT:
        if os.path.exists(p) and "OK" in at(p, "AT", 1.0):
            return p
    return None


def esperar_puerto(timeout=90):
    """Espera a que el puerto AT vuelva despues de un reset del modulo."""
    fin = time.time() + timeout
    while time.time() < fin:
        p = puerto_at()
        if p:
            return p
        time.sleep(2)
    return None


def estado_modem():
    """Devuelve (state, failed_reason) segun ModemManager."""
    rc, salida = sh(["mmcli", "-L"])
    m = re.search(r"/Modem/(\d+)", salida)
    if not m:
        return (None, None)
    rc, salida = sh(["mmcli", "-m", m.group(1)])
    st = re.search(r"state:\s*(\S+)", salida)
    fr = re.search(r"failed reason:\s*(\S+)", salida)
    return (st.group(1) if st else None, fr.group(1) if fr else None)


def leer_qsimvol(puerto):
    m = re.search(r"\+QSIMVOL:\s*(\d+)", at(puerto, "AT+QSIMVOL?"))
    return m.group(1) if m else None


def fijar_qsimvol(puerto, valor):
    """Fija el valor, lo guarda y resetea el modulo. Devuelve el puerto nuevo."""
    at(puerto, "AT+QSIMVOL=%s" % valor)
    at(puerto, "AT&W", 2)
    at(puerto, "AT+CFUN=1,1", 2)
    log("  modulo reiniciando, esperando %ss..." % ESPERA_RESET)
    time.sleep(ESPERA_RESET)
    return esperar_puerto(90)


def main():
    ap = argparse.ArgumentParser(
        description="Corrige el sim-missing del EC25 via AT+QSIMVOL.")
    ap.add_argument("--apply", action="store_true",
                    help="aplica el workaround (por defecto solo diagnostica)")
    args = ap.parse_args()

    if os.geteuid() != 0:
        log("hay que correrlo con sudo")
        return 2

    state, reason = estado_modem()
    log("ModemManager -> state=%s  failed_reason=%s" % (state, reason))

    if state is None:
        log("ModemManager no lista ningun modem - nada que hacer")
        return 3

    if not (state == "failed" and reason == "sim-missing"):
        log("el modem NO esta en 'failed/sim-missing': no se toca nada")
        return 0

    # A partir de aca el equipo ya esta caido, no hay conectividad que perder.
    log("sim-missing confirmado - se libera el puerto AT")
    sh(["systemctl", "stop", "ModemManager"])
    time.sleep(3)

    try:
        p = esperar_puerto(30)
        if p is None:
            log("no se encontro ningun puerto AT que conteste")
            return 4

        original = leer_qsimvol(p) or "0"
        log("puerto AT: %s" % p)
        log("QSIMVOL actual: %s" % original)
        log("rango soportado: %s" % at(p, "AT+QSIMVOL=?"))
        log("CPIN?     -> %s" % at(p, "AT+CPIN?", 2))
        log("QINISTAT  -> %s" % at(p, "AT+QINISTAT"))

        if not args.apply:
            log("modo diagnostico. Para intentar el fix: sudo python3 %s --apply"
                % os.path.basename(__file__))
            return 10

        for valor in [v for v in VALORES_QSIMVOL if v != original]:
            log("probando QSIMVOL=%s ..." % valor)
            p = fijar_qsimvol(p, valor)
            if p is None:
                log("  el modulo no volvio tras el reset")
                p = esperar_puerto(60)
                if p is None:
                    log("  se perdio el puerto AT, se aborta")
                    return 1
                continue
            respuesta = at(p, "AT+CPIN?", 3)
            log("  CPIN? -> %s" % respuesta)
            if "READY" in respuesta:
                at(p, "AT&W", 2)
                log("*** ARREGLADO: la SIM inicializo con QSIMVOL=%s (guardado) ***" % valor)
                log("ICCID: %s" % at(p, "AT+QCCID", 2))
                return 0

        log("ningun valor funciono - restaurando QSIMVOL=%s" % original)
        p = fijar_qsimvol(p, original) or p
        log("no se pudo arreglar por software: el equipo necesita banco")
        log("revisar soldaduras y contactos del portasim")
        return 1

    finally:
        sh(["systemctl", "start", "ModemManager"])
        log("ModemManager reiniciado")


if __name__ == "__main__":
    sys.exit(main())

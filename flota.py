# -*- coding: utf-8 -*-
"""Opera la flota de watchdogs por SSH, en paralelo e insistiendo.

La password sale de la variable de entorno OSMA_PASS: no se escribe en ningun
archivo ni se commitea, y se filtra del output (el PTY de sudo la eco-a).

Con la flota rebooteando cada ~29 min, en cualquier instante hay equipos con
el puerto 22 cerrado. Por eso se hacen pasadas sucesivas sobre los que faltan.

Uso:
    OSMA_PASS=... python flota.py hosts.txt estado   # solo lee: uptime, reboots, servicio
    OSMA_PASS=... python flota.py hosts.txt stop     # EMERGENCIA: detiene el watchdog
    OSMA_PASS=... python flota.py hosts.txt apply    # despliega ARCHIVOS y reinicia

hosts.txt: una IP por linea, se ignoran vacias y las que empiezan con #

    10.200.6.117   # coche 1148 / sensing 199

Variables de entorno:
    OSMA_PASS   (obligatoria) password SSH de los equipos
    REPO        de donde salen los archivos a copiar (default: este directorio)
    HILOS       conexiones en paralelo (default 24)
    MAX_PASADAS vueltas sobre los pendientes (default 40)
    ESPERA      segundos entre pasadas (default 45)
    TIMEOUT     timeout de conexion (default 12)

En modo apply, por equipo: backup con sufijo fijo, copia por sftp, verificacion
de sha256, import bajo python2.7 CON SUDO (como pi falla al abrir el log, que es
de root), reinicio del servicio y confirmacion de que aparezca Pong. Si el hash
no coincide o el import falla, restaura el backup y NO reinicia: el equipo se
queda con la version vieja funcionando.
"""
import os, sys, time, socket, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import paramiko
paramiko.util.log_to_file(os.devnull)

PASS = os.environ.get("OSMA_PASS")
USER = "pi"
SERVICIO = "sensing_check_conn.service"
DESTINO = "/home/pi/sensing_scripts"
SUFIJO = ".bak-predeploy"          # fijo: el reloj arranca en epoch
REPO = os.environ.get("REPO", os.path.dirname(os.path.abspath(__file__)))
ARCHIVOS = ["reset_modem_sensing.py", "fix_sim_missing.py", "mqtt_tap.py"]
MAX_PASADAS = int(os.environ.get("MAX_PASADAS", "40"))
ESPERA = int(os.environ.get("ESPERA", "45"))
HILOS = int(os.environ.get("HILOS", "24"))
TIMEOUT = int(os.environ.get("TIMEOUT", "12"))

lock = threading.Lock()
HASHES = {}


def sha_local(nombre):
    import hashlib
    with open(os.path.join(REPO, nombre), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def limpiar(texto):
    fuera = ("password for", "[sudo]")
    out = []
    for l in texto.splitlines():
        l = l.strip()
        if not l or any(f in l.lower() for f in fuera):
            continue
        if PASS:
            l = l.replace(PASS, "***")
        if l and l != "***":
            out.append(l)
    return " | ".join(out)


def correr(cli, cmd, timeout=30):
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=timeout, get_pty=True)
    try:
        stdin.write(PASS + "\n"); stdin.flush()      # por si sudo pide password
    except Exception:
        pass
    out = stdout.read().decode(errors="ignore")
    stdout.channel.recv_exit_status()
    return out


def trabajar(host, etiqueta, modo):
    """Devuelve (host, estado, detalle). estado: ok | reintentar | problema"""
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(hostname=host, port=22, username=USER, password=PASS,
                    timeout=TIMEOUT, banner_timeout=TIMEOUT, auth_timeout=TIMEOUT,
                    allow_agent=False, look_for_keys=False)
    except paramiko.AuthenticationException:
        return (host, "problema", "autenticacion rechazada")
    except Exception as e:
        return (host, "reintentar", "sin acceso (%s)" % type(e).__name__)

    try:
        if modo == "stop":
            antes = limpiar(correr(cli, "systemctl is-active %s || true" % SERVICIO))
            correr(cli, "sudo systemctl stop %s" % SERVICIO)
            desp = limpiar(correr(cli, "systemctl is-active %s || true" % SERVICIO))
            if "inactive" in desp or "failed" in desp:
                return (host, "ok", "detenido (estaba: %s)" % (antes or "?"))
            return (host, "reintentar", "no se detuvo (%s)" % desp[:30])
        elif modo == "apply":
            lista = " ".join(ARCHIVOS)
            correr(cli, "cd %s && for f in %s; do [ -f $f ] && cp -a $f $f%s; done"
                        % (DESTINO, lista, SUFIJO))
            try:
                sftp = cli.open_sftp()
                for nombre in ARCHIVOS:
                    remoto = "%s/%s" % (DESTINO, nombre)
                    sftp.put(os.path.join(REPO, nombre), remoto)
                    sftp.chmod(remoto, 0o755)
                sftp.close()
            except Exception as e:
                return (host, "reintentar", "sftp fallo (%s)" % type(e).__name__)

            out = correr(cli, "cd %s && sha256sum %s" % (DESTINO, lista))
            remotos = {}
            for linea in limpiar(out).split(" | "):
                partes = linea.split()
                if len(partes) == 2:
                    remotos[partes[1].split("/")[-1]] = partes[0]
            malos = [n for n in ARCHIVOS if remotos.get(n) != HASHES[n]]
            if malos:
                correr(cli, "cd %s && for f in %s; do [ -f $f%s ] && cp -a $f%s $f; done"
                            % (DESTINO, lista, SUFIJO, SUFIJO))
                return (host, "reintentar", "hash no coincide: %s" % ",".join(malos))

            # con sudo, igual que corre el servicio: como 'pi' el import falla
            # al abrir check_connectivity.log, que es de root (falso negativo)
            out = correr(cli, "cd %s && sudo python2.7 -c 'import reset_modem_sensing' "
                              "&& echo IMPORT_OK" % DESTINO)
            if "IMPORT_OK" not in out:
                correr(cli, "cd %s && for f in %s; do [ -f $f%s ] && cp -a $f%s $f; done"
                            % (DESTINO, lista, SUFIJO, SUFIJO))
                return (host, "problema", "IMPORT FALLO - backup restaurado, NO se reinicio")

            correr(cli, "sudo systemctl restart %s" % SERVICIO)
            time.sleep(12)
            out = correr(cli, "tail -40 %s/check_connectivity.log" % DESTINO)
            if " - Pong" in out:
                return (host, "ok", "desplegado y reporta Pong")
            motivo = ""
            for linea in out.splitlines():
                if "No Pong Error" in linea:
                    motivo = linea.split("No Pong Error")[-1].strip(" -")[:45]
            return (host, "problema", "desplegado pero SIN Pong (%s)" % (motivo or "?"))

        else:
            out = correr(cli, "hostname; uptime -s; last reboot | wc -l; "
                              "systemctl is-active %s || true" % SERVICIO)
            return (host, "ok", limpiar(out)[:90])
    except Exception as e:
        return (host, "reintentar", "error en el comando (%s)" % type(e).__name__)
    finally:
        try:
            cli.close()
        except Exception:
            pass


def main():
    if not PASS:
        print("falta OSMA_PASS"); return 2
    ruta = sys.argv[1]
    modo = sys.argv[2] if len(sys.argv) > 2 else "estado"

    pendientes, etiqueta = [], {}
    for l in open(ruta):
        ip = l.split("#")[0].strip()
        if ip:
            pendientes.append(ip)
            etiqueta[ip] = l.split("#")[1].strip() if "#" in l else ""

    if modo == "apply":
        print("=== archivos a desplegar ===")
        for n in ARCHIVOS:
            HASHES[n] = sha_local(n)
            print("  %-26s %s" % (n, HASHES[n]))
        print()
    print("=== %d equipos | modo: %s | %d hilos ===\n" % (len(pendientes), modo, HILOS))
    listos, problemas = {}, {}
    pasadas = 1 if modo == "estado" else MAX_PASADAS
    t0 = time.time()

    for p in range(1, pasadas + 1):
        if not pendientes:
            break
        print("########## PASADA %d/%d - faltan %d ##########" % (p, pasadas, len(pendientes)))
        quedan = []
        with ThreadPoolExecutor(max_workers=HILOS) as ex:
            futs = {ex.submit(trabajar, h, etiqueta.get(h, ""), modo): h for h in pendientes}
            for fut in as_completed(futs):
                host, estado, detalle = fut.result()
                with lock:
                    if estado == "ok":
                        listos[host] = detalle
                        print("  OK   %-15s %-22s %s" % (host, etiqueta.get(host, ""), detalle))
                    elif estado == "problema":
                        problemas[host] = detalle
                        print("  PROB %-15s %-22s %s" % (host, etiqueta.get(host, ""), detalle))
                    else:
                        quedan.append(host)
        print("  --- pasada %d: %d ok acumulados, %d pendientes ---" % (p, len(listos), len(quedan)))
        pendientes = quedan
        if pendientes and p < pasadas:
            print("  ... esperando %ds\n" % ESPERA)
            time.sleep(ESPERA)
        else:
            print()

    print("################ RESUMEN (%.1f min) ################" % ((time.time() - t0) / 60))
    print("LISTOS           : %d" % len(listos))
    print("PROBLEMAS        : %d  %s" % (len(problemas), list(problemas)[:10]))
    print("NO ALCANZADOS    : %d  %s" % (len(pendientes), pendientes[:20]))
    if modo == "estado":
        act = sum(1 for v in listos.values() if v.rstrip().endswith("active")
                  and not v.rstrip().endswith("inactive"))
        print("\ncon el watchdog ACTIVO: %d de %d alcanzados" % (act, len(listos)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Verifica el estado de la flota despues de un despliegue.

Por cada equipo comprueba: sha256 de reset_modem_sensing.py contra el local,
presencia de mqtt_tap.py, si el servicio esta activo, si el detector es TCP o
el ICMP viejo, y el ultimo Pong / No Pong Error del log.

    OSMA_PASS=... python verificar.py hosts.txt
"""
import os, sys, hashlib, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import paramiko
paramiko.util.log_to_file(os.devnull)

PASS = os.environ["OSMA_PASS"]
REPO = os.environ.get("REPO", os.path.dirname(os.path.abspath(__file__)))
D = "/home/pi/sensing_scripts"
ESPERADO = hashlib.sha256(open(os.path.join(REPO, "reset_modem_sensing.py"), "rb").read()).hexdigest()
lock = threading.Lock()
res = []

def uno(host, etq):
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username="pi", password=PASS, timeout=15,
                  banner_timeout=15, auth_timeout=15, allow_agent=False, look_for_keys=False)
    except Exception as e:
        return (host, etq, "SIN_ACCESO", type(e).__name__, "", "")
    try:
        i, o, e = c.exec_command(
            "cd %s && sha256sum reset_modem_sensing.py | cut -d' ' -f1; "
            "[ -f mqtt_tap.py ] && echo TAP_SI || echo TAP_NO; "
            "systemctl is-active sensing_check_conn.service; "
            "grep -a 'Detector:' check_connectivity.log | tail -1; "
            "tail -40 check_connectivity.log | grep -aoE '(- Pong$|No Pong Error - .*)' | tail -1" % D,
            timeout=30, get_pty=True)
        i.write(PASS + "\n"); i.flush()
        out = o.read().decode(errors="ignore")
        L = [l.strip() for l in out.splitlines()
             if l.strip() and PASS not in l and "password" not in l.lower()]
        sha = next((l for l in L if len(l) == 64), "")
        tap = "SI" if "TAP_SI" in out else "NO"
        act = "active" if any(l == "active" for l in L) else "inactive"
        det = "TCP" if any("TCP a" in l for l in L) else ("ICMP" if any("ping a" in l for l in L) else "?")
        ult = next((l for l in reversed(L) if "Pong" in l), "")
        return (host, etq, "OK" if sha == ESPERADO else "SHA_DISTINTO", tap, act + "/" + det, ult[:48])
    except Exception as e:
        return (host, etq, "ERROR_CMD", type(e).__name__, "", "")
    finally:
        c.close()

hosts = []
for l in open(sys.argv[1]):
    ip = l.split("#")[0].strip()
    if ip:
        hosts.append((ip, l.split("#")[1].strip() if "#" in l else ""))

with ThreadPoolExecutor(max_workers=24) as ex:
    futs = [ex.submit(uno, h, e) for h, e in hosts]
    for f in as_completed(futs):
        res.append(f.result())

from collections import Counter
print("=== VERIFICACION DE %d EQUIPOS ===\n" % len(res))
print("version del script : %s" % dict(Counter(r[2] for r in res)))
print("mqtt_tap.py presente: %s" % dict(Counter(r[3] for r in res if r[2] == "OK")))
print("servicio/detector   : %s" % dict(Counter(r[4] for r in res if r[2] == "OK")))
print("ultimo estado       : %s" % dict(Counter(
    ("Pong" if "- Pong" in r[5] else (r[5][:34] or "?")) for r in res if r[2] == "OK")))
print("\n--- los que no quedaron OK ---")
for r in sorted(res, key=lambda x: x[2]):
    if r[2] != "OK" or "Pong" not in r[5]:
        print("  %-15s %-22s %-12s %s %s" % (r[0], r[1], r[2], r[4], r[5][:40]))

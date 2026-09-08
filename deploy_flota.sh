#!/bin/bash
# Despliega el watchdog en la flota, equipo por equipo y verificando cada paso.
#
# Los equipos no tienen internet (VPN de chips que solo llega al broker), asi
# que no se puede hacer git pull: hay que copiar por scp desde la maquina del
# operador.
#
# Uso:
#   ./deploy_flota.sh hosts.txt              # solo diagnostica, no toca nada
#   ./deploy_flota.sh hosts.txt --stop       # EMERGENCIA: para el watchdog en todos
#   ./deploy_flota.sh hosts.txt --apply      # despliega la version corregida
#
# --stop existe para cortar un incidente rapido. Si el detector esta midiendo
# mal, el watchdog rebootea los coches en loop; pararlo lleva segundos por
# equipo, mientras que desplegar lleva minutos. Los coches siguen publicando
# MQTT sin watchdog: se pierde la recuperacion automatica, no el servicio.
#
# hosts.txt: una IP por linea. Se ignoran lineas vacias y las que empiezan con #
#   10.200.6.117   # bus 1148 / sensingBus199
#   10.200.6.204   # sensingBus88
#
# Conviene tener clave SSH configurada; si no, pide password en cada paso.

set -u
ARCHIVOS="reset_modem_sensing.py fix_sim_missing.py"
DESTINO="/home/pi/sensing_scripts"
SERVICIO="sensing_check_conn.service"
SUFIJO_BACKUP=".bak-predeploy"   # fijo a proposito: el reloj de estos equipos arranca en epoch

HOSTS_FILE="${1:-}"
APPLY="${2:-}"
[ -z "$HOSTS_FILE" ] && { echo "uso: $0 hosts.txt [--apply]"; exit 2; }
[ -f "$HOSTS_FILE" ] || { echo "no existe $HOSTS_FILE"; exit 2; }

for f in $ARCHIVOS; do
    [ -f "$f" ] || { echo "falta $f en el directorio actual"; exit 2; }
done

echo "=== hashes locales ==="
for f in $ARCHIVOS; do
    printf "  %-26s %s\n" "$f" "$(sha256sum "$f" | cut -d' ' -f1)"
done
echo

OK=(); FALLO=(); SALTEADO=()

while read -r linea; do
    HOST=$(echo "$linea" | sed 's/#.*//' | tr -d '[:space:]')
    [ -z "$HOST" ] && continue
    echo "############ $HOST ############"

    if ! ssh -o ConnectTimeout=15 -o BatchMode=no "pi@$HOST" true 2>/dev/null; then
        echo "  SIN ACCESO SSH - se saltea"; SALTEADO+=("$HOST"); echo; continue
    fi

    echo "  --- estado actual ---"
    ssh "pi@$HOST" "uptime -s; last reboot | wc -l; tail -2 $DESTINO/check_connectivity.log 2>/dev/null | cut -c1-110"

    if [ "$APPLY" = "--stop" ]; then
        if ssh "pi@$HOST" "sudo systemctl stop $SERVICIO && systemctl is-active $SERVICIO || true" 2>&1 | tail -1 | grep -q inactive; then
            echo "  >>> watchdog DETENIDO"; OK+=("$HOST")
        else
            echo "  >>> NO SE PUDO DETENER"; FALLO+=("$HOST stop")
        fi
        echo; continue
    fi

    if [ "$APPLY" != "--apply" ]; then
        echo "  (modo diagnostico: sin --apply ni --stop no se toca nada)"; echo; continue
    fi

    echo "  --- backup ---"
    ssh "pi@$HOST" "cd $DESTINO && for f in $ARCHIVOS; do [ -f \$f ] && cp -a \$f \$f$SUFIJO_BACKUP; done; ls -1 *$SUFIJO_BACKUP" || {
        echo "  BACKUP FALLO - se aborta este equipo"; FALLO+=("$HOST backup"); echo; continue; }

    echo "  --- copia ---"
    if ! scp -q $ARCHIVOS "pi@$HOST:$DESTINO/"; then
        echo "  SCP FALLO"; FALLO+=("$HOST scp"); echo; continue
    fi

    echo "  --- verificacion de hash ---"
    ESPERADO=$(sha256sum $ARCHIVOS | sort)
    REMOTO=$(ssh "pi@$HOST" "cd $DESTINO && sha256sum $ARCHIVOS" | sort)
    if [ "$ESPERADO" != "$REMOTO" ]; then
        echo "  HASH NO COINCIDE - se restaura el backup"
        echo "$REMOTO"
        ssh "pi@$HOST" "cd $DESTINO && for f in $ARCHIVOS; do [ -f \$f$SUFIJO_BACKUP ] && cp -a \$f$SUFIJO_BACKUP \$f; done"
        FALLO+=("$HOST hash"); echo; continue
    fi
    echo "  hash OK"

    echo "  --- import bajo python2.7 (si falla NO se reinicia el servicio) ---"
    if ! ssh "pi@$HOST" "cd $DESTINO && python2.7 -c 'import reset_modem_sensing'" 2>&1; then
        echo "  IMPORT FALLO - se restaura el backup y se deja el viejo corriendo"
        ssh "pi@$HOST" "cd $DESTINO && for f in $ARCHIVOS; do [ -f \$f$SUFIJO_BACKUP ] && cp -a \$f$SUFIJO_BACKUP \$f; done"
        FALLO+=("$HOST import"); echo; continue
    fi
    echo "  import OK"

    echo "  --- reinicio del servicio ---"
    ssh "pi@$HOST" "sudo systemctl restart $SERVICIO"
    sleep 20

    echo "  --- verificacion post-arranque ---"
    RES=$(ssh "pi@$HOST" "tail -30 $DESTINO/check_connectivity.log")
    echo "$RES" | grep -aE "Detector:|conexion 'LTE'|AVISO" | sed 's/^/    /' | cut -c1-120
    if echo "$RES" | grep -qa " - Pong$"; then
        echo "  >>> $HOST OK: reporta Pong"; OK+=("$HOST")
    else
        echo "  >>> $HOST ATENCION: no aparece Pong todavia"
        echo "$RES" | grep -a "No Pong Error" | tail -2 | sed 's/^/    /' | cut -c1-120
        FALLO+=("$HOST sin-pong")
    fi
    echo
done < "$HOSTS_FILE"

echo "################ RESUMEN ################"
echo "OK        (${#OK[@]}): ${OK[*]:-ninguno}"
echo "FALLO     (${#FALLO[@]}): ${FALLO[*]:-ninguno}"
echo "SALTEADO  (${#SALTEADO[@]}): ${SALTEADO[*]:-ninguno}"
[ ${#FALLO[@]} -eq 0 ] || exit 1

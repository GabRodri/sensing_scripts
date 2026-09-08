# sensing_scripts

Watchdog de conectividad para los equipos Sensing de la flota CUTCSA
(Raspberry Pi + módem Quectel EC25-AUX sobre APN privada de ANTEL).

> **NOTA:** este repositorio debe clonarse en `/home/pi/`. Los equipos **no
> tienen salida a internet** — la VPN de chips sólo llega al broker MQTT — así
> que en producción no se puede hacer `git pull`: se copia por SSH con
> [`flota.py`](flota.py).

## Contenido

| Archivo | Qué es |
|---|---|
| `reset_modem_sensing.py` | El watchdog. Corre como servicio en cada coche |
| `reset_modem_sensing.sh` | Lanzador que usa el unit de systemd |
| `fix_sim_missing.py` | Repara el `sim-missing` del EC25. Lo usa el nivel 3 del watchdog, y sirve a mano |
| `flota.py` | Opera la flota por SSH: diagnostica, detiene o despliega |
| `verificar.py` | Verifica el estado de la flota después de un despliegue |
| `mqtt_tap.py` | Decodifica el MQTT de un equipo desde una captura `tcpdump` |

---

## El watchdog

### Cómo detecta que hay falla

Dos condiciones, en orden:

1. `wwan0` tiene IP (`ip addr show wwan0`)
2. Abre TCP contra el broker `10.220.0.17:1883`

**No usar ICMP.** La VPN de chips lo bloquea entero: ni el broker ni el propio
gateway del enlace responden ping, con el módem sano y publicando MQTT sin
problemas. Un detector por ping reporta falla permanente y termina rebooteando
el coche en loop — ver el incidente más abajo.

Mirar sólo si hay IP tampoco alcanza: un PDP colgado deja la dirección asignada
y el tráfico igual no pasa.

### La escalera

Con la cadencia en 15 s durante la falla, se ejecuta **una acción por vuelta** y
se vuelve a medir antes de escalar.

| # | A los | Acción |
|---|---|---|
| 1 | 2 min | `nmcli connection down/up LTE` |
| 2 | 4 min | `systemctl restart ModemManager` + levantar el bearer |
| 3 | 6 min | Fix de `sim-missing` — **condicional**, sólo si `mmcli` lo reporta |
| 4 | 8 min | `mmcli --disable` / `--enable`, con 3 reintentos |
| 5 | 11 min | `mmcli --reset` (equivale a `AT+CFUN=1,1`) |
| 6 | 15 min | `unbind`/`bind` del USB — lo más parecido a un corte de alimentación |
| 7 | 20 min | Pulso por GPIO 10, con verificación |
| 8 | 27 min | `reboot`, último recurso |

**El orden no es cosmético.** Cuando el pulso GPIO funciona se lleva puesto el
GPS: `gpsd` pierde el `ttyUSB` y no lo vuelve a tomar hasta el próximo reinicio.
Los escalones blandos del medio existen para que, si alguno destraba el PDP, el
pulso no llegue a ocurrir. **No adelantar el nivel 7.**

El `reboot` está último a propósito: un reinicio en caliente **no le corta la
alimentación al módulo**, así que no saca al módem de un cuelgue. Está medido —
272 reboots en 45 h no recuperaron un equipo que volvió a los 12 s de un corte
de alimentación manual.

### Corta-circuitos

`MAX_CICLOS_ESCALERA = 3`. Después de 3 escaleras completas sin recuperar, deja
de escalar y pasa a modo observación: sigue midiendo, y si la conexión vuelve
sola reactiva todo.

Existe porque el detector depende de algo externo que puede cambiar sin aviso.
Si 3 escaleras con GPIO y reboot no arreglaron nada, la cuarta tampoco: o es una
falla de hardware que necesita una persona, o el detector está midiendo mal.

### Diferencias entre equipos

**El pulso del GPIO 10 no llega al módulo en todos los coches.** En el
sensingBus199 resetea de verdad (`/dev/ttyUSB1` desaparece del bus); en el
sensingBus88, 272 pulsos no produjeron ninguna desaparición. Por eso la acción
verifica si el módulo cae del bus USB y lo deja escrito: es lo único que lo
prueba equipo por equipo.

Queda pendiente confirmar contra el esquemático del HAT a qué pin llega el
GPIO 10 (`PWRKEY`, `RESET_N`, o nada). El ancho del pulso es la constante
`GPIO_PULSO_S`, hoy en 5 s: fuera del spec del EC25 en los dos casos, pero es el
único valor medido resolviendo cortes reales.

---

## Operar la flota

`flota.py` toma la password de la variable `OSMA_PASS` — nunca escribirla en un
archivo. Necesita `paramiko` en la máquina del operador.

```bash
cp hosts.txt.ejemplo hosts.txt      # y poner las IPs reales (está en .gitignore)

OSMA_PASS=... python flota.py hosts.txt estado   # sólo lee
OSMA_PASS=... python flota.py hosts.txt stop     # EMERGENCIA: detiene el watchdog
OSMA_PASS=... python flota.py hosts.txt apply    # despliega y reinicia
OSMA_PASS=... python verificar.py hosts.txt      # verifica después del despliegue
```

Las IPs salen de `osma-scripts/cutcsa/custom_scripts/bus_scripts/bus_config.json`
(campo `coches_map`). Ojo que trae entradas duplicadas y alguna inválida como
`0.0.0.0`.

**Por qué insiste:** si la flota está rebooteando, en cualquier instante hay
equipos con el puerto 22 cerrado. Una sola pasada deja muchos afuera, así que
`flota.py` da vueltas sucesivas sobre los pendientes.

**Candados del modo `apply`:** si el sha256 no coincide o el `import` bajo
python2.7 falla, restaura el backup y **no reinicia el servicio** — el equipo se
queda con la versión vieja andando. El import se corre **con sudo**, igual que
el servicio: como `pi` falla al abrir `check_connectivity.log`, que es de root.

---

## Instalación del servicio

```bash
cd /home/pi
git clone https://github.com/GabRodri/sensing_scripts.git
cd /home/pi/sensing_scripts && chmod +x reset_modem_sensing.sh
sudo nano /etc/systemd/system/sensing_check_conn.service
```

```ini
[Unit]
Description=Sensing internet watchdog
After=network.target

[Service]
Type=simple
ExecStart=/home/pi/sensing_scripts/reset_modem_sensing.sh
Restart=always
RestartSec=3
User=root
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable sensing_check_conn.service
sudo systemctl start sensing_check_conn.service
```

El log queda en `/home/pi/sensing_scripts/check_connectivity.log` (rotación de
10 MB × 3). Corre como **root**, así que el log es de root: ejecutar el script a
mano como `pi` falla con `Permission denied`, y además arrancaría una segunda
instancia peleando con el servicio. Para probar a mano, parar el servicio primero.

---

## `sim-missing` del EC25

Si ModemManager reporta `state: failed / failed reason: sim-missing` **con la SIM
puesta**, casi siempre es la tensión de la interfaz de SIM, no el hardware:

```
AT+CPIN?    -> +CME ERROR: 10      AT+QINISTAT -> 0
AT+QCCID    -> +CME ERROR: 13      AT+QSIMDET? -> 0,0
```

**Probar `AT+QSIMVOL=1` + `AT&W` antes de mandar el equipo a reparación.**
Verificado en `sensingOCPPCome`, que llevaba tiempo caído y no se arreglaba ni
cambiando la SIM: pasó de `failed` a `connected` con 91 % de señal.

`fix_sim_missing.py` lo automatiza y **se niega a tocar un módem sano**: si el
estado no es exactamente `failed/sim-missing`, sale sin hacer nada. Por defecto
sólo diagnostica; hay que pasar `--apply`.

---

## Incidente del 08/09/2026

El detector usaba `ping` contra el broker. En agosto respondía ICMP (~26 ms
verificados); en algún momento ANTEL lo bloqueó en toda la VPN. El watchdog pasó
a reportar falla permanente y **rebooteó la flota entera cada ~29 minutos**, con
los módems sanos y MQTT publicando sin errores.

Al momento de detectarlo: **160 de 163 coches** medidos habían arrancado hacía
menos de una hora, con una mediana de **364 reboots** acumulados por equipo y
57.942 en total.

Lo que dejó:

- El detector pasó de ICMP a **TCP 1883**, que además es la función real del equipo.
- Se agregó el **corta-circuitos**, para que un detector equivocado no pueda
  volver a rebootear la flota indefinidamente.
- `flota.py` con modo `stop`, porque frenar lleva segundos por equipo y
  desplegar lleva minutos: ante un incidente, primero se frena.

La lección de fondo no es que ICMP fuera mala elección: es que **un watchdog
cuyo detector depende de un tercero se convierte en una máquina de rebootear
cuando ese tercero cambia**. Si el broker se cae por mantenimiento, hoy toda la
flota haría 3 escaleras completas antes de rendirse.

---

## Pendientes

- **Esquemático del HAT**, para saber a qué pin llega el GPIO 10 y ajustar
  `GPIO_PULSO_S`.
- **`mmcli --enable` devuelve `QMI InvalidTransition`** después de un `--disable`
  exitoso, dejando el módem apagado. Mitigado con reintentos; falta la causa.
- **Salud de las SD** tras ~360 apagones sucios por coche: buscar corrupción de
  `ext4` antes de que las unidades se caigan solas.
- **El reloj arranca en epoch** en cada boot (sin RTC confiable, `fake-hwclock`
  restaura). Los timestamps retroceden y las duraciones medidas sobre los logs
  son un piso, no una medición. Para contar reinicios usar `last reboot`, que lee
  `wtmp` y sí es persistente; el journal es volátil y se borra en cada arranque.
- **Rotar la password SSH**, que es la misma en los ~195 equipos y está en texto
  plano en `config_sensing_bug.py` (no trackeado, ver `.gitignore`).

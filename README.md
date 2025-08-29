### NOTA: Este repositorio debe clonarse en /home/pi/

# Comandos para configurar servicio de chequeo de conexión en Sensing

cd /home/pi/sensing_scripts

chmod +x reset_modem_sensing.sh

cd /etc/systemd/system

sudo nano sensing_check_conn.service


## Acá tenemos que pegar el siguiente texto

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


## Seguimos con los siguientes comandos


sudo systemctl daemon-reload

sudo systemctl enable sensing_check_conn.service

sudo systemctl start sensing_check_conn.service


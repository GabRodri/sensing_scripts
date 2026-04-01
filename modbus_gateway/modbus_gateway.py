from pymodbus.server.sync import StartTcpServer
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext, ModbusSequentialDataBlock
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.client.sync import ModbusTcpClient
import logging
import time
import threading

# ============================================
# CONFIGURACIÓN
# ============================================

# Multimedidores ABB (Modbus TCP)
DEVICES = {
    1: {"host": "10.10.12.98", "port": 502, "unit_id": 1, "name": "MU1_SE1"},
    2: {"host": "10.10.12.99", "port": 502, "unit_id": 1, "name": "MU2_SE1"},
}

# Registros específicos ABB PowerMeter (address, type)
# Agrupados en bloques consecutivos para minimizar lecturas
REGISTER_BLOCKS = [
    (4105, 6),    # tensionCompuesta12, 13, 23
    (4113, 8),    # corriente1, 2, 3, factorPotencia
    (4143, 2),    # potenciaActivaKW
    (4151, 2),    # potenciaReactivaKVAR
    (4159, 4),    # energiaActivaKWH, energiaReactivaKVARH
    (4227, 12),   # thdTC12, 13, 23, thdCorriente1, 2, 3
]

# Datastore: rango continuo que cubre todos los registros
DS_START = 4105
DS_SIZE = 4239 - 4105  # 134 registros

TCP_HOST = "0.0.0.0"
TCP_PORT = 11234
UPDATE_INTERVAL = 5  # segundos

# ============================================
# LOGGING
# ============================================
logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger()
log.setLevel(logging.INFO)

# ============================================
# CREAR CONTEXTO TCP (UN SLAVE POR DISPOSITIVO)
# ============================================
slave_contexts = {}
for sid in DEVICES:
    slave_contexts[sid] = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(DS_START, [0] * DS_SIZE)
    )

context = ModbusServerContext(slaves=slave_contexts, single=False)

# ============================================
# LOOP DE LECTURA POR DISPOSITIVO
# ============================================
def poll_device(slave_id, cfg):
    client = ModbusTcpClient(cfg["host"], port=cfg["port"])
    while True:
        try:
            if not client.connect():
                log.warning(f"[{cfg['name']}] No se puede conectar a {cfg['host']}:{cfg['port']}")
                time.sleep(UPDATE_INTERVAL)
                continue

            for start, count in REGISTER_BLOCKS:
                result = client.read_holding_registers(address=start, count=count, unit=cfg["unit_id"])
                if not result.isError():
                    context[slave_id].setValues(3, start, result.registers)
                    log.info(f"[{cfg['name']}] Bloque {start}-{start+count-1} OK")
                else:
                    log.warning(f"[{cfg['name']}] Error en bloque {start}: {result}")

        except Exception as e:
            log.error(f"[{cfg['name']}] Error en loop: {e}")

        time.sleep(UPDATE_INTERVAL)

# ============================================
# IDENTIFICACIÓN TCP
# ============================================
identity = ModbusDeviceIdentification()
identity.VendorName = "Sensing Gateway"
identity.ProductName = "TCP-TCP ABB PowerMeter"
identity.MajorMinorRevision = "1.0"

# ============================================
# ARRANQUE DE HILOS DE POLLING
# ============================================
for sid, cfg in DEVICES.items():
    threading.Thread(target=poll_device, args=(sid, cfg), daemon=True).start()

# ============================================
# INICIAR SERVIDOR TCP
# ============================================
log.info("=" * 60)
log.info(" GATEWAY MODBUS TCP->TCP ABB POWER METERS")
for sid, cfg in DEVICES.items():
    log.info(f"  Slave {sid} -> {cfg['name']} ({cfg['host']}:{cfg['port']})")
log.info(f" Servidor: {TCP_HOST}:{TCP_PORT}")
log.info(f" Intervalo: {UPDATE_INTERVAL}s")
log.info(f" Registros: {', '.join(f'{s}-{s+c-1}' for s, c in REGISTER_BLOCKS)}")
log.info("=" * 60)

StartTcpServer(context=context, identity=identity, address=(TCP_HOST, TCP_PORT))

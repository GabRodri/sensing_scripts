import asyncio
import logging
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.server import StartAsyncTcpServer
from pymodbus.datastore import (
    ModbusSlaveContext,
    ModbusServerContext,
    ModbusSequentialDataBlock,
)

# --- Logging ---
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger()
log.setLevel(logging.INFO)

# --- Configuración ---

DEVICES = {
    1: {"host": "10.10.12.98", "port": 502, "unit_id": 1, "name": "MU1_SE1"},
    2: {"host": "10.10.12.99", "port": 502, "unit_id": 1, "name": "MU2_SE1"},
}

# Bloques de registros a leer (start, count)
# Bloque 1: tensiones, corrientes, FP, potencias, energías (4105-4162)
# Bloque 2: THD tensión y corriente (4227-4238)
REGISTER_BLOCKS = [
    (4105, 58),   # 4105..4162
    (4227, 12),   # 4227..4238
]

# Datastore: bloque continuo que cubre todo el rango (4105-4238)
DS_START = 4105
DS_SIZE = 4239 - 4105  # 134 registros

POLL_INTERVAL = 5

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 11234


def create_context():
    """Crea el contexto del servidor con un slave por cada dispositivo."""
    slaves = {}
    for slave_id in DEVICES:
        slaves[slave_id] = ModbusSlaveContext(
            hr=ModbusSequentialDataBlock(DS_START, [0] * DS_SIZE),
            ir=ModbusSequentialDataBlock(0, [0] * 1),
            di=ModbusSequentialDataBlock(0, [0] * 1),
            co=ModbusSequentialDataBlock(0, [0] * 1),
        )
    return ModbusServerContext(slaves=slaves, single=False)


async def poll_device(client, device_cfg, context, slave_id):
    """Lee los bloques de registros y actualiza el datastore."""
    for start, count in REGISTER_BLOCKS:
        try:
            result = await client.read_holding_registers(
                start, count=count, slave=device_cfg["unit_id"]
            )
            if not result.isError():
                context[slave_id].setValues(3, start, result.registers)
            else:
                log.warning("[%s] Error en bloque %d: %s", device_cfg["name"], start, result)
        except Exception as e:
            log.error("[%s] Excepción en bloque %d: %s", device_cfg["name"], start, e)


async def polling_loop(context):
    """Polling periódico a todos los multimedidores."""
    clients = {}
    for slave_id, cfg in DEVICES.items():
        clients[slave_id] = AsyncModbusTcpClient(cfg["host"], port=cfg["port"])

    while True:
        for slave_id, cfg in DEVICES.items():
            client = clients[slave_id]
            if not client.connected:
                try:
                    await client.connect()
                    log.info("[%s] Conectado a %s:%s", cfg["name"], cfg["host"], cfg["port"])
                except Exception as e:
                    log.error("[%s] No se pudo conectar: %s", cfg["name"], e)
                    continue
            await poll_device(client, cfg, context, slave_id)

        await asyncio.sleep(POLL_INTERVAL)


async def main():
    context = create_context()

    asyncio.create_task(polling_loop(context))

    log.info("Gateway Modbus TCP en %s:%s", SERVER_HOST, SERVER_PORT)
    for sid, cfg in DEVICES.items():
        log.info("  Slave %d -> %s (%s:%s)", sid, cfg["name"], cfg["host"], cfg["port"])
    log.info("Registros: %s", ", ".join(f"{s}-{s+c-1}" for s, c in REGISTER_BLOCKS))

    await StartAsyncTcpServer(
        context=context,
        address=(SERVER_HOST, SERVER_PORT),
    )


if __name__ == "__main__":
    asyncio.run(main())

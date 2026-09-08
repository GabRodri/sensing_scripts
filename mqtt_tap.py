#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Decodifica el MQTT que sale de un device, a partir de una captura tcpdump.

Sirve cuando el broker no te deja suscribirte (ACL de solo escritura): el
trafico a 10.220.0.17:1883 va SIN TLS, asi que se ve entero en la red.

Uso en la Raspberry:

    # captura a archivo, 5 minutos
    sudo timeout 300 tcpdump -i any -s 0 -w /tmp/mqtt.pcap 'tcp port 1883'
    python2.7 mqtt_tap.py /tmp/mqtt.pcap

    # o en vivo, decodificando a medida que pasa
    sudo tcpdump -i any -s 0 -U -w - 'tcp port 1883' | python2.7 mqtt_tap.py -

    # solo los realtime
    python2.7 mqtt_tap.py /tmp/mqtt.pcap -t realtime

Sin dependencias: parsea el pcap, IPv4/TCP y MQTT a mano. py2.7 y py3.
"""

import argparse
import json
import struct
import sys
from collections import OrderedDict
from datetime import datetime

# --- tipos de paquete MQTT ---
TIPOS = {
    1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK", 5: "PUBREC",
    6: "PUBREL", 7: "PUBCOMP", 8: "SUBSCRIBE", 9: "SUBACK",
    10: "UNSUBSCRIBE", 11: "UNSUBACK", 12: "PINGREQ", 13: "PINGRESP",
    14: "DISCONNECT",
}

# largo del header de enlace segun linktype del pcap
LINK_HDR = {
    0: 4,     # NULL
    1: 14,    # ETHERNET
    101: 0,   # RAW IP
    113: 16,  # LINUX_SLL  (tcpdump -i any)
    276: 20,  # LINUX_SLL2 (libpcap >= 1.10, tcpdump -i any)
    228: 0,   # IPV4
}


def leer_pcap(f):
    """Generador de (timestamp, bytes_del_frame) desde un pcap clasico."""
    cabecera = f.read(24)
    if len(cabecera) < 24:
        raise ValueError("archivo demasiado corto para ser un pcap")

    magic = cabecera[:4]
    if magic == b"\xa1\xb2\xc3\xd4":
        endian, divisor = ">", 1000000.0
    elif magic == b"\xd4\xc3\xb2\xa1":
        endian, divisor = "<", 1000000.0
    elif magic == b"\xa1\xb2\x3c\x4d":
        endian, divisor = ">", 1000000000.0
    elif magic == b"\x4d\x3c\xb2\xa1":
        endian, divisor = "<", 1000000000.0
    elif magic == b"\x0a\x0d\x0d\x0a":
        raise ValueError("es un pcapng, no un pcap clasico. Capturá con "
                         "'tcpdump -w' (que escribe pcap) o convertí con "
                         "'editcap -F pcap entrada.pcapng salida.pcap'")
    else:
        raise ValueError("magic desconocido: %r" % magic)

    linktype = struct.unpack(endian + "I", cabecera[20:24])[0]
    if linktype not in LINK_HDR:
        raise ValueError("linktype %d no soportado" % linktype)
    salto = LINK_HDR[linktype]

    while True:
        ph = f.read(16)
        if len(ph) < 16:
            return
        ts_sec, ts_frac, incl_len, _ = struct.unpack(endian + "IIII", ph)
        datos = f.read(incl_len)
        if len(datos) < incl_len:
            return
        ts = ts_sec + ts_frac / divisor

        if linktype == 113:      # SLL: el protocolo esta en los ultimos 2 bytes
            if len(datos) < 16 or struct.unpack(">H", datos[14:16])[0] != 0x0800:
                continue
        elif linktype == 276:    # SLL2: protocolo al principio
            if len(datos) < 20 or struct.unpack(">H", datos[0:2])[0] != 0x0800:
                continue
        elif linktype == 1:      # Ethernet: saltear VLAN si la hay
            if len(datos) < 14:
                continue
            tipo = struct.unpack(">H", datos[12:14])[0]
            if tipo == 0x8100:
                if len(datos) < 18 or struct.unpack(">H", datos[16:18])[0] != 0x0800:
                    continue
                yield ts, datos[18:]
                continue
            if tipo != 0x0800:
                continue
        yield ts, datos[salto:]


def parsear_tcp(ip_bytes):
    """Devuelve (flujo, seq, payload) de un IPv4/TCP, o None."""
    if len(ip_bytes) < 20:
        return None
    vihl = ord(ip_bytes[0:1])
    if (vihl >> 4) != 4:
        return None
    ihl = (vihl & 0x0F) * 4
    if ord(ip_bytes[9:10]) != 6:     # no es TCP
        return None
    total = struct.unpack(">H", ip_bytes[2:4])[0]
    ip_bytes = ip_bytes[:total] if 0 < total <= len(ip_bytes) else ip_bytes
    src = ".".join(str(ord(ip_bytes[12 + i:13 + i])) for i in range(4))
    dst = ".".join(str(ord(ip_bytes[16 + i:17 + i])) for i in range(4))

    tcp = ip_bytes[ihl:]
    if len(tcp) < 20:
        return None
    sport, dport = struct.unpack(">HH", tcp[0:4])
    seq = struct.unpack(">I", tcp[4:8])[0]
    off = (ord(tcp[12:13]) >> 4) * 4
    payload = tcp[off:]
    if not payload:
        return None
    return ("%s:%d>%s:%d" % (src, sport, dst, dport), seq, payload)


def leer_varint(datos, i):
    """Remaining Length de MQTT. Devuelve (valor, indice_siguiente) o None."""
    mult, valor = 1, 0
    for _ in range(4):
        if i >= len(datos):
            return None
        b = ord(datos[i:i + 1])
        i += 1
        valor += (b & 0x7F) * mult
        if not (b & 0x80):
            return valor, i
        mult *= 128
    return None


def paquetes_mqtt(buf):
    """Corta el buffer en paquetes MQTT completos. Devuelve (lista, resto)."""
    salida, i = [], 0
    while i < len(buf):
        primero = ord(buf[i:i + 1])
        r = leer_varint(buf, i + 1)
        if r is None:
            break                      # header incompleto, esperar mas bytes
        largo, inicio = r
        if inicio + largo > len(buf):
            break                      # cuerpo incompleto
        salida.append((primero, buf[inicio:inicio + largo]))
        i = inicio + largo
    return salida, buf[i:]


def decodificar_publish(primero, cuerpo):
    """(topico, qos, retain, dup, payload) de un PUBLISH."""
    qos = (primero >> 1) & 0x03
    retain = bool(primero & 0x01)
    dup = bool(primero & 0x08)
    if len(cuerpo) < 2:
        return None
    tlen = struct.unpack(">H", cuerpo[0:2])[0]
    if len(cuerpo) < 2 + tlen:
        return None
    topico = cuerpo[2:2 + tlen].decode("utf-8", "replace")
    i = 2 + tlen
    if qos > 0:
        if len(cuerpo) < i + 2:
            return None
        i += 2                          # packet id
    return topico, qos, retain, dup, cuerpo[i:]


class Tap(object):
    def __init__(self, args):
        self.args = args
        self.flujos = {}      # flujo -> {"buf": bytes, "seq": next_seq}
        self.stats = OrderedDict()
        self.total = 0
        self.otros = OrderedDict()

    def procesar(self, ts, flujo, seq, payload):
        st = self.flujos.get(flujo)
        if st is None:
            st = {"buf": b"", "seq": None}
            self.flujos[flujo] = st

        # Reensamblado simple: alcanza porque los paquetes son chicos y van
        # en orden. Solo hay que descartar retransmisiones y avisar los huecos.
        if st["seq"] is not None:
            if seq < st["seq"]:
                salto = st["seq"] - seq
                if salto >= len(payload):
                    return                       # retransmision entera
                payload = payload[salto:]
                seq = st["seq"]
            elif seq > st["seq"]:
                if st["buf"]:
                    sys.stderr.write(
                        "[aviso] hueco en %s (faltan %d bytes), se descarta "
                        "el buffer parcial\n" % (flujo, seq - st["seq"]))
                st["buf"] = b""
        st["seq"] = seq + len(payload)
        st["buf"] += payload

        paquetes, st["buf"] = paquetes_mqtt(st["buf"])
        for primero, cuerpo in paquetes:
            self.despachar(ts, flujo, primero, cuerpo)

    def despachar(self, ts, flujo, primero, cuerpo):
        tipo = primero >> 4
        nombre = TIPOS.get(tipo, "TIPO_%d" % tipo)
        if tipo != 3:
            self.otros[nombre] = self.otros.get(nombre, 0) + 1
            if self.args.control:
                print("[%s] %s (%s)" % (self.hora(ts), nombre, flujo))
            return

        r = decodificar_publish(primero, cuerpo)
        if r is None:
            return
        topico, qos, retain, dup, payload = r
        if self.args.topic and self.args.topic not in topico:
            return

        self.total += 1
        st = self.stats.get(topico)
        if st is None:
            st = {"n": 0, "bytes": 0, "dup": 0, "ultimo": None}
            self.stats[topico] = st
        st["n"] += 1
        st["bytes"] += len(payload)
        st["ultimo"] = ts
        if dup:
            st["dup"] += 1

        if self.args.quiet:
            return

        texto = payload.decode("utf-8", "replace")
        if self.args.pretty:
            try:
                texto = json.dumps(json.loads(texto), indent=2, ensure_ascii=False)
            except Exception:
                pass
        elif len(texto) > self.args.maxlen:
            texto = texto[:self.args.maxlen] + "...(%d bytes)" % len(payload)
        marcas = "qos=%d%s%s" % (qos, " RETAIN" if retain else "",
                                 " DUP" if dup else "")
        print("[%s] %s (%s)\n    %s" % (self.hora(ts), topico, marcas, texto))

    @staticmethod
    def hora(ts):
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]

    def resumen(self):
        print("\n" + "=" * 78)
        print("RESUMEN  %d PUBLISH  |  %d topicos" % (self.total, len(self.stats)))
        if self.otros:
            print("otros paquetes: %s"
                  % ", ".join("%s=%d" % kv for kv in self.otros.items()))
        print("=" * 78)
        if not self.stats:
            print("Ningun PUBLISH capturado.")
            if not self.otros:
                print("Tampoco hubo trafico MQTT: revisá el filtro de tcpdump "
                      "y que hayas capturado con '-i any'.")
            else:
                print("Hubo trafico MQTT pero ningun PUBLISH: el device esta "
                      "conectado y no publica nada.")
            return
        filas = sorted(self.stats.items(), key=lambda kv: kv[1]["n"], reverse=True)
        print("%-48s %6s %9s %6s %10s" % ("TOPICO", "MSGS", "BYTES", "DUP", "ULTIMO"))
        for topico, st in filas:
            print("%-48s %6d %9d %6d %10s"
                  % (topico[:48], st["n"], st["bytes"], st["dup"],
                     self.hora(st["ultimo"])))

    def run(self):
        if self.args.pcap == "-":
            f = getattr(sys.stdin, "buffer", sys.stdin)
        else:
            f = open(self.args.pcap, "rb")
        try:
            for ts, frame in leer_pcap(f):
                r = parsear_tcp(frame)
                if r is None:
                    continue
                flujo, seq, payload = r
                self.procesar(ts, flujo, seq, payload)
        except KeyboardInterrupt:
            print("\n[Ctrl-C]")
        except ValueError as e:
            sys.stderr.write("ERROR: %s\n" % e)
            return 2
        finally:
            if self.args.pcap != "-":
                f.close()
        self.resumen()
        return 0


def main():
    p = argparse.ArgumentParser(
        description="Decodifica MQTT desde una captura tcpdump (sin TLS)")
    p.add_argument("pcap", help="archivo .pcap, o '-' para leer de stdin")
    p.add_argument("-t", "--topic", default=None,
                   help="mostrar solo topicos que contengan este texto")
    p.add_argument("--control", action="store_true",
                   help="mostrar tambien CONNECT/PUBACK/PINGREQ/etc")
    p.add_argument("--pretty", action="store_true", help="JSON indentado")
    p.add_argument("--quiet", action="store_true", help="solo el resumen")
    p.add_argument("--maxlen", type=int, default=400)
    sys.exit(Tap(p.parse_args()).run())


if __name__ == "__main__":
    main()

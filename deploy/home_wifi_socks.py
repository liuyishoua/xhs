"""Loopback-only SOCKS CONNECT relay with DNS and TCP bound to home Wi-Fi.

Run on the Mac home endpoint. It never reads proxy settings or credentials.
An SSH local forward carries the server's existing SOCKS clients to this relay.
"""
import argparse
import ipaddress
import os
import re
import select
import socket
import socketserver
import struct
import subprocess
import sys


def wifi_settings(interface):
    address = subprocess.check_output(['/usr/sbin/ipconfig', 'getifaddr', interface], text=True).strip()
    ipaddress.IPv4Address(address)
    packet = subprocess.check_output(['/usr/sbin/ipconfig', 'getpacket', interface], text=True)
    match = re.search(r'domain_name_server[^\n]*\{([^}]+)\}', packet)
    if not match:
        raise OSError('Wi-Fi DHCP DNS is unavailable')
    nameservers = re.findall(r'\d+\.\d+\.\d+\.\d+', match.group(1))
    if not nameservers:
        raise OSError('Wi-Fi DHCP DNS is unavailable')
    return address, nameservers


def bind_wifi(sock, interface, address):
    if sys.platform == 'darwin':
        # IP_BOUND_IF = 25 in the macOS SDK netinet/in.h.
        sock.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(interface))
    sock.bind((address, 0))
    sock.settimeout(10)


def skip_dns_name(data, offset):
    while True:
        length = data[offset]
        if length & 0xc0 == 0xc0:
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        if length > 63:
            raise ValueError('Invalid DNS name')
        offset += length


def dns_ipv4(host, interface, address, nameservers):
    labels = host.encode('idna').split(b'.')
    if any(not label or len(label) > 63 for label in labels):
        raise ValueError('Invalid DNS hostname')
    qname = b''.join(bytes([len(label)]) + label for label in labels) + b'\0'
    ident = os.urandom(2)
    query = ident + struct.pack('!5H', 0x0100, 1, 0, 0, 0) + qname + struct.pack('!HH', 1, 1)
    for ns in nameservers:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                bind_wifi(udp, interface, address)
                udp.settimeout(4)
                udp.connect((ns, 53))
                udp.send(query)
                data = udp.recv(4096)
            if len(data) < 12 or data[:2] != ident:
                continue
            flags, questions, answers, _, _ = struct.unpack('!5H', data[2:12])
            if not flags & 0x8000 or flags & 0x020f:
                continue
            offset = 12
            for _ in range(questions):
                offset = skip_dns_name(data, offset) + 4
            result = []
            for _ in range(answers):
                offset = skip_dns_name(data, offset)
                kind, cls, _, size = struct.unpack('!HHIH', data[offset:offset + 10])
                offset += 10
                if kind == 1 and cls == 1 and size == 4:
                    result.append(socket.inet_ntoa(data[offset:offset + size]))
                offset += size
            if result:
                return result
        except (OSError, ValueError, IndexError, struct.error):
            continue
    raise OSError('Home Wi-Fi DNS lookup failed')


def recv_exact(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError('Disconnected SOCKS client')
        data += chunk
    return data


def connect_home(host, port, interface):
    address, nameservers = wifi_settings(interface)
    try:
        ip = ipaddress.IPv4Address(host)
        targets = [str(ip)]
    except ipaddress.AddressValueError:
        targets = dns_ipv4(host, interface, address, nameservers)
    for target in targets:
        # This relay is an Internet egress, not an entry to home LAN services.
        if not ipaddress.IPv4Address(target).is_global:
            continue
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            bind_wifi(upstream, interface, address)
            upstream.connect((target, port))
            return upstream
        except OSError:
            upstream.close()
    raise OSError('Home Wi-Fi connection failed')


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(15)
        upstream = None
        accepted = False
        try:
            version, count = recv_exact(client, 2)
            methods = recv_exact(client, count)
            if version != 5 or 0 not in methods:
                client.sendall(b'\x05\xff')
                return
            client.sendall(b'\x05\x00')
            version, command, reserved, kind = recv_exact(client, 4)
            if version != 5 or command != 1 or reserved != 0:
                raise ValueError('Only SOCKS CONNECT is supported')
            if kind == 1:
                host = socket.inet_ntoa(recv_exact(client, 4))
            elif kind == 3:
                host = recv_exact(client, recv_exact(client, 1)[0]).decode('ascii')
            else:
                raise ValueError('Unsupported address family')
            port = struct.unpack('!H', recv_exact(client, 2))[0]
            upstream = connect_home(host, port, self.server.interface)
            bound_ip, bound_port = upstream.getsockname()
            client.sendall(b'\x05\x00\x00\x01' + socket.inet_aton(bound_ip) + struct.pack('!H', bound_port))
            accepted = True
            client.settimeout(30)
            upstream.settimeout(30)
            peers = [client, upstream]
            while peers:
                readable, _, _ = select.select(peers, [], [], 300)
                if not readable:
                    break
                for source in readable:
                    destination = upstream if source is client else client
                    chunk = source.recv(65536)
                    if chunk:
                        destination.sendall(chunk)
                    else:
                        peers.remove(source)
                        destination.shutdown(socket.SHUT_WR)
        except (OSError, ValueError, IndexError):
            if not accepted:
                try:
                    client.sendall(b'\x05\x01\x00\x01' + b'\0' * 6)
                except OSError:
                    pass
        finally:
            if upstream:
                upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


if __name__ == '__main__':
    import threading
    parser = argparse.ArgumentParser()
    parser.add_argument('--interface', required=True)
    parser.add_argument('--port', type=int, default=18891)
    args = parser.parse_args()
    with Server(('127.0.0.1', args.port), Handler) as server:
        server.interface = args.interface
        server.slots = threading.BoundedSemaphore(32)
        server.serve_forever()

import socket
import struct
import threading
import unittest
from unittest.mock import patch, MagicMock
from deploy import home_wifi_socks as relay


class HomeWifiRelayTests(unittest.TestCase):
    def test_wifi_socket_is_bound_to_interface_and_source(self):
        sock = MagicMock()
        with patch.object(relay.sys, 'platform', 'darwin'), patch.object(relay.socket, 'if_nametoindex', return_value=7):
            relay.bind_wifi(sock, 'en1', '192.168.2.93')
        sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP, 25, 7)
        sock.bind.assert_called_once_with(('192.168.2.93', 0))

    def test_dns_uses_home_dns_and_parses_compressed_answer(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as dns:
            dns.bind(('127.0.0.1', 0))
            port = dns.getsockname()[1]
            def answer():
                data, peer = dns.recvfrom(4096)
                response = data[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 0) + data[12:]
                response += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + socket.inet_aton('93.184.216.34')
                dns.sendto(response, peer)
            thread = threading.Thread(target=answer)
            thread.start()
            original_connect = socket.socket.connect
            def connect(sock, address):
                self.assertEqual(address, ('127.0.0.1', 53))
                return original_connect(sock, ('127.0.0.1', port))
            with patch.object(socket.socket, 'connect', connect):
                result = relay.dns_ipv4('example.com', 'unused', '127.0.0.1', ['127.0.0.1'])
            thread.join(2)
            self.assertEqual(result, ['93.184.216.34'])

    def test_does_not_connect_to_home_lan_or_fallback(self):
        with patch.object(relay, 'wifi_settings', return_value=('192.168.2.93', ['192.168.2.1'])), patch.object(relay.socket, 'socket') as socket_factory:
            with self.assertRaises(OSError):
                relay.connect_home('192.168.2.1', 80, 'en1')
            socket_factory.assert_not_called()

    def test_wifi_failure_does_not_use_system_proxy(self):
        with patch.object(relay, 'wifi_settings', side_effect=OSError('Wi-Fi disconnected')), patch.object(relay.socket, 'socket') as socket_factory:
            with self.assertRaises(OSError):
                relay.connect_home('example.com', 443, 'en1')
            socket_factory.assert_not_called()

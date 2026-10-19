
import socket
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.infrastructure.network import public_url


class PublicVideoUrlTests(unittest.TestCase):
    def resolve(self, *addresses):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]

    def test_bilibili_episode_url_accepts_proxy_fake_ip(self):
        url = "https://www.bilibili.com/video/BV1qW4y1a7fU/?spm_id_from=333.788.videopod.episodes&p=11"
        with patch("backend.infrastructure.network.socket.getaddrinfo", return_value=self.resolve("198.18.3.47")):
            self.assertEqual(public_url(url), url)

    def test_fake_ip_exception_is_limited_to_trusted_platform_domains(self):
        for host in ("198.18.3.47", "unknown.example", "bilibili.com.evil.example", "evilbilibili.com"):
            with self.subTest(host=host), patch("backend.infrastructure.network.socket.getaddrinfo", return_value=self.resolve("198.18.3.47")):
                with self.assertRaises(HTTPException):
                    public_url("https://" + host)

    def test_platform_domains_cannot_resolve_to_private_or_mixed_addresses(self):
        for addresses in (("127.0.0.1",), ("10.0.0.2",), ("169.254.169.254",), ("198.18.3.47", "192.168.1.1")):
            with self.subTest(addresses=addresses), patch("backend.infrastructure.network.socket.getaddrinfo", return_value=self.resolve(*addresses)):
                with self.assertRaises(HTTPException):
                    public_url("https://www.bilibili.com/video/test")

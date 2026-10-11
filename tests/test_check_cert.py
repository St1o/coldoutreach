import smtplib
import socket
import ssl
import unittest
from unittest import mock

import check_cert
from check_cert import DROPPED, NO_CONNECTION, OK, UNTRUSTED


class ClassifyTest(unittest.TestCase):
    def test_a_dropped_handshake_is_not_a_certificate_problem(self):
        eof = ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:997)")
        self.assertEqual(check_cert.classify(eof), DROPPED)
        self.assertEqual(check_cert.classify(ConnectionResetError("reset")), DROPPED)
        self.assertEqual(check_cert.classify(smtplib.SMTPServerDisconnected("closed")), DROPPED)
        self.assertEqual(check_cert.classify(OSError("EOF occurred in violation of protocol")), DROPPED)

    def test_an_untrusted_certificate_is_told_apart(self):
        error = ssl.SSLCertVerificationError(1, "certificate verify failed")
        self.assertEqual(check_cert.classify(error), UNTRUSTED)

    def test_everything_else_is_no_connection(self):
        self.assertEqual(check_cert.classify(socket.timeout("timed out")), NO_CONNECTION)
        self.assertEqual(check_cert.classify(ConnectionRefusedError("refused")), NO_CONNECTION)


class VerdictTest(unittest.TestCase):
    def test_port_465_working_means_all_clear(self):
        code, text = check_cert.verdict({465: [OK, OK], 587: [DROPPED, DROPPED]})
        self.assertEqual(code, 0)
        self.assertIn("порт 465 работает", text)

    def test_only_587_working_tells_to_set_the_port_in_local_env(self):
        code, text = check_cert.verdict({465: [DROPPED, DROPPED], 587: [OK, OK]})
        self.assertEqual(code, 0)
        self.assertIn("MAILRU_PORT=587", text)

    def test_a_drop_on_every_port_points_to_vpn_proxy_or_antivirus_not_to_the_password(self):
        code, text = check_cert.verdict({465: [DROPPED, DROPPED], 587: [DROPPED, DROPPED]})
        self.assertEqual(code, 1)
        for word in ("VPN", "антивирус", "python.exe"):
            self.assertIn(word, text)
        self.assertIn("не пароль", text)

    def test_untrusted_certificate_and_no_connection_have_their_own_advice(self):
        self.assertIn("не доверяет", check_cert.verdict({465: [UNTRUSTED, UNTRUSTED], 587: [NO_CONNECTION]})[1])
        code, text = check_cert.verdict({465: [NO_CONNECTION], 587: [NO_CONNECTION]})
        self.assertEqual(code, 1)
        self.assertIn("не устанавливается вообще", text)


class TlsCheckTest(unittest.TestCase):
    def test_a_dropped_handshake_is_reported_as_dropped_with_the_reason(self):
        with mock.patch.object(check_cert.socket, "create_connection", side_effect=ssl.SSLEOFError(8, "EOF occurred in violation of protocol")):
            self.assertEqual(check_cert.tls_check(465, ssl.create_default_context(), "тест"), DROPPED)

    def test_starttls_port_is_checked_through_smtplib(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.sock.version.return_value = "TLSv1.3"
        with mock.patch.object(check_cert.smtplib, "SMTP", return_value=fake) as smtp_class:
            self.assertEqual(check_cert.tls_check(587, ssl.create_default_context(), "тест"), OK)
        smtp_class.assert_called_once()
        fake.starttls.assert_called_once()


class MainTest(unittest.TestCase):
    def test_no_dns_stops_with_a_plain_message(self):
        with mock.patch.object(check_cert.socket, "getaddrinfo", side_effect=socket.gaierror("no dns")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(check_cert.main(), 1)
        self.assertIn("не найден", " ".join(str(call.args[0]) for call in printed.call_args_list if call.args))


if __name__ == "__main__":
    unittest.main()

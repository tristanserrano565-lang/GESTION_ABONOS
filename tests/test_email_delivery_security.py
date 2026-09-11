"""Contrato HTTP de correo: no accede a n8n, SMTP ni la BD configurada."""
import hashlib
import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ['DATABASE_URL'] = os.environ.get('TEST_DATABASE_URL') or 'postgresql+psycopg2://unused@127.0.0.1:1/unused'
os.environ.update(SECRET_KEY='only-test-key', DEFAULT_ADMIN_USERNAME='', API_FOOTBALL_KEY='')
from gestion_abonos_app import config
from gestion_abonos_app.services import email_delivery as delivery
import requests


class SecureDeliveryTest(unittest.TestCase):
    def setUp(self):
        for key,value in {'N8N_WEBHOOK_URL':'https://n8n.example.test/webhook/secure',
                          'N8N_WEBHOOK_SECRET':'a'*48, 'N8N_WEBHOOK_SECRET_HEADER':'X-Workflow-Token',
                          'N8N_WEBHOOK_BEARER_TOKEN':'', 'N8N_CA_CERT_FILE':'',
                          'N8N_WEBHOOK_RETRY_DELAY_SECONDS':0}.items():
            self.enterContext(patch.object(config,key,value))
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.enterContext(patch.object(delivery.requests,'Session',return_value=self.session))
        binary = b'%PDF-1.4\n%%EOF\n'
        self.payload = dict(pdf_data=binary,documento_sha256=hashlib.sha256(binary).hexdigest(),
            destino_email='test@example.com',tipo_recurso='abono',recurso_id=1,partido_id=1,
            idempotency_key='b'*64,filename='entrada.pdf',rival='Rival',localia=1,
            equipo_local='Atleti',equipo_visitante='Rival')
        self.ack = dict(ok=True,protocol_version='1',status='sent',idempotency_key='b'*64,
                        pdf_sha256=self.payload['documento_sha256'])

    def respond(self, body=None, status=200, mime='application/json'):
        response = MagicMock(status_code=status,headers={'Content-Type':mime})
        response.__enter__.return_value=response
        response.iter_content.return_value = [json.dumps(self.ack if body is None else body).encode()]
        self.session.post.return_value = response
        return response

    def test_tls_explicit_auth_no_redirects_and_exact_binary(self):
        self.respond()
        delivery._send_to_n8n(self.payload)
        kwargs=self.session.post.call_args.kwargs
        self.assertFalse(self.session.trust_env)
        self.assertTrue(kwargs['verify'])
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['headers']['X-Workflow-Token'],'a'*48)
        self.assertEqual(kwargs['files']['pdf'][1],self.payload['pdf_data'])
        self.assertEqual(kwargs['data']['pdf_sha256'],self.payload['documento_sha256'])

    def test_uses_explicit_internal_ca_file(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        self.respond()
        with TemporaryDirectory() as directory:
            ca_file = Path(directory) / 'root.crt'
            ca_file.write_text('test certificate', encoding='ascii')
            with patch.object(config, 'N8N_CA_CERT_FILE', str(ca_file)):
                delivery._send_to_n8n(self.payload)
            self.assertEqual(self.session.post.call_args.kwargs['verify'], str(ca_file))

        with patch.object(config, 'N8N_CA_CERT_FILE', 'relative/root.crt'):
            with self.assertRaises(delivery.DeliverySendError):
                delivery._send_to_n8n(self.payload)

    def test_rejects_unsafe_configuration_before_network(self):
        for url in ['http://n8n.test/webhook/a','https://n8n.test/webhook-test/a',
                    'https://secret@n8n.test/webhook/a','https://n8n.test/webhook/a?token=x']:
            with self.subTest(url=url),patch.object(config,'N8N_WEBHOOK_URL',url):
                with self.assertRaises(delivery.DeliverySendError):delivery._send_to_n8n(self.payload)
        for secret in ['', 'short', 'a'*32+'\n']:
            with patch.object(config,'N8N_WEBHOOK_SECRET',secret):
                with self.assertRaises(delivery.DeliverySendError):delivery._send_to_n8n(self.payload)
        self.session.post.assert_not_called()

    def test_rejects_wrong_hash_before_network(self):
        self.payload['pdf_data']=b'changed'
        with self.assertRaises(delivery.DeliverySendError):delivery._send_to_n8n(self.payload)
        self.session.post.assert_not_called()

    def test_requires_strict_correlated_acknowledgement(self):
        for body in [{}, [], {'ok':True}, self.ack|{'ok':1}, self.ack|{'status':'accepted'},
                     self.ack|{'pdf_sha256':'c'*64}, self.ack|{'idempotency_key':'d'*64}]:
            with self.subTest(body=body):
                self.respond(body)
                with self.assertRaises(delivery.DeliverySendError):delivery._send_to_n8n(self.payload)
        response=self.respond()
        response.iter_content.return_value=[b'<html>ok</html>']
        with self.assertRaises(delivery.DeliverySendError):delivery._send_to_n8n(self.payload)
        response=self.respond()
        response.iter_content.return_value=[b'x'*4097]
        with self.assertRaises(delivery.DeliverySendError):delivery._send_to_n8n(self.payload)

    def test_status_errors_never_follow_redirect_or_disclose_body(self):
        for code in [302,401,403,409,429,500,503]:
            self.session.post.reset_mock()
            self.respond({'secret':'must not appear'},status=code)
            with self.assertRaises(delivery.DeliverySendError) as error:delivery._send_to_n8n(self.payload)
            self.assertNotIn('must not appear',str(error.exception))
            self.session.post.assert_called_once()

    def test_read_timeout_does_not_retry_and_does_not_expose_exception(self):
        self.session.post.side_effect=requests.ReadTimeout('secret URL and token')
        with self.assertRaises(delivery.DeliverySendError) as error:delivery._send_to_n8n(self.payload)
        self.session.post.assert_called_once()
        self.assertNotIn('token',str(error.exception))

    def test_only_connection_timeout_is_retried(self):
        response=self.respond()
        self.session.post.side_effect=[requests.ConnectTimeout(),response]
        delivery._send_to_n8n(self.payload)
        self.assertEqual(self.session.post.call_count,2)

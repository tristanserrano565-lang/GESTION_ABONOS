"""Pruebas sobre una BD PostgreSQL desechable; nunca usar la BD de la aplicación.

Ejecutar: TEST_DATABASE_URL=postgresql+psycopg2://... .venv/bin/python -m unittest discover -s tests -v
"""
import io
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

url = os.environ.get('TEST_DATABASE_URL', '')
if not url.startswith('postgresql'):
    raise unittest.SkipTest('Se requiere TEST_DATABASE_URL de PostgreSQL desechable.')
os.environ.update(DATABASE_URL=url, SECRET_KEY='test-key-only', DEFAULT_ADMIN_USERNAME='', API_FOOTBALL_KEY='')

from gestion_abonos_app import create_app, db, config, cache
from gestion_abonos_app.auth import rate_limit
from gestion_abonos_app.auth import routes as auth_routes
from gestion_abonos_app.auth.security import hash_password, password_policy_error
from gestion_abonos_app.blueprints import home
from gestion_abonos_app.services import email_delivery
from werkzeug.datastructures import MultiDict


def pdf(label):
    """Genera un PDF pequeño con estructura válida para las pruebas."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>']
    content = b'%PDF-1.4\n%' + label.encode() + b'\n'
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content += f'{n} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(content)
    content += b'xref\n0 4\n0000000000 65535 f \n'
    content += b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
    return content + f'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()


class MatchDocumentsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Aísla las tablas en un esquema propio, sin borrar tablas ajenas."""
        cls.schema = f'test_match_documents_{os.getpid()}'
        with db.engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA {cls.schema}')
        db.engine.dispose()
        from sqlalchemy import event
        @event.listens_for(db.engine, 'connect')
        def set_schema(connection, _record):
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO {cls.schema}')
            connection.commit()
            cursor.close()
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls):
        """Elimina únicamente el esquema creado por esta ejecución."""
        with db.engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA {cls.schema} CASCADE')
        db.engine.dispose()

    def setUp(self):
        now_ts = int(time.time())
        self.session_token = 'test-session-token-for-gestor'
        with db.engine.begin() as conn:
            for table in reversed(db.metadata.sorted_tables):
                conn.execute(table.delete())
            conn.execute(db.usuarios.insert().values(username='gestor', password_hash='unused', salt='unused', role='operador'))
            conn.execute(db.user_sessions.insert().values(
                session_hash=auth_routes._session_token_hash(self.session_token),
                username='gestor', created_at=now_ts, last_seen_at=now_ts,
                expires_at=now_ts + config.SESSION_MAX_AGE_SECONDS,
            ))
            conn.execute(db.clientes.insert(), [{'id':1,'nombre':'Propietario','email':'cliente@example.com'}])
            conn.exec_driver_sql("SELECT setval(pg_get_serial_sequence('clientes', 'id'), 1)")
            conn.execute(db.partidos.insert(), [{'id':1,'localia':1,'rival':'A'}, {'id':2,'localia':1,'rival':'B'}, {'id':3,'localia':0,'rival':'C'}])
            conn.execute(db.abonos.insert(), [{'id':1,'sector':1,'puerta':2,'fila':3,'asiento':4,'id_propietario':1}, {'id':2,'sector':1,'puerta':2,'fila':3,'asiento':5,'id_propietario':1}])
            conn.exec_driver_sql("SELECT setval(pg_get_serial_sequence('abonos', 'id'), 2)")
        cache.bump_cache_version('partidos','abonos','documentos_pdf','envios_email','clientes','asignaciones_abonos','parkings','asignaciones_parkings')
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(
                username='gestor', login_ts=now_ts, csrf_token='csrf',
                session_token=self.session_token,
            )

    def upload(self, partido=1, ids=('1',), docs=None, csrf='csrf'):
        if docs is None:
            docs = [pdf('A')]
        data = MultiDict([('_csrf_token',csrf)])
        for abono_id in ids:
            data.add('recurso_ids',abono_id)
        for index, document in enumerate(docs):
            data.add('pdf_files',(io.BytesIO(document),f'entrada{index}.pdf'))
        return self.client.post(f'/partidos/{partido}/entradas',data=data)

    def documents(self):
        with db.engine.connect() as conn:
            return conn.execute(db.documentos_pdf.select()).mappings().all()

    def test_robots_discourages_indexing_without_creating_a_session(self):
        anonymous = self.app.test_client()
        response = anonymous.get('/robots.txt')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/plain')
        self.assertEqual(response.get_data(as_text=True), 'User-agent: *\nDisallow: /\n')
        self.assertEqual(
            response.headers['X-Robots-Tag'],
            'noindex, nofollow, noarchive',
        )
        self.assertNotIn('Set-Cookie', response.headers)

    def test_batch_and_match_isolation(self):
        self.assertEqual(self.upload(ids=('1','2'),docs=[pdf('A'),pdf('B')]).status_code,302)
        self.assertEqual(self.upload(partido=2,docs=[pdf('C')]).status_code,302)
        self.assertEqual(len(self.documents()),3)
        conn=db.get_connection()
        try:
            self.assertEqual(home._assignable_resource_ids(conn,'abono',[1,2],1),{1,2})
            self.assertEqual(home._assignable_resource_ids(conn,'abono',[1,2],2),{1})
            self.assertEqual(home._assignable_resource_ids(conn,'abono',[1,2],3),set())
        finally:
            conn.close()
        response=self.client.get('/partidos/1')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'data-match-pdf-form',response.data)
        self.assertIn(b'data-pdf-dropzone',response.data)
        self.assertIn(b'data-match-pdf-rows',response.data)
        self.assertIn(b'data-upload-status',response.data)
        self.assertEqual(self.client.get('/abonos').status_code,200)
        self.assertEqual(self.client.get('/partidos/2/abonos/1/asignar').status_code,200)

    def test_atomic_rejection_and_unknown_abono(self):
        self.upload(ids=('1','2'),docs=[pdf('A'),b'not a pdf'])
        self.assertEqual(self.documents(),[])
        self.upload(ids=('999',))
        self.assertEqual(self.documents(),[])
        self.upload(ids=('1','1'),docs=[pdf('A'),pdf('B')])
        self.assertEqual(self.documents(),[])
        self.upload(ids=('1','2'),docs=[pdf('A'),pdf('A')])
        self.assertEqual(self.documents(),[])

    def test_existing_pdf_is_immutable_and_repeat_is_noop(self):
        self.upload()
        original=self.documents()[0]
        self.upload()
        self.upload(docs=[pdf('different')])
        self.assertEqual(len(self.documents()),1)
        self.assertEqual(self.documents()[0]['pdf_data'],original['pdf_data'])
        self.upload(ids=('2',),docs=[pdf('A')])
        self.assertEqual(len(self.documents()),1)

    def test_permissions_csrf_localia_and_limits(self):
        self.assertEqual(self.upload(csrf='bad').status_code,400)
        self.upload(partido=3)
        self.assertEqual(self.documents(),[])
        with patch.object(config,'MAX_PDF_BATCH_FILES',1):
            self.upload(ids=('1','2'),docs=[pdf('A'),pdf('B')])
        with patch.object(config,'MAX_PDF_BATCH_BYTES',10):
            self.upload()
        with patch.object(config,'MAX_PDF_UPLOAD_BYTES',10):
            self.upload()
        self.assertEqual(self.documents(),[])
        with self.client.session_transaction() as session:
            session.clear()
        self.assertIn(self.upload().status_code,(302,400))
        self.assertEqual(self.documents(),[])

    def test_abono_creation_without_pdf(self):
        response=self.client.post('/insertar/abono',data={'_csrf_token':'csrf','sector':'1','puerta':'2','fila':'4','asiento':'6'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.documents(),[])
        self.assertNotIn(b'name="pdf_file"',self.client.get('/insertar/abono').data)

    def test_resource_delete_removes_untraced_match_pdfs_and_assignments(self):
        with db.engine.begin() as conn:
            conn.execute(db.parkings.insert().values(id=1, nombre='Norte'))
        self.upload(
            ids=('abono:1', 'parking:1'),
            docs=[pdf('seat'), pdf('parking')],
        )
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.insert().values(
                id_partido=1, abono_id=1, id_cliente=1, asignador='gestor'
            ))
            conn.execute(db.asignaciones_parkings.insert().values(
                id_partido=1, parking_id=1, id_cliente=1, asignador='gestor'
            ))

        parking_response = self.client.post(
            '/parkings/1/eliminar', data={'_csrf_token': 'csrf'}
        )
        abono_response = self.client.post(
            '/abonos/1/eliminar', data={'_csrf_token': 'csrf'}
        )
        self.assertEqual(parking_response.status_code, 302)
        self.assertEqual(abono_response.status_code, 302)
        with db.engine.connect() as conn:
            self.assertIsNone(conn.execute(
                db.parkings.select().where(db.parkings.c.id == 1)
            ).fetchone())
            self.assertIsNone(conn.execute(
                db.abonos.select().where(db.abonos.c.id == 1)
            ).fetchone())
            self.assertEqual(conn.execute(db.documentos_pdf.select()).fetchall(), [])
            self.assertEqual(conn.execute(db.asignaciones_abonos.select()).fetchall(), [])
            self.assertEqual(conn.execute(db.asignaciones_parkings.select()).fetchall(), [])

    def test_resource_delete_preserves_resource_with_delivery_history(self):
        self.upload()
        document = self.documents()[0]
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.insert().values(
                id_partido=1, abono_id=1, id_cliente=1, asignador='gestor'
            ))
            conn.execute(db.envios_email.insert().values(
                partido_id=1, cliente_id=1, documento_pdf_id=document['id'],
                tipo_recurso='abono', recurso_id=1,
                destino_email='cliente@example.com',
                documento_sha256=document['content_sha256'],
                idempotency_key='resource-delete-history', estado='sent',
                intentos=1, solicitado_en=1,
            ))

        response = self.client.post(
            '/abonos/1/eliminar', data={'_csrf_token': 'csrf'}
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            messages = [message for _category, message in session.get('_flashes', [])]
        self.assertTrue(any('historial de envíos' in message for message in messages))
        with db.engine.connect() as conn:
            self.assertIsNotNone(conn.execute(
                db.abonos.select().where(db.abonos.c.id == 1)
            ).fetchone())
            self.assertEqual(len(conn.execute(db.documentos_pdf.select()).fetchall()), 1)
            self.assertEqual(len(conn.execute(db.asignaciones_abonos.select()).fetchall()), 1)
            self.assertEqual(len(conn.execute(db.envios_email.select()).fetchall()), 1)

    def test_match_delete_is_blocked_with_assignments_and_visible_in_ui(self):
        self.upload()
        with db.engine.begin() as conn:
            conn.execute(db.partidos.update().where(
                db.partidos.c.id == 1
            ).values(fecha='2099-01-01T20:00'))
            conn.execute(db.asignaciones_abonos.insert().values(
                id_partido=1, abono_id=1, id_cliente=1, asignador='gestor'
            ))

        response = self.client.post(
            '/partidos/1/eliminar', data={'_csrf_token': 'csrf'}
        )
        self.assertEqual(response.status_code, 302)
        with db.engine.connect() as conn:
            self.assertIsNotNone(conn.execute(
                db.partidos.select().where(db.partidos.c.id == 1)
            ).fetchone())
            self.assertEqual(len(conn.execute(db.documentos_pdf.select()).fetchall()), 1)
            self.assertEqual(len(conn.execute(db.asignaciones_abonos.select()).fetchall()), 1)

        page = self.client.get('/partidos').get_data(as_text=True)
        self.assertIn('disabled aria-disabled="true"', page)
        self.assertIn('Libera todos los abonos y parkings', page)
        self.assertIn('data-delete-dialog', page)

    def test_match_delete_removes_only_untraced_documents(self):
        self.upload()
        response = self.client.post(
            '/partidos/1/eliminar', data={'_csrf_token': 'csrf'}
        )
        self.assertEqual(response.status_code, 302)
        with db.engine.connect() as conn:
            self.assertIsNone(conn.execute(
                db.partidos.select().where(db.partidos.c.id == 1)
            ).fetchone())
            self.assertEqual(conn.execute(db.documentos_pdf.select()).fetchall(), [])

    def test_match_and_client_delete_preserve_delivery_history(self):
        self.upload()
        document = self.documents()[0]
        with db.engine.begin() as conn:
            conn.execute(db.envios_email.insert().values(
                partido_id=1, cliente_id=1, documento_pdf_id=document['id'],
                tipo_recurso='abono', recurso_id=1,
                destino_email='cliente@example.com',
                documento_sha256=document['content_sha256'],
                idempotency_key='delete-history-protection', estado='sent',
                intentos=1, solicitado_en=1,
            ))

        match_response = self.client.post(
            '/partidos/1/eliminar', data={'_csrf_token': 'csrf'}
        )
        client_response = self.client.post(
            '/clientes/1/eliminar', data={'_csrf_token': 'csrf'}
        )
        self.assertEqual(match_response.status_code, 302)
        self.assertEqual(client_response.status_code, 302)
        with db.engine.connect() as conn:
            self.assertIsNotNone(conn.execute(
                db.partidos.select().where(db.partidos.c.id == 1)
            ).fetchone())
            self.assertIsNotNone(conn.execute(
                db.clientes.select().where(db.clientes.c.id == 1)
            ).fetchone())
            self.assertEqual(len(conn.execute(db.documentos_pdf.select()).fetchall()), 1)
            self.assertEqual(len(conn.execute(db.envios_email.select()).fetchall()), 1)

    def test_clients_may_share_email(self):
        response = self.client.post('/insertar/cliente', data={
            '_csrf_token': 'csrf',
            'nombre': 'Segundo cliente',
            'email': 'cliente@example.com',
        })
        self.assertEqual(response.status_code, 302)
        with db.engine.connect() as conn:
            rows = conn.execute(db.clientes.select().where(db.clientes.c.email == 'cliente@example.com')).fetchall()
            self.assertEqual(len(rows), 2)

    def test_client_history_counts_distinct_current_assignments(self):
        with db.engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE partidos SET fecha = "
                "CASE id WHEN 1 THEN to_char(now() - interval '10 days', 'YYYY-MM-DD\"T\"HH24:MI') "
                "WHEN 2 THEN to_char(now() + interval '10 days', 'YYYY-MM-DD\"T\"HH24:MI') END "
                "WHERE id IN (1, 2)"
            )
            conn.execute(db.parkings.insert().values(id=1, nombre='Norte'))
            conn.execute(db.asignaciones_abonos.insert(), [
                {'id_partido': 1, 'abono_id': 1, 'id_cliente': 1, 'asignador': 'gestor'},
                {'id_partido': 2, 'abono_id': 2, 'id_cliente': 1, 'asignador': 'gestor'},
            ])
            conn.execute(db.asignaciones_parkings.insert().values(
                id_partido=1, parking_id=1, id_cliente=1, asignador='gestor'
            ))

        page = self.client.get('/clientes').get_data(as_text=True)
        self.assertIn('1 partido', page)
        self.assertEqual(page.count('class="list-group-item py-3 client-match-item'), 2)
        self.assertIn('Histórico', page)
        self.assertIn('Próximo', page)
        self.assertEqual(page.count('data-client-match-anchor'), 1)

        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.delete().where(
                db.asignaciones_abonos.c.id_partido == 1
            ))
            conn.execute(db.asignaciones_parkings.delete().where(
                db.asignaciones_parkings.c.id_partido == 1
            ))
        page = self.client.get('/clientes').get_data(as_text=True)
        self.assertIn('0 partidos', page)
        self.assertNotIn('Histórico', page)

    def test_admin_user_management_and_password_policy(self):
        self.assertIsNotNone(password_policy_error('abcdefgh'))
        self.assertIsNotNone(password_policy_error('12345678'))
        self.assertIsNone(password_policy_error('gestor123'))
        admin_hash, admin_salt = hash_password('Admin123')
        with db.engine.begin() as conn:
            conn.execute(db.usuarios.insert().values(
                username='admin', password_hash=admin_hash, salt=admin_salt, role='admin'
            ))
            admin_token = 'test-session-token-for-admin'
            now_ts = int(time.time())
            conn.execute(db.user_sessions.insert().values(
                session_hash=auth_routes._session_token_hash(admin_token),
                username='admin', created_at=now_ts, last_seen_at=now_ts,
                expires_at=now_ts + config.SESSION_MAX_AGE_SECONDS,
            ))

        self.assertEqual(self.client.get('/administracion/usuarios').status_code, 403)
        with self.client.session_transaction() as session:
            session.update(
                username='admin', login_ts=now_ts, csrf_token='csrf',
                session_token=admin_token,
            )
        users_page = self.client.get('/administracion/usuarios')
        self.assertEqual(users_page.status_code, 200)
        self.assertIn(b'Administrar usuarios', self.client.get('/').data)
        self.assertIn(b'aria-label="Eliminar usuario"', users_page.data)

        self.client.post('/insertar/usuario', data={
            '_csrf_token': 'csrf', 'username': 'rol-falso',
            'password': 'Valida123', 'role': 'superadmin',
        })
        self.client.post('/insertar/usuario', data={
            '_csrf_token': 'csrf', 'username': 'clave-debil',
            'password': 'sololetras', 'role': 'operador',
        })
        self.client.post('/insertar/usuario', data={
            '_csrf_token': 'csrf', 'username': 'temporal',
            'password': 'Valida123', 'role': 'operador',
        })
        with db.engine.connect() as conn:
            usernames = set(conn.execute(db.usuarios.select()).scalars())
        self.assertNotIn('rol-falso', usernames)
        self.assertNotIn('clave-debil', usernames)
        self.assertIn('temporal', usernames)

        self.client.post('/administracion/usuarios/eliminar', data={
            '_csrf_token': 'csrf', 'username': 'admin',
        })
        self.client.post('/administracion/usuarios/eliminar', data={
            '_csrf_token': 'csrf', 'username': 'temporal',
        })
        with db.engine.connect() as conn:
            usernames = set(conn.execute(db.usuarios.select()).scalars())
        self.assertIn('admin', usernames)
        self.assertNotIn('temporal', usernames)

        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.insert().values(
                id_partido=1, abono_id=1, id_cliente=1, asignador='gestor'
            ))
        self.client.post('/administracion/usuarios/eliminar', data={
            '_csrf_token': 'csrf', 'username': 'gestor',
        })
        with db.engine.connect() as conn:
            gestor = conn.execute(
                db.usuarios.select().where(db.usuarios.c.username == 'gestor')
            ).mappings().one()
            gestor_sessions = conn.execute(
                db.user_sessions.select().where(db.user_sessions.c.username == 'gestor')
            ).fetchall()
        self.assertFalse(gestor['active'])
        self.assertEqual(gestor_sessions, [])

    def test_login_rotates_server_session_and_old_cookie_stops_working(self):
        password_hash, salt = hash_password('Gestor123')
        with db.engine.begin() as conn:
            conn.execute(db.usuarios.update().where(
                db.usuarios.c.username == 'gestor'
            ).values(password_hash=password_hash, salt=salt))

        new_client = self.app.test_client()
        new_client.get('/login')
        with new_client.session_transaction() as login_session:
            csrf = login_session['csrf_token']
        response = new_client.post('/login', data={
            '_csrf_token': csrf, 'username': 'gestor', 'password': 'Gestor123',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(new_client.get('/').status_code, 200)
        self.assertIn('/login', self.client.get('/').location)
        with db.engine.connect() as conn:
            sessions = conn.execute(db.user_sessions.select()).fetchall()
        self.assertEqual(len(sessions), 1)

    def test_zap_login_payloads_do_not_bypass_auth_redirect_or_html_escaping(self):
        """Cubre las heurísticas de SQLi, open redirect y XSS señaladas por ZAP."""
        password_hash, salt = hash_password('Gestor123')
        with db.engine.begin() as conn:
            conn.execute(db.usuarios.update().where(
                db.usuarios.c.username == 'gestor'
            ).values(password_hash=password_hash, salt=salt))

        attacker = self.app.test_client()
        attacker.get('/login')
        with attacker.session_transaction() as login_session:
            csrf = login_session['csrf_token']
        injected_username = '\" autofocus onfocus=alert(1) data-zap=\"\' OR 1=1 --'
        rejected = attacker.post('/login', data={
            '_csrf_token': csrf,
            'username': injected_username,
            'password': "' OR 1=1 --",
        })
        rejected_html = rejected.get_data(as_text=True)
        self.assertEqual(rejected.status_code, 200)
        self.assertNotIn('onfocus=alert(1) data-zap="', rejected_html)
        self.assertIn('&#34; autofocus onfocus=alert(1)', rejected_html)
        with attacker.session_transaction() as rejected_session:
            self.assertNotIn('session_token', rejected_session)

        allowed = self.app.test_client()
        allowed.get('/login')
        with allowed.session_transaction() as login_session:
            csrf = login_session['csrf_token']
        response = allowed.post(
            '/login?next=https://example.invalid/robo',
            data={
                '_csrf_token': csrf,
                'username': 'gestor',
                'password': 'Gestor123',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/')

    def test_password_change_and_logout_revoke_sessions(self):
        old_hash, old_salt = hash_password('Gestor123')
        with db.engine.begin() as conn:
            conn.execute(db.usuarios.update().where(
                db.usuarios.c.username == 'gestor'
            ).values(password_hash=old_hash, salt=old_salt))
        response = self.client.post('/perfil/password', data={
            '_csrf_token': 'csrf', 'password_actual': 'Gestor123',
            'password_nueva': 'Nueva123', 'password_confirmacion': 'Nueva123',
        })
        self.assertTrue(response.location.endswith('/login'))
        with db.engine.connect() as conn:
            self.assertEqual(conn.execute(db.user_sessions.select()).fetchall(), [])

        new_client = self.app.test_client()
        new_client.get('/login')
        with new_client.session_transaction() as login_session:
            csrf = login_session['csrf_token']
        new_client.post('/login', data={
            '_csrf_token': csrf, 'username': 'gestor', 'password': 'Nueva123',
        })
        with new_client.session_transaction() as active_session:
            logout_csrf = active_session['csrf_token']
        new_client.post('/logout', data={'_csrf_token': logout_csrf})
        with db.engine.connect() as conn:
            self.assertEqual(conn.execute(db.user_sessions.select()).fetchall(), [])

    def test_idle_timeout_is_enforced_by_server(self):
        with db.engine.begin() as conn:
            conn.execute(db.user_sessions.update().values(
                last_seen_at=int(time.time()) - config.SESSION_IDLE_TIMEOUT_SECONDS
            ))
        response = self.client.get('/')
        self.assertIn('/login', response.location)
        with db.engine.connect() as conn:
            self.assertEqual(conn.execute(db.user_sessions.select()).fetchall(), [])

    def test_absolute_timeout_and_login_csrf_are_enforced(self):
        with db.engine.begin() as conn:
            conn.execute(db.user_sessions.update().values(
                expires_at=int(time.time()), last_seen_at=int(time.time())
            ))
        response = self.client.get('/')
        self.assertIn('/login', response.location)
        with db.engine.connect() as conn:
            self.assertEqual(conn.execute(db.user_sessions.select()).fetchall(), [])

        anonymous = self.app.test_client()
        response = anonymous.post('/login', data={
            'username': 'gestor', 'password': 'cualquier-clave1',
        })
        self.assertEqual(response.status_code, 400)

    def test_concurrent_logins_leave_only_one_server_session(self):
        password_hash, salt = hash_password('Gestor123')
        with db.engine.begin() as conn:
            conn.execute(db.usuarios.update().where(
                db.usuarios.c.username == 'gestor'
            ).values(password_hash=password_hash, salt=salt))
        user = {
            'username': 'gestor', 'role': 'operador',
            'password_hash': password_hash, 'salt': salt,
        }

        def start_session(_index):
            with self.app.test_request_context('/'):
                return auth_routes._start_authenticated_session(user)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(start_session, range(2)))
        self.assertEqual(results, [True, True])
        with db.engine.connect() as conn:
            sessions = conn.execute(
                db.user_sessions.select().where(db.user_sessions.c.username == 'gestor')
            ).fetchall()
        self.assertEqual(len(sessions), 1)

    def test_database_rejects_unknown_user_role(self):
        from sqlalchemy.exc import IntegrityError
        with self.assertRaises(IntegrityError):
            with db.engine.begin() as conn:
                conn.execute(db.usuarios.insert().values(
                    username='intruso', password_hash='x', salt='x', role='superadmin'
                ))

    def test_deleted_bootstrap_admin_is_not_recreated(self):
        admin_hash, admin_salt = hash_password('Admin123')
        with patch.object(config, 'DEFAULT_ADMIN_USERNAME', 'bootstrap-admin'), \
             patch.object(config, 'DEFAULT_ADMIN_HASH', admin_hash), \
             patch.object(config, 'DEFAULT_ADMIN_SALT', admin_salt):
            db.init_db()
            with db.engine.begin() as conn:
                conn.execute(db.usuarios.delete().where(db.usuarios.c.username == 'bootstrap-admin'))
            db.init_db()
        with db.engine.connect() as conn:
            restored = conn.execute(
                db.usuarios.select().where(db.usuarios.c.username == 'bootstrap-admin')
            ).fetchone()
        self.assertIsNone(restored)

    def test_rate_limit_consumption_is_atomic(self):
        def consume(_index):
            return rate_limit.consume_limit('concurrent-test', 'same-ip', 3, 60, now_ts=1000)[0]

        with ThreadPoolExecutor(max_workers=8) as executor:
            blocked = list(executor.map(consume, range(12)))
        self.assertEqual(blocked.count(False), 3)
        self.assertEqual(blocked.count(True), 9)
        with db.engine.connect() as conn:
            total = conn.execute(
                db.rate_limit_events.select().where(
                    db.rate_limit_events.c.scope == 'concurrent-test'
                )
            ).fetchall()
        self.assertEqual(len(total), 3)

    def test_delivery_uses_match_pdf_for_abonos_and_parkings(self):
        self.upload()
        self.upload(partido=2,docs=[pdf('B')])
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.insert(),[{'id_partido':n,'abono_id':1,'id_cliente':1,'asignador':'gestor'} for n in (1,2)])
            conn.execute(db.parkings.insert().values(id=1,nombre='Norte'))
            for match in (1, 2):
                conn.execute(db.documentos_pdf.insert().values(parking_id=1,partido_id=match,filename='parking.pdf',content_type='application/pdf',byte_size=8,content_sha256=f'parking-hash-{match}',pdf_data=f'parking{match}'.encode(),uploaded_by='gestor',created_at=1,updated_at=1))
            conn.execute(db.asignaciones_parkings.insert(),[{'id_partido':n,'parking_id':1,'id_cliente':1,'asignador':'gestor'} for n in (1,2)])
        with self.app.app_context(), patch.object(config,'N8N_WEBHOOK_URL','https://example.invalid/webhook/test'), patch.object(config,'N8N_WEBHOOK_SECRET','s'*32), patch.object(config,'N8N_WEBHOOK_BEARER_TOKEN',''), patch.object(config,'EMAIL_DELIVERY_INTER_SEND_DELAY_SECONDS',0), patch.object(email_delivery,'_send_to_n8n') as send:
            token=email_delivery.build_delivery_snapshot_token(1)
            run=email_delivery.send_assigned_resources_for_partido(1,solicitado_por='gestor',snapshot_token=token)
            self.assertEqual(run.sent_now,2)
            self.assertEqual(bytes(send.call_args_list[0].args[0]['pdf_data']),pdf('A'))
            self.assertEqual(bytes(send.call_args_list[1].args[0]['pdf_data']),b'parking1')
            email_delivery.send_assigned_resources_for_partido(1,solicitado_por='gestor',snapshot_token=token)
            self.assertEqual(send.call_count,2)
            run=email_delivery.send_assigned_resources_for_partido(2,solicitado_por='gestor',snapshot_token=email_delivery.build_delivery_snapshot_token(2))
            self.assertEqual(run.sent_now,2)
            self.assertEqual(bytes(send.call_args_list[2].args[0]['pdf_data']),pdf('B'))
            self.assertEqual(bytes(send.call_args_list[3].args[0]['pdf_data']),b'parking2')

    def test_cannot_assign_using_another_match_pdf(self):
        self.upload(partido=1)
        data={'_csrf_token':'csrf','cliente_id':'1'}
        self.assertEqual(self.client.post('/partidos/2/abonos/1/asignar',data=data).status_code,302)
        data['abono_ids']='1'
        self.assertEqual(self.client.post('/partidos/2/asignar',data=data).status_code,302)
        with db.engine.connect() as conn:
            self.assertEqual(conn.execute(db.asignaciones_abonos.select()).all(),[])

    def test_schema_enforces_match_scope_and_duplicate_content(self):
        from sqlalchemy.exc import IntegrityError
        self.upload()
        original=dict(self.documents()[0])
        original.pop('id')
        for change in ({'partido_id':None}, {'abono_id':2}, {'partido_id':999}):
            with self.assertRaises(IntegrityError):
                with db.engine.begin() as conn:
                    conn.execute(db.documentos_pdf.insert().values(**(original | change)))

    def test_request_limit_and_mismatched_file_mapping(self):
        with patch.object(config,'MAX_PDF_BATCH_BYTES',1):
            self.assertEqual(self.upload(docs=[b'x' * (300 * 1024)]).status_code,413)
        self.upload(ids=())
        self.assertEqual(self.documents(),[])

    def edit_document(self, document_id, action, target='2', partido=1, csrf='csrf'):
        """Solicita una edición autenticada con datos equivalentes al menú del PDF."""
        return self.client.post(f'/partidos/{partido}/entradas/{document_id}/editar', data={
            '_csrf_token': csrf, 'accion': action, 'recurso_id': target,
        })

    def test_relink_and_delete_free_document(self):
        self.upload()
        document = self.documents()[0]
        self.assertEqual(self.edit_document(document['id'], 'vincular').status_code, 302)
        updated = self.documents()[0]
        self.assertEqual(updated['abono_id'], 2)
        self.assertEqual(updated['pdf_data'], document['pdf_data'])
        self.assertEqual(updated['content_sha256'], document['content_sha256'])
        self.assertEqual(updated['filename'], document['filename'])
        self.assertEqual(self.client.get('/partidos/1').status_code, 200)
        self.assertEqual(self.edit_document(document['id'], 'borrar').status_code, 302)
        self.assertEqual(self.documents(), [])

    def test_document_edit_rejects_wrong_match_csrf_and_target(self):
        self.upload()
        document = self.documents()[0]
        self.edit_document(document['id'], 'borrar', partido=2)
        self.assertEqual(self.edit_document(document['id'], 'borrar', csrf='bad').status_code, 400)
        for target in ('', 'wrong', '0', '999', '1'):
            self.edit_document(document['id'], 'vincular', target=target)
        self.assertEqual(self.documents()[0]['abono_id'], 1)
        self.assertEqual(self.edit_document(document['id'], 'wrong').status_code, 400)
        with self.client.session_transaction() as session:
            session.pop('username', None)
        self.assertIn('/login', self.edit_document(document['id'], 'borrar').location)
        self.assertEqual(len(self.documents()), 1)

    def test_document_edit_preserves_occupied_target_and_assignment(self):
        self.upload(ids=('1', '2'), docs=[pdf('A'), pdf('B')])
        document = next(row for row in self.documents() if row['abono_id'] == 1)
        self.edit_document(document['id'], 'vincular')
        self.assertEqual(len(self.documents()), 2)
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.insert().values(id_partido=1, abono_id=1, id_cliente=1, asignador='gestor'))
        self.edit_document(document['id'], 'borrar')
        self.assertEqual(len(self.documents()), 2)

    def test_document_edit_preserves_delivery_history(self):
        self.upload()
        document = self.documents()[0]
        with db.engine.begin() as conn:
            conn.execute(db.envios_email.insert().values(
                partido_id=1, cliente_id=1, documento_pdf_id=document['id'], tipo_recurso='abono',
                recurso_id=1, destino_email='cliente@example.com', documento_sha256=document['content_sha256'],
                idempotency_key='test-sent-document', estado='sent', intentos=1, solicitado_en=1,
            ))
        self.edit_document(document['id'], 'borrar')
        self.edit_document(document['id'], 'vincular')
        self.assertEqual(self.documents()[0]['abono_id'], 1)
        with db.engine.connect() as conn:
            self.assertEqual(len(conn.execute(db.envios_email.select()).all()), 1)

    def test_document_edit_waits_for_delivery_lock(self):
        from gestion_abonos_app.services.partido_locks import try_acquire_partido_operation_lock, release_partido_operation_lock
        self.upload()
        document = self.documents()[0]
        conn = db.get_connection()
        try:
            self.assertTrue(try_acquire_partido_operation_lock(conn, 1))
            self.edit_document(document['id'], 'borrar')
            self.assertEqual(len(self.documents()), 1)
        finally:
            release_partido_operation_lock(conn, 1)
            conn.close()
        self.edit_document(document['id'], 'borrar')
        self.assertEqual(self.documents(), [])

    def test_parking_creation_without_pdf_and_match_upload(self):
        response = self.client.post('/insertar/parking', data={
            '_csrf_token': 'csrf', 'parking_id': '1', 'nombre': 'Exterior Oeste',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.documents(), [])
        self.upload(ids=('abono:1', 'parking:1'), docs=[pdf('seat'), pdf('park1')])
        self.upload(partido=2, ids=('parking:1',), docs=[pdf('park2')])
        self.assertEqual(len(self.documents()), 3)
        for url in ('/partidos/1', '/partidos/2', '/parkings', '/insertar/parking'):
            self.assertEqual(self.client.get(url).status_code, 200)
        html = self.client.get('/partidos/1').get_data(as_text=True)
        self.assertIn('parking:1', html)
        self.assertIn('Exterior Oeste', html)
        self.assertNotIn('PDF del parking', self.client.get('/insertar/parking').get_data(as_text=True))

    def test_parking_cannot_assign_pdf_from_another_match(self):
        with db.engine.begin() as conn:
            conn.execute(db.parkings.insert().values(id=1, nombre='Norte'))
        self.upload(ids=('parking:1',))
        data = {'_csrf_token': 'csrf', 'cliente_id': '1', 'parking_ids': '1'}
        self.client.post('/partidos/2/parkings/1/asignar', data=data)
        self.client.post('/partidos/2/asignar', data=data)
        with db.engine.connect() as conn:
            self.assertEqual(conn.execute(db.asignaciones_parkings.select()).all(), [])
        self.assertEqual(self.client.get('/partidos/1/parkings/1/asignar').status_code, 200)
        self.client.post('/partidos/1/parkings/1/asignar', data=data)
        with db.engine.connect() as conn:
            self.assertEqual(len(conn.execute(db.asignaciones_parkings.select()).all()), 1)

    def test_successful_assignments_redirect_to_match_detail(self):
        with db.engine.begin() as conn:
            conn.execute(db.parkings.insert().values(id=1, nombre='Norte'))
        self.upload(
            ids=('abono:1', 'abono:2', 'parking:1'),
            docs=[pdf('seat1'), pdf('seat2'), pdf('parking1')],
        )
        data = {'_csrf_token': 'csrf', 'cliente_id': '1'}
        abono_response = self.client.post('/partidos/1/abonos/1/asignar', data=data)
        parking_response = self.client.post('/partidos/1/parkings/1/asignar', data=data)
        multiple_response = self.client.post('/partidos/1/asignar', data={
            **data, 'abono_ids': '2',
        })
        self.assertTrue(abono_response.location.endswith('/partidos/1'))
        self.assertTrue(parking_response.location.endswith('/partidos/1'))
        self.assertTrue(multiple_response.location.endswith('/partidos/1'))

    def test_parking_relink_delete_and_assignment_protection(self):
        with db.engine.begin() as conn:
            conn.execute(db.parkings.insert(), [{'id': 1, 'nombre': 'Norte'}, {'id': 2, 'nombre': 'Sur'}])
        self.upload(ids=('parking:1',))
        document = self.documents()[0]
        self.edit_document(document['id'], 'vincular')
        self.assertEqual(self.documents()[0]['parking_id'], 2)
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_parkings.insert().values(id_partido=1, parking_id=2, id_cliente=1, asignador='gestor'))
        self.edit_document(document['id'], 'borrar')
        self.assertEqual(len(self.documents()), 1)
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_parkings.delete())
        self.edit_document(document['id'], 'borrar')
        self.assertEqual(self.documents(), [])

    def test_mixed_batch_invalid_resource_is_atomic(self):
        for invalid in ('parking:999', 'cliente:1', 'parking:0', 'parking:abc'):
            self.upload(ids=('abono:1', invalid), docs=[pdf('seat'), pdf('parking')])
            self.assertEqual(self.documents(), [])
        with db.engine.begin() as conn:
            conn.execute(db.parkings.insert().values(id=1, nombre='Norte'))
        self.upload(ids=('abono:1', 'parking:1'), docs=[pdf('same'), pdf('same')])
        self.assertEqual(self.documents(), [])

    def test_parking_schema_requires_match_and_preserves_history(self):
        from sqlalchemy.exc import IntegrityError
        with db.engine.begin() as conn:
            conn.execute(db.parkings.insert().values(id=1, nombre='Norte'))
        self.upload(ids=('parking:1',))
        document = self.documents()[0]
        original = dict(document)
        original.pop('id')
        with self.assertRaises(IntegrityError):
            with db.engine.begin() as conn:
                conn.execute(db.documentos_pdf.insert().values(**(original | {'partido_id': None})))
        with db.engine.begin() as conn:
            conn.execute(db.envios_email.insert().values(
                partido_id=1, cliente_id=1, documento_pdf_id=document['id'], tipo_recurso='parking',
                recurso_id=1, destino_email='cliente@example.com', documento_sha256=document['content_sha256'],
                idempotency_key='parking-history', estado='sent', intentos=1, solicitado_en=1,
            ))
        self.edit_document(document['id'], 'borrar')
        self.assertEqual(len(self.documents()), 1)

    def test_repair_legacy_parking_schema_allows_mixed_batch_without_data_loss(self):
        """Reproduce el fallo del lote con el CHECK antiguo y conserva sus PDFs y trazas."""
        from pathlib import Path
        from sqlalchemy.exc import IntegrityError
        with db.engine.begin() as conn:
            conn.exec_driver_sql('ALTER TABLE documentos_pdf DROP CONSTRAINT ck_documentos_pdf_one_resource')
            conn.exec_driver_sql('''ALTER TABLE documentos_pdf ADD CONSTRAINT ck_documentos_pdf_one_resource CHECK (
                (abono_id IS NOT NULL AND parking_id IS NULL AND partido_id IS NOT NULL)
                OR (abono_id IS NULL AND parking_id IS NOT NULL AND partido_id IS NULL))''')
            conn.exec_driver_sql('DROP INDEX idx_documentos_pdf_parking_partido_unique')
            conn.exec_driver_sql('CREATE UNIQUE INDEX idx_documentos_pdf_parking_unique ON documentos_pdf (parking_id)')
            conn.execute(db.parkings.insert(), [{'id':1,'nombre':'Antiguo'}, {'id':2,'nombre':'Nuevo'}])
            document_id = conn.execute(db.documentos_pdf.insert().values(
                parking_id=1, filename='antiguo.pdf', content_type='application/pdf', byte_size=3,
                content_sha256='old-hash', pdf_data=b'old', uploaded_by='gestor', created_at=1, updated_at=1,
            ).returning(db.documentos_pdf.c.id)).scalar_one()
            conn.execute(db.envios_email.insert().values(
                partido_id=1, cliente_id=1, documento_pdf_id=document_id, tipo_recurso='parking',
                recurso_id=1, destino_email='cliente@example.com', documento_sha256='old-hash',
                idempotency_key='old-delivery', estado='sent', intentos=1, solicitado_en=1,
            ))
        original = dict(self.documents()[0])
        self.upload(ids=('abono:1','parking:2'), docs=[pdf('seat'),pdf('park')])
        self.assertEqual([dict(row) for row in self.documents()], [original])
        page = self.client.get('/partidos/1').get_data(as_text=True)
        self.assertIn('no está adaptada a entradas por partido', page)
        self.assertNotIn('otra carga ha cambiado', page)
        repair = (Path(__file__).resolve().parents[1] / 'scripts/repair_match_pdf_schema.sql').read_text()
        with db.engine.begin() as conn:
            conn.exec_driver_sql(repair)
        self.upload(ids=('abono:1','parking:2'), docs=[pdf('seat'),pdf('park')])
        self.upload(partido=2, ids=('parking:2',), docs=[pdf('park match2')])
        self.assertEqual(len(self.documents()), 4)
        self.assertEqual(dict(next(row for row in self.documents() if row['id'] == document_id)), original)
        with db.engine.connect() as conn:
            self.assertEqual(len(conn.execute(db.envios_email.select()).all()), 1)
        with self.assertRaises(IntegrityError):
            with db.engine.begin() as conn:
                conn.execute(db.documentos_pdf.insert().values(**{k:v for k,v in original.items() if k != 'id'}))

    def test_n8n_ledger_claim_is_atomic_and_survives_replays(self):
        """Ejecuta las consultas del workflow con dos reservas simultáneas reales."""
        import json
        from pathlib import Path
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        root=Path(__file__).resolve().parents[1]
        workflow=json.loads((root/'workflows/n8n_envio_seguro.json').read_text())
        query=next(n for n in workflow['nodes'] if n['name']=='Reservar envio')['parameters']['query']
        query=query.replace('n8n_delivery.',self.schema+'.')
        for i in (1,2,3):query=query.replace(f'${i}','%s',1)
        ddl=(root/'workflows/n8n_delivery_ledger.sql').read_text().replace('n8n_delivery',self.schema)
        with db.engine.begin() as conn:conn.exec_driver_sql(ddl)
        first=db.engine.connect()
        try:
            self.assertEqual(first.exec_driver_sql(query,('a'*64,'b'*64,'first')).mappings().one()['owner_token'],'first')
            entered=Event()
            def reserve():
                with db.engine.begin() as conn:
                    entered.set()
                    return dict(conn.exec_driver_sql(query,('a'*64,'b'*64,'second')).mappings().one())
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(reserve)
                self.assertTrue(entered.wait(5))
                first.commit()
                receipt=future.result(timeout=5)
            self.assertEqual(receipt['owner_token'],'first')
            self.assertEqual(receipt['state'],'processing')
            with db.engine.begin() as conn:
                conn.exec_driver_sql(f"UPDATE {self.schema}.receipts SET state='sent' WHERE idempotency_key=%s",('a'*64,))
                self.assertEqual(conn.exec_driver_sql(query,('a'*64,'b'*64,'third')).mappings().one()['state'],'sent')
                self.assertEqual(conn.exec_driver_sql(query,('a'*64,'c'*64,'fourth')).mappings().one()['payload_hash'],'b'*64)
        finally:
            first.close()

    def test_release_preserves_sent_history_and_reactivates_only_unattempted(self):
        self.upload()
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.insert().values(id_partido=1,abono_id=1,id_cliente=1,asignador='gestor'))
        connection=db.get_connection()
        rows,keys=email_delivery._ready_assignment_rows(connection,1)
        email_delivery._enqueue_current_assignments(connection,rows,solicitado_por='gestor',snapshot_keys=keys)
        home._cancel_pending_deliveries_for_resource(connection,'abono',1,1)
        connection.commit()
        email_delivery._enqueue_current_assignments(connection,rows,solicitado_por='gestor',snapshot_keys=keys)
        self.assertEqual(connection.execute('SELECT estado FROM envios_email').fetchone()['estado'],'pending')
        connection.execute("UPDATE envios_email SET estado='sent', intentos=1")
        connection.commit();connection.close()
        self.client.post('/partidos/1/abonos/1/liberar',data={'_csrf_token':'csrf'})
        with db.engine.connect() as conn:
            deliveries=conn.execute(db.envios_email.select()).mappings().all()
            self.assertEqual(len(deliveries),1)
            self.assertEqual(deliveries[0]['estado'],'sent')

    def test_delivery_revalidates_recipient_before_http(self):
        self.upload()
        with db.engine.begin() as conn:
            conn.execute(db.asignaciones_abonos.insert().values(id_partido=1,abono_id=1,id_cliente=1,asignador='gestor'))
        connection=db.get_connection()
        rows,keys=email_delivery._ready_assignment_rows(connection,1)
        email_delivery._enqueue_current_assignments(connection,rows,solicitado_por='gestor',snapshot_keys=keys)
        delivery_id=connection.execute('SELECT id FROM envios_email').fetchone()['id']
        payload=email_delivery._load_delivery_payload(connection,delivery_id)
        connection.conn.rollback()
        with db.engine.begin() as conn:conn.execute(db.clientes.update().values(email='changed@example.com'))
        self.assertFalse(email_delivery._lock_current_delivery(connection,payload))
        connection.close()

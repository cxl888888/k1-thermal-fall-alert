"""Regression checks for historical voice import and missing-event handling."""
import hashlib
import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from portal.app import Settings, create_app, digest
from portal.cli import server_configuration
from portal.db import Client, Credential, Database, Pairing, now
from portal.hardware_status import build_snapshot
from portal.voice_history import historical_events


class PortalIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        dbpath = (self.root / 'portal.db').as_posix()
        self.db = Database('sqlite:///' + dbpath)
        self.db.initialize_fresh()
        with self.db.session() as session:
            client = Client(id='test_family', label='test')
            session.add(client)
            session.add(Credential(digest=digest('family-token'), role='family', client_id=client.id))
            session.add(Credential(digest=digest('producer-token'), role='producer'))
            session.commit()
        self.client = TestClient(create_app(Settings(database_url='sqlite:///' + dbpath,
                                                   allow_live_ingest=True), self.db))
        self.client.__enter__()
        self.producer = {'Authorization': 'Bearer producer-token'}
        self.client.cookies.set('k1_family_session', 'family-token', path='/api')

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def event(self, sequence, boot='boot-a'):
        return dict(device_id='k1', boot_id=boot, source_event_id=f'evt-{boot}-{sequence}',
                    sequence=sequence, state_version=sequence, origin='live', kind='status',
                    time_quality='device_unverified', source='test')

    def post(self, event):
        # A producer is not a browser and must not send a family session cookie.
        self.client.cookies.clear()
        result = self.client.post('/api/ingest', json=event, headers=self.producer)
        self.client.cookies.set('k1_family_session', 'family-token', path='/api')
        return result

    def test_gap_stays_visible_until_new_boot(self):
        self.assertEqual(self.post(self.event(1)).status_code, 200)
        self.assertEqual(self.post(self.event(3)).status_code, 200)
        self.assertTrue(self.client.get('/api/devices').json()['items'][0]['source_gap'])
        self.assertEqual(self.post(self.event(4)).status_code, 200)
        device = self.client.get('/api/devices').json()['items'][0]
        self.assertTrue(device['source_gap'])
        self.assertFalse(device['online'])
        self.assertTrue(self.post(self.event(4)).json()['duplicate'])
        self.assertEqual(self.post(self.event(1, 'boot-b')).status_code, 200)
        self.assertFalse(self.client.get('/api/devices').json()['items'][0]['source_gap'])

    def test_background_speech_keeps_latched_alert_summary_and_review_version(self):
        event = dict(self.event(1), kind='incident', incident_id='alert', red_latched=True,
                     route='emergency_help', title='紧急语音告警', reason='明确紧急呼救', source='kws_live')
        result = self.post(event)
        self.assertEqual(result.status_code, 200)
        path = '/api/incidents/' + result.json()['incident_id']
        before = self.client.get(path).json()
        follow_up = dict(self.event(2), kind='incident', incident_id='alert', red_latched=True,
                         route='normal', title='紧急语音告警', reason='日常语音', source='llm')
        self.assertEqual(self.post(follow_up).status_code, 200)
        after = self.client.get(path).json()
        for key in ('version', 'reason', 'route', 'source', 'red_latched'):
            self.assertEqual(after[key], before[key])

    def test_voice_history_pages_through_sparse_legacy_records_and_new_messages(self):
        expected = []
        for seq in range(1, 131):
            body = dict(self.event(seq), kind='incident', incident_id='history')
            if seq % 3 == 1:
                phrase = f'现场记录 {seq}'
                expected.append(phrase)
                if seq == 1:
                    body['transcript'] = phrase
                else:
                    body['voice_entry'] = dict(kind='answer', text=phrase)
            result = self.post(body)
            self.assertEqual(result.status_code, 200)

        path = '/api/incidents/' + result.json()['incident_id']
        page = self.client.get(path).json()
        self.assertEqual([row['text'] for row in page['voice_records']], expected[-20:])
        self.assertEqual(self.post(dict(self.event(131), kind='incident', incident_id='history',
                                       transcript='分页后收到的新记录')).status_code, 200)
        pages = [page['voice_records']]
        while page['voice_before'] is not None:
            url = f"{path}/voice?live_only=true&before={page['voice_before']}"
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            page = response.json()
            pages.append(page['voice_records'])
        recovered = [record for rows in reversed(pages) for record in rows]
        self.assertEqual([record['text'] for record in recovered], expected)
        self.assertEqual(len({record['id'] for record in recovered}), len(expected))
        self.client.cookies.clear()
        self.assertEqual(self.client.get(f'{path}/voice?before=100').status_code, 401)

    def test_shared_phone_waits_for_every_other_family(self):
        origin = {'Origin': 'http://127.0.0.1:8794'}
        proposal = {'phone': '+12025550101'}
        self.assertEqual(self.client.post('/api/contact/propose', json=proposal, headers=origin).status_code, 409)
        with self.db.session() as session:
            for ident in ('family-two', 'family-three'):
                session.add(Client(id=ident, label=ident))
                session.add(Credential(digest=digest(ident), role='family', client_id=ident))
            session.commit()

        def login(ident):
            self.client.cookies.set('k1_family_session', ident, path='/api')

        def act(action):
            return self.client.post(f'/api/contact/{change_id}/{action}', json={}, headers=origin)

        response = self.client.post('/api/contact/propose', json=proposal, headers=origin)
        self.assertEqual(response.status_code, 200)
        change_id = response.json()['pending']['id']
        self.assertEqual(act('approve').status_code, 403)
        self.assertEqual(act('apply').status_code, 409)
        login('family-two')
        self.assertEqual(act('approve').json()['pending']['approved'], 1)
        self.assertEqual(act('approve').json()['pending']['approved'], 1)
        self.assertEqual(act('apply').status_code, 403)
        login('family-token')
        self.assertEqual(act('apply').status_code, 409)
        self.assertIsNone(self.client.get('/api/me').json()['contact_phone'])
        login('family-three')
        self.assertEqual(act('approve').json()['pending']['approved'], 2)
        self.assertIsNone(self.client.get('/api/me').json()['contact_phone'])
        login('family-token')
        self.assertEqual(act('apply').json()['phone'], proposal['phone'])
        login('family-two')
        self.assertEqual(self.client.get('/api/me').json()['contact_phone'], proposal['phone'])

    def test_readonly_history_replay_and_idempotence(self):
        source = self.root / 'voice.db'
        with closing(sqlite3.connect(source)) as c, c:
            c.execute('create table journal (id integer primary key, mono_ns integer, kind text, payload text)')
            events = [
                dict(route='uncertain', reason='身体风险', new_red_trigger=False, prompt=1,
                     prompt_kind=None, source='llm', state=dict(red=False, prompt_status='requested')),
                dict(route='uncertain', reason='澄清', new_red_trigger=False, prompt=2,
                     prompt_kind='clarify', source='llm', state=dict(red=False, prompt_status='requested')),
                dict(route='emergency_help', reason='紧急呼救', new_red_trigger=True, prompt=None,
                     source='kws_live', state=dict(red=True, prompt_status='red_latched')),
                dict(route='safety_response', reason='安全回答', new_red_trigger=False, prompt=None,
                     source='llm', state=dict(red=True, prompt_status='red_latched')),
            ]
            c.executemany('insert into journal(mono_ns,kind,payload) values(?,?,?)',
                          [(n, 'facts', json.dumps(row, ensure_ascii=False)) for n,row in enumerate(events,1)])
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        rows = [x.model_dump(mode='json') for x in historical_events(source, 'run-1', 'history-device')]
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        self.assertEqual(len(rows), 2)
        self.assertEqual([x['origin'] for x in rows], ['historical_replay'] * 2)
        self.assertEqual([x['red_latched'] for x in rows], [False, True])
        self.assertTrue(all(x['transcript'] is None and x['occurred_at'] is None for x in rows))
        self.assertEqual([x['sequence'] for x in rows], [1, 2])
        for row in rows:
            self.assertFalse(self.post(row).json()['duplicate'])
        for row in rows:
            self.assertTrue(self.post(row).json()['duplicate'])
        incidents = self.client.get('/api/incidents').json()['items']
        self.assertEqual(len(incidents), 2)
        self.assertEqual(sum(x['red_latched'] for x in incidents), 1)


class ServerConfigurationTest(unittest.TestCase):
    def test_lan_requires_tls_and_exact_private_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = root / 'cert.pem', root / 'key.pem'
            cert.write_text('test certificate')
            key.write_text('test key')
            key.chmod(0o600)
            with self.assertRaises(ValueError):
                server_configuration('sqlite://', '10.0.0.3', 8794)
            with self.assertRaises(ValueError):
                server_configuration('sqlite://', '0.0.0.0', 8794, cert, key)
            with self.assertRaises(ValueError):
                server_configuration('sqlite://', '8.8.8.8', 8794, cert, key)
            with self.assertRaises(ValueError):
                server_configuration('sqlite://', '10.0.0.3', 8794, cert, None)
            with self.assertRaises(ValueError):
                server_configuration('sqlite://', '10.0.0.3', 8794, demo_mode=True, allow_live_ingest=True)
            settings, options = server_configuration('sqlite://', '10.0.0.3', 8794, cert, key)
            self.assertTrue(settings.secure_cookies)
            self.assertEqual(settings.allowed_origins, ('https://10.0.0.3:8794',))
            self.assertEqual(options['host'], '10.0.0.3')
            self.assertFalse(options['proxy_headers'])
            settings, options = server_configuration('sqlite://', '127.0.0.1', 8794)
            self.assertFalse(settings.secure_cookies)
            self.assertIsNone(options['ssl_certfile'])
            settings, options = server_configuration('sqlite://', '10.0.0.3', 8794, demo_mode=True)
            self.assertTrue(settings.demo_mode)
            self.assertTrue(settings.hardware_status_enabled)
            self.assertFalse(settings.allow_live_ingest)
            self.assertEqual(settings.allowed_origins, ('http://10.0.0.3:8794',))

    def test_https_pairing_issues_secure_http_only_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            path = (Path(directory) / 'db.sqlite').as_posix()
            db = Database('sqlite:///' + path)
            db.initialize_fresh()
            code = 'test-pairing-code-at-least-24-characters'
            with db.session() as session:
                session.add(Pairing(digest=digest(code), label='test',
                                    expires_at=now() + timedelta(minutes=2)))
                session.commit()
            settings = Settings(database_url='sqlite:///' + path, secure_cookies=True,
                                allowed_origins=('https://testserver',))
            with TestClient(create_app(settings, db), base_url='https://testserver') as client:
                response = client.post('/api/pair', json={'code': code},
                                       headers={'Origin': 'https://testserver'})
                self.assertEqual(response.status_code, 200)
                cookie = response.headers['set-cookie'].lower()
                self.assertIn('secure', cookie)
                self.assertIn('httponly', cookie)
                self.assertIn('samesite=strict', cookie)
                self.assertEqual(client.get('/api/me').status_code, 200)
                self.assertNotEqual(client.post('/api/pair', json={'code': code},
                                                headers={'Origin': 'https://testserver'}).status_code, 200)

    def test_lan_demo_auto_guest_and_synthetic_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database('sqlite:///' + (root / 'demo.db').as_posix())
            db.initialize_fresh()
            frontend = root / 'front'
            frontend.mkdir()
            (frontend / 'index.html').write_text('<html><head></head><body><div id="app"></div></body></html>', encoding='utf-8')
            with db.session() as session:
                session.add(Credential(digest=digest('demo-producer-token'), role='producer'))
                session.commit()
            settings = Settings(database_url='sqlite:///' + (root / 'demo.db').as_posix(),
                                frontend=frontend, demo_mode=True, hardware_status_enabled=True,
                                allowed_origins=('http://testserver',))
            fake_hardware = build_snapshot([], [], False, False, None)
            with patch('portal.app.HardwareMonitor') as monitor:
                monitor.return_value.snapshot.return_value = fake_hardware
                app = create_app(settings, db)
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/hardware-status').status_code, 401)
                page = client.get('/')
                self.assertEqual(page.status_code, 200)
                self.assertIn('事件为模拟', page.text)
                self.assertIn('family-api', page.text)
                self.assertIn('HttpOnly', page.headers['set-cookie'])
                self.assertEqual(client.get('/api/me').json()['label'], '体验手机')
                hardware = client.get('/api/hardware-status').json()
                self.assertEqual(hardware['source'], 'k1_local_observation')
                self.assertEqual(hardware['items']['thermal']['state'], 'not_detected')
                self.assertNotIn('set-cookie', client.get('/').headers)
                headers = {'Authorization': 'Bearer demo-producer-token', 'Origin': 'http://testserver'}
                event = dict(device_id='demo', boot_id='demo-boot', source_event_id='event-1',
                             sequence=1, state_version=1, origin='synthetic', kind='incident',
                             incident_id='incident-1', time_quality='synthetic', source='test',
                             red_latched=True)
                self.assertEqual(client.post('/api/ingest', json=event, headers=headers).status_code, 200)
                incident = client.get('/api/incidents').json()['items'][0]
                self.assertEqual(incident['origin'], 'synthetic')
                self.assertTrue(incident['red_latched'])
                event_path = '/api/incidents/' + incident['id']
                action = dict(request_id='demo-ack-1', expected_version=incident['version'])
                response = client.post(event_path + '/acknowledge', json=action,
                                       headers={'Origin': 'http://testserver'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(client.get(event_path).json()['red_latched'])
                review = dict(request_id='demo-review-1', expected_version=incident['version'],
                              note='演示核查', human_checked=True)
                self.assertEqual(client.post(event_path + '/review', json=review,
                                            headers={'Origin': 'http://testserver'}).status_code, 200)
                review['request_id'] = 'demo-resolve-1'
                self.assertEqual(client.post(event_path + '/resolve_request', json=review,
                                            headers={'Origin': 'http://testserver'}).status_code, 409)
                event.update(source_event_id='event-2', sequence=2, state_version=2,
                             origin='historical_replay', time_quality='historical')
                self.assertEqual(client.post('/api/ingest', json=event, headers=headers).status_code, 403)
                event['origin'] = 'live'
                self.assertEqual(client.post('/api/ingest', json=event, headers=headers).status_code, 403)


class HardwareObservationTest(unittest.TestCase):
    def test_failed_capture_remains_fault_and_recovery_needs_fresh_data(self):
        failed=dict(state='failed',error='read failed',radar={'state':'error','error':'closed'})
        result=build_snapshot([],[],True,True,failed)['items']
        self.assertEqual(result['thermal']['state'],'error')
        self.assertEqual(result['radar']['state'],'error')
        result=build_snapshot([],[],True,True,dict(state='idle'))['items']
        self.assertEqual(result['thermal']['state'],'unknown')
        self.assertEqual(result['radar']['state'],'connected')

    @patch.dict('os.environ', K1_AUDIO_SOURCE='test-microphone', K1_AUDIO_SINK='test-speaker')
    def test_idle_nodes_and_interface_presence_do_not_claim_sensor_data(self):
        source = {'name': 'test-microphone', 'state': 'SUSPENDED', 'mute': False, 'volume': {'mono': {'value': 65536}}}
        sink = {'name': 'test-speaker', 'state': 'SUSPENDED', 'mute': False, 'volume': {'left': {'value': 65536}}}
        result = build_snapshot([source], [sink], True, True, None)['items']
        self.assertEqual(result['microphone']['state'], 'connected')
        self.assertEqual(result['speaker']['state'], 'connected')
        self.assertEqual(result['thermal']['state'], 'unknown')
        self.assertEqual(result['radar']['state'], 'connected')
        self.assertIn('雷达本体待核验', result['radar']['detail'])
        self.assertEqual(build_snapshot(None, None, True, True, None)['items']['microphone']['state'], 'unknown')

    def test_fresh_frames_required_and_stale_frames_are_not_normal(self):
        capture = dict(state='recording', latest={'valid': True, 'valid_pixels': 768}, latest_age_s=0.5,
                       radar=dict(state='reading', age_s=0.3, counts={'valid_frames': 5}, error=None))
        result = build_snapshot([], [], True, True, capture)['items']
        self.assertEqual(result['thermal']['state'], 'active')
        self.assertEqual(result['radar']['state'], 'active')
        capture['latest_age_s'] = 4
        capture['radar']['age_s'] = 3
        result = build_snapshot([], [], True, True, capture)['items']
        self.assertEqual(result['thermal']['state'], 'stale')
        self.assertEqual(result['radar']['state'], 'stale')
        capture['latest_age_s'] = .1
        capture['latest'] = {'valid': False, 'status': 'warmup'}
        self.assertEqual(build_snapshot([], [], True, True, capture)['items']['thermal']['state'], 'waiting')
        capture['radar']['error'] = 'closed'
        self.assertEqual(build_snapshot([], [], True, True, capture)['items']['radar']['state'], 'error')


if __name__ == '__main__':
    unittest.main()

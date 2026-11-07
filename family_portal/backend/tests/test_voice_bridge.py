"""Durable delivery, restart, latch and pending-disposition integration checks."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import test_voice_history as history
from fastapi.testclient import TestClient
from portal.app import Settings, create_app
from portal.db import Database
from portal.voice_bridge import VoiceOutbox, journal_voice_entry, sender


def source_db(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE journal(id INTEGER PRIMARY KEY,mono_ns INTEGER,kind TEXT,payload TEXT)')


def append(path, red=True, pending=False, kind='facts'):
    value=dict(boot_id='test-boot',state=dict(red=red,pending=pending,needs_review=red or pending,
        prompt_status='red_latched' if red else 'requested' if pending else 'none'),
        route='emergency_help' if red else 'safety_response',reason='test',source='kws_live',
        transcript='测试语句',new_red_trigger=red,prompt=None)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('INSERT INTO journal(mono_ns,kind,payload) VALUES(1,?,?)',(kind,json.dumps(value)))


class DurableOutboxTest(unittest.TestCase):
    def test_latched_follow_up_preserves_relevant_speech_without_labeling_it_safe(self):
        follow_up = dict(transcript='我没事了', state={'red': True}, route='normal',
                         answer_eligible=False, effective_events=['明确安全'])
        self.assertEqual(journal_voice_entry('facts', follow_up),
                         {'kind':'utterance', 'text':'我没事了'})
        self.assertIsNone(journal_voice_entry('facts', dict(follow_up, effective_events=[])))
        self.assertEqual(journal_voice_entry('facts', dict(follow_up, effective_events=['跌倒']))['risk'],
                         'self_reported_fall')
        self.assertEqual(journal_voice_entry('facts', dict(follow_up, source='kws_live',
                         transcript=None, new_red_trigger=True, effective_events=['求救'])),
                         {'kind': 'utterance', 'risk': 'emergency_help'})
        self.assertTrue(journal_voice_entry('prompt_finished', dict(succeeded=True, spoken_text='再说一次',
                        state={'clarification_count': 1}))['follow_up'])

    def test_default_tail_and_live_history_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);src=root/'source.db';source_db(src);append(src)
            q=VoiceOutbox(src,root/'queue.db','test','live')
            self.assertEqual(q.collect(),0)
            with self.assertRaises(ValueError):VoiceOutbox(src,root/'another.db','test','live',True)
            append(src);self.assertEqual(q.collect(),1)

    def test_failed_send_restart_and_no_duplicate_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);src=root/'source.db';source_db(src)
            q=VoiceOutbox(src,root/'queue.db','test','controlled_simulation');append(src)
            self.assertEqual(q.collect(),1)
            with self.assertRaises(OSError):q.drain(lambda body: (_ for _ in ()).throw(OSError('offline')))
            q=VoiceOutbox(src,root/'queue.db','test','controlled_simulation')
            self.assertEqual(q.collect(),0);sent=[]
            self.assertEqual(q.drain(lambda body:(sent.append(body) or {'duplicate':False})),1)
            self.assertEqual(q.drain(lambda body:None),0)
            self.assertTrue(sent[0]['red_latched'])
            with self.assertRaises(ValueError):VoiceOutbox(src,root/'queue.db','other','controlled_simulation')

    def test_remote_credential_destination_rejected(self):
        for url in ('http://10.0.0.2:8794','https://example.com','http://127.0.0.1:8794/?token=x'):
            with self.assertRaises(ValueError):sender(url,'secret')

    def test_source_reset_cannot_silently_skip_new_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);src=root/'source.db';source_db(src)
            q=VoiceOutbox(src,root/'queue.db','test','controlled_simulation')
            append(src);q.collect()
            with closing(sqlite3.connect(src)) as db,db:db.execute('DELETE FROM journal')
            with self.assertRaises(ValueError):q.collect()

    def test_simulated_source_cannot_be_presented_as_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);src=root/'source.db';source_db(src)
            q=VoiceOutbox(src,root/'queue.db','test','live');append(src)
            with closing(sqlite3.connect(src)) as db,db:
                row=json.loads(db.execute('SELECT payload FROM journal').fetchone()[0])
                row['source']='simulated_facts'
                db.execute('UPDATE journal SET payload=?',(json.dumps(row),))
            with self.assertRaises(ValueError):q.collect()


class BridgePortalIntegration(history.PortalIntegrationTest):
    def test_live_evidence_survives_follow_up_and_stays_out_of_list(self):
        src = self.root / 'voice.db'
        source_db(src)
        queue = VoiceOutbox(src, self.root / 'outbox.db', 'runtime1', 'live')
        state = dict(red=False, pending=True, needs_review=True, prompt_status='requested')
        common = dict(boot_id='test-boot', state=state)
        frames = [{'offset_ms': -1000, 'pixels_decic': [220] * 768},
                  {'offset_ms': 0, 'pixels_decic': [240] * 768}]
        rows = [
            ('sensor', dict(source='thermal', kind='candidate', simulated=False, evidence={'thermal_frames': frames})),
            ('prompt_finished', dict(succeeded=True, spoken_text='请问您需要帮助吗？')),
            ('facts', dict(source='llm', route='continue_safety_confirmation',
                           answer_eligible=True, transcript='请再说一遍。')),
            ('no_answer', {}),
        ]
        with closing(sqlite3.connect(src)) as db, db:
            db.executemany('INSERT INTO journal(mono_ns,kind,payload) VALUES(1,?,?)',
                           [(kind, json.dumps(dict(common, **value))) for kind, value in rows])
        self.assertEqual(queue.collect(), len(rows))
        queue.drain(lambda body: self.post(body).json())
        summary = self.client.get('/api/incidents?live_only=true').json()['items'][0]
        self.assertNotIn('thermal_frames', summary)
        detail = self.client.get('/api/incidents/' + summary['id']).json()
        self.assertEqual(detail['trigger_source'], 'thermal')
        self.assertEqual(detail['thermal_frames'], frames)
        self.assertEqual([row['kind'] for row in detail['voice_records']],
                         ['thermal_candidate', 'question', 'answer', 'no_answer'])
        self.assertEqual(detail['voice_records'][0]['risk'], 'thermal_candidate')

    def test_delivery_ack_review_and_restart_readback(self):
        src=self.root/'voice.db';source_db(src)
        q=VoiceOutbox(src,self.root/'outbox.db','runtime1','live');append(src)
        self.assertEqual(q.collect(),1)
        first=[]
        def accepted_then_connection_lost(body):
            response=self.post(body);self.assertEqual(response.status_code,200)
            first.append(response.json()['incident_id'])
            raise OSError('response lost')
        with self.assertRaises(OSError):q.drain(accepted_then_connection_lost)
        q=VoiceOutbox(src,self.root/'outbox.db','runtime1','live')
        duplicates=[]
        def retry(body):
            response=self.post(body);self.assertEqual(response.status_code,200)
            result=response.json();duplicates.append(result['duplicate']);return result
        self.assertEqual(q.drain(retry),1);self.assertEqual(duplicates,[True])
        incidents=self.client.get('/api/incidents').json()['items'];self.assertEqual(len(incidents),1)
        incident=incidents[0];path='/api/incidents/'+incident['id']
        origin={'Origin':'http://127.0.0.1:8794'}
        ack=dict(request_id='ack-bridge',expected_version=incident['version'])
        self.assertEqual(self.client.post(path+'/acknowledge',json=ack,headers=origin).status_code,200)
        review=dict(request_id='review-bridge',expected_version=incident['version'],note='测试核查',human_checked=True)
        self.assertEqual(self.client.post(path+'/review',json=review,headers=origin).status_code,200)
        review['request_id']='resolve-bridge'
        response=self.client.post(path+'/resolve_request',json=review,headers=origin)
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['device_command_sent'])
        self.assertEqual(response.json()['status'],'pending_device_validation')
        self.assertTrue(self.client.get(path).json()['red_latched'])
        append(src,False,False);self.assertEqual(q.collect(),1);q.drain(retry)
        self.assertTrue(self.client.get(path).json()['red_latched'])
        self.assertIn('解除申请已记录',[x['title'] for x in self.client.get(path).json()['timeline']])
        # A new application/database connection reads the committed dispositions.
        url='sqlite:///'+(self.root/'portal.db').as_posix()
        with TestClient(create_app(Settings(database_url=url,allow_live_ingest=True),Database(url))) as restarted:
            restarted.cookies.set('k1_family_session','family-token',path='/api')
            restored=restarted.get(path).json()
            self.assertTrue(restored['red_latched'])
            self.assertEqual(restored['resolve_request']['status'],'pending_device_validation')


if __name__=='__main__':unittest.main()

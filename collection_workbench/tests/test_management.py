"""Isolated lifecycle and recoverable-removal checks; no hardware access."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from storage import Store,atomic_json,digest,text_field
from preview import Preview
from test_targeted import FixtureCapture


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='workbench_management_tests_')
        self.root=Path(self.tmp.name)
        self.capture=FixtureCapture(True,self.root/'raw',self.root/'unused.py')
        self.capture.start(dict(recording_profile='preliminary',collection_purpose='functional_trial',
            activity_source='everyday_only',installation_id='桌面位置一',simulation_protocol=''))
        self.capture.command('stop',dict(session_id=self.capture.meta['session_id'],request_id=uuid.uuid4().hex))
        self.folder=self.capture.folder
        self.store=Store([self.root/'raw'],self.root/'reviewed')
        self.key=self.store.scan()['sessions'][0]['key']

    def tearDown(self):
        self.capture.shutdown();self.tmp.cleanup()

    def hashes(self):
        return {p.relative_to(self.folder).as_posix():digest(p) for p in self.folder.rglob('*') if p.is_file()}

    def test_chinese_display_name_keeps_raw_immutable(self):
        before=self.hashes();name='卧室行走第一轮·志愿者甲'
        self.store.rename(self.key,dict(display_name=name))
        self.assertEqual(self.store.scan()['sessions'][0]['display_name'],name)
        self.assertEqual(self.hashes(),before)
        self.assertEqual(text_field('  靠窗区域  ','场景',100,True),'靠窗区域')

    def test_remove_restore_preserves_raw_and_revision_bytes(self):
        details=self.store.details(self.key)
        self.store.save_review(self.key,dict(labels=details['labels'],base_revision_id=None,
            source_manifest_sha256=details['source_manifest_sha256']))
        self.store.rename(self.key,dict(display_name='可恢复的中文编号'))
        before=self.hashes();revision=self.root/'reviewed'/self.key
        revision_hashes={str(p.relative_to(revision)):digest(p) for p in revision.rglob('*') if p.is_file()}
        result=self.store.recycle(self.key,dict(confirmed_session_id=self.capture.meta['session_id']))
        self.assertFalse(self.folder.exists());self.assertEqual(self.store.scan()['sessions'],[])
        self.assertEqual(len(self.store.trash.list()['items']),1)
        self.store.trash.restore(result['trash_id'])
        self.assertEqual(self.hashes(),before)
        self.assertEqual({str(p.relative_to(revision)):digest(p) for p in revision.rglob('*') if p.is_file()},revision_hashes)
        self.assertEqual(self.store.scan()['sessions'][0]['display_name'],'可恢复的中文编号')
        self.assertEqual(self.store.trash.list()['items'],[])

    def test_running_session_or_wrong_confirmation_cannot_be_removed(self):
        with self.assertRaises(ValueError):self.store.recycle(self.key,dict(confirmed_session_id='wrong'))
        meta=json.loads((self.folder/'metadata.json').read_text(encoding='utf-8'));meta['status']='recording'
        atomic_json(self.folder/'metadata.json',meta)
        with self.assertRaisesRegex(ValueError,'仍在采集'):self.store.recycle(self.key,dict(confirmed_session_id=meta['session_id']))
        self.assertTrue(self.folder.exists())

    def test_restore_never_overwrites_existing_session(self):
        result=self.store.recycle(self.key,dict(confirmed_session_id=self.capture.meta['session_id']))
        self.folder.mkdir();(self.folder/'keep.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError,'没有覆盖'):self.store.trash.restore(result['trash_id'])
        self.assertEqual((self.folder/'keep.txt').read_text(),'keep')

    def test_outside_paths_and_invalid_ids_rejected(self):
        outside=self.root/'outside';outside.mkdir()
        with self.assertRaises(ValueError):self.store.trash.remove(outside,'x',self.key,'x','test')
        with self.assertRaises(ValueError):self.store.trash.restore('../../outside')
        self.assertTrue(self.folder.exists())

    def test_failed_revision_move_rolls_back_raw(self):
        self.store.rename(self.key,dict(display_name='测试'))
        import session_trash
        real=session_trash.os.rename;calls=[]
        def fail_second(a,b):
            calls.append((a,b))
            if len(calls)==2:raise OSError('simulated move failure')
            return real(a,b)
        before=self.hashes()
        with patch('session_trash.os.rename',side_effect=fail_second):
            with self.assertRaises(OSError):self.store.recycle(self.key,dict(confirmed_session_id=self.capture.meta['session_id']))
        self.assertEqual(self.hashes(),before)
        self.assertEqual(len(self.store.scan()['sessions']),1)

    def test_cancel_waits_until_writer_closed_then_recycles(self):
        # Restart a simulated fixture and exercise the real cancel/finalizer code.
        self.capture.start(dict(recording_profile='preliminary',collection_purpose='functional_trial',
            activity_source='everyday_only',installation_id='测试',simulation_protocol=''))
        self.capture.discard_callback=self.store.recycle_folder
        class Process:
            returncode=0
            def wait(self,timeout=None):return 0
        self.capture.process=Process()
        self.capture.reader=threading.Thread(target=lambda:None);self.capture.reader.start()
        folder=self.capture.folder
        self.capture.command('cancel',dict(session_id=self.capture.meta['session_id'],request_id=uuid.uuid4().hex,confirm_discard=True))
        self.assertTrue(folder.exists())
        self.capture._watch(self.capture.process,folder,10)
        self.assertFalse(folder.exists());self.assertEqual(self.capture.state()['state'],'cancelled')
        self.assertTrue(all(stream.closed for stream in [self.capture.labels,self.capture.events,self.capture.notes]))
        self.assertEqual(self.store.trash.list()['items'][0]['reason'],'operator_cancel')


class FakePreview(Preview):
    def _spawn(self):
        code="""import json,time
def emit(kind,**kw):print('WORKBENCH_EVENT '+json.dumps(dict(kind=kind,**kw)),flush=True)
emit('ready')
now=time.monotonic_ns()
emit('frame',row=dict(frame_id=1,read_start_mono_ns=now,read_end_mono_ns=now+1000,
     representative_mono_ns=now+500,valid=True,valid_pixels=768,grid_c=[25.]*768,
     valid_mask=[True]*768,status='valid',simulated=True,read_ms=1),valid_frames=1)
time.sleep(60)
"""
        return subprocess.Popen([sys.executable,'-u','-c',code],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')


class LifecycleFixtureCapture(FixtureCapture):
    def command(self,kind,data):
        if kind!='cancel':return super().command(kind,data)
        if data.get('confirm_discard') is not True:raise ValueError('请先确认取消')
        super().command('stop',data)
        self.meta['status']='cancelled'
        atomic_json(self.folder/'metadata.json',self.meta)
        self.cancel_result=self.discard_callback(self.folder,'operator_cancel')
        self.folder=None;self.latest=None
        return self.state()


class PreviewTests(unittest.TestCase):
    def test_preview_start_stop_does_not_create_dataset_files(self):
        with tempfile.TemporaryDirectory() as directory:
            driver=Path(directory)/'fixture.py';driver.write_text('# simulated preview only')
            preview=FakePreview(True,driver)
            try:
                preview.start()
                deadline=time.monotonic()+4
                while preview.state()['state']=='starting' and time.monotonic()<deadline:time.sleep(.02)
                self.assertEqual(preview.state()['state'],'previewing')
                self.assertFalse(preview.state()['saved'])
            finally:preview.shutdown()
            self.assertFalse(preview.active());self.assertEqual(list(Path(directory).iterdir()),[driver])
            preview.reset();self.assertEqual(preview.state()['state'],'idle')


if __name__=='__main__':unittest.main()

"""Bounded, read-only Windows/K1 UART capture with raw evidence and strict decoding."""
import argparse
import bisect
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import statistics
import sys
import time
import uuid

import serial
from serial.tools import list_ports
from ld2410_protocol import scan_reports


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',required=True)
    parser.add_argument('--duration',type=float,default=10)
    parser.add_argument('--label',default='initial')
    parser.add_argument('--baudrate',type=int,default=256000,
                        help='Host receive setting only; never changes the radar configuration')
    parser.add_argument('--output-root',type=Path,help='Parent directory for a new, unique raw capture')
    args=parser.parse_args()
    if not 0<args.duration<=1800 or not re.fullmatch(r'[a-z0-9_]{1,40}',args.label):
        parser.error('Invalid duration or case label')
    if not 9600<=args.baudrate<=3000000:
        parser.error('Host baudrate must be between 9600 and 3000000')
    if sys.platform=='win32':
        if not re.fullmatch(r'COM[1-9][0-9]*',args.port.upper()):
            parser.error('Expected an explicitly confirmed Windows COM port')
        ports=[p for p in list_ports.comports() if p.device.upper()==args.port.upper()]
        default_root=Path(__file__).resolve().parent.parent/'雷达串口测试'/datetime.now().strftime('%Y%m%d')
    elif sys.platform=='linux':
        requested=Path(args.port)
        resolved=requested.resolve()
        if not requested.is_absolute() or resolved.parent!=Path('/dev') or not re.fullmatch(r'ttyUSB[0-9]+',resolved.name):
            parser.error('Expected an explicitly confirmed /dev/ttyUSB* device or its stable /dev/serial/by-id link')
        ports=[p for p in list_ports.comports() if Path(p.device).resolve()==resolved]
        default_root=Path.home()/'k1-collection'/'radar-checks'
    else:
        parser.error('Only Windows and Linux/K1 are supported')
    if len(ports)!=1 or (ports[0].vid,ports[0].pid)!=(0x1a86,0x7523):
        parser.error('Confirmed CH340 port is no longer present; re-enumerate rather than guessing')
    device=ports[0]
    root=(args.output_root or default_root).expanduser().resolve()
    folder=root/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_')+args.label+'_'+uuid.uuid4().hex[:6])
    folder.mkdir(parents=True,exist_ok=False)
    report={'model_declared_by_user':'HLK-LD2410C','port':device.device,'requested_port':args.port,
            'usb_vid_pid':'1A86:7523','usb_location':device.location,
            'serial_number':device.serial_number,'requested_seconds':args.duration,
            'baudrate':args.baudrate,'bytesize':8,'parity':'N','stopbits':1,
            'xonxoff':False,'rtscts':False,'dsrdtr':False,'dtr':False,'rts':False,
            'read_only':True,'configuration_bytes_written':0,'electrical_voltage_measured':False,
            'host':platform.node(),'python':sys.version,'pyserial':serial.__version__,
            'clock':'Host time.monotonic_ns read intervals; not sensor exposure times; compare only on the same host and boot',
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip() if sys.platform=='linux' else None,
            'exclusive_port_lock_requested':sys.platform=='linux',
            'started_wall_unix_ns':time.time_ns(),
            'source_sha256':{Path(f).name:hashlib.sha256(Path(f).read_bytes()).hexdigest()
                             for f in [__file__,Path(__file__).with_name('ld2410_protocol.py')]}}
    (folder/'capture-parameters.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'source').mkdir()
    for source in [Path(__file__),Path(__file__).with_name('ld2410_protocol.py')]:
        shutil.copyfile(source,folder/'source'/source.name)
    print(f'[开始] {device.device}，主机接收 {args.baudrate} 8N1，只读 {args.duration:g} 秒。',flush=True)
    print('[目录] '+str(folder),flush=True)
    port=serial.Serial(port=None,baudrate=args.baudrate,bytesize=serial.EIGHTBITS,
                       parity=serial.PARITY_NONE,stopbits=serial.STOPBITS_ONE,timeout=.05,
                       xonxoff=False,rtscts=False,dsrdtr=False,
                       **({'exclusive':True} if sys.platform=='linux' else {}))
    port.port=args.port
    port.dtr=False
    port.rts=False
    started=None
    count=0
    try:
        port.open()
        port.reset_input_buffer()
        report['capture_start_mono_ns']=time.monotonic_ns()
        started=time.monotonic()
        next_progress=started+10
        with (folder/'raw.bin').open('wb') as raw,(folder/'read-intervals.jsonl').open('w',encoding='utf-8') as intervals:
            while time.monotonic()-started<args.duration:
                begin=time.monotonic_ns()
                data=port.read(4096)
                end=time.monotonic_ns()
                if data:
                    raw.write(data)
                    intervals.write(json.dumps({'byte_offset':count,'length':len(data),
                        'read_start_mono_ns':begin,'read_end_mono_ns':end})+'\n')
                    count+=len(data)
                if time.monotonic()>=next_progress:
                    raw.flush()
                    intervals.flush()
                    print(f'[采样] 已运行 {time.monotonic()-started:.0f} 秒，收到 {count} 字节。',flush=True)
                    next_progress+=10
        report['stop_reason']='duration_complete'
    except KeyboardInterrupt:
        report['stop_reason']='operator_interrupted'
    except Exception as exc:
        report['stop_reason']='serial_error'
        report['error']=type(exc).__name__+': '+str(exc)
    finally:
        report['capture_end_mono_ns']=time.monotonic_ns()
        port.close()
        report['actual_seconds']=time.monotonic()-started if started is not None else 0
    raw_path=folder/'raw.bin'
    raw=raw_path.read_bytes() if raw_path.exists() else b''
    frames,checks=scan_reports(raw)
    reads=[json.loads(line) for line in (folder/'read-intervals.jsonl').read_text(encoding='utf-8').splitlines()] if (folder/'read-intervals.jsonl').exists() else []
    offsets=[row['byte_offset'] for row in reads]
    frame_times=[]
    for frame in frames:
        end_byte=frame['byte_offset']+frame['wire_length']-1
        index=bisect.bisect_right(offsets,end_byte)-1
        if index<0 or not reads[index]['byte_offset']<=end_byte<reads[index]['byte_offset']+reads[index]['length']:
            raise RuntimeError('Frame bytes are not covered by saved read intervals')
        frame_times.append(reads[index]['read_end_mono_ns'])
        frame.update(read_start_mono_ns=reads[index]['read_start_mono_ns'],
                     read_end_mono_ns=reads[index]['read_end_mono_ns'],
                     timing_scope='host_read_containing_last_frame_byte')
    gaps=[(b-a)/1e9 for a,b in zip(frame_times,frame_times[1:])]
    windows=list(gaps)
    if frame_times:
        windows.extend([(frame_times[0]-report['capture_start_mono_ns'])/1e9,
                        (report['capture_end_mono_ns']-frame_times[-1])/1e9])
    else:
        windows.append(report['actual_seconds'])
    report['delivery_observation']={'median_frame_gap_seconds':statistics.median(gaps) if gaps else None,
        'max_frame_gap_seconds':max(gaps,default=None),
        'max_no_complete_report_window_seconds':max(windows,default=0),
        'gaps_over_1s':sum(gap>1 for gap in gaps),
        'mode_transitions':sum(a['mode']!=b['mode'] for a,b in zip(frames,frames[1:])),
        'diagnostic_threshold_seconds':1.0,'threshold_is_not_manufacturer_specification':True,
        'scope':'host USB delivery, including initial and final silence; not hardware sampling times'}
    report.update(checks)
    report.update(received_bytes=len(raw),raw_sha256=hashlib.sha256(raw).hexdigest(),
                  observed_valid_frames_per_second=len(frames)/max(report['actual_seconds'],.001),
                  first_valid_frames=frames[:3],last_valid_frame=frames[-1] if frames else None)
    with (folder/'frames.jsonl').open('w',encoding='utf-8') as out:
        for frame in frames:
            out.write(json.dumps(frame,ensure_ascii=False)+'\n')
    (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'folder':str(folder),'received_bytes':len(raw),
                     'strict_valid_frames':len(frames),'invalid_candidate_count':checks['invalid_candidate_count'],
                     'mode_counts':checks['frame_mode_counts'],'state_counts':checks['target_state_counts'],
                     'framing_checks_passed':checks['framing_checks_passed'],
                     'delivery_observation':report['delivery_observation'],
                     'error':report.get('error'),'last_frame':report['last_valid_frame']},ensure_ascii=False),flush=True)
    return 0 if checks['framing_checks_passed'] and report['stop_reason']=='duration_complete' else 2


if __name__=='__main__':
    raise SystemExit(main())

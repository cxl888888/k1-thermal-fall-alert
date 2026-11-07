"""Strict LD2410C report decoding, based on Hi-Link protocol V1.09 tables 10-15.

Manufacturer source:
https://h.hlktech.com/download/HLK-LD2410C-24G/1/LD2410C%20串口通信协议%20V1.09.pdf
This does not send commands, repair bytes, or infer a fall from presence data.
"""
from collections import Counter

HEADER = bytes.fromhex('f4 f3 f2 f1')
FOOTER = bytes.fromhex('f8 f7 f6 f5')
STATE_NAMES = {0:'无人',1:'运动',2:'静止',3:'运动+静止',
               4:'底噪检测中',5:'底噪检测成功',6:'底噪检测失败'}


def decode_payload(payload):
    if len(payload)<13:
        raise ValueError('payload_too_short')
    mode = payload[0]
    if mode not in (1,2):
        raise ValueError('unknown_data_type')
    if payload[1]!=0xaa:
        raise ValueError('inner_header_mismatch')
    if payload[-2:]!=b'\x55\x00':
        raise ValueError('inner_footer_mismatch')
    if payload[2] not in STATE_NAMES:
        raise ValueError('undefined_target_state')
    if payload[5]>100 or payload[8]>100:
        raise ValueError('basic_energy_out_of_range')
    if mode==2 and len(payload)!=13:
        raise ValueError('basic_length_mismatch')
    result = {'data_type_code':mode,'mode':'engineering' if mode==1 else 'basic',
              'state':payload[2],'state_name':STATE_NAMES[payload[2]],
              'moving_dist_cm':int.from_bytes(payload[3:5],'little'),
              'moving_energy':payload[5],
              'static_dist_cm':int.from_bytes(payload[6:8],'little'),
              'static_energy':payload[8],
              'detect_dist_cm':int.from_bytes(payload[9:11],'little')}
    if mode==1:
        moving_gate,static_gate=payload[11],payload[12]
        if moving_gate>8 or static_gate>8:
            raise ValueError('gate_index_out_of_range')
        if len(payload)!=19+moving_gate+static_gate:
            raise ValueError('engineering_length_mismatch')
        split=14+moving_gate
        energies_end=split+static_gate+1
        moving=list(payload[13:split])
        stationary=list(payload[split:energies_end])
        if any(value>100 for value in moving+stationary):
            raise ValueError('gate_energy_out_of_range')
        if payload[energies_end+1] not in (0,1):
            raise ValueError('out_pin_out_of_range')
        result.update(max_moving_gate=moving_gate,max_static_gate=static_gate,
                      moving_gates=moving,static_gates=stationary,
                      light=payload[energies_end],out_pin=payload[energies_end+1])
    return result


def scan_reports(raw):
    """Resynchronize at exact headers; retain every invalid candidate as evidence."""
    frames=[]
    invalid=Counter()
    examples=[]
    incomplete=[]
    pos=0
    while True:
        start=raw.find(HEADER,pos)
        if start<0:
            break
        if len(raw)-start<6:
            incomplete.append({'offset':start,'reason':'partial_header_or_length'})
            break
        length=int.from_bytes(raw[start+4:start+6],'little')
        reason=None
        end=start+10+length
        if length<13 or length>64:
            reason='implausible_payload_length'
        elif end>len(raw):
            incomplete.append({'offset':start,'payload_length':length,'reason':'partial_frame_at_capture_end'})
            break
        elif raw[start+6+length:end]!=FOOTER:
            reason='outer_footer_mismatch'
        else:
            try:
                fields=decode_payload(raw[start+6:start+6+length])
            except ValueError as exc:
                reason=str(exc)
        if reason:
            invalid[reason]+=1
            if len(examples)<8:
                examples.append({'offset':start,'payload_length':length,'reason':reason,
                                 'raw_hex':raw[start:min(end,start+80,len(raw))].hex(' ')})
            pos=start+1
            continue
        frames.append(dict(fields,byte_offset=start,wire_length=end-start,payload_length=length))
        pos=end
    gaps=[b['byte_offset']-(a['byte_offset']+a['wire_length']) for a,b in zip(frames,frames[1:])]
    prefix=frames[0]['byte_offset'] if frames else len(raw)
    suffix=len(raw)-frames[-1]['byte_offset']-frames[-1]['wire_length'] if frames else 0
    boundary_limit=max((f['wire_length'] for f in frames),default=0)
    boundary_ok=bool(frames) and prefix<boundary_limit and suffix<boundary_limit
    summary={'strict_valid_frames':len(frames),'invalid_candidate_count':sum(invalid.values()),
             'invalid_candidate_reasons':dict(invalid),'invalid_examples':examples,
             'incomplete_end_candidates':incomplete,
             'frame_mode_counts':dict(Counter(f['mode'] for f in frames)),
             'target_state_counts':dict(Counter(f['state_name'] for f in frames)),
             'accepted_wire_bytes':sum(f['wire_length'] for f in frames),
             'bytes_between_valid_frames':sum(gaps),'gaps_between_valid_frames':sum(g>0 for g in gaps),
             'prefix_bytes_before_first_valid':prefix,
             'suffix_bytes_after_last_valid':suffix,
             'capture_boundaries_within_one_frame':boundary_ok,
             'framing_checks_passed':bool(frames) and not invalid and not any(gaps) and boundary_ok,
             'integrity_scope':'exact structure and field-range checks; no byte repair; fixed 0x00 is not a CRC'}
    return frames,summary

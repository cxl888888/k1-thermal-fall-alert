"""Narrow dialogue safeguards and bounded capture queue; not a replacement model."""
import math
import queue
import unicodedata


CONTEXT_ONLY_REPLIES = frozenset({
    '', '嗯', '嗯嗯', '啊', '哦', '哦哦', '是', '是的', '好', '好的', '对', '对的',
    '那里', '这里', '这个', '那个', '还是那样', '还是刚才那样', '不知道', '可能吧',
})


def reply_quality(transcript, awaiting):
    """Only reject context-dependent standalone replies during a pending inquiry.

    No length rule: short but explicit '救命'/'我没事' must not be discarded.
    Original model output remains in the result; this never edits gold labels.
    """
    normalized = ''.join(c for c in unicodedata.normalize('NFKC', transcript)
                         if not c.isspace() and not unicodedata.category(c).startswith('P'))
    return 'context_only' if awaiting and normalized in CONTEXT_ONLY_REPLIES else 'clear'


def submit_audio(tasks, pcm, started, detected, generation, clock, endpoint_silence_ms=0, cut_reason='unknown'):
    task = {'pcm': pcm, 'started': started, 'detected': detected,
            'generation': generation, 'submitted': clock(),
            'endpoint_silence_ms': endpoint_silence_ms, 'cut_reason': cut_reason}
    try:
        tasks.put_nowait(task)
        return True
    except queue.Full:
        return False


def queue_age(task, now, maximum):
    if not math.isfinite(maximum) or maximum <= 0:
        raise ValueError('maximum queue age must be positive')
    seconds = max(0., now - task['submitted'])
    return round(seconds * 1000), seconds > maximum

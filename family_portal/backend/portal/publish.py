"""Explicit event-file publisher; no microphone, radar, or historical-file guessing."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request,HTTPRedirectHandler,build_opener

from .schemas import EventBody


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('events',type=Path,help='JSONL matching family_event_0.1; origin must be explicit')
    parser.add_argument('--url',default='http://127.0.0.1:8794')
    args=parser.parse_args()
    parts=urlsplit(args.url)
    if parts.scheme not in ('http','https') or parts.username or parts.password or parts.query or parts.fragment:
        parser.error('服务地址无效；凭证不能出现在URL')
    if parts.scheme=='http' and parts.hostname not in ('127.0.0.1','localhost'):
        parser.error('事件凭证只允许通过回环HTTP或HTTPS发送')
    secret=os.environ.get('FAMILY_PRODUCER_TOKEN')
    if not secret: parser.error('需在本地设置FAMILY_PRODUCER_TOKEN，不把凭证写入事件文件')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            # SECURITY: never forward the producer credential to a redirect target.
            return None
    opener=build_opener(NoRedirect)
    with args.events.open(encoding='utf-8-sig') as stream:
        for i,line in enumerate(stream,1):
            if not line.strip(): continue
            event=EventBody.model_validate_json(line)
            body=json.dumps(event.model_dump(mode='json'),ensure_ascii=False).encode()
            req=Request(args.url.rstrip('/')+'/api/ingest',data=body,
                        headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json'})
            with opener.open(req,timeout=10) as response:
                result=json.load(response)
            print(json.dumps({'line':i,'origin':event.origin,'source_event_id':event.source_event_id,**result},ensure_ascii=False))


if __name__=='__main__':
    main()

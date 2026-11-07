"""Recoverable session removal. Never permanently deletes sensor recordings."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import uuid

from storage import atomic_json, read_json, utc_now


class SessionTrash:
    def __init__(self, roots, derived, location):
        self.roots = [Path(p).resolve() for p in roots]
        self.derived, self.location = Path(derived).resolve(), Path(location).resolve()
        self.lock = threading.RLock()

    def _key(self,key):
        if not isinstance(key,str) or len(key)!=24 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('会话索引无效')
        return key

    def _source(self, path):
        path = Path(path)
        resolved = path.resolve()
        # SECURITY: only configured session roots; no user-supplied arbitrary paths/symlinks.
        if path.is_symlink() or not any(resolved.is_relative_to(root) for root in self.roots):
            raise ValueError('数据集路径不在已配置的采集目录中')
        if resolved == self.location or resolved.is_relative_to(self.location):
            raise ValueError('回收区中的内容不能再次移入回收区')
        if any(p.is_symlink() for p in resolved.rglob('*')):
            raise ValueError('数据集包含符号链接，请先人工核查；本次未移动')
        return resolved

    def remove(self, path, session_id, key, display_name, reason):
        with self.lock:
            self._key(key)
            source = self._source(path)
            meta = read_json(source/'metadata.json')
            if meta['session_id'] != session_id or meta.get('status') in ('starting','recording','stopping'):
                raise ValueError('数据集仍在采集或身份已变化，请先结束并刷新列表')
            revisions = self.derived/key
            if not revisions.resolve().is_relative_to(self.derived) or revisions.is_symlink():
                raise ValueError('标签修订目录异常')
            if revisions.exists() and any(p.is_symlink() for p in revisions.rglob('*')):
                raise ValueError('标签修订包含符号链接，本次未移动')
            identity = uuid.uuid4().hex
            entry = self.location/identity
            entry.mkdir(parents=True,exist_ok=False)
            record = dict(version='session_trash_0.1',trash_id=identity,session_id=session_id,
                          key=key,display_name=display_name,source_path=str(source),
                          revision_path=str(revisions),had_revisions=revisions.exists(),
                          removed_at=utc_now(),reason=reason,status='moving')
            atomic_json(entry/'trash.json',record)
            moved = []
            try:
                # Rename is atomic on the same filesystem. Do not fall back to copy-and-delete.
                os.rename(source,entry/'raw'); moved.append((entry/'raw',source))
                if record['had_revisions']:
                    os.rename(revisions,entry/'revisions'); moved.append((entry/'revisions',revisions))
                record['status']='trashed'; atomic_json(entry/'trash.json',record)
            except BaseException:
                for current,original in reversed(moved): os.rename(current,original)
                record['status']='move_failed'; atomic_json(entry/'trash.json',record)
                raise
            return dict(trash_id=identity,session_id=session_id,display_name=display_name,recoverable=True)

    def list(self):
        with self.lock:
            items,issues = [],[]
            if not self.location.exists():return dict(items=items,issues=issues)
            for entry in self.location.iterdir():
                if not entry.is_dir() or entry.is_symlink():continue
                try:
                    record=read_json(entry/'trash.json')
                    if record['status'] in ('trashed','moving','restoring') and (entry/'raw').exists():
                        items.append({k:record.get(k) for k in ('trash_id','session_id','display_name','removed_at','reason','status')})
                except (OSError,ValueError,KeyError) as exc:issues.append(entry.name+'：'+str(exc))
            return dict(items=sorted(items,key=lambda r:r['removed_at'],reverse=True),issues=issues)

    def restore(self, identity):
        if not isinstance(identity,str) or len(identity)!=32:
            raise ValueError('回收记录编号无效')
        try: uuid.UUID(hex=identity)
        except ValueError as exc:raise ValueError('回收记录编号无效') from exc
        with self.lock:
            entry=self.location/identity
            if entry.is_symlink() or not entry.resolve().is_relative_to(self.location):
                raise ValueError('回收记录路径异常')
            record=read_json(entry/'trash.json')
            if record['trash_id']!=identity or record['status'] not in ('trashed','moving','restoring'):
                raise ValueError('回收记录已恢复或当前不可恢复')
            source=self._source(Path(record['source_path']))
            self._key(record['key'])
            revisions=Path(record['revision_path'])
            if revisions.resolve()!=self.derived/record['key']:
                raise ValueError('原标签修订路径不匹配')
            if source.exists() or (record['had_revisions'] and revisions.exists()):
                raise ValueError('原位置已有同名数据，已保留双方文件，没有覆盖')
            if not (entry/'raw').is_dir() or (record['had_revisions'] and not (entry/'revisions').is_dir()):
                raise ValueError('回收内容不完整，请保留目录并人工核查')
            record['status']='restoring';atomic_json(entry/'trash.json',record)
            moved=[]
            try:
                source.parent.mkdir(parents=True,exist_ok=True)
                os.rename(entry/'raw',source);moved.append((source,entry/'raw'))
                if record['had_revisions']:
                    revisions.parent.mkdir(parents=True,exist_ok=True)
                    os.rename(entry/'revisions',revisions);moved.append((revisions,entry/'revisions'))
                record.update(status='restored',restored_at=utc_now());atomic_json(entry/'trash.json',record)
            except BaseException:
                for current,original in reversed(moved):os.rename(current,original)
                record['status']='trashed';atomic_json(entry/'trash.json',record)
                raise
            return dict(restored=True,session_id=record['session_id'])

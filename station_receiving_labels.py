"""Typed bounded label artifacts for an explicitly restored original stock plan.

Rendering has no financial or provider side effects. Current remote authority
and exact local evidence are checked again after the private worker finishes.
"""
import copy
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
import uuid

from hosted_labels import fingerprint
from station_cloud import CloudError,ORIGIN
from station_store import StationError,now
from station_receiving_label_plan import build_plan,part,MAX_ARTIFACT_BYTES,LabelPlanError
from station_receiving_label_render import RENDERER_VERSION,FONT_HASHES,LabelRenderError
import station_receiving_label_publication as publications
import station_receiving_label_worker as worker


def _error(message='The original-stock label preview changed. Preview it again; no inventory or credit changed.',
           code='ORIGINAL_STOCK_LABELS_CHANGED',status=409):
    return CloudError(message,status=status,code=code)


def _integer(value,low=0,high=2**63-1):
    if type(value) is not int or not low<=value<=high:raise _error(code='ORIGINAL_STOCK_LABELS_INPUT',status=400)
    return value


def _uuid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise _error(code='ORIGINAL_STOCK_LABELS_INPUT',status=400) from None
    return value


def _safe_path(path):
    """Reject reparse/symlink components before using an existing local path."""
    path=Path(path)
    if not path.is_absolute():raise _error('The label output folder must be an absolute local folder.')
    for value in (path,*path.parents):
        try:info=value.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise _error('The label output folder needs a direct local path without redirected links.')
    return path.resolve()


def _typed_plan_errors(method):
    @wraps(method)
    def invoke(*args,**kwargs):
        try:return method(*args,**kwargs)
        except (LabelPlanError,LabelRenderError) as error:
            raise _error(str(error),code=error.code,status=400) from None
    return invoke


class OriginalStockLabels:
    def __init__(self,service,*,clock=None):
        self.service=service;self.clock=clock or time.monotonic
        self._previews={};self._lock=threading.RLock()

    @property
    def cloud(self):return self.service.cloud

    @property
    def restoration(self):return self.service.receiving_restoration

    def clear(self):
        with self._lock:self._previews.clear()

    def _time(self):
        value=self.clock()
        if type(value) not in (int,float) or not math.isfinite(value):raise _error()
        return value

    def _purge(self):
        current=self._time();self._previews={key:value for key,value in self._previews.items() if value['expires']>current}
        return current

    def _input(self,data,expected,kind):
        base={'sessionId','stockReceiptId','revision'}
        extra={'preview':{'copyOffset','copyCount','jobId','partIndex','jobCreatedAt',
                          'expectedPublicationBasisHash','resumeUnpublishedJobConfirmed'},
               'generate':{'jobId','partIndex','publicationKey','confirmationHash','expectedPublicationBasisHash',
                           'generateLabelsConfirmed','labelTextClippingConfirmed'},
               'list':{'jobId','offset','limit'},'status':{'publicationKey'}}[kind]
        allowed=extra|({'sessionId'} if kind=='status' else base)
        if (type(data) is not dict or set(data)-allowed or type(expected) is not str or not 1<=len(expected)<=200
            or data.get('sessionId',expected)!=expected):raise _error(code='ORIGINAL_STOCK_LABELS_INPUT',status=400)
        if kind!='status':
            ident=data.get('stockReceiptId')
            if type(ident) is not str or not ident.startswith('R-') or len(ident)>102:raise _error(status=400)
            _integer(data.get('revision'),1)
        for name in ('jobId','publicationKey'):
            if name in data:_uuid(data[name])
        if kind=='preview' and any(name in data for name in ('jobCreatedAt','expectedPublicationBasisHash','resumeUnpublishedJobConfirmed')):
            try:
                if data.get('resumeUnpublishedJobConfirmed') is not True:raise ValueError()
                _uuid(data.get('jobId'));publications._stamp(data.get('jobCreatedAt'))
                publications._hash(data.get('expectedPublicationBasisHash'))
                _integer(data.get('copyOffset'));_integer(data.get('copyCount'),1,1000000)
            except (ValueError,TypeError,AttributeError):raise _error(code='ORIGINAL_STOCK_LABELS_INPUT',status=400) from None
        if kind=='generate':
            _uuid(data.get('jobId'));_uuid(data.get('publicationKey'));_integer(data.get('partIndex'))
            if (data.get('generateLabelsConfirmed') is not True
                or any(type(data.get(name)) is not str or len(data[name])!=64 or any(c not in '0123456789abcdef' for c in data[name])
                       for name in ('confirmationHash','expectedPublicationBasisHash'))
                or 'labelTextClippingConfirmed' in data and type(data['labelTextClippingConfirmed']) is not bool):raise _error(status=400)
        if kind=='status':_uuid(data.get('publicationKey'))
        if kind=='list':_integer(data.get('offset',0));_integer(data.get('limit',100),1,100)

    def _context(self,ident,revision,expected):
        if revision is None:revision=self.restoration._raw(ident)['receipt']['revision']
        return self.restoration.output_context(ident,revision,expected)

    def _facts(self,context):
        return fingerprint({'child':context['child']['hash'],'financial':context['financial']['hash'],
            'link':context['registry']['link'],'snapshot':context['snapshot'],'role':context['bundle']['role'],
            'proofHash':context['proof']['proofHash'],'stateBasisHash':context['proof']['stateBasisHash']})

    def _basis(self,context,plan):
        proof=context['proof']
        return fingerprint({'domain':'stock-and-sleeve:original-stock-label-publication:v1',
            'serviceOrigin':ORIGIN,'tenantId':context['bundle']['tenantId'],
            'stockReceiptId':context['child']['receipt']['id'],'receivingId':proof['receivingId'],
            'serverRevision':proof['serverRevision'],'planHash':proof['planHash'],'proofHash':proof['proofHash'],
            'restorationSourceHash':proof['restorationSourceHash'],'provider':proof['provider'],
            'renderPlanHash':plan['renderPlanHash'],'rendererVersion':RENDERER_VERSION,'fonts':FONT_HASHES})

    def _job(self,context,plan,existing=None):
        proof=context['proof'];basis=self._basis(context,plan)
        if existing:
            job=self._job_record(existing)
            rebuilt=build_plan(context['child']['receipt']['cards'],proof['labelProfile'],
                               job['plan']['copyOffset'],job['plan']['copyCount'])
            if (fingerprint(rebuilt)!=fingerprint(job['plan']) or job['publicationBasisHash']!=self._basis(context,rebuilt)
                or job['stockReceiptId']!=context['child']['receipt']['id']):raise _error()
            return job
        return {'version':1,'serviceOrigin':ORIGIN,'tenantId':context['bundle']['tenantId'],
            'jobId':str(uuid.uuid4()),'stockReceiptId':context['child']['receipt']['id'],
            'receivingId':proof['receivingId'],'serverRevision':proof['serverRevision'],
            'planHash':proof['planHash'],'proofHash':proof['proofHash'],'publicationBasisHash':basis,
            'plan':plan,'createdAt':now()}

    def _job_record(self,row):
        try:
            job=json.loads(row['job_body']);publications._validate_job(job)
            fields={'service_origin':'serviceOrigin','tenant_id':'tenantId','job_id':'jobId',
                'receipt_id':'stockReceiptId','receiving_id':'receivingId','server_revision':'serverRevision',
                'plan_hash':'planHash','proof_hash':'proofHash','publication_basis_hash':'publicationBasisHash',
                'created_at':'createdAt'}
            if (publications.canonical(job)!=row['job_body']
                or any(type(row[key]) is not type(job[field]) or row[key]!=job[field] for key,field in fields.items())
                or type(row['part_count']) is not int or row['part_count']!=job['plan']['partCount']
                or type(row['published_parts']) is not int or not 1<=row['published_parts']<=row['part_count']
                or type(row['revision']) is not int or row['revision']<1
                or row['state']!=('published' if row['published_parts']==row['part_count'] else 'planned')):raise ValueError()
            return job
        except (ValueError,TypeError,KeyError,AttributeError):
            raise _error('The saved label job metadata changed. Keep its original reference for owner review.') from None

    def _directories(self):
        output=_safe_path(Path(self.service.core.OUTPUT_DIR))
        output.mkdir(parents=True,exist_ok=True);output=_safe_path(output)
        stage=output/'.ss-stock-label-staging'
        stage.mkdir(exist_ok=True);stage=_safe_path(stage)
        if stage.parent!=output:raise _error()
        return output,stage

    def _stop_event(self):
        event=getattr(self.service,'stop_event',None)
        if event is not None and event.is_set():
            raise _error('The app is stopping. Reopen the same stock record before preparing labels.',
                         code='ORIGINAL_STOCK_LABELS_STOPPING',status=503)
        return event

    def _authorize_final(self,context,expected,publication_rows=()):
        sources=self.restoration._expected_sources(context,include_child=True)
        link=context['registry']['link'];snapshot=context['snapshot'];bundle=context['bundle'];proof=context['proof']
        link_expected=tuple(link[key] for key in publications.LINK_FIELDS)
        snapshot_expected=tuple(snapshot[key] for key in publications.SNAPSHOT_FIELDS)
        pairs=tuple((tuple(job[key] for key in publications.JOB_FIELDS),
                     tuple(row[key] for key in publications.PUBLICATION_FIELDS)) for job,row in publication_rows)
        self.restoration._reauthorize(context,expected)
        with self.cloud._session_guard(),self.service.lock:
            saved=self.cloud.assert_recovery_session_unlocked(expected,bundle['tenantId'],bundle['store'])
            if saved.get('role') not in ('owner','manager'):raise _error(status=403)
            # One coherent read snapshot includes the source and committed pair.
            # Only exact prepared scalar/raw-byte comparisons run in this guard.
            with self.service.store.db() as db:
                db.execute('BEGIN')
                for ident,raw,deleted in sources:
                    row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(ident,)).fetchone()
                    if not row or tuple(row)!=(raw,deleted):raise _error()
                key=(ORIGIN,bundle['tenantId'],proof['receivingId'])
                actual=db.execute('SELECT '+','.join(publications.LINK_FIELDS)+
                    ' FROM receiving_recovery_links WHERE service_origin=? AND tenant_id=? AND receiving_id=?',key).fetchone()
                if not actual or tuple(actual)!=link_expected:raise _error()
                actual=db.execute('SELECT '+','.join(publications.SNAPSHOT_FIELDS)+
                    ' FROM receiving_recovery_snapshots WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?',
                    key+(proof['serverRevision'],)).fetchone()
                if not actual or tuple(actual)!=snapshot_expected:raise _error()
                for job_expected,pub_expected in pairs:
                    job_id=job_expected[publications.JOB_FIELDS.index('job_id')]
                    pub_id=pub_expected[publications.PUBLICATION_FIELDS.index('publication_id')]
                    actual=db.execute('SELECT '+','.join(publications.JOB_FIELDS)+
                        ' FROM receiving_label_jobs WHERE service_origin=? AND tenant_id=? AND job_id=?',
                        (ORIGIN,bundle['tenantId'],job_id)).fetchone()
                    if not actual or tuple(actual)!=job_expected:raise _error()
                    actual=db.execute('SELECT '+','.join(publications.PUBLICATION_FIELDS)+
                        ' FROM receiving_label_publications WHERE service_origin=? AND tenant_id=? AND publication_id=?',
                        (ORIGIN,bundle['tenantId'],pub_id)).fetchone()
                    if not actual or tuple(actual)!=pub_expected:raise _error()

    @_typed_plan_errors
    def preview(self,data,expected):
        self._input(data,expected,'preview')
        context=self._context(data['stockReceiptId'],data['revision'],expected)
        cards=context['child']['receipt']['cards'];proof=context['proof'];tenant=context['bundle']['tenantId']
        existing=publications.read_job(self.service.store,tenant,data['jobId']) if 'jobId' in data else None
        resume=data.get('resumeUnpublishedJobConfirmed') is True
        if 'jobId' in data and (not existing and not resume
            or existing and not resume and ('copyOffset' in data or 'copyCount' in data)):raise _error()
        plan=build_plan(cards,proof['labelProfile'],data.get('copyOffset',0),data.get('copyCount'))
        job=self._job(context,plan,existing)
        if resume:
            if (job['publicationBasisHash']!=data['expectedPublicationBasisHash']
                or job['plan']['copyOffset']!=data['copyOffset'] or job['plan']['copyCount']!=data['copyCount']):raise _error()
            if existing:
                if job['createdAt']!=data['jobCreatedAt']:raise _error()
            else:
                job['jobId']=data['jobId'];job['createdAt']=data['jobCreatedAt']
                publications._validate_job(job)
        selected=part(job['plan'],data.get('partIndex',0))
        with self.service.store.db() as db:
            row=db.execute('SELECT '+','.join(publications.PUBLICATION_FIELDS)+
                ' FROM receiving_label_publications WHERE service_origin=? AND tenant_id=? AND job_id=? AND part_index=?',
                (ORIGIN,tenant,job['jobId'],selected['partIndex'])).fetchone()
        occupied=dict(row) if row else None
        if occupied:
            if not existing:raise _error()
            self._validate_publication(occupied,existing,context)
        _,stage=self._directories()
        metadata=worker.preflight_pair(cards,proof['labelProfile'],selected['renderCounts'],stage,
                                       cancel_event=self._stop_event())
        refreshed=self._context(data['stockReceiptId'],data['revision'],expected)
        if self._facts(refreshed)!=self._facts(context):raise _error()
        confirmation=fingerprint({'random':secrets.token_hex(32),'basis':job['publicationBasisHash'],
                                  'part':selected['partHash'],'facts':self._facts(context)})
        value={'job':job,'partIndex':selected['partIndex'],'partHash':selected['partHash'],
            'factsHash':self._facts(context),'renderBasisHash':metadata['renderBasisHash'],
            'clipped':bool(metadata['overflows']),'sessionId':expected,'stockReceiptId':data['stockReceiptId']}
        self.restoration._reauthorize(refreshed,expected)
        with self.cloud._session_guard(),self.service.lock:
            self.restoration._final(refreshed,expected)
            with self._lock:
                current=self._purge()
                while len(self._previews)>=16:self._previews.pop(next(iter(self._previews)))
                self._previews[confirmation]={**value,'expires':current+300}
        return {'preview':{'jobId':job['jobId'],'jobCreatedAt':job['createdAt'],'stockReceiptId':data['stockReceiptId'],
            'publicationBasisHash':job['publicationBasisHash'],'receivingId':proof['receivingId'],
            'serverRevision':proof['serverRevision'],'planHash':proof['planHash'],'proofHash':proof['proofHash'],
            'labelProfile':proof['labelProfile'],'totalCopies':job['plan']['totalCopies'],
            'copyOffset':job['plan']['copyOffset'],'copyCount':job['plan']['copyCount'],
            'partIndex':selected['partIndex'],'partCopyOffset':selected['copyOffset'],'partCopyCount':selected['copyCount'],
            'partSize':job['plan']['partSize'],'partCount':job['plan']['partCount'],
            'existingPublicationKey':occupied['publication_key'] if occupied else None,
            'existingPublicationId':occupied['publication_id'] if occupied else None,
            'textReadiness':metadata,'providerActionPerformed':False},'confirmationHash':confirmation,'expiresIn':300}

    def _cached(self,data,expected):
        with self._lock:
            self._purge();value=copy.deepcopy(self._previews.get(data['confirmationHash']))
        if (not value or value['sessionId']!=expected or value['stockReceiptId']!=data['stockReceiptId']
            or value['job']['jobId']!=data['jobId'] or value['partIndex']!=data['partIndex']
            or value['job']['publicationBasisHash']!=data['expectedPublicationBasisHash']):raise _error(code='ORIGINAL_STOCK_LABELS_PREVIEW_EXPIRED')
        return value

    def _validate_publication(self,row,job_row,context):
        if not row or not job_row:raise _error('The saved label pair is unavailable. Keep its publication reference for owner review.')
        job=self._job_record(job_row)
        try:
            manifest=json.loads(row['manifest_body'])
            if publications.canonical(manifest)!=row['manifest_body']:raise ValueError()
        except (ValueError,TypeError,KeyError):raise _error('The saved label publication metadata changed. Keep its original reference for owner review.') from None
        if publications.request_fingerprint(job,manifest)!=row['request_fingerprint']:raise _error()
        mapping={'service_origin':'serviceOrigin','tenant_id':'tenantId','publication_id':'publicationId',
            'publication_key':'publicationKey','job_id':'jobId','part_index':'partIndex','receipt_id':'stockReceiptId',
            'receiving_id':'receivingId','server_revision':'serverRevision','plan_hash':'planHash',
            'proof_hash':'proofHash','publication_basis_hash':'publicationBasisHash'}
        if any(type(row[key]) is not type(manifest[field]) or row[key]!=manifest[field] for key,field in mapping.items()):raise _error()
        current=self._job(context,job['plan'],job_row)
        if fingerprint(current)!=fingerprint(job):raise _error()
        selected=part(job['plan'],manifest['partIndex'])
        if (selected['partHash']!=manifest['partHash'] or manifest['rendererVersion']!=RENDERER_VERSION
            or fingerprint(manifest['fontHashes'])!=fingerprint(FONT_HASHES)):raise _error()
        return job,manifest

    def _file(self,metadata,kind,*,content=False):
        root,_=self._directories();name=metadata['basename'];path=root/name
        if path.parent!=root or name!=Path(name).name:raise _error()
        _safe_path(path);limit=metadata['bytes']
        if type(limit) is not int or not 1<=limit<=MAX_ARTIFACT_BYTES:raise _error()
        pieces=[];digest=hashlib.sha256();total=0
        try:
            with path.open('rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_size!=limit:raise ValueError()
                while chunk:=stream.read(min(1024*1024,limit-total+1)):
                    total+=len(chunk)
                    if total>limit:raise ValueError()
                    digest.update(chunk)
                    if content:pieces.append(chunk)
            if total!=limit or digest.hexdigest()!=metadata['sha256']:raise ValueError()
        except (OSError,ValueError):raise _error('A saved label file is missing or changed. Keep the original publication for owner review; it was not regenerated.',code='ORIGINAL_STOCK_LABELS_FILE_INVALID') from None
        return b''.join(pieces) if content else None

    def _response(self,context,manifest,reused):
        result=copy.deepcopy(manifest)
        for kind in ('pdf','zpl'):
            result['files'][kind].pop('basename',None)
            result['files'][kind]['downloadPath']='/api/cloud/receiving-label-publications/'+manifest['publicationId']+'/'+kind
        return {'publication':result,'receiptSummary':{'id':context['child']['receipt']['id'],
            'revision':context['child']['receipt']['revision']},'reused':reused,'providerActionPerformed':False}

    def _existing(self,row,expected,*,format=None):
        context=self._context(row['receipt_id'],None,expected)
        job_row=publications.read_job(self.service.store,context['bundle']['tenantId'],row['job_id'])
        job,manifest=self._validate_publication(row,job_row,context);buffer=None
        for kind in ('pdf','zpl'):
            value=self._file(manifest['files'][kind],kind,content=kind==format)
            if kind==format:buffer=value
        refreshed=self._context(row['receipt_id'],context['child']['receipt']['revision'],expected)
        if self._facts(context)!=self._facts(refreshed):raise _error()
        self._authorize_final(refreshed,expected,((job_row,row),))
        return refreshed,manifest,buffer

    def _authority(self,expected):
        authority=self.cloud.record_authority(expected_session_id=expected)
        if authority.get('role') not in ('owner','manager'):raise _error(status=403)
        return authority

    def status(self,data,expected):
        self._input(data,expected,'status');authority=self._authority(expected)
        row=publications.read_publication(self.service.store,authority['tenantId'],publication_key=data['publicationKey'])
        if not row:return {'found':False,'providerActionPerformed':False}
        context,manifest,_=self._existing(row,expected)
        return {'found':True,**self._response(context,manifest,True)}

    def download(self,publication_id,format,expected):
        _uuid(publication_id)
        if format not in ('pdf','zpl'):raise _error(status=400)
        authority=self._authority(expected)
        row=publications.read_publication(self.service.store,authority['tenantId'],publication_id=publication_id)
        if not row:raise _error('The original-stock publication was not found.',status=404)
        _,manifest,buffer=self._existing(row,expected,format=format)
        return {'content':buffer,'filename':manifest['files'][format]['basename'],
                'contentType':'application/pdf' if format=='pdf' else 'application/octet-stream'}

    def list(self,data,expected):
        self._input(data,expected,'list');context=self._context(data['stockReceiptId'],data['revision'],expected)
        options={'receipt_id':data['stockReceiptId'],'limit':data.get('limit',100),'offset':data.get('offset',0)}
        if 'jobId' in data:options['job_id']=data['jobId']
        rows=publications.list_publications(self.service.store,context['bundle']['tenantId'],**options)
        result=[];checks=[]
        for row in rows:
            job_row=publications.read_job(self.service.store,context['bundle']['tenantId'],row['job_id'])
            _,manifest=self._validate_publication(row,job_row,context)
            result.append(self._response(context,manifest,True)['publication'])
            checks.append((job_row,row))
        self._authorize_final(context,expected,checks)
        return {'publications':result,'offset':options['offset'],'limit':options['limit'],
                'filesVerified':False,'providerActionPerformed':False}

    def _cleanup_uncommitted(self,tenant,key,publication_id,moved):
        # A failed response/commit may already have committed. A failed readback
        # deliberately preserves hidden complete orphans for owner recovery.
        try:
            by_key=publications.read_publication(self.service.store,tenant,publication_key=key)
            by_id=publications.read_publication(self.service.store,tenant,publication_id=publication_id)
            if by_id or by_key and by_key['publication_id']==publication_id:return
            for path,metadata in moved:
                if path.name==metadata['basename']:
                    self._file(metadata,path.suffix[1:])
                    path.unlink()
        except (OSError,sqlite3.Error,StationError,ValueError,KeyError,TypeError):pass

    @_typed_plan_errors
    def generate(self,data,expected):
        self._input(data,expected,'generate');authority=self._authority(expected)
        prior=publications.read_publication(self.service.store,authority['tenantId'],publication_key=data['publicationKey'])
        if prior:
            context,manifest,_=self._existing(prior,expected)
            if (manifest['stockReceiptId']!=data['stockReceiptId'] or manifest['jobId']!=data['jobId']
                or manifest['partIndex']!=data['partIndex'] or manifest['publicationBasisHash']!=data['expectedPublicationBasisHash']):raise _error()
            return self._response(context,manifest,True)
        cached=self._cached(data,expected);context=self._context(data['stockReceiptId'],data['revision'],expected)
        if self._facts(context)!=cached['factsHash']:raise _error()
        if cached['clipped'] and data.get('labelTextClippingConfirmed') is not True:
            raise _error('Confirm the displayed shortened label text before generating this part.',code='ORIGINAL_STOCK_LABELS_TEXT_CONFIRMATION',status=400)
        job=cached['job'];selected=part(job['plan'],data['partIndex']);output,stage_root=self._directories()
        staged=None;moved=[];publication_id=str(uuid.uuid4());tenant=context['bundle']['tenantId']
        try:
            staged=worker.run_pair(context['child']['receipt']['cards'],context['proof']['labelProfile'],selected['renderCounts'],stage_root,
                                   cancel_event=self._stop_event())
            metadata=staged['metadata']
            if metadata['renderBasisHash']!=cached['renderBasisHash']:raise _error()
            current=self._context(data['stockReceiptId'],data['revision'],expected)
            if self._facts(current)!=cached['factsHash']:raise _error()
            with self._lock:
                self._purge()
                if data['confirmationHash'] not in self._previews:raise _error(code='ORIGINAL_STOCK_LABELS_PREVIEW_EXPIRED')
            manifest={'version':1,'serviceOrigin':ORIGIN,'tenantId':tenant,'publicationId':publication_id,
                'publicationKey':data['publicationKey'],'jobId':job['jobId'],'partIndex':selected['partIndex'],
                'stockReceiptId':data['stockReceiptId'],'receivingId':job['receivingId'],'serverRevision':job['serverRevision'],
                'planHash':job['planHash'],'proofHash':job['proofHash'],'publicationBasisHash':job['publicationBasisHash'],
                'partHash':selected['partHash'],'copyOffset':selected['copyOffset'],'copyCount':selected['copyCount'],
                'totalCopies':job['plan']['totalCopies'],'rendererVersion':metadata['rendererVersion'],
                'fontHashes':metadata['fonts'],'renderBasisHash':metadata['renderBasisHash'],
                'files':{kind:{**metadata[kind],'basename':'ss-stock-label-'+publication_id+'.'+kind} for kind in ('pdf','zpl')}}
            expected_job=publications.read_job(self.service.store,tenant,job['jobId'])
            prepared=publications.prepare(self.service.store,child=current['child']['receipt'],
                expected_sources=self.restoration._expected_sources(current,include_child=True),
                link=current['registry']['link'],snapshot=current['snapshot'],job=job,manifest=manifest,expected_job=expected_job)
            def publish():
                for kind in ('pdf','zpl'):
                    target=output/manifest['files'][kind]['basename']
                    # Windows rename refuses an existing destination. Other
                    # systems use exclusive hard links, never replace().
                    if os.name=='nt':os.rename(staged[kind],target)
                    else:os.link(staged[kind],target);staged[kind].unlink()
                    moved.append((target,manifest['files'][kind]))
            self.restoration._reauthorize(current,expected)
            with self.cloud._session_guard(),self.service.lock:
                saved=self.cloud.assert_recovery_session_unlocked(expected,tenant,current['bundle']['store'])
                if saved.get('role') not in ('owner','manager'):raise _error(status=403)
                self._stop_event()
                with self._lock:
                    self._purge()
                    if data['confirmationHash'] not in self._previews:raise _error(code='ORIGINAL_STOCK_LABELS_PREVIEW_EXPIRED')
                result=publications.commit(self.service.store,prepared,publish)
            # A later authorization loss hides the response but preserves the
            # committed complete pair; publication-key lookup recovers it.
            row=publications.read_publication(self.service.store,tenant,publication_key=data['publicationKey'])
            refreshed,verified,_=self._existing(row,expected)
            return self._response(refreshed,verified,result['reused'])
        except BaseException:
            self._cleanup_uncommitted(tenant,data['publicationKey'],publication_id,moved)
            raise
        finally:
            if staged is not None:worker.cleanup_attempt(staged)

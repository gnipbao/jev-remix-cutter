"""Single-JSON CLI; costly transfers require explicit per-command authorization."""
from __future__ import annotations
import argparse
import functools
import http.server
import json
import sys
from pathlib import Path
from .common import ROOT, RemixError, read_json, run_dir

class Parser(argparse.ArgumentParser):
    def error(self,message):raise RemixError(message,2)

def common(parser):
    parser.add_argument('--run',default=argparse.SUPPRESS)
    parser.add_argument('--json',action='store_true',default=argparse.SUPPRESS)

def parser():
    p=Parser(prog='remix',description='Evidence-backed CPU remix workflow; see README for current capability limits')
    common(p);p.set_defaults(run=None,json=False)
    subs=p.add_subparsers(dest='command',required=True,parser_class=Parser)
    def sub(name):
        x=subs.add_parser(name);common(x);return x
    d=sub('doctor');d.add_argument('--tier',choices=['core','quality'],default='core')
    i=sub('init');i.add_argument('--fps',type=int,default=30);i.add_argument('--format',choices=['9:16','16:9','1:1'],default='9:16');i.add_argument('--target-s',type=float,default=30);i.add_argument('--pack',default='film-epic');i.add_argument('--mode',choices=['music','narration'],default='music')
    c=sub('configure');c.add_argument('--patch',required=True)
    x=sub('ingest');x.add_argument('--src',nargs='+',default=[]);x.add_argument('--license',choices=['own','licensed','unknown'],required=True);x.add_argument('--from-highlight');x.add_argument('--self-shot',action='store_true')
    x=sub('cards');g=x.add_mutually_exclusive_group(required=True);g.add_argument('--prepare',action='store_true');g.add_argument('--import',dest='import_file');g.add_argument('--provider');x.add_argument('--allow-image-upload',action='store_true')
    x=sub('music');x.add_argument('--audio',required=True);x.add_argument('--stems',action='store_true');x.add_argument('--lyrics',action='store_true')
    x=sub('music-edit');x.add_argument('--target-s',type=float,required=True);x.add_argument('--keep')
    x=sub('intent');x.add_argument('--validate',dest='intent_file',required=True)
    x=sub('slots');x.add_argument('--pack');x.add_argument('--mode',choices=['music','narration']);x.add_argument('--density');x.add_argument('--allow-low-confidence',action='store_true')
    x=sub('score');x.add_argument('--prepare',action='store_true');x.add_argument('--mock',action='store_true');x.add_argument('--allow-text-upload',action='store_true');x.add_argument('--workers',type=int,default=4);x.add_argument('--model',default='jev-1.13.0');x.add_argument('--key-file');x.add_argument('--k',type=int);x.add_argument('--pairs',type=int)
    x=sub('cast');x.add_argument('--beam',type=int)
    x=sub('assemble');x.add_argument('--only')
    x=sub('review');x.add_argument('--serve',action='store_true');x.add_argument('--port',type=int,default=8765)
    x=sub('frames');x.add_argument('--slots');x.add_argument('--times')
    x=sub('swap');x.add_argument('--slot',required=True);g=x.add_mutually_exclusive_group();g.add_argument('--rank',type=int,default=1);g.add_argument('--shot')
    x=sub('lock');x.add_argument('--slot',required=True);x.add_argument('--unlock',action='store_true')
    x=sub('render');x.add_argument('--preview',action='store_true');x.add_argument('--allow-mock',action='store_true');x.add_argument('--confirm-unknown-license',action='store_true')
    sub('qa')
    x=sub('feedback');x.add_argument('--file',required=True)
    x=sub('variants');x.add_argument('--table',required=True)
    x=sub('export');x.add_argument('--format',choices=['edl','otio'],default='edl');x.add_argument('--output')
    x=sub('explain');x.add_argument('--slot')
    x=sub('export-profile');x.add_argument('--name',required=True);x.add_argument('--output')
    x=sub('originality');x.add_argument('--profile',default='default')
    x=sub('license-catalog');x.add_argument('--import',dest='catalog_file',required=True);x.add_argument('--map',action='append',default=[])
    x=sub('consent');x.add_argument('--config-dir');s=x.add_subparsers(dest='action',required=True,parser_class=Parser)
    for name in ['add','list','revoke','verify']:
        z=s.add_parser(name);common(z);z.add_argument('--config-dir',default=argparse.SUPPRESS)
        if name=='add':
            z.add_argument('--name',required=True);z.add_argument('--doc',required=True);z.add_argument('--refs',nargs='+',required=True);z.add_argument('--roles',required=True);z.add_argument('--scope',required=True);z.add_argument('--relation',choices=['self','employee','model','other'],default='other')
        if name=='revoke':z.add_argument('--id',required=True)
    x=sub('faces');s=x.add_subparsers(dest='action',required=True,parser_class=Parser)
    for name in ['scan','match','plan','prepare','apply','qa']:
        z=s.add_parser(name);common(z);z.add_argument('--config-dir')
        if name=='plan':
            z.add_argument('--map',action='append',default=[]);z.add_argument('--default',choices=['keep','blur','anonymize','swap'],default='keep');z.add_argument('--purpose',default='personal_creation');z.add_argument('--platform',default='local');z.add_argument('--full-frame',action='store_true')
        if name in ['prepare','apply']:z.add_argument('--backend',default='opencv-blur')
        if name=='apply':z.add_argument('--allow-image-upload',action='store_true')
    return p

def dispatch(a):
    from . import workflow
    cmd=a.command
    if cmd=='doctor':return workflow.doctor(a.tier)
    if cmd=='consent':
        from . import face
        if a.action=='add':return face.consent_add(a.name,a.doc,a.refs,a.roles.split(','),a.scope,a.relation,a.config_dir)
        if a.action=='list':return face.consent_list(a.config_dir)
        if a.action=='revoke':return face.consent_revoke(a.id,a.config_dir)
    if not a.run:raise RemixError('--run is required for this command')
    run=run_dir(a.run,create=cmd=='init')
    if cmd=='init':return workflow.init(run,fps=a.fps,format=a.format,target_seconds=a.target_s,pack=a.pack,mode=a.mode)
    if cmd=='configure':return workflow.configure(run,read_json(a.patch))
    if cmd in ['ingest','music','music-edit']:
        from . import media
        if cmd=='ingest':return media.ingest(run,a.src,a.license,a.from_highlight,a.self_shot)
        if cmd=='music':
            if a.stems or a.lyrics:raise RemixError('Stems and word alignment need quality/ASR backends; not enabled in this release',5)
            return media.music(run,a.audio)
        return media.music_edit(run,a.target_s,a.keep)
    if cmd=='cards':
        from . import cards
        if a.prepare:return cards.prepare(run)
        if a.import_file:return cards.import_cards(run,a.import_file)
        return cards.provider(run,a.provider,a.allow_image_upload)
    if cmd=='score':
        from .judging import score
        return score(run,prepare=a.prepare,mock=a.mock,allow_text_upload=a.allow_text_upload,workers=a.workers,model=a.model,top_k=a.k,pair_k=a.pairs,key_file=a.key_file)
    if cmd in ['intent','slots','cast','assemble','swap','lock','explain','export']:
        from . import planning
        if cmd=='intent':return planning.validate_intent(run,a.intent_file)
        if cmd=='slots':return planning.slots(run,pack=a.pack,mode=a.mode,density=a.density,allow_low_confidence=a.allow_low_confidence)
        if cmd=='cast':return planning.cast(run,beam=a.beam)
        if cmd=='assemble':
            if a.only:raise RemixError('--only is not yet supported; assembly applies the supported deterministic core',5)
            return planning.assemble(run)
        if cmd=='swap':return planning.swap(run,a.slot,alternative=a.rank,shot_id=a.shot)
        if cmd=='lock':return planning.lock(run,[a.slot],locked=not a.unlock)
        if cmd=='explain':return planning.explain(run,a.slot)
        if a.format!='edl':raise RemixError('OTIO export is not enabled; JSON EDL v2 is supported',5)
        return planning.export(run,a.output)
    if cmd=='review':
        result=workflow.review(run)
        if a.serve:
            print(json.dumps({'ok':True,**result,'url':f'http://127.0.0.1:{a.port}/review.html'},ensure_ascii=False),flush=True)
            handler=functools.partial(http.server.SimpleHTTPRequestHandler,directory=str(run))
            with http.server.ThreadingHTTPServer(('127.0.0.1',a.port),handler) as server:server.serve_forever()
        return result
    if cmd in ['frames','render','qa']:
        from . import rendering
        if cmd=='frames':return rendering.frames(run,slots=a.slots.split(',') if a.slots else None,times=[float(x) for x in a.times.split(',')] if a.times else None)
        if cmd=='qa':return rendering.qa(run)
        result=rendering.render(run,a.preview,a.allow_mock,a.confirm_unknown_license)
        rendering.qa(run)
        workflow.delivery(run,result)
        return result
    if cmd=='feedback':return workflow.feedback(run,a.file)
    if cmd=='variants':return workflow.variants(run,a.table)
    if cmd=='export-profile':return workflow.export_profile(run,a.name,a.output)
    if cmd=='originality':
        from .originality import originality
        return originality(run,a.profile)
    if cmd=='license-catalog':
        from .originality import import_catalog
        return import_catalog(run,a.catalog_file,a.map)
    if cmd=='consent':
        from .face import consent_verify
        return consent_verify(run,a.config_dir)
    if cmd=='faces':
        from . import face
        if a.action=='scan':return face.faces_scan(run)
        if a.action=='match':return face.faces_match(run,a.config_dir)
        if a.action=='plan':return face.faces_plan(run,a.map,a.default,a.purpose,a.platform,a.config_dir,a.full_frame)
        if a.action=='prepare':return face.faces_prepare(run,a.backend,a.config_dir)
        if a.action=='apply':return face.faces_apply(run,a.backend,a.allow_image_upload,a.config_dir)
        return face.faces_qa(run,a.config_dir)
    raise RemixError('Unknown command')

def main(argv=None):
    try:
        a=parser().parse_args(argv)
        result=dispatch(a)
        print(json.dumps({'ok':True,'command':a.command,'result':result},ensure_ascii=False,allow_nan=False))
        return 0
    except RemixError as e:
        print(json.dumps({'ok':False,'error':str(e),'code':e.code,'details':e.details},ensure_ascii=False,allow_nan=False))
        return e.code
    except KeyboardInterrupt:return 130
    except (KeyError,TypeError,ValueError,OSError) as e:
        print(json.dumps({'ok':False,'error':f'Invalid input or local artifact ({type(e).__name__}); inspect the input contract','code':2}))
        return 2

if __name__=='__main__':raise SystemExit(main())

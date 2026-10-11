"""Run setup, local review and reversible feedback operations."""
from __future__ import annotations
import copy
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path
from .common import ROOT, RemixError, archive, digest, now, read_json, read_jsonl, save_artifact, validate, write_json

def doctor(tier='core'):
    packages = ['numpy','cv2','soundfile','librosa','scenedetect','jsonschema','yaml']
    if tier == 'quality':
        packages += ['torch','torchvision','faster_whisper']
    deps = {x:bool(importlib.util.find_spec(x)) for x in packages}
    tools = {x:bool(shutil.which(x)) for x in ['ffmpeg','ffprobe']}
    libass = False
    if tools['ffmpeg']:
        from .common import command
        libass = b' ass ' in command(['ffmpeg','-hide_banner','-filters']).stdout
    report = {'version':1,'tier':tier,'python':sys.version.split()[0],'python_supported':sys.version_info >= (3,11),'packages':deps,'tools':tools,'libass':libass,'TYPESAFE_API_KEY':'present' if bool(os.environ.get('TYPESAFE_API_KEY')) else 'absent','capabilities':{'sense':'CPU proxy and frame features','music':'local onset grid with confidence','face':'keep / blur only; identity/age/NSFW models unavailable','gpu_backends':'not bundled'},'ok':all(deps.values()) and all(tools.values()) and libass}
    if not report['ok']:
        raise RemixError('Dependencies missing; install core extras and FFmpeg with libass',5,report)
    return report

def default_brief(fps=30,format='9:16',target_seconds=30,pack='film-epic',mode='music'):
    width,height = {'9:16':(1080,1920),'16:9':(1920,1080),'1:1':(1080,1080)}[format]
    return {'version':1,'revision':1,'created_at':now(),'fps':fps,'format':format,'width':width,'height':height,'target_seconds':target_seconds,'pack':pack,'mode':mode,'purpose':'personal_creation','platform':'local','request':'','music_license':'unknown','feedback':{'exclude':[]},'uploads':{'text':False,'images':False}}

def init(run,**kwargs):
    brief = validate('brief',default_brief(**kwargs))
    write_json(run/'brief.json',brief)
    return {'run':str(run),'brief':brief}

def merge(base,patch):
    result = copy.deepcopy(base)
    for key,value in patch.items():
        if isinstance(value,dict) and isinstance(result.get(key),dict):
            result[key]=merge(result[key],value)
        else:
            result[key]=value
    return result

def configure(run,patch):
    if not isinstance(patch,dict):
        raise RemixError('Brief patch must be a JSON object')
    if any(key in patch for key in ['version','created_at','revision']):
        raise RemixError('Patch cannot replace version, creation time or revision')
    brief=merge(read_json(run/'brief.json'),patch)
    if 'format' in patch and not any(k in patch for k in ['width','height']):
        brief['width'],brief['height']={'9:16':(1080,1920),'16:9':(1920,1080),'1:1':(1080,1080)}.get(brief['format'],(0,0))
    brief['revision'] += 1
    validate('brief',brief)
    if brief['width']%2 or brief['height']%2:
        raise RemixError('Output dimensions must be even')
    save_artifact(run,'brief.json',brief)
    return {'brief':brief,'rebuild':['slots','score','cast','assemble']}

def review(run):
    cast=read_json(run/'cast.json')
    from .originality import originality
    original = originality(run) if (run/'timeline.json').exists() else None
    sources=read_json(run/'sources.json')['items']
    shots=read_jsonl(run/'shots.jsonl')
    music=read_json(run/'music.json') if (run/'music.json').exists() else None
    render_result=read_json(run/'render-result.json') if (run/'render-result.json').exists() else None
    video=None
    if render_result:
        from .common import file_hash
        if render_result.get('timeline_hash')==digest(read_json(run/'timeline.json')):
            try: video=str(Path(render_result['path']).relative_to(run))
            except ValueError: pass
    data={'cast':cast,'sources':sources,'shots':shots,'music':music,'originality':original,'video':video}
    template=(ROOT/'assets'/'review.html').read_text()
    encoded=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('&','\\u0026')
    (run/'review.html').write_text(template.replace('__REMIX_DATA__',encoded),encoding='utf-8')
    return {'path':str(run/'review.html'),'mock':cast.get('mock',False)}

def feedback(run,file):
    data=validate('feedback',read_json(file))
    cast=read_json(run/'cast.json')
    slots={x['slot']:x for x in cast['items']}
    for action in data['actions']:
        if action['slot'] not in slots:
            raise RemixError('Feedback references an unknown slot')
        if action['type']=='swap':
            choices=slots[action['slot']].get('alternatives',[])
            if 'shot_id' in action:
                if not any(x['shot_id']==action['shot_id'] for x in choices):
                    raise RemixError('Feedback replacement is not a scored alternative')
            elif not 1 <= action.get('rank',1) <= len(choices):
                raise RemixError('Feedback alternative rank unavailable')
    from .planning import swap,lock
    excludes=[]
    for action in data['actions']:
        if action['type']=='swap':swap(run,action['slot'],alternative=action.get('rank',1),shot_id=action.get('shot_id'))
        elif action['type']=='lock':lock(run,[action['slot']],locked=action.get('locked',True))
        else:excludes.append(slots[action['slot']]['shot_id'])
    if excludes:
        brief=read_json(run/'brief.json')
        values=sorted(set(brief.get('feedback',{}).get('exclude',[])+excludes))
        configure(run,{'feedback':{'exclude':values}})
    return {'applied':len(data['actions']),'rebuild':['slots','score','cast','assemble'] if excludes else ['assemble']}

def variants(run,table):
    if (run/'face-plan.json').is_file() and any(x.get('mode') != 'keep' for x in read_json(run/'face-plan.json').get('items',[])):
        raise RemixError('A face-processed run needs a new scan and processing for every new timeline; automatic variants are disabled',4)
    data=read_json(table)
    rows=data.get('items',[]) if isinstance(data,dict) else data
    if not isinstance(rows,list) or not 1 <= len(rows) <= 10:
        raise RemixError('A/B variants require 1–10 items')
    from .planning import swap,assemble
    names=[]
    for row in rows:
        if not isinstance(row,dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}',row.get('name','')) or row['name'] in names:
            raise RemixError('Variants need distinct safe names')
        if set(row)-{'name','swaps'}:
            raise RemixError('This version supports hook/candidate A/B swaps; music/rhythm variants require separate new runs',5)
        names.append(row['name'])
    outputs=[]
    for row in rows:
        target=run/'variants'/row['name']
        if target.exists():raise RemixError('Variant already exists; choose a new name')
        shutil.copytree(run,target,ignore=shutil.ignore_patterns('variants','renders','history','face-*','faces.json','face-audit.jsonl','review.html','render-result.json'))
        for selection in row.get('swaps',[]):
            swap(target,selection['slot'],alternative=selection.get('rank',1),shot_id=selection.get('shot_id'))
        assemble(target)
        write_json(target/'variant-purpose.json',{'purpose':'A/B test only','parent_run':str(run),'name':row['name']})
        outputs.append({'name':row['name'],'run':str(target)})
    return {'purpose':'变体用于测试','variants':outputs,'next':'review then render each run'}

def export_profile(run,name,output=None):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}',name):raise RemixError('Use a safe profile name')
    brief=read_json(run/'brief.json')
    profile={'version':1,'name':name,'preferences':{k:brief[k] for k in ['pack','mode','fps','format','target_seconds']}}
    path=Path(output).expanduser() if output else run/'profiles'/(name+'.json')
    if path.exists():raise RemixError('Profile already exists; choose a new name')
    write_json(path,profile)
    return {'path':str(path),'profile':profile}

def delivery(run,result):
    from .originality import originality
    timeline=read_json(run/'timeline.json')
    used={x['src']['source'] for x in timeline['items']}
    license_state={x['id']:x['license'] for x in read_json(run/'sources.json')['items'] if x['id'] in used}
    license_state.update(music=timeline.get('audio',{}).get('license','not used'),narration=timeline.get('audio',{}).get('narration',{}).get('license','not used'))
    report={'version':1,'created_at':now(),'render':result,'originality':originality(run),'qa':read_json(run/'qa.json') if (run/'qa.json').exists() else None,'gaps':read_json(run/'gaps.json') if (run/'gaps.json').exists() else None,'face_audit':str(run/'face-audit.jsonl') if (run/'face-audit.jsonl').exists() else None,'mock':result.get('mock',False),'licenses':license_state,'evidence':{'mock':result.get('mock',False),'human_quality_evaluated':False}}
    write_json(run/'delivery.json',report)
    if (run/'cast.json').exists():
        review(run)
    return report

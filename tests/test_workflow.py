from __future__ import annotations
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from jev_remix.cards import verbalize, import_cards, prepare
from jev_remix.cli import main
from jev_remix.common import RemixError, ensure_snapshot, snapshot, read_json, write_json, write_jsonl
from jev_remix.workflow import configure, default_brief, init, review

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        init(self.root,target_seconds=8,format='16:9')
    def tearDown(self):self.tmp.cleanup()
    def test_cli_json_single_object_and_exit_codes(self):
        for args,code in [(['consent','add','--name','Synthetic','--doc','not-real.pdf','--refs','not-real.png','--roles','subject','--scope','scope.json','--json'],4),(['configure','--json'],2),(['slots','--run',str(self.root),'--json'],2)]:
            with patch('sys.stdin.isatty',return_value=False),patch('sys.stdout',new_callable=io.StringIO) as out:
                self.assertEqual(main(args),code)
                data=json.loads(out.getvalue());self.assertFalse(data['ok'])
                self.assertEqual(data['code'],code)
    def test_configuration_history_and_snapshot_staleness(self):
        old=snapshot(self.root,['brief.json']);configure(self.root,{'format':'1:1','music_license':'own'})
        b=read_json(self.root/'brief.json');self.assertEqual((b['width'],b['height']),(1080,1080))
        self.assertEqual(b['revision'],2);self.assertEqual(len(list((self.root/'history').iterdir())),1)
        with self.assertRaises(RemixError) as err:ensure_snapshot(self.root,old)
        self.assertEqual(err.exception.code,3)
        with self.assertRaises(RemixError):configure(self.root,{'fps':0})
        self.assertEqual(read_json(self.root/'brief.json')['fps'],30)
    def test_card_requires_own_contact_evidence(self):
        contact=self.root/'contact'/'s1.jpg';contact.parent.mkdir();contact.write_bytes(b'fixture')
        other=self.root/'contact'/'s2.jpg';other.write_bytes(b'other')
        write_jsonl(self.root/'shots.jsonl',[{'shot_id':'s1','source':'src01','start_us':0,'end_us':1000000,'contact':'contact/s1.jpg'}])
        manifest=prepare(self.root);self.assertEqual(manifest['image_count'],1)
        card={'shot_id':'s1','words':{'画面':'observed fixture'},'provenance':{'mode':'agent','frames':['contact/s2.jpg']}}
        incoming=self.root/'incoming.jsonl';write_jsonl(incoming,[card])
        with self.assertRaises(RemixError):import_cards(self.root,incoming)
        card['provenance']['frames']=['contact/s1.jpg'];write_jsonl(incoming,[card]);self.assertEqual(import_cards(self.root,incoming)['total'],1)
    def test_verbalize_boundaries_and_unknown(self):
        for x,w in [(0,'低'),(10,'中'),(19,'中'),(20,'高')]:self.assertEqual(verbalize(x,[10,20]),w)
        self.assertEqual(verbalize(float('nan'),[10,20]),'uncertain')
        with self.assertRaises(RemixError):verbalize(3,[20,10])
    def test_card_evidence_changes_and_endpoint_secrets_are_rejected(self):
        from jev_remix.cards import validate_evidence
        p=self.root/'contact'/'s.jpg';p.parent.mkdir();p.write_bytes(b'observed')
        shot={'shot_id':'s1','source':'src01','start_us':0,'end_us':1000000,'contact':'contact/s.jpg'}
        write_jsonl(self.root/'shots.jsonl',[shot])
        row={'shot_id':'s1','words':{'画面':'observed'},'provenance':{'mode':'agent','frames':['contact/s.jpg']}}
        incoming=self.root/'incoming.jsonl';write_jsonl(incoming,[row]);import_cards(self.root,incoming)
        from jev_remix.common import read_jsonl
        cards=read_jsonl(self.root/'cards.jsonl');validate_evidence(self.root,cards,[shot])
        p.write_bytes(b'changed')
        with self.assertRaises(RemixError) as err:validate_evidence(self.root,cards,[shot])
        self.assertEqual(err.exception.code,3)
        with patch.dict('os.environ',{'JEV_REMIX_VLM_ENDPOINT':'https://name:secret@example.com/path?token=secret'}):
            manifest=prepare(self.root)
        self.assertNotIn('secret',json.dumps(manifest))
    def test_face_variants_cannot_remove_processing_requirement(self):
        from jev_remix.workflow import variants
        write_json(self.root/'face-plan.json',{'items':[{'cluster':'c01','mode':'blur'}]})
        with self.assertRaises(RemixError) as err:variants(self.root,self.root/'table.json')
        self.assertEqual(err.exception.code,4)
    def test_decision_configuration_changes_invalidate_timeline(self):
        from jev_remix.common import ROOT, file_hash, ensure_decision_hashes
        values={'questions':file_hash(ROOT/'questions'/'remix.yaml'),'thresholds':file_hash(ROOT/'thresholds.toml')}
        ensure_decision_hashes({'decision_hashes':values})
        values['questions']='old-version'
        with self.assertRaises(RemixError) as err:ensure_decision_hashes({'decision_hashes':values})
        self.assertEqual(err.exception.code,3)
        with self.assertRaises(RemixError):ensure_decision_hashes({'dependencies':{'cast.json':'hash'}})
    def test_highlight_changed_index_is_rejected_before_source_import(self):
        from jev_remix.media import _highlight_sources
        from jev_remix.common import digest
        index={'media':{'path':'fixture.mp4'},'version':1}
        write_json(self.root/'plan.json',{'index_hash':digest(index),'clips':[{'start_us':0,'end_us':1000000}]})
        index['version']=2;write_json(self.root/'index.json',index)
        with self.assertRaises(RemixError) as err:_highlight_sources(self.root/'plan.json')
        self.assertEqual(err.exception.code,3)
    def test_unknown_music_blocks_formal_export_before_encoding(self):
        from jev_remix.common import fingerprint
        from jev_remix.rendering import render
        source=self.root/'source.mp4';source.write_bytes(b'local fixture only; never encoded')
        music=self.root/'music.wav';music.write_bytes(b'music fixture')
        write_json(self.root/'sources.json',{'version':1,'items':[{'id':'src01','path':str(source),'fingerprint':fingerprint(source),'license':'own','media':{'duration_us':1000000,'width':16,'height':16,'fps':30,'audio':False}}]})
        timeline={'version':2,'fps':30,'width':16,'height':16,'duration_frames':30,'mock':False,'dependencies':{},'items':[{'slot':'s','src':{'source':'src01','in_us':0,'out_us':1000000},'out':{'start_f':0,'end_f':30}}],'audio':{'path':str(music),'fingerprint':fingerprint(music),'license':'unknown'}}
        write_json(self.root/'timeline.json',timeline)
        with self.assertRaises(RemixError) as err:render(self.root)
        self.assertEqual(err.exception.code,4);self.assertIn('music',err.exception.details['sources'])
    def test_review_escapes_untrusted_script_closing_text(self):
        write_json(self.root/'cast.json',{'version':1,'mock':True,'items':[{'slot':'x','shot_id':'s1','source':'src01','out':{'start_f':0,'end_f':30},'reasons':['</script><script>evil()</script>'],'alternatives':[]}]})
        write_json(self.root/'sources.json',{'version':1,'items':[]});write_jsonl(self.root/'shots.jsonl',[])
        review(self.root)
        html=(self.root/'review.html').read_text();self.assertNotIn('</script><script>evil()',html);self.assertIn('\\u003c/script',html)

if __name__=='__main__':unittest.main()

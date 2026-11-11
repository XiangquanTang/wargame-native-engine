"""Generic raw JSON compact codec regression coverage; no user data fixtures."""
from __future__ import annotations
import copy
import gzip
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest
from zipfile import ZipFile

from wargame_native_engine.replay.compact import (
    CompactReplayReader, CompactReplayWriter, apply_patch, make_patch, json_equal,
)

class CompactReplayCodecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        first = {'operators': [{'obj_id': 2, 'name': 'first'}, {'obj_id': 9, 'name': 'second'}],
                 'time': {'cur_step': 3}, 'actions': [], 'unknown': {'0': None}}
        second = copy.deepcopy(first)
        second['time']['cur_step'] = 8
        second['operators'][0]['name'] = 'changed'
        self.source = [('3.json', first), ('8.json', second)]

    def write(self, frames, *, interval=2, evidence=None, assets=None, name='compact'):
        writer = CompactReplayWriter(self.root / name, checkpoint_interval=interval)
        writer.start({'test_origin': 'independent_synthetic_mutations' if evidence else 'synthetic_fixture'},
                     assets=assets or {})
        for i, (frame_name, frame) in enumerate(frames):
            writer.append(frame, name=frame_name, evidence=evidence[i] if evidence else None)
        writer.finalize({'test_only': True})
        archive = writer.package_zip(self.root / f'{name}.zip')
        return CompactReplayReader(archive), Path(archive)

    def test_missing_null_empty_types_keys_list_order_and_delete(self):
        before = copy.deepcopy(self.source[0][1])
        before['future_extension'] = {'null':None,'to_delete':5, 'empty_list':[], 'empty_object':{},
            'string_ids':{'0':None,'00':False,'100':0},
            'typed':[1,True,0,False,1.0,0.0,'1','0',0.9750000000000005],
            'ordered':[{'obj_id':17,'value':'first'},{'obj_id':2,'value':'second'}]}
        after = copy.deepcopy(before)
        ext=after['future_extension']
        del ext['to_delete']; ext['was_missing']=None
        ext['empty_list']={}; ext['empty_object']=[]
        ext['typed']=[True,1,False,0,0.0,1.0,'0','1',0.9750000000000005]
        ext['ordered'].reverse()
        after['operators'] = list(reversed(after['operators']))
        after['operators'][0]['unknown_extension']={'a/b':{'~tilde':None},'empty':[]}
        third = copy.deepcopy(after)
        third['future_extension']=None
        fourth = copy.deepcopy(third); del fourth['future_extension']
        frames=[('2.json',before),('7.json',after),('99.json',third),('100.json',fourth)]
        reader,_=self.write(frames)
        for i, (_, expected) in enumerate(frames):
            actual=reader.raw_frame(i)
            self.assertEqual(actual,expected)
            # Python equality alone treats 1 == True and misses type changes.
            self.assertEqual(json.dumps(actual,sort_keys=True,ensure_ascii=False),
                             json.dumps(expected,sort_keys=True,ensure_ascii=False))
        self.assertIn('future_extension',reader.raw_frame(2))
        self.assertNotIn('future_extension',reader.raw_frame(3))

    def test_evidence_assets_and_return_value_mutation_isolation(self):
        frames=[(f'{i}.json', copy.deepcopy(self.source[i%2][1])) for i in range(5)]
        evidence=[{'test_only':True,'tool_call':{'ordinal':i,'arguments':{'path':[1,2]}}} for i in range(5)]
        assets={'map':{'note':'synthetic_asset','cells':[1,2]},'scenario':{'id':'test'}}
        reader,_=self.write(frames,evidence=evidence,assets=assets)
        for i in (4,0,3,1,2):
            self.assertEqual(reader.raw_frame(i), frames[i][1])
            self.assertEqual(reader.evidence(i),evidence[i])
        decoded=reader.raw_frame(0); decoded['operators'][0]['name']='MUTATED'
        self.assertEqual(reader.raw_frame(0),frames[0][1])
        e=reader.evidence(0); e['tool_call']['arguments']['path'].clear()
        self.assertEqual(reader.evidence(0), evidence[0])
        for value in (-1,5,True,'0'):
            with self.subTest(index=value), self.assertRaises((IndexError,TypeError,ValueError)):
                reader.raw_frame(value)

    def test_patch_fuzz_json_type_and_order_preservation(self):
        rng=random.Random(23023)
        atoms=[None,False,True,0,1,-1,2**90,0.0,-0.0,1.0,0.9750000000000005,"", "零", "0"]
        def tree(depth=0):
            choice=rng.randrange(5 if depth<4 else 3)
            if choice<3: return copy.deepcopy(rng.choice(atoms))
            if choice==3: return [tree(depth+1) for _ in range(rng.randrange(5))]
            keys=rng.sample(['0','00','-1','a/b','~','unknown','obj_id','空'],rng.randrange(6))
            return {k:tree(depth+1) for k in keys}
        for index in range(1000):
            before,after=tree(),tree()
            original=copy.deepcopy(before)
            result=apply_patch(before,make_patch(before,after))
            self.assertTrue(json_equal(result,after),index)
            self.assertTrue(json_equal(before,original),index)

    def test_malformed_patch_fails_closed_without_mutating_input(self):
        before={'a':[{'obj_id':1},None], '0':3}
        malformed=[None,{},'patch', [None],
            [{'op':'remove','path':['missing']}],
            [{'op':'add','path':['0'],'value':4}],
            [{'op':'replace','path':['a',-1],'value':4}],
            [{'op':'replace','path':['a',True],'value':4}],
            [{'op':'replace','path':['a','0'],'value':4}],
            [{'op':'replace','path':[0],'value':4}],
            [{'op':'replace','path':['a',1,'x'],'value':4}],
            [{'op':'remove','path':[]}],
            [{'op':'remove','path':['0'],'value':None}],
            [{'op':'replace','path':['0']}],
            [{'op':'replace','path':['0'],'value':float('nan')}],
            [{'op':'move','path':['0'],'value':4}],
            [{'op':'remove','path':['a',0]}],
            [{'op':'add','path':['a',2],'value':4}],
            [{'op':'replace','path':['0'],'value':4,'unknown':True}],
        ]
        for operations in malformed:
            with self.subTest(operations=operations),self.assertRaises((ValueError,TypeError)):
                apply_patch(before,operations)
            self.assertEqual(before,{'a':[{'obj_id':1},None], '0':3})

    def test_package_single_external_hash_and_tamper_rejection(self):
        reader,archive=self.write(self.source)
        self.assertTrue(reader.hash_verified)
        content=Path(str(archive)+'.sha256').read_text().split()
        self.assertEqual(content,[hashlib.sha256(archive.read_bytes()).hexdigest(),archive.name])
        with ZipFile(archive) as z:
            manifest=json.loads(z.read('compact_manifest.json'))
            self.assertNotIn('frame_hashes',manifest)
            self.assertNotIn('frames_merkle_root',manifest)
            self.assertFalse(any(n.endswith('.sha256') for n in z.namelist()))
        with archive.open('ab') as f: f.write(b'TAMPER')
        with self.assertRaisesRegex(ValueError,'SHA256'):
            CompactReplayReader(archive)

    def test_manifest_and_chunk_malformed_or_reordered_reject(self):
        _,archive=self.write(self.source)
        with ZipFile(archive) as z: original={n:z.read(n) for n in z.namelist()}
        def mutated(kind):
            values=copy.deepcopy(original)
            manifest=json.loads(values['compact_manifest.json'])
            chunkname=manifest['chunks'][0]['name']
            records=[json.loads(line) for line in gzip.decompress(values[chunkname]).splitlines()]
            if kind=='manifest_names': manifest['frame_names'].reverse()
            elif kind=='record_order': records.reverse()
            elif kind=='record_name': records[1]['name']='9999.json'
            elif kind=='patch_delete_missing': records[1]['patch']=[{'op':'remove','path':['not_present']}]
            elif kind=='missing_frame': records[0].pop('frame')
            elif kind=='extra_record': records.append(records[-1])
            elif kind=='bad_obj_id_coercion': records[1]['patch']=[{'op':'replace','path':['operators','62','obj_id'],'value':0}]
            values['compact_manifest.json']=json.dumps(manifest).encode()
            values[chunkname]=gzip.compress(b''.join(json.dumps(r).encode()+b'\n' for r in records),mtime=0)
            path=self.root/(kind+'.zip')
            with ZipFile(path,'w') as z:
                for name,data in values.items(): z.writestr(name,data)
            return path
        for kind in ('manifest_names','record_order','record_name','patch_delete_missing','missing_frame','extra_record','bad_obj_id_coercion'):
            with self.subTest(kind=kind),self.assertRaises((ValueError,KeyError)):
                list(CompactReplayReader(mutated(kind)).iter_raw_frames())

if __name__ == '__main__':
    unittest.main()

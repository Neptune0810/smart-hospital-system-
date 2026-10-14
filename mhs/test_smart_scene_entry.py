"""Real Python entry imports only; no Isaac, ROS, model or scene construction."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


HERE = Path(__file__).resolve().parent
BASELINE_NON_IMPORT_AST = 'b8d0ba2dbbd967d176e850ad0d96aa4b73c02530818a83d83226eb01bf263a9c'
HELPER_SHA256 = 'd2736ebb9d06841729d9100be9152ce51ad376126a83969ef96c8ac233fcee06'
ALIASES = [('SceneIOConflict', None), ('iter_commands', None), ('publish_ack_bytes', None),
           ('read_ack', 'read_scene_ack'), ('scene_path', None), ('write_ack', 'publish_scene_ack')]



def canonical_ast_dump(tree):
    """Match Python3.10/3.11 AST only when later type_params are empty.

    Type parameters are semantic when nonempty and must never disappear from
    the invariant. Shadow only this parsed node's field list, not ast classes.
    """
    for node in ast.walk(tree):
        if 'type_params' in node._fields:
            value = getattr(node, 'type_params', None)
            if not isinstance(value, list) or value:
                raise ValueError('nonempty_or_invalid_type_params_cannot_be_normalized')
            node._fields = tuple(field for field in node._fields if field != 'type_params')
    return ast.dump(tree, include_attributes=False)


def subprocess_import(package):
    # A real interpreter import, not a mocked module or caught ImportError.
    # The package entry uses the existing adjacent mhs/__init__.py. The bare
    # entry is exactly hospital_sim.py's `from smart_scene import ...` mode.
    statement = ('from mhs import smart_scene as scene; from mhs import scene_io as helper'
                 if package else 'import smart_scene as scene; import scene_io as helper')
    program = statement + """
import hashlib,json
from pathlib import Path
expected=Path(%s).resolve()
assert Path(scene.__file__).resolve()==expected/'smart_scene.py'
assert Path(helper.__file__).resolve()==expected/'scene_io.py'
aliases={'SceneIOConflict':'SceneIOConflict','iter_commands':'iter_commands',
 'publish_ack_bytes':'publish_ack_bytes','read_scene_ack':'read_ack',
 'scene_path':'scene_path','publish_scene_ack':'write_ack'}
assert all(getattr(scene,left) is getattr(helper,right) for left,right in aliases.items())
assert scene.scene_path(Path('/fixture'),'acks','run','epoch','same')==Path('/fixture/acks/run/epoch/same.json')
try:scene.scene_path(Path('/fixture'),'acks','run','..','same')
except ValueError:pass
else:raise AssertionError('invalid namespace accepted')
print(json.dumps({'scene_package':scene.__package__,'helper_package':helper.__package__,
 'helper_sha256':hashlib.sha256(Path(helper.__file__).read_bytes()).hexdigest(),
 'world_class':scene.SmartWardWorld.__name__,'isaac_class':scene.SmartWardScene.__name__,
 'default_operation':helper.default_operation_id('run','epoch','hospital-cabinet-b','handoff_order','1')}))
""" % repr(str(HERE))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable,'-B','-X','utf8','-c',program],
        cwd=HERE.parent if package else HERE, env=env, capture_output=True,text=True,
        encoding='utf-8',timeout=10,check=True)
    return json.loads(result.stdout)


class SceneEntryTests(unittest.TestCase):
    def test_actual_bare_host_entry_imports_the_adjacent_frozen_helper(self):
        result=subprocess_import(False)
        self.assertIn(result['scene_package'],(None,''))
        self.assertIn(result['helper_package'],(None,''))
        self.assertEqual(result['helper_sha256'],HELPER_SHA256)
        self.assertEqual(result['world_class'],'SmartWardWorld')
        self.assertEqual(result['isaac_class'],'SmartWardScene')

    def test_actual_package_entry_imports_the_same_frozen_helper_contract(self):
        result=subprocess_import(True)
        self.assertEqual(result['scene_package'],'mhs')
        self.assertEqual(result['helper_package'],'mhs')
        self.assertEqual(result['helper_sha256'],HELPER_SHA256)
        self.assertEqual(result['world_class'],'SmartWardWorld')

    def test_modes_generate_the_same_operation_identity_without_constructing_world(self):
        self.assertEqual(subprocess_import(False)['default_operation'],subprocess_import(True)['default_operation'])

    def test_only_explicit_import_branch_changes_from_frozen121_AST(self):
        tree=ast.parse((HERE/'smart_scene.py').read_text(encoding='utf-8'))
        branches=[node for node in tree.body if isinstance(node,ast.If)
            and any(isinstance(child,ast.Name) and child.id=='__package__' for child in ast.walk(node.test))]
        self.assertEqual(len(branches),1)
        branch=branches[0]
        self.assertEqual(ast.dump(branch.test,include_attributes=False),
            ast.dump(ast.parse("__package__ in {None, ''}",mode='eval').body,include_attributes=False))
        for body,level in ((branch.body,0),(branch.orelse,1)):
            self.assertEqual(len(body),1)
            self.assertIsInstance(body[0],ast.ImportFrom)
            self.assertEqual((body[0].module,body[0].level),('scene_io',level))
            self.assertEqual([(alias.name,alias.asname) for alias in body[0].names],ALIASES)
        tree.body.remove(branch)
        self.assertEqual(hashlib.sha256(canonical_ast_dump(tree).encode()).hexdigest(),BASELINE_NON_IMPORT_AST)


    def test_nonempty_future_type_parameters_are_not_hidden_by_compatibility(self):
        tree = ast.parse('def typed(): pass')
        node = tree.body[0]
        if 'type_params' not in node._fields:
            node._fields = (*node._fields, 'type_params')
        node.type_params = [ast.Name(id='T', ctx=ast.Load())]
        with self.assertRaisesRegex(ValueError, 'type_params_cannot_be_normalized'):
            canonical_ast_dump(tree)

    def test_empty_future_type_parameter_fields_match_original_AST_shape(self):
        tree = ast.parse('class Plain:\n    def method(self): pass')
        raw = ast.parse('class Plain:\n    def method(self): pass')
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                if 'type_params' not in node._fields:
                    node._fields = (*node._fields, 'type_params')
                node.type_params = []
        for node in ast.walk(raw):
            if 'type_params' in node._fields:
                node._fields = tuple(field for field in node._fields if field != 'type_params')
        self.assertEqual(canonical_ast_dump(tree), ast.dump(raw, include_attributes=False))


if __name__=='__main__':
    unittest.main()

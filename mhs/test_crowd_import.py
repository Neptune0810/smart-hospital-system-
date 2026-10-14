"""Startup import contracts only; no Isaac initialization or crowd simulation."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class CrowdImportTests(unittest.TestCase):
    def test_package_import_uses_runtime_data_directory(self):
        from . import crowd_v2
        with patch.dict('os.environ', {'HOSPITAL_MODE': 'smart-ward-v1'}):
            self.assertEqual(crowd_v2.data_dir(ROOT), ROOT / 'runs/smart-ward-v1')

    def test_isaac_script_directory_import_has_no_parent_package(self):
        spec = importlib.util.spec_from_file_location('crowd_v2_script_contract', ROOT / 'mhs/crowd_v2.py')
        module = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', [str(ROOT / 'mhs'), *sys.path]):
            spec.loader.exec_module(module)
        self.assertFalse(module.__package__)
        with patch.dict('os.environ', {'HOSPITAL_MODE': 'smart-ward-v1'}):
            self.assertEqual(module.data_dir(ROOT), ROOT / 'runs/smart-ward-v1')
        with patch.dict('os.environ', {'HOSPITAL_MODE': 'mhs-v2'}):
            self.assertEqual(module.data_dir(ROOT), ROOT / 'runs/mhs-v2')

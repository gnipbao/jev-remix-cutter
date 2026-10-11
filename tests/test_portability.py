"""Independent checkouts, registration fixtures, and optional offline wheel builds."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="remix-portability-")
        self.root = Path(self.temp.name).resolve()
        self.checkout = self.root/"checkout"
        shutil.copytree(Path(__file__).resolve().parents[1],self.checkout,ignore=shutil.ignore_patterns(
            ".git","__pycache__","*.egg-info","build","dist"))
        self.env = {"PATH":os.environ.get("PATH",os.defpath),"JEV_REMIX_PYTHON":sys.executable,
                    "PYTHONDONTWRITEBYTECODE":"1"}
        if "PYTHONPATH" in os.environ:
            self.env["PYTHONPATH"] = os.environ["PYTHONPATH"]

    def tearDown(self):
        self.temp.cleanup()

    def command(self,args,env=None):
        result = subprocess.run(args,cwd=self.root,text=True,capture_output=True,env=env or self.env)
        self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-2000:])
        return result.stdout

    def test_copied_checkout_entrypoint_uses_its_own_contracts(self):
        script = self.checkout/"scripts"/"remix.py"
        result = json.loads(self.command([sys.executable,"-I",str(script),"init","--run",str(self.root/"run"),
                                         "--target-s","120","--json"]))
        self.assertEqual(result["result"]["brief"]["target_seconds"],120)
        report = json.loads(self.command([sys.executable,"-I",str(script),"doctor","--json"]))
        self.assertTrue(report["result"]["ok"])
        self.assertEqual(report["result"]["TYPESAFE_API_KEY"],"absent")

    def test_fixture_registration_keeps_real_global_state_untouched(self):
        spec = importlib.util.spec_from_file_location("remix_install_fixture",self.checkout/"scripts"/"install_skill.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        target,home = self.root/"skills"/"jev-remix-cutter",self.root/"fixture-home"
        with patch.object(installer.Path,"home",return_value=home):
            result = installer.install(str(target),sys.executable)
            again = installer.install(str(target),sys.executable)
        self.assertEqual(result,again)
        self.assertEqual(target.resolve(),self.checkout)
        config = json.loads((home/".config"/"jev-remix"/"runtime.json").read_text())
        self.assertEqual(config["python"],str(Path(sys.executable).absolute()))
        self.assertEqual(set(config),{"version","python"})
        existing = self.root/"unrelated"; existing.mkdir()
        with self.assertRaises(ValueError):
            installer.install(str(existing))

    @unittest.skipUnless(importlib.util.find_spec("setuptools"),"Offline wheel test needs setuptools>=77 in the test interpreter")
    def test_offline_wheel_survives_removing_source_checkout(self):
        # Build the source distribution first: its extracted contents must be
        # sufficient to rebuild all wheel resources outside this repository.
        sdists = self.root/"sdists"
        sdists.mkdir()
        code = ("import os; from setuptools.build_meta import build_sdist; "
                f"os.chdir({str(self.checkout)!r}); build_sdist({str(sdists)!r})")
        self.command([sys.executable,"-c",code])
        unpacked = self.root/"sdist-unpacked"
        with tarfile.open(next(sdists.glob("*.tar.gz"))) as archive:
            archive.extractall(unpacked,**({"filter":"data"} if hasattr(tarfile,"data_filter") else {}))
        sdist_source = next(unpacked.iterdir())
        wheels = self.root/"wheels"
        self.command([sys.executable,"-m","pip","wheel","--no-index","--no-deps","--no-build-isolation",
                      "--wheel-dir",str(wheels),str(sdist_source)])
        wheel = next(wheels.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            self.assertEqual(archive.read("jev_remix/_data/thresholds.toml"),(self.checkout/"thresholds.toml").read_bytes())
            self.assertEqual(archive.read("jev_remix/_data/questions/remix.yaml"),(self.checkout/"questions"/"remix.yaml").read_bytes())
            self.assertIn("jevkit/LICENSE",archive.namelist())
        site = self.root/"site"
        self.command([sys.executable,"-m","pip","install","--no-index","--no-deps","--target",str(site),str(wheel)])
        self.checkout.rename(self.root/"source-hidden")
        sdist_source.rename(self.root/"sdist-hidden")
        env = {**self.env,"PYTHONPATH":str(site)}
        entrypoint = site/("Scripts" if os.name=="nt" else "bin")/"remix"
        result = json.loads(self.command([str(entrypoint),"init","--run",str(self.root/"wheel-run"),
                                         "--target-s","120","--json"],env))
        self.assertEqual(result["result"]["brief"]["target_seconds"],120)
        code = ("from pathlib import Path; from jev_remix.common import ROOT,thresholds; "
                "from jev_remix.planning import load_pack; from jev_remix.judging import load_questions; "
                f"assert ROOT.is_relative_to(Path({str(site)!r})); "
                "assert thresholds()['sense']['scene_threshold']==27.0; "
                "assert load_pack('amv')['id']=='amv'; assert 'slot_fit' in load_questions()['questions']; "
                "assert (ROOT/'assets'/'review.html').is_file(); assert (ROOT/'assets'/'card-prompt.txt').is_file()")
        self.command([sys.executable,"-c",code],env)

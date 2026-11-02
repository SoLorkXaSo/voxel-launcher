import hashlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile


spec = importlib.util.spec_from_file_location(
    "voxel_launcher", Path(__file__).resolve().parents[1] / "voxel-launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ModUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mods = Path(tmp.name)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as jar:
            jar.writestr("fabric.mod.json", '{"id": "example", "version": "2.0"}')
        self.data = archive.getvalue()
        self.info = dict(key="example", filename="example-2.jar",
                         url="https://example.invalid/example-2.jar",
                         sha1=hashlib.sha1(self.data).hexdigest(), deps=[])
        self.provider = Mock()
        self.provider.files.return_value = self.info
        self.response = Mock(content=self.data)
        self.get = Mock(return_value=self.response)
        requests_patch = patch.object(launcher, "requests", SimpleNamespace(get=self.get))
        requests_patch.start()
        self.addCleanup(requests_patch.stop)

    def old_mod(self, recorded, actual):
        launcher.manifest_save(self.mods, {"example": recorded})
        (self.mods / actual).write_bytes(b"original mod")

    def install(self):
        return launcher.install_mod(self.provider, "example", "1.20.1", self.mods)

    def assert_installed(self, filename):
        self.assertEqual((self.mods / filename).read_bytes(), self.data)
        self.assertEqual(launcher.manifest_load(self.mods), {"example": filename})
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()),
                         [".voxel-mods.json", filename])

    def test_enabled_update(self):
        self.old_mod("example-1.jar", "example-1.jar")
        self.assertEqual(self.install(), ["example-2.jar"])
        self.assert_installed("example-2.jar")

    def test_disabled_update(self):
        self.old_mod("example-1.jar.disabled", "example-1.jar.disabled")
        self.install()
        self.assert_installed("example-2.jar.disabled")

    def test_manually_disabled_update(self):
        self.old_mod("example-1.jar", "example-1.jar.disabled")
        self.install()
        self.assert_installed("example-2.jar.disabled")

    def test_manually_enabled_update(self):
        self.old_mod("example-1.jar.disabled", "example-1.jar")
        self.install()
        self.assert_installed("example-2.jar")

    def test_existing_new_version_keeps_disabled_state(self):
        self.old_mod("example-1.jar.disabled", "example-1.jar.disabled")
        (self.mods / "example-2.jar").write_bytes(self.data)
        self.install()
        self.assert_installed("example-2.jar.disabled")
        self.get.assert_not_called()

    def test_existing_disabled_new_version_stays_disabled(self):
        self.old_mod("example-1.jar", "example-1.jar")
        (self.mods / "example-2.jar.disabled").write_bytes(self.data)
        self.install()
        self.assert_installed("example-2.jar.disabled")
        self.get.assert_not_called()

    def test_missing_old_file_keeps_recorded_disabled_state(self):
        launcher.manifest_save(self.mods, {"example": "example-1.jar.disabled"})
        self.install()
        self.assert_installed("example-2.jar.disabled")

    def test_failed_update_preserves_old_mod_and_manifest(self):
        self.old_mod("example-1.jar", "example-1.jar.disabled")
        before = {p.name: p.read_bytes() for p in self.mods.iterdir()}
        for failure in ("network", "hash", "archive"):
            with self.subTest(failure=failure):
                self.get.side_effect = OSError("offline") if failure == "network" else None
                self.response.content = b"invalid jar"
                self.info["sha1"] = None if failure == "archive" else "0" * 40
                with self.assertRaises((OSError, RuntimeError)):
                    self.install()
                self.assertEqual({p.name: p.read_bytes() for p in self.mods.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("desktop_verification", ROOT / "scripts" / "verify-desktop.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class DesktopArtifactTest(unittest.TestCase):
    def test_packaged_default_config_cannot_silently_remain_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo, package = Path(temporary) / "source", Path(temporary) / "package"
            relative = Path("work_catalog_yaml/jp_tv/browse_config.default.yaml")
            source = repo / "apps" / "framework" / "backend" / relative
            bundled = package / relative
            source.parent.mkdir(parents=True)
            bundled.parent.mkdir(parents=True)
            source.write_bytes(b"version: 1\n")
            bundled.write_bytes(source.read_bytes())
            verify.verify_default_config(repo, package)
            source.write_bytes(b"version: 2\n")
            with self.assertRaisesRegex(ValueError, "default configuration differs"):
                verify.verify_default_config(repo, package)

    def test_code_comparison_detects_stale_source_without_executing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "fixture.py"
            source.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
            code = compile(source.read_bytes(), "packaged.py", "exec", dont_inherit=True, optimize=1)
            verify.verify_code(source, code)
            source.write_text("raise RuntimeError('modified source')\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "differs"):
                verify.verify_code(source, code)

    def test_frontend_detects_changed_missing_and_extra_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, package = root / "source", root / "package"
            relative = Path("apps/framework/frontend/index.html")
            source, bundled = repo / relative, package / relative
            source.parent.mkdir(parents=True)
            bundled.parent.mkdir(parents=True)
            source.write_bytes(b"new UI")
            bundled.write_bytes(b"new UI")
            self.assertEqual(verify.verify_frontend(repo, package), 1)
            bundled.write_bytes(b"old UI")
            with self.assertRaisesRegex(ValueError, "differs from source"):
                verify.verify_frontend(repo, package)
            bundled.unlink()
            with self.assertRaisesRegex(ValueError, "file set differs"):
                verify.verify_frontend(repo, package)
            bundled.write_bytes(b"new UI")
            (bundled.parent / "stale.js").write_bytes(b"unused")
            with self.assertRaisesRegex(ValueError, "file set differs"):
                verify.verify_frontend(repo, package)

    def test_layout_requires_bootstrap_and_excludes_shared_config_and_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package"
            package.mkdir()
            config = package / "nimda-desktop.yaml"
            config.write_text("version: 1\npaths:\n  workspace_root: ../shared\n", encoding="utf-8")
            verify.verify_layout(package, package.parent / "shared")
            with self.assertRaisesRegex(ValueError, "different workspace"):
                verify.verify_layout(package, package.parent / "different")
            for forbidden in ("config", "data"):
                (package / forbidden).mkdir()
                with self.assertRaisesRegex(ValueError, "must not contain"):
                    verify.verify_layout(package)
                (package / forbidden).rmdir()
            config.write_text("paths: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "workspace_root"):
                verify.verify_layout(package)

    def test_archive_detects_stale_content_extra_missing_and_duplicate_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "Nimda"
            package.mkdir()
            (package / "Nimda.exe").write_bytes(b"new executable fixture")
            archive = package.parent / "fixture.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.write(package / "Nimda.exe", "Nimda/Nimda.exe")
            self.assertEqual(verify.verify_archive(package, archive), 1)
            for entries in (
                [("Nimda/Nimda.exe", b"old executable fixture")],
                [],
                [("Nimda/Nimda.exe", b"new executable fixture"), ("Nimda/extra", b"x")],
                [("Nimda/Nimda.exe", b"x"), ("Nimda/Nimda.exe", b"x")],
            ):
                with self.subTest(entries=entries), warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(archive, "w") as zipped:
                        for name, content in entries:
                            zipped.writestr(name, content)
                    with self.assertRaises(ValueError):
                        verify.verify_archive(package, archive)


if __name__ == "__main__":
    unittest.main()

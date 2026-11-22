# SPDX-FileCopyrightText: 2026 Björn Strausmann and Labelyard contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for scripts/catalog_index.py (the sheet-type catalog index, ADR 0028)."""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog_index

CATALOG = Path(__file__).resolve().parent.parent / "backend" / "app" / "catalog"


class CatalogIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.folder = self.tmp / "catalog"
        shutil.copytree(CATALOG, self.folder)

    def run_main(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = catalog_index.main([*args, "--folder", str(self.folder)])
        return code, out.getvalue() + err.getvalue()

    def test_check_mode_passes_on_the_shipped_catalog(self) -> None:
        code, output = self.run_main("--check")
        self.assertEqual(code, 0, output)

    def test_check_mode_fails_on_a_stale_index(self) -> None:
        path = self.folder / "sheet_types" / "avery-7160.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["version"]["geometry"]["pitch_x"] = 66.05
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        code, output = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("index.json is stale", output)
        code, _ = self.run_main()  # regenerate
        self.assertEqual(code, 0)
        self.assertEqual(self.run_main("--check")[0], 0)

    def test_check_mode_fails_when_meta_and_documents_disagree(self) -> None:
        meta_path = self.folder / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.pop("avery-7160")
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        code, output = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("avery-7160", output)

    def test_a_document_under_the_wrong_file_name_is_refused(self) -> None:
        src = self.folder / "sheet_types" / "avery-7160.json"
        src.rename(self.folder / "sheet_types" / "avery-7999.json")
        with self.assertRaises(ValueError):
            catalog_index.build_index(self.folder)

    def test_fields_hash_ignores_key_prefix_and_defaults(self) -> None:
        base = {"key": "a-b", "prefix": "AB", "allow_qr": True, "vendor": "V"}
        same = {**base, "key": "c-d", "prefix": "CD", "verified": False, "product_codes": []}
        self.assertEqual(catalog_index.fields_hash_of(base), catalog_index.fields_hash_of(same))
        changed = {**base, "vendor": "W"}
        self.assertNotEqual(
            catalog_index.fields_hash_of(base), catalog_index.fields_hash_of(changed)
        )

    def test_geometry_hash_normalises_page_size_and_round_radius(self) -> None:
        g = {
            "page_size": "A4",
            "label_w": 40.0,
            "label_h": 40.0,
            "cols": 4,
            "rows": 6,
            "pitch_x": 45.0,
            "pitch_y": 45.0,
            "margin_top": 10.0,
            "margin_left": 10.0,
            "corner_radius": 2.0,
            "shape": "round",
        }
        explicit = {**g, "page_w": 210.0, "page_h": 297.0, "corner_radius": None}
        self.assertEqual(
            catalog_index.geometry_hash_of(g), catalog_index.geometry_hash_of(explicit)
        )
        with self.assertRaises(ValueError):
            catalog_index.geometry_hash_of({**g, "pitch_x": 45.001})


if __name__ == "__main__":
    unittest.main()

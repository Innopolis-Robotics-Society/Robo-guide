from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import wakeword_review as review


def example_rows():
    rows = []
    for kind, name in (("positive", "crowd_01.wav"), ("negative", "background.wav")):
        rows.append(
            {
                "source": f"{kind}/train/{name}",
                "source_sha256": "abc",
                "candidate": f"{kind}_candidates/train/{name}",
                "kind": kind,
                "crop_start_sample": 16000,
                "crop_end_sample": 32000,
                "crop_start_seconds": 1,
                "crop_end_seconds": 2,
                "candidate_seconds": 1,
                "method": "test",
                "review_required": True,
                "review_reasons": [],
                "asr_text": "",
            }
        )
    return rows


def test_fingerprint_is_order_independent_but_changes_with_crop_and_source():
    rows = example_rows()
    fingerprint = review.dataset_fingerprint(rows)
    assert review.dataset_fingerprint(rows[::-1]) == fingerprint
    for field, value in (
        ("source_sha256", "changed"),
        ("crop_start_sample", 16001),
        ("kind", "negative"),
        ("candidate", "new.wav"),
    ):
        changed = copy.deepcopy(rows)
        changed[0][field] = value
        assert review.dataset_fingerprint(changed) != fingerprint


def test_page_has_controls_for_all_windows_and_escapes_untrusted_text(tmp_path):
    output = tmp_path / "prepared/run_02"
    output.mkdir(parents=True)
    raw = tmp_path / "raw"
    rows = example_rows()
    rows[0]["asr_text"] = '</script><script>alert("oops")</script>'
    review.write_review_page(output, raw, rows, [])
    page = (output / "review.html").read_text()
    assert page.count('class="review-card"') == 2
    assert 'data-choice="accepted"' in page and 'data-choice="rejected"' in page
    assert "Да, имя целиком" in page and "Да, имени нет" in page
    assert "Отменить решение" in page and 'id="export"' in page
    assert rows[0]["asr_text"] not in page
    assert "&lt;/script&gt;" in page and r"\u003c/script\u003e" in page
    assert 'src="../../raw/positive/train/crowd_01.wav"' in page
    assert "<!-- REVIEW_CARDS -->" not in page and "/* REVIEW_SCRIPT */" not in page
    embedded = page.split('<script type="application/json" id="review-data">', 1)[1]
    payload = json.loads(embedded.split("</script>", 1)[0])
    assert payload["rows"] == rows


def test_refresh_only_changes_html(tmp_path):
    output = tmp_path / "prepared/run_02"
    output.mkdir(parents=True)
    raw = tmp_path / "raw"
    rows = example_rows()
    for row in rows:
        source = raw / row["source"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source must stay unchanged")
        candidate = output / row["candidate"]
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"candidate must stay unchanged")
    (output / "manifest.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (output / "negative_scan.json").write_text("[]")
    (output / "review.csv").write_text("original report")
    (output / "review_decisions_run_02.json").write_text('{"user": "decisions"}')
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert review.main([str(output), "--raw-root", str(raw)]) == 0
    assert all(
        hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in before.items()
    )
    assert (output / "review.html").is_file()


def test_javascript_decisions_restore_export_import_and_storage_failure():
    executable = shutil.which("gjs")
    if not executable:
        pytest.skip("GJS not available for JavaScript tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [executable, str(root / "test/test_wakeword_review.js")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "review state tests passed" in result.stdout

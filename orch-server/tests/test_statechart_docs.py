"""그림 생성기 시험: 표시 이름 누락과 생성물 불일치는 실패한다(카드 #1259)."""

import shutil

import pytest

from haniel_orch.statechart import docs, labels
from haniel_orch.statechart.definitions import DIVERGENCE, AttemptEvidence, OrchRequest


def test_committed_generated_files_match_the_definitions():
    assert docs.check(docs.default_out_dir()) == []
    assert docs.main(["--check"]) == 0


def test_every_view_stays_within_the_arrow_limit_and_names_every_state():
    for view in docs.VIEWS:
        text = docs.mermaid(view)
        arrows = [line for line in text.splitlines() if "-->" in line]
        assert len(arrows) <= docs.MAX_ARROWS, view.stem
        assert text.startswith("stateDiagram-v2\n")
        assert "state " in text and "classDef breaks" in text


def test_request_views_split_by_the_divergence_mark_and_cover_every_transition():
    status = docs.mermaid(docs.VIEWS[0])
    divergence = docs.mermaid(docs.VIEWS[1])
    assert "(어긋남 8)" not in status and "(PR 2에서 바뀜)" not in status
    assert "(어긋남 8)" in divergence and "(PR 2에서 바뀜)" in divergence
    total = len(docs.transitions(OrchRequest))
    count = lambda text: len([line for line in text.splitlines() if "-->" in line and "[*]" not in line])
    assert count(status) + count(divergence) == total


def test_divergence_marks_use_only_the_two_agreed_texts():
    assert {mark[1] for mark in DIVERGENCE.values()} == {"어긋남 8", "PR 2에서 바뀜"}


def test_a_state_without_a_display_name_fails(monkeypatch):
    broken = {**labels.STATE_LABELS["OrchRequest"]}
    broken.pop("retry")
    monkeypatch.setattr(docs, "STATE_LABELS", {**labels.STATE_LABELS, "OrchRequest": broken})
    with pytest.raises(docs.DocsError, match="상태 'retry'"):
        docs.validate(OrchRequest)


def test_an_event_without_a_display_name_fails(monkeypatch):
    broken = {**labels.EVENT_LABELS["OrchRequest"]}
    broken.pop("reopen")
    monkeypatch.setattr(docs, "EVENT_LABELS", {**labels.EVENT_LABELS, "OrchRequest": broken})
    with pytest.raises(docs.DocsError, match="사건 'reopen'"):
        docs.validate(OrchRequest)


def test_a_condition_without_a_display_name_fails(monkeypatch):
    broken = {**labels.COND_LABELS["AttemptEvidence"]}
    broken.pop("recovery_mode")
    monkeypatch.setattr(docs, "COND_LABELS", {**labels.COND_LABELS, "AttemptEvidence": broken})
    with pytest.raises(docs.DocsError, match="조건 'recovery_mode'"):
        docs.validate(AttemptEvidence)


def test_a_mark_on_a_transition_that_does_not_exist_fails(monkeypatch):
    stale = {**DIVERGENCE, ("OrchRequest", "success", "reopen", "pending"): ("changes", "PR 2에서 바뀜")}
    monkeypatch.setattr(docs, "DIVERGENCE", stale)
    with pytest.raises(docs.DocsError, match="정의에 없는 전이"):
        docs.validate(OrchRequest)


def _copy(tmp_path):
    out = tmp_path / "generated"
    shutil.copytree(docs.default_out_dir(), out)
    return out


def test_a_generated_file_that_differs_from_the_definition_fails(tmp_path):
    out = _copy(tmp_path)
    path = out / "request-status.mmd"
    path.write_text(path.read_text(encoding="utf-8").replace("거절한다", "거절하라"), encoding="utf-8")

    problems = docs.check(out)

    assert any("request-status.mmd" in p and "다르다" in p for p in problems)
    assert docs.main(["--check", "--out", str(out)]) == 1


def test_a_missing_file_and_a_stale_svg_fail(tmp_path):
    out = _copy(tmp_path)
    (out / "transitions.md").unlink()
    svg = out / "attempt-evidence.svg"
    svg.write_text(svg.read_text(encoding="utf-8").replace(docs.SVG_STAMP, "other"), encoding="utf-8")
    (out / "request-divergence.svg").unlink()

    problems = docs.check(out)

    assert any("transitions.md" in p and "없다" in p for p in problems)
    assert any("attempt-evidence.svg" in p for p in problems)
    assert any("request-divergence.svg" in p for p in problems)


def test_writing_without_arguments_regenerates_every_text_file(tmp_path):
    out = tmp_path / "fresh"
    assert docs.main(["--out", str(out)]) == 0
    assert {p.name for p in out.iterdir()} == {
        "request-status.mmd",
        "request-divergence.mmd",
        "attempt-evidence.mmd",
        "transitions.md",
    }
    # svg는 render.py가 만든다. 쓰기만 한 폴더는 svg가 없어서 검사가 실패한다
    assert docs.main(["--check", "--out", str(out)]) == 1


def test_no_png_is_generated_or_committed():
    assert not list(docs.default_out_dir().glob("*.png"))

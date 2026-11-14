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


def test_request_views_split_by_region_and_mark_and_cover_every_transition():
    status = docs.mermaid(docs.VIEWS[0])
    divergence = docs.mermaid(docs.VIEWS[1])
    lock = docs.mermaid(docs.VIEWS[3])
    tag = docs.mermaid(docs.VIEWS[4])
    # 어긋남 8이 없어졌다. 설계와 다른 전이의 표시는 옛 노드 규칙(PR 2에서 바뀜)과 설계에 없음 둘이다
    assert "어긋남" not in status + divergence + lock + tag
    assert "(PR 2에서 바뀜)" not in status and "(PR 2에서 바뀜)" in divergence
    assert "(설계에 없음)" in divergence
    # 점검 필요 영역과 꼬리표 영역은 자기 그림이 있고, 요청 상태 그림에는 그 영역의 잎이 없다
    assert 'state "점검 필요" as lock' in lock and "l_on" in lock
    assert 'state "닫힌 뒤 적용 꼬리표" as tag' in tag and "t_on" in tag
    assert "l_on" not in status + divergence + tag and "t_on" not in status + divergence + lock
    # 접힌 화살표(합류 막대)는 그림에서 한 줄이므로 정의의 전이 전부가 네 그림 가운데 한 곳에 든다는 것으로 견준다
    drawn_status = {a for a in docs._view_arrows(docs.VIEWS[0])}
    drawn_divergence = {a for a in docs._view_arrows(docs.VIEWS[1])}
    drawn_lock = {a for a in docs._view_arrows(docs.VIEWS[3])}
    drawn_tag = {a for a in docs._view_arrows(docs.VIEWS[4])}
    assert drawn_status | drawn_divergence | drawn_lock | drawn_tag == set(docs.transitions(OrchRequest))
    assert not drawn_status & drawn_divergence
    assert not drawn_lock & (drawn_status | drawn_divergence)
    assert not drawn_tag & (drawn_status | drawn_divergence | drawn_lock)


def test_divergence_marks_use_only_the_agreed_texts():
    assert {mark[1] for mark in DIVERGENCE.values()} == {"PR 2에서 바뀜", "설계에 없음"}


def test_attempt_evidence_has_no_divergence_mark():
    """실행 방식 조건, 증거 회수, 노드 성공이 시도를 밀어냄은 설계 글(design.md)이 받친다."""
    assert not [key for key in DIVERGENCE if key[0] == "AttemptEvidence"]


def test_every_pr2_or_absent_mark_names_a_design_line():
    from haniel_orch.statechart.definitions import CHANGES, ABSENT, DESIGN_LINES

    for key, (kind, _) in DIVERGENCE.items():
        if kind in (CHANGES, ABSENT):
            assert key in DESIGN_LINES and DESIGN_LINES[key].startswith("design.md:"), key
    assert set(DESIGN_LINES) <= set(DIVERGENCE)


def test_state_colors_follow_what_the_picture_draws():
    status = docs.mermaid(docs.VIEWS[0])
    divergence = docs.mermaid(docs.VIEWS[1])
    evidence = docs.mermaid(docs.VIEWS[2])
    # 어긋남(breaks)으로 적은 전이가 지금은 없으므로 요청 그림에는 빨강도 주황도 없다
    assert "class " not in status.split("\n    }\n", 1)[1]
    assert "class " not in divergence.split("\n    }\n", 1)[1]
    assert "class " not in docs.mermaid(docs.VIEWS[3]).split("\n    }\n", 1)[1]
    assert "class " not in docs.mermaid(docs.VIEWS[4]).split("\n    }\n", 1)[1]
    # 주황은 이름으로 적어 둔 설계 밖 잎 하나뿐이다
    assert [line for line in evidence.splitlines() if line.startswith("    class ")] == [
        "    class ev_superseded notInDesign"
    ]


def test_a_breaks_mark_colors_the_target_of_the_arrow_it_marks(monkeypatch):
    """어긋남 표시를 다는 길은 남겨 두었다. 표시를 달면 그 화살표가 닿는 상태가 빨갛게 칠해진다."""
    key = ("OrchRequest", "pending", "reject", "rejected")
    monkeypatch.setattr(docs, "DIVERGENCE", {**DIVERGENCE, key: ("breaks", "어긋남 0")})
    divergence = docs.mermaid(docs.VIEWS[1])
    assert "(어긋남 0)" in divergence and "class rejected breaks" in divergence


def test_a_folded_arrow_is_drawn_once_through_a_join_bar():
    status = docs.mermaid(docs.VIEWS[0])
    assert status.count("노드가 자기 배포의 성공을 알린다") == 1
    assert "join1 --> success" in status
    evidence = docs.mermaid(docs.VIEWS[2])
    assert evidence.count("증거 회수가 검증을 통과한다") == 1
    assert evidence.count("실패로 닫는다") == 1


def test_generated_files_use_no_middle_dot_or_horizontal_bar():
    banned = ("\u00b7", "\u2015")
    texts = {name: text for name, text in docs.outputs().items()}
    readme = docs.repo_root() / "docs" / "deploy-state" / "README.md"
    texts["README.md"] = readme.read_text(encoding="utf-8")
    for svg in docs.default_out_dir().glob("*.svg"):
        texts[svg.name] = svg.read_text(encoding="utf-8")
    for name, text in texts.items():
        for char in banned:
            assert char not in text, f"{name}: U+{ord(char):04X}"


def test_a_leaf_inside_the_region_box_shows_its_group_after_the_name():
    request = docs.mermaid(docs.VIEWS[0])
    assert "pending: 처음 대기 (열림)" in request
    assert " \u00b7 " not in request


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
        "request-lock.mmd",
        "request-tag.mmd",
        "transitions.md",
    }
    # svg는 render.py가 만든다. 쓰기만 한 폴더는 svg가 없어서 검사가 실패한다
    assert docs.main(["--check", "--out", str(out)]) == 1


def test_no_png_is_generated_or_committed():
    assert not list(docs.default_out_dir().glob("*.png"))

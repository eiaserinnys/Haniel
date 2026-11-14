"""그림 생성기 시험: 표시 이름 누락과 생성물 불일치는 실패한다(카드 #1259)."""

import pytest

from haniel_orch.statechart import docs
from haniel_orch.statechart.definitions import DIVERGENCE, AttemptEvidence, OrchRequest


def _views_by_stem() -> dict[str, docs.View]:
    return {view.stem: view for view in docs.VIEWS}


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


def test_request_views_split_by_region_mark_and_event_group_and_cover_every_transition():
    views = _views_by_stem()
    request_views = {stem: view for stem, view in views.items() if view.cls is OrchRequest}
    texts = {stem: docs.mermaid(view) for stem, view in request_views.items()}
    # 어긋남 8이 없어졌다. 설계와 다른 전이의 표시는 옛 노드 규칙(PR 2에서 바뀜)과 설계에 없음 둘이다
    assert "어긋남" not in "".join(texts.values())
    for stem, text in texts.items():
        if stem == "request-divergence":
            assert "(PR 2에서 바뀜)" in text and "(설계에 없음)" in text
        elif request_views[stem].marked is False:
            assert "(PR 2에서 바뀜)" not in text and "(설계에 없음)" not in text, stem
    # 영역마다 자기 그림이 있고, 요청 상태 그림들에는 다른 영역의 잎이 없다
    assert 'state "점검 필요" as lock' in texts["request-lock"] and "l_on" in texts["request-lock"]
    assert 'state "닫힌 뒤 적용 꼬리표" as tag' in texts["request-tag"] and "t_on" in texts["request-tag"]
    for stem in ("request-belief-open", "request-belief-probe", "request-belief-close"):
        assert 'state "지금 시도에 대한 믿음" as belief' in texts[stem] and "b_probe" in texts[stem] or "b_queued" in texts[stem], stem
    for stem, view in request_views.items():
        if view.region == "status":
            assert "l_on" not in texts[stem] and "t_on" not in texts[stem] and "b_unknown" not in texts[stem], stem
    # 접힌 화살표(합류 막대)는 그림에서 한 줄이므로 정의의 전이 전부가 그림 가운데 한 곳에 든다는 것으로 견준다.
    # 요청 상태 영역의 그림 넷(사건 묶음)과 표시 그림은 서로 겹치지 않는다
    drawn = {stem: set(docs._view_arrows(view)) for stem, view in request_views.items()}
    assert set().union(*drawn.values()) == set(docs.transitions(OrchRequest))
    stems = list(drawn)
    for i, left in enumerate(stems):
        for right in stems[i + 1 :]:
            assert not (drawn[left] & drawn[right]), (left, right)


def test_evidence_views_split_moving_and_self_arrows_and_cover_every_transition():
    views = _views_by_stem()
    moving = set(docs._view_arrows(views["attempt-evidence"]))
    dup = set(docs._view_arrows(views["attempt-evidence-dup"]))
    assert moving | dup == set(docs.transitions(AttemptEvidence))
    assert not (moving & dup)
    assert all(a.source != a.target for a in moving)
    assert all(a.source == a.target for a in dup)


def test_divergence_marks_use_only_the_agreed_texts():
    assert {mark[1] for mark in DIVERGENCE.values()} == {"PR 2에서 바뀜", "설계에 없음"}


def test_attempt_evidence_has_no_divergence_mark():
    """실행 방식 조건, 증거 회수, 노드 성공이 시도를 밀어냄, dup 제자리 전이는 설계 글(design.md)이 받친다."""
    assert not [key for key in DIVERGENCE if key[0] == "AttemptEvidence"]


def test_every_pr2_or_absent_mark_names_a_design_line():
    from haniel_orch.statechart.definitions import CHANGES, ABSENT, DESIGN_LINES

    for key, (kind, _) in DIVERGENCE.items():
        if kind in (CHANGES, ABSENT):
            assert key in DESIGN_LINES and DESIGN_LINES[key].startswith("design.md:"), key
    assert set(DESIGN_LINES) <= set(DIVERGENCE)


def test_cond_specific_marks_apply_only_to_their_branch():
    """보내기 실패의 판 없는 가지만 표시가 달리고, 판 1 가지(다시 대기, 서 있는 lock의 실패 뒤 대기)는 설계대로다."""
    arrows = [a for a in docs.transitions(OrchRequest) if a.event == "send_failed" and a.source == "deploying"]
    marked = {a.target for a in arrows if docs._mark(a) is not None}
    unmarked = {(a.target, a.conds) for a in arrows if docs._mark(a) is None}
    assert marked == {"retry"}
    assert ("again", ("is_unknown", "not_newer", "not_legacy", "unlocked")) in unmarked
    assert ("retry", ("is_unknown", "not_newer", "not_legacy", "locked")) in unmarked
    assert ("superseded", ("is_unknown", "newer")) in unmarked


@pytest.mark.parametrize("view", docs.VIEWS, ids=lambda v: v.stem)
def test_state_colors_follow_what_the_picture_draws(view):
    text = docs.mermaid(view)
    classes = [line for line in text.splitlines() if line.startswith("    class ")]
    if view.stem == "attempt-evidence":
        # 주황은 이름으로 적어 둔 설계 밖 잎 하나뿐이다
        assert classes == ["    class ev_superseded notInDesign"]
    else:
        # 어긋남(breaks)으로 적은 전이가 지금은 없으므로 요청 그림에는 빨강도 주황도 없다. dup 그림은 밀림 잎을 그리지 않는다
        assert classes == []

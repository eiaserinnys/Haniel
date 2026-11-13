"""동작 기준 대조: 기준 커밋 155b69f에서 뜬 입출력과 지금이 같은가(카드 #1259 PR 1).

fixture는 `behavior_scenarios.py --write`가 기준 커밋에서 한 번 떴고 다시 뜨지 않는다.
어긋나면 동작이 바뀐 것이다. 기준을 고쳐 맞추지 말고 코드를 본다.
"""

import json

import pytest

from behavior_scenarios import FIXTURE, SCENARIOS, run_rest_scenarios, run_scenario, _norm

BASELINE = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _plain(value):
    return json.loads(json.dumps(value, default=str))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_scenario_matches_baseline(name: str):
    assert _plain(await run_scenario(name)) == BASELINE[name]


async def test_rest_reject_matches_baseline():
    assert _plain(await run_rest_scenarios()) == BASELINE["rest_reject"]


def test_baseline_covers_every_scenario():
    assert set(BASELINE) == set(SCENARIOS) | {"rest_reject"}
    assert _norm({"completed_at": "x", "k": 1}) == {"completed_at": True, "k": 1}

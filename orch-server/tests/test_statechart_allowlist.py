"""허용 목록 시험: 상태 표에 쓰는 SQL은 한 걸음 모듈과 스키마 초기화에만 있다(카드 #1259).

orch-server/src의 문자열 상수에서 INSERT, UPDATE, DELETE 문을 찾는다.
- 네 표(deploy_events, deploy_attempts, deploy_retry_requirements, deploy_retry_source_attempts)는
  한 걸음 모듈(statechart/steps.py, attempt_steps.py, backfill.py)과 스키마 초기화(deploy_attempt_schema.py) 밖에 있으면 실패한다.
- 다른 표의 쓰기는 까닭을 단 아래 명시 목록과 견준다. 새로 생기거나 사라지면 실패한다.
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "haniel_orch"

PROTECTED = {
    "deploy_events",
    "deploy_attempts",
    "deploy_retry_requirements",
    "deploy_retry_source_attempts",
}
PROTECTED_WRITERS = {
    "statechart/steps.py": "한 걸음 모듈(요청 줄): 요청 표와 딸린 표의 상태 쓰기가 여기를 거친다",
    "statechart/attempt_steps.py": "한 걸음 모듈(시도 줄): 시도 표의 상태 쓰기가 여기를 거친다",
    "statechart/backfill.py": "기동 때 다시 계산: 잎이 비었거나 어긋난 줄만 옛 칸에서 채운다",
    "deploy_attempt_schema.py": "스키마 초기화: 칸을 더할 때 한 번 도는 옛 줄 표시(is_self_update)",
}

# 정의 밖 저장의 쓰기. (파일, 표) -> 까닭
OTHER_WRITES = {
    ("deploy_attempt_store.py", "deploy_plan_probes"): "계획 묻기 줄은 정의 밖 저장(PR 2의 믿음 영역이 가져간다)",
    ("deploy_attempt_store_support.py", "deploy_plan_probes"): "계획 묻기 줄 닫기. 정의 밖 저장",
    ("event_store.py", "deploy_plan_probes"): "새 커밋이 옛 대기를 밀 때 열린 계획 묻기를 닫는다. 정의 밖 저장",
    ("event_store_lifecycle.py", "deploy_plan_probes"): "거절할 때 열린 계획 묻기를 닫는다. 정의 밖 저장",
    ("event_store_mutations.py", "deploy_plan_probes"): "닫힌 요청의 열린 계획 묻기를 닫는다. 정의 밖 저장",
    ("node_deploy_store.py", "deploy_plan_probes"): "노드의 배포 성공 보고가 열린 계획 묻기를 닫는다. 정의 밖 저장",
    ("node_deploy_store.py", "node_deploy_attempts"): "노드 시도 줄은 정의 밖 저장(설계의 실행 정의는 노드 쪽)",
    ("event_store_nodes.py", "nodes"): "노드 접속 표. 배포 상태가 아니다",
    ("deploy_attempt_schema.py", "deploy_retry_requirements_new"): "표시 줄 표를 다시 만드는 이전의 임시 표(두 시도 번호 칸의 NOT NULL을 풀고 inspection_required를 더한다). 이전이 끝나면 이름을 바꿔 표시 줄 표가 된다",
}

_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO\s+(\w+)|UPDATE\s+(\w+)\s+SET|DELETE\s+FROM\s+(\w+))",
    re.IGNORECASE,
)


def _sql_writes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in _WRITE.finditer(node.value):
                    table = next(group for group in match.groups() if group)
                    found.add((relative, table.lower()))
    return found


def test_protected_tables_are_written_only_by_the_step_module_and_schema_init():
    outside = sorted(
        (file, table)
        for file, table in _sql_writes()
        if table in PROTECTED and file not in PROTECTED_WRITERS
    )
    assert outside == [], f"한 걸음 모듈 밖에서 상태 표에 쓴다: {outside}"


def test_step_modules_really_write_every_protected_table():
    step_modules = {"statechart/steps.py", "statechart/attempt_steps.py"}
    written = {table for file, table in _sql_writes() if file in step_modules}
    assert PROTECTED <= written


def test_other_table_writes_match_the_explicit_list():
    other = {
        (file, table)
        for file, table in _sql_writes()
        if table not in PROTECTED
    }
    assert other == set(OTHER_WRITES), (
        "새로 생긴 쓰기: "
        f"{sorted(other - set(OTHER_WRITES))}, 사라진 쓰기: {sorted(set(OTHER_WRITES) - other)}"
    )
    assert all(OTHER_WRITES.values()), "모든 항목에 까닭이 있어야 한다"


def test_scanner_sees_a_planted_direct_update():
    sample = ast.parse('SQL = "UPDATE deploy_events SET status = ? WHERE deploy_id = ?"')
    constants = [
        node.value
        for node in ast.walk(sample)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert _WRITE.search(constants[0]).group(2) == "deploy_events"


def test_removed_general_write_apis_are_gone():
    import haniel_orch.deploy_attempt_store as attempt_store
    from haniel_orch.event_store import EventStore

    for name in ("update_deploy_status", "transition_deploy_status", "_update_deploy_status_unlocked"):
        assert not hasattr(EventStore, name), name
    assert not hasattr(attempt_store.DeployAttemptStore, "cleanup_retry")

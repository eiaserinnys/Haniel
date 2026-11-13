# 배포 상태 전이 표

`python -m haniel_orch.statechart.docs`가 정의에서 만든 파일이다. 손으로 고치지 않는다.
「설계와 다른 점」에 번호가 있으면 설계가 어긋남으로 적은 동작이고, `PR 2에서 바뀜`은 지금 동작을 그대로 옮겼으나 설계 정의에는 없는 전이다.

## OrchRequest

| 출발 | 사건 | 조건 | 도착 | 함께 쓰는 것 | 설계와 다른 점 |
|---|---|---|---|---|---|
| 열림 · 처음 대기 | 승인해서 시도를 시작한다 (`begin_attempt`) | - | 열림 · 배포 중 | approved_by를 적는다. 시도 줄은 같은 걸음에서 만든다 | - |
| 열림 · 처음 대기 | 거절한다 (`reject`) | - | 닫힘 · 거절 | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다 | - |
| 열림 · 처음 대기 | 더 새 커밋이 들어온다 (`newer_commit`) | - | 닫힘 · 새 커밋에 밀림 | reject_reason을 `superseded by <끝 줄>`로 적는다. 표시 줄과 계보 줄을 지운다 | - |
| 열림 · 처음 대기 | 노드가 자기 배포의 성공을 알린다 (`node_succeeded`) | - | 닫힘 · 성공 | approved_by와 error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 도는 시도는 같은 걸음에서 밀어낸다 | - |
| 열림 · 처음 대기 | 목표 커밋에 와 있다고 관측한다 (`observed`) | 머리 셋이 같음 그리고 도는 시도 없음 그리고 실패한 노드 보고 없음 | 닫힘 · 성공 | status와 잎만 바꾼다 | - |
| 열림 · 처음 대기 | 더 새 관측이 자기 갱신을 닫는다 (`observed_self_update`) | 자기 갱신 그리고 머리 셋이 같음 그리고 도는 시도 없음 | 닫힘 · 성공 | 표시 줄과 계보 줄을 지운다 | PR 2에서 바뀜 |
| 열림 · 표시 줄 있는 대기 | 승인해서 시도를 시작한다 (`begin_attempt`) | - | 열림 · 배포 중 | approved_by를 적는다. 시도 줄은 같은 걸음에서 만든다 | - |
| 열림 · 표시 줄 있는 대기 | 거절한다 (`reject`) | - | 닫힘 · 거절 | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다 | - |
| 열림 · 표시 줄 있는 대기 | 더 새 커밋이 들어온다 (`newer_commit`) | - | 닫힘 · 새 커밋에 밀림 | reject_reason을 `superseded by <끝 줄>`로 적는다. 표시 줄과 계보 줄을 지운다 | - |
| 열림 · 표시 줄 있는 대기 | 노드가 자기 배포의 성공을 알린다 (`node_succeeded`) | - | 닫힘 · 성공 | approved_by와 error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 도는 시도는 같은 걸음에서 밀어낸다 | - |
| 열림 · 표시 줄 있는 대기 | 더 새 관측이 자기 갱신을 닫는다 (`observed_self_update`) | 자기 갱신 그리고 머리 셋이 같음 그리고 도는 시도 없음 | 닫힘 · 성공 | 표시 줄과 계보 줄을 지운다 | PR 2에서 바뀜 |
| 닫힘 · 성공 | 거절한다 (`reject`) | - | 닫힘 · 거절 | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다 | 어긋남 8 |
| 열림 · 배포 중 | 시도가 성공으로 끝난다 (`attempt_succeeded`) | - | 닫힘 · 성공 | error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다 | - |
| 열림 · 배포 중 | 시도가 실패로 끝난다 (`attempt_failed`) | 더 새 커밋이 있음 | 닫힘 · 새 커밋에 밀림 | reject_reason을 `superseded by newer canonical`로 적는다. 표시 줄과 계보 줄을 지운다 | - |
| 열림 · 배포 중 | 시도가 실패로 끝난다 (`attempt_failed`) | - | 열림 · 표시 줄 있는 대기 | approved_by, error, duration_ms를 지운다. 표시 줄을 세우고 계보 줄을 더한다 | - |
| 열림 · 배포 중 | 거절한다 (`reject`) | - | 닫힘 · 거절 | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다 | 어긋남 8 |
| 열림 · 배포 중 | 노드가 자기 배포의 성공을 알린다 (`node_succeeded`) | - | 닫힘 · 성공 | approved_by와 error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 도는 시도는 같은 걸음에서 밀어낸다 | - |
| 열림 · 배포 중 | 노드가 자기 배포의 실패를 알린다 (`node_failed`) | 표시 줄 있음 | 열림 · 표시 줄 있는 대기 | approved_by, error, duration_ms를 지운다. 표시 줄은 세우지 않는다 | PR 2에서 바뀜 |
| 열림 · 배포 중 | 노드가 자기 배포의 실패를 알린다 (`node_failed`) | 표시 줄 없음 | 열림 · 처음 대기 | approved_by, error, duration_ms를 지운다. 표시 줄은 세우지 않는다 | PR 2에서 바뀜 |
| 닫힘 · 거절 | 거절한다 (`reject`) | - | 닫힘 · 거절 | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다 | 어긋남 8 |
| 닫힘 · 거절 | 보류 시한이 지나 되살린다 (`reopen`) | 자기 갱신 그리고 보류 시한 지남 그리고 다른 열린 줄 없음 | 열림 · 처음 대기 | reject_reason, postponed_until, approved_by, error를 지운다 | PR 2에서 바뀜 |
| 닫힘 · 새 커밋에 밀림 | 거절한다 (`reject`) | - | 닫힘 · 거절 | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다 | 어긋남 8 |

## AttemptEvidence

| 출발 | 사건 | 조건 | 도착 | 함께 쓰는 것 | 설계와 다른 점 |
|---|---|---|---|---|---|
| 증거 아직 없음 | 성공 결과가 온다 (`result_ok`) | execute 방식 | 성공 결과만 옴 | result_status와 duration_ms를 적는다. 대조가 이미 왔으면 outcome을 success로 닫는다 | PR 2에서 바뀜 |
| 증거 아직 없음 | 목표와 같은 대조가 온다 (`settled_ok`) | execute 방식 | 체크아웃 대조만 옴 | settled 세 칸을 적는다. 결과가 이미 왔으면 outcome을 success로 닫는다 | PR 2에서 바뀜 |
| 증거 아직 없음 | 대조가 온다 (`settled_noted`) | execute 방식 아님 | 체크아웃 대조만 옴 | settled 세 칸만 적는다 | PR 2에서 바뀜 |
| 증거 아직 없음 | 실패로 닫는다 (`fail`) | - | 실패로 종결 | outcome을 failed로 닫고 terminal 네 칸을 적는다. 결과 글이나 대조 글의 칸을 함께 적을 수 있다 | - |
| 증거 아직 없음 | 증거 회수가 검증을 통과한다 (`recovered`) | 증거 회수 방식 | 성공으로 종결 | outcome을 evidence_recovered_success로 닫는다 | PR 2에서 바뀜 |
| 증거 아직 없음 | 노드의 배포 성공이 밀어낸다 (`superseded_by_node`) | - | 노드의 배포 성공에 밀려 종결 | outcome을 superseded로 닫고 terminal 네 칸을 적는다 | PR 2에서 바뀜 |
| 성공 결과만 옴 | 성공 결과가 온다 (`result_ok`) | execute 방식 | 성공 결과만 옴 | result_status와 duration_ms를 적는다. 대조가 이미 왔으면 outcome을 success로 닫는다 | PR 2에서 바뀜 |
| 성공 결과만 옴 | 목표와 같은 대조가 온다 (`settled_ok`) | execute 방식 | 성공으로 종결 | settled 세 칸을 적는다. 결과가 이미 왔으면 outcome을 success로 닫는다 | PR 2에서 바뀜 |
| 성공 결과만 옴 | 실패로 닫는다 (`fail`) | - | 실패로 종결 | outcome을 failed로 닫고 terminal 네 칸을 적는다. 결과 글이나 대조 글의 칸을 함께 적을 수 있다 | - |
| 성공 결과만 옴 | 증거 회수가 검증을 통과한다 (`recovered`) | 증거 회수 방식 | 성공으로 종결 | outcome을 evidence_recovered_success로 닫는다 | PR 2에서 바뀜 |
| 성공 결과만 옴 | 노드의 배포 성공이 밀어낸다 (`superseded_by_node`) | - | 노드의 배포 성공에 밀려 종결 | outcome을 superseded로 닫고 terminal 네 칸을 적는다 | PR 2에서 바뀜 |
| 체크아웃 대조만 옴 | 성공 결과가 온다 (`result_ok`) | execute 방식 | 성공으로 종결 | result_status와 duration_ms를 적는다. 대조가 이미 왔으면 outcome을 success로 닫는다 | PR 2에서 바뀜 |
| 체크아웃 대조만 옴 | 목표와 같은 대조가 온다 (`settled_ok`) | execute 방식 | 체크아웃 대조만 옴 | settled 세 칸을 적는다. 결과가 이미 왔으면 outcome을 success로 닫는다 | PR 2에서 바뀜 |
| 체크아웃 대조만 옴 | 대조가 온다 (`settled_noted`) | execute 방식 아님 | 체크아웃 대조만 옴 | settled 세 칸만 적는다 | PR 2에서 바뀜 |
| 체크아웃 대조만 옴 | 실패로 닫는다 (`fail`) | - | 실패로 종결 | outcome을 failed로 닫고 terminal 네 칸을 적는다. 결과 글이나 대조 글의 칸을 함께 적을 수 있다 | - |
| 체크아웃 대조만 옴 | 증거 회수가 검증을 통과한다 (`recovered`) | 증거 회수 방식 | 성공으로 종결 | outcome을 evidence_recovered_success로 닫는다 | PR 2에서 바뀜 |
| 체크아웃 대조만 옴 | 노드의 배포 성공이 밀어낸다 (`superseded_by_node`) | - | 노드의 배포 성공에 밀려 종결 | outcome을 superseded로 닫고 terminal 네 칸을 적는다 | PR 2에서 바뀜 |

# 배포 상태 전이 표

`python -m haniel_orch.statechart.docs`가 정의에서 만든 파일이다. 손으로 고치지 않는다.
「설계와 다른 점」은 설계 글(design.md)과 견준 표시다. `어긋남 N`은 설계가 어긋남으로 적은 동작이고, `PR 2에서 바뀜`은 괄호 속 design.md 줄이 옛 노드 규칙이나 PR 2에서 바꾼다고 적은 동작이며, `설계에 없음`은 설계에 그 전이가 없는 동작이다. 표시가 없는 전이는 설계 글이 받친다. 요청 정의의 점검 필요(lock) 영역과 닫힌 뒤 적용 꼬리표(tag) 영역은 요청 상태 영역과 함께 한 사건으로 움직이므로, 세 영역의 화살표가 같은 사건 이름으로 표에 나란히 있다. 시도의 닫힘(outcome)은 증거 정의의 상태가 아니라 시도 줄의 닫힘 칸이다. 오케스트레이터가 스스로 내는 닫힘(실행 임대 마감, 보내기 실패)은 증거 잎을 옮기지 않으므로 이 표에 없다.

## OrchRequest

| 출발 | 사건 | 조건 | 도착 | 함께 쓰는 것 | 설계와 다른 점 |
|---|---|---|---|---|---|
| 처음 대기 (열림) | 승인해서 시도를 시작한다 (`begin_attempt`) | 점검 필요가 없거나 승인 원천이 단건 승인 | 배포 중 (열림) | approved_by를 적는다. 시도 줄은 같은 걸음에서 만든다 | - |
| 처음 대기 (열림) | 거절한다 (`reject`) | - | 거절 (닫힘) | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다 | - |
| 처음 대기 (열림) | 더 새 커밋이 들어온다 (`newer_commit`) | - | 새 커밋에 밀림 (닫힘) | reject_reason을 `superseded by <끝 줄>`로 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다 | - |
| 처음 대기 (열림) | 노드가 자기 배포의 성공을 알린다 (`node_succeeded`) | - | 성공 (닫힘) | approved_by와 error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 도는 시도는 같은 걸음에서 밀어낸다 | - |
| 처음 대기 (열림) | 노드가 자기 배포의 실패를 알린다 (`node_failed`) | - | 표시 줄 있는 대기 (열림) | 배포 중이었으면 approved_by, error, duration_ms를 지운다. 표시 줄이 없으면 두 시도 번호 칸이 빈 표시 줄을 만든다 | - |
| 처음 대기 (열림) | 지금 시도가 아닌 실행의 성공 증거가 닿는다 (`success_evidence`) | 뒤 시도를 연 적이 없음 | 성공 (닫힘) | error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 증거를 적은 시도의 닫힘(outcome)은 그대로다 | PR 2에서 바뀜 (design.md:123, 431) |
| 처음 대기 (열림) | 닫힌 시도의 늦은 실패가 닿는다 (`late_failed`) | - | 표시 줄 있는 대기 (열림) | 실행한 그 시도를 source로 둔 표시 줄과 계보 줄을 만든다 | - |
| 처음 대기 (열림) | 목표 커밋에 와 있다고 관측한다 (`observed`) | 머리 셋이 같음 그리고 도는 시도 없음 그리고 실패한 노드 보고 없음 그리고 점검 필요 꺼짐 | 성공 (닫힘) | status와 잎만 바꾼다 | - |
| 처음 대기 (열림) | 더 새 관측이 자기 갱신을 닫는다 (`observed_self_update`) | 자기 갱신 그리고 머리 셋이 같음 그리고 도는 시도 없음 | 성공 (닫힘) | 표시 줄과 계보 줄을 지운다 | PR 2에서 바뀜 (design.md:382, 489) |
| 점검 필요 없음 | 시도가 실패로 끝난다 (`attempt_failed`) | - | 점검 필요 | 점검 필요를 켠다(표시 줄이 있으면 inspection_required를 1로). 요청이 이미 닫혀 있어도 켜고, 그때 요청 줄의 다른 칸과 표시 줄은 쓰지 않는다 | - |
| 점검 필요 없음 | 허가가 노드에 닿지 못해 시도를 닫는다 (`send_failed`) | - | 점검 필요 | 점검 필요를 켠다(표시 줄이 있으면 inspection_required를 1로). 옛 노드 규칙이라 보내기 실패도 켠다 | PR 2에서 바뀜 (design.md:429-430) |
| 점검 필요 없음 | 노드가 자기 배포의 실패를 알린다 (`node_failed`) | 요청이 열려 있음 | 점검 필요 | 점검 필요를 켠다(표시 줄이 없으면 두 시도 번호 칸이 빈 표시 줄을 만든다). 도는 시도가 있는 배포 중이면 칸만 켜고 요청 상태와 시도는 그대로 둔다 | - |
| 점검 필요 없음 | 닫힌 시도의 늦은 실패가 닿는다 (`late_failed`) | - | 점검 필요 | 점검 필요를 켠다(표시 줄이 있으면 inspection_required를 1로). 배포 중이면 칸만 서고 도는 시도는 그대로다. 닫힌 요청도 칸만 켠다 | - |
| 표시 줄 있는 대기 (열림) | 승인해서 시도를 시작한다 (`begin_attempt`) | 점검 필요가 없거나 승인 원천이 단건 승인 | 배포 중 (열림) | approved_by를 적는다. 시도 줄은 같은 걸음에서 만든다 | - |
| 표시 줄 있는 대기 (열림) | 거절한다 (`reject`) | - | 거절 (닫힘) | reject_reason을 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다 | - |
| 표시 줄 있는 대기 (열림) | 더 새 커밋이 들어온다 (`newer_commit`) | - | 새 커밋에 밀림 (닫힘) | reject_reason을 `superseded by <끝 줄>`로 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다 | - |
| 표시 줄 있는 대기 (열림) | 노드가 자기 배포의 성공을 알린다 (`node_succeeded`) | - | 성공 (닫힘) | approved_by와 error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 도는 시도는 같은 걸음에서 밀어낸다 | - |
| 표시 줄 있는 대기 (열림) | 지금 시도가 아닌 실행의 성공 증거가 닿는다 (`success_evidence`) | 뒤 시도를 연 적이 없음 | 성공 (닫힘) | error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 증거를 적은 시도의 닫힘(outcome)은 그대로다 | PR 2에서 바뀜 (design.md:123, 431) |
| 표시 줄 있는 대기 (열림) | 더 새 관측이 자기 갱신을 닫는다 (`observed_self_update`) | 자기 갱신 그리고 머리 셋이 같음 그리고 도는 시도 없음 | 성공 (닫힘) | 표시 줄과 계보 줄을 지운다 | PR 2에서 바뀜 (design.md:382, 489) |
| 점검 필요 | 승인해서 시도를 시작한다 (`begin_attempt`) | 승인 원천이 단건 승인 | 점검 필요 없음 | 점검 필요를 내린다(바깥 칸: 표시 줄의 inspection_required를 0으로). 내릴 수 있는 원천은 단건 승인 하나다 | - |
| 꼬리표 없음 | 지금 시도가 아닌 실행의 성공 증거가 닿는다 (`success_evidence`) | 거절이나 밀림으로 닫힘 | 닫힌 뒤 적용 | 닫힌 뒤 적용 꼬리표를 세운다(applied_after_close_at과 applied_after_close_attempt_id를 적는다). 요청 상태와 다른 칸은 그대로다. 한 번 서면 내리지 않는다 | - |
| 배포 중 (열림) | 시도가 성공으로 끝난다 (`attempt_succeeded`) | - | 성공 (닫힘) | error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 점검 필요는 건드리지 않는다 | - |
| 배포 중 (열림) | 시도가 실패로 끝난다 (`attempt_failed`) | 더 새 커밋이 있음 | 새 커밋에 밀림 (닫힘) | reject_reason을 `superseded by newer canonical`로 적는다(바깥 status는 rejected가 된다). 표시 줄과 계보 줄을 지운다 | - |
| 배포 중 (열림) | 시도가 실패로 끝난다 (`attempt_failed`) | 더 새 커밋이 없음 | 표시 줄 있는 대기 (열림) | approved_by, error, duration_ms를 지운다. 표시 줄을 세우고(source를 이 시도로 옮긴다) 계보 줄을 더한다 | - |
| 배포 중 (열림) | 허가가 노드에 닿지 못해 시도를 닫는다 (`send_failed`) | 더 새 커밋이 있음 | 새 커밋에 밀림 (닫힘) | reject_reason을 `superseded by newer canonical`로 적는다(바깥 status는 rejected가 된다). 표시 줄과 계보 줄을 지운다 | - |
| 배포 중 (열림) | 허가가 노드에 닿지 못해 시도를 닫는다 (`send_failed`) | 더 새 커밋이 없음 | 표시 줄 있는 대기 (열림) | approved_by, error, duration_ms를 지운다. 표시 줄을 세우고 계보 줄을 더한다. 실행하지 않았음이 확실하므로 source는 옮기지 않는다(첫 시도면 빈 칸) | PR 2에서 바뀜 (design.md:429-430) |
| 배포 중 (열림) | 노드가 자기 배포의 성공을 알린다 (`node_succeeded`) | - | 성공 (닫힘) | approved_by와 error를 지우고 duration_ms를 적는다. 표시 줄과 계보 줄을 지운다. 도는 시도는 같은 걸음에서 밀어낸다 | - |
| 배포 중 (열림) | 노드가 자기 배포의 실패를 알린다 (`node_failed`) | 도는 시도 없음 | 표시 줄 있는 대기 (열림) | 배포 중이었으면 approved_by, error, duration_ms를 지운다. 표시 줄이 없으면 두 시도 번호 칸이 빈 표시 줄을 만든다 | PR 2에서 바뀜 (design.md:125) |
| 거절 (닫힘) | 보류 시한이 지나 되살린다 (`reopen`) | 자기 갱신 그리고 보류 시한 지남 그리고 다른 열린 줄 없음 그리고 점검 필요 켜짐 | 표시 줄 있는 대기 (열림) | reject_reason, postponed_until, approved_by, error를 지운다. 두 시도 번호 칸이 빈 표시 줄을 만든다(점검 필요는 켜진 채 그대로) | 설계에 없음 (design.md:489) |
| 거절 (닫힘) | 보류 시한이 지나 되살린다 (`reopen`) | 자기 갱신 그리고 보류 시한 지남 그리고 다른 열린 줄 없음 그리고 점검 필요 꺼짐 | 처음 대기 (열림) | reject_reason, postponed_until, approved_by, error를 지운다 | 설계에 없음 (design.md:489) |
| 새 커밋에 밀림 (닫힘) | 보류 시한이 지나 되살린다 (`reopen`) | 자기 갱신 그리고 보류 시한 지남 그리고 다른 열린 줄 없음 그리고 점검 필요 켜짐 | 표시 줄 있는 대기 (열림) | reject_reason, postponed_until, approved_by, error를 지운다. 두 시도 번호 칸이 빈 표시 줄을 만든다(점검 필요는 켜진 채 그대로) | 설계에 없음 (design.md:489) |
| 새 커밋에 밀림 (닫힘) | 보류 시한이 지나 되살린다 (`reopen`) | 자기 갱신 그리고 보류 시한 지남 그리고 다른 열린 줄 없음 그리고 점검 필요 꺼짐 | 처음 대기 (열림) | reject_reason, postponed_until, approved_by, error를 지운다 | 설계에 없음 (design.md:489) |

## AttemptEvidence

| 출발 | 사건 | 조건 | 도착 | 함께 쓰는 것 | 설계와 다른 점 |
|---|---|---|---|---|---|
| 증거 아직 없음 (열림) | 성공 결과가 온다 (`result_ok`) | execute 방식 | 성공 결과만 옴 (열림) | result_status와 duration_ms를 적는다. 대조가 이미 왔으면 outcome을 success로 닫는다 | - |
| 증거 아직 없음 (열림) | 목표와 같은 대조가 온다 (`settled_ok`) | execute 방식 | 체크아웃 대조만 옴 (열림) | settled 세 칸을 적는다. 결과가 이미 왔으면 outcome을 success로 닫는다 | - |
| 증거 아직 없음 (열림) | 대조가 온다 (`settled_noted`) | execute 방식 아님 | 체크아웃 대조만 옴 (열림) | settled 세 칸만 적는다 | - |
| 증거 아직 없음 (열림) | 실패로 닫는다 (`fail`) | - | 실패로 종결 (닫힘) | outcome을 failed로 닫고 terminal 네 칸을 적는다. 결과 글이나 대조 글의 칸을 함께 적을 수 있다 | - |
| 증거 아직 없음 (열림) | 증거 회수가 검증을 통과한다 (`recovered`) | 증거 회수 방식 | 성공으로 종결 (닫힘) | outcome을 evidence_recovered_success로 닫는다 | - |
| 증거 아직 없음 (열림) | 노드의 배포 성공이 밀어낸다 (`superseded_by_node`) | - | 노드의 배포 성공에 밀려 종결 (닫힘) | outcome을 superseded로 닫고 terminal 네 칸을 적는다 | - |
| 성공 결과만 옴 (열림) | 성공 결과가 온다 (`result_ok`) | execute 방식 | 성공 결과만 옴 (열림) | result_status와 duration_ms를 적는다. 대조가 이미 왔으면 outcome을 success로 닫는다 | - |
| 성공 결과만 옴 (열림) | 목표와 같은 대조가 온다 (`settled_ok`) | execute 방식 | 성공으로 종결 (닫힘) | settled 세 칸을 적는다. 결과가 이미 왔으면 outcome을 success로 닫는다 | - |
| 성공 결과만 옴 (열림) | 실패로 닫는다 (`fail`) | - | 실패로 종결 (닫힘) | outcome을 failed로 닫고 terminal 네 칸을 적는다. 결과 글이나 대조 글의 칸을 함께 적을 수 있다 | - |
| 성공 결과만 옴 (열림) | 증거 회수가 검증을 통과한다 (`recovered`) | 증거 회수 방식 | 성공으로 종결 (닫힘) | outcome을 evidence_recovered_success로 닫는다 | - |
| 성공 결과만 옴 (열림) | 노드의 배포 성공이 밀어낸다 (`superseded_by_node`) | - | 노드의 배포 성공에 밀려 종결 (닫힘) | outcome을 superseded로 닫고 terminal 네 칸을 적는다 | - |
| 체크아웃 대조만 옴 (열림) | 성공 결과가 온다 (`result_ok`) | execute 방식 | 성공으로 종결 (닫힘) | result_status와 duration_ms를 적는다. 대조가 이미 왔으면 outcome을 success로 닫는다 | - |
| 체크아웃 대조만 옴 (열림) | 목표와 같은 대조가 온다 (`settled_ok`) | execute 방식 | 체크아웃 대조만 옴 (열림) | settled 세 칸을 적는다. 결과가 이미 왔으면 outcome을 success로 닫는다 | - |
| 체크아웃 대조만 옴 (열림) | 대조가 온다 (`settled_noted`) | execute 방식 아님 | 체크아웃 대조만 옴 (열림) | settled 세 칸만 적는다 | - |
| 체크아웃 대조만 옴 (열림) | 실패로 닫는다 (`fail`) | - | 실패로 종결 (닫힘) | outcome을 failed로 닫고 terminal 네 칸을 적는다. 결과 글이나 대조 글의 칸을 함께 적을 수 있다 | - |
| 체크아웃 대조만 옴 (열림) | 증거 회수가 검증을 통과한다 (`recovered`) | 증거 회수 방식 | 성공으로 종결 (닫힘) | outcome을 evidence_recovered_success로 닫는다 | - |
| 체크아웃 대조만 옴 (열림) | 노드의 배포 성공이 밀어낸다 (`superseded_by_node`) | - | 노드의 배포 성공에 밀려 종결 (닫힘) | outcome을 superseded로 닫고 terminal 네 칸을 적는다 | - |

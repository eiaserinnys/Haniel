# 오케스트레이터 배포 상태 정의

오케스트레이터(`orch-server`)가 배포 요청 줄(`deploy_events`)과 시도 줄(`deploy_attempts`)의 상태를
바꾸는 길은 모두 코드의 상태 정의를 거친다(카드 #1259 PR 1이 지금 동작 그대로 옮겼고, PR 2a가 「점검 필요」 영역을,
PR 2b-1이 닫힌 시도의 늦은 글 규칙과 「닫힌 뒤 적용」 꼬리표 영역을, PR 2b-2가 「지금 시도에 대한 믿음」 영역과 다시 대기(again),
닫힘 일곱, hello 대조, 믿음별 마감, 할 일 기호를 더했다).
오케스트레이터 정본은 `orch-server/src/haniel_orch/statechart/definitions.py`, 노드 실행 정본은
`src/haniel/core/node_run.py`이며 여기 그림과 표는 그 정의에서 뽑는다.

노드 정의는 제품의 실행 경계를 반영한다. 순서기가 자리를 줄 때 재검증을 통과하면 시작하고,
가드나 재검증에 실패하면 실행하지 않음으로 닫는다. 자체 배포의 시작도 기존 시작 보고를 보존하도록
시작 기호를 낸다.

| 정의 | 단위 | 잎 칸 | 바깥 칸 |
|---|---|---|---|
| `OrchRequest` | 배포 요청 한 건. 영역 넷: 요청 상태, 지금 시도에 대한 믿음, 점검 필요, 닫힌 뒤 적용 꼬리표 | `deploy_events.sm_status`(요청 상태), `deploy_events.sm_belief`(믿음), `deploy_events.sm_lock`(점검 필요), `deploy_events.sm_tag`(꼬리표) | `status`, `reject_reason`, 표시 줄(점검 필요의 바깥 칸은 `inspection_required`)과 계보 줄, 꼬리표의 `applied_after_close_at`과 `applied_after_close_attempt_id`. 믿음의 바깥 칸은 시도 줄의 마감 종류(`deadline_kind`)다 |
| `AttemptEvidence` | 시도 한 건의 종결 증거 | `deploy_attempts.sm_evidence` | `result_status`, `settled_*`. 시도의 닫힘(`outcome`, `terminal_*`, `closure`, `completed_at`)은 증거와 따로 움직이는 닫힘 칸이다 |
| `NodeRun` | 노드 실행 한 건의 실행과 보고 영역 | 실행 중 잎은 메모리, 빚진 종결 묶음은 보낼 것 파일 | 보낼 것 파일의 글과 시도 번호 |

요청 상태의 대기는 셋이다: 처음 대기(표시 줄 없음), 다시 대기(표시 줄 있고 점검 필요 꺼짐, `again`), 실패 뒤 대기(표시 줄 있고 점검
필요 켜짐, `retry`). 바깥 `status`는 셋 모두 `pending`이다. 판 1 노드(wire-v1을 아는 노드, hello의 `deploy_protocol`)의 시도만 다시
대기에 간다: 실행하지 않았거나 실행 여부가 불명한 닫힘(보내기 실패, 노드의 「실행하지 않음」 답, 짧은 마감, hello 목록에 없는 닿았는지
모름과 줄 서 있음)은 점검 필요를 세우지 않고 다시 대기로 가되 이미 서 있으면 실패 뒤 대기로 간다. 실제로 돌다 실패한 닫힘(노드 실패 보고,
실행 임대 마감, 유예 마감, hello 목록에 없는 시작했다)은 점검 필요를 켠다. 판 없는 시도(`deploy_attempts.node_protocol`이 NULL)는
옛 노드 규칙대로 모든 닫힘이 점검 필요를 켠다. 시도 줄의 `closure`가 닫힘의 종류(`not_executed`, `execution_unknown`, `failed`)를 적는다.

믿음별 마감(wire-v1 5절)은 시도 줄의 `deadline_kind`로 가른다: 판 없는 시도는 실행 임대(`lease`) 하나, 판 1 시도는 닿았는지 모름의 짧은
마감(`ack`, 30초), 줄 서 있음이고 붙어 있는 동안의 세지 않음(`none`. `deadline_at`은 마지막 값 그대로 두고 읽지 않는다), 줄 서 있음이고
끊긴 동안의 유예 마감(`grace`, 실행 임대와 같은 5분), 시작했다의 실행 임대(`lease`)다. 마감 사건은 종류와 믿음이 맞을 때만 정의에 들어간다.

할 일 기호는 전이의 on 콜백이 내고(`send_permit`, `send_probe`, `auto_reject`, `closed_plan`, `ack`, `tag`, 증거의 `terminal_ok`,
`terminal_fail`, `dup`) 호출부가 커밋 뒤 그 기호가 나왔을 때만 허가와 탐침을 보내고 거절 답과 「종결을 받았다」를 보낸다. DB에 적지 않는다.
노드 글을 (사건, 사실)로 바꾸는 받기 함수는 `deploy_attempt_intake.py`에 있고 코디네이터와 곱 훑기가 같이 부른다.

한 걸음은 저장소 변경 잠금과 트랜잭션 안에서 돈다(요청 줄은 `statechart/steps.py`, 시도 줄은 `statechart/attempt_steps.py`).
줄에서 새 인스턴스를 되살려 사건 하나를 넣고, 잎과 바깥 칸과 딸린 줄을 같은 트랜잭션에 쓴다. 전이가 없는 사건은
`NoTransition`으로 거절되고 줄에는 아무것도 쓰이지 않는다. 방송, 푸시, 타이머는 지금처럼 커밋 뒤 호출부에서 돈다.
기동 때 잎이 비었거나 바깥 칸과 어긋난 줄은 옛 칸에서 다시 계산하고(`statechart/backfill.py`), 빈 시도 순서 칸(`attempt_seq`)과
꼬리표 칸과 믿음 칸(도는 시도가 없으면 도는 시도 없음, 있으면 실행 시작 시각이 있으면 시작했다, 없으면 닿았는지 모름)을 채운다.

시도의 닫힘과 증거 종결은 다른 것이다(design.md 79). 노드 글로 닫히는 시도(실패 결과, `deploy_attempt_terminal`)는 닫힘과
증거를 한 걸음에 끝내지만, 오케스트레이터가 스스로 내는 닫힘(실행 임대 마감, 보내기 실패, 다른 성공에 밀림)은 닫힘 칸만 쓴다.
그림과 표의 `superseded_by_node` 전이와 `ev_superseded` 잎은 기존 저장 행과 정의 호환을 위해 남아 있다. 현재 제품의 두 성공 경로는
`supersede_attempt`로 닫힘만 적고 증거 잎은 그대로 둔다.
그 뒤에 닿은 결과와 정착은 그 시도의 증거로 적힌다(`late_evidence`). 성공으로 종결되면 판 1 시도는 뒤 시도가 실행을 시작하지 않았을 때,
판 없는 시도는 뒤 시도를 연 적이 없을 때 열린 요청을 성공으로 닫는다. 도는 시도가 있으면 함께 밀어 닫는다. 실패로 종결되면 점검 필요를
켠다. 실행 상한은 첫 실행 증거(진행 알림, 결과, 정착)의 시각 `execution_started_at`에서 잰다.

## 그림

그림은 `generated/` 아래에 있다. `.mmd`가 원본이고 `.svg`는 statechart 스킬의 `render.py`(Pele)로 만든다.

| 그림 | 내용 |
|---|---|
| [request-status](generated/request-status.svg) | 요청 상태 가운데 승인과 종결(승인, 시도의 성공과 실패, 거절, 새 커밋, 관측, 되살림). 설계 정의와 같은 전이 |
| [request-status-node](generated/request-status-node.svg) | 요청 상태 가운데 노드 스스로 한 배포의 보고와 지금 시도가 아닌 실행의 증거(늦은 성공, 늦은 실패) |
| [request-status-closings](generated/request-status-closings.svg) | 요청 상태 가운데 판 1 시도의 닫힘 일곱(보내기 실패, 실행하지 않음 답, 짧은 마감, 실행 임대 마감, 유예 마감, hello 목록에 없음) |
| [request-status-auto](generated/request-status-auto.svg) | 요청 상태 가운데 자동 요청과 계획 답의 실패 저널 |
| [request-divergence](generated/request-divergence.svg) | 요청 상태 가운데 설계와 다른 전이(PR 2에서 바뀜, 설계에 없음) |
| [request-belief-open](generated/request-belief-open.svg) | 믿음 영역 가운데 허가를 보내고 노드가 답하는 길(승인, 계획 묻기, 받았다, 시작했다, hello 목록에 있음) |
| [request-belief-probe](generated/request-belief-probe.svg) | 믿음 영역 가운데 계획 묻기가 닫히는 길(답, 재확인, 마감, 끊김, 거절, 새 커밋, 실패 저널) |
| [request-belief-close](generated/request-belief-close.svg) | 믿음 영역 가운데 시도가 닫히는 길(닫힘 일곱, 성공, 실패, 늦은 성공, 노드 성공) |
| [request-lock](generated/request-lock.svg) | 점검 필요 영역. 실제로 돌다 실패한 닫힘(과 판 없는 시도의 모든 닫힘)이 켜고, 원천이 단건 승인인 승인이 시도를 열 때에만 내린다 |
| [request-tag](generated/request-tag.svg) | 닫힌 뒤 적용 꼬리표 영역. 거절이나 밀림으로 닫힌 요청에 성공 증거가 닿으면 서고, 내리지 않는다 |
| [attempt-evidence](generated/attempt-evidence.svg) | 시도 종결 증거 가운데 자리를 옮기는 전이. 오케스트레이터가 스스로 내는 닫힘은 증거 잎을 옮기지 않으므로 여기 없다 |
| [attempt-evidence-dup](generated/attempt-evidence-dup.svg) | 시도 종결 증거 가운데 이미 종결된 뒤 같은 글이 또 오는 제자리 전이(dup. 같은 답을 다시 보낸다) |
| [node-execution](generated/node-execution.svg) | 노드의 허가, 순서기, 실행과 종료 |
| [node-execution-link](generated/node-execution-link.svg) | 노드의 연결과 재연결에 따른 실행 상태 |
| [node-report](generated/node-report.svg) | 노드의 종결 묶음과 확인 응답 |
| [transitions.md](generated/transitions.md) | 두 정의의 전이 표(함께 쓰는 것, 설계와 다른 점) |

범례(색은 그 그림에 그린 화살표와 이름으로 적어 둔 잎만으로 정한다):

| 표시 | 뜻 |
|---|---|
| 빨강 상태 | 그 그림에 그린 `(어긋남 N)` 화살표가 닿는 상태. 설계가 어긋남으로 적은 동작이다(PR 2a에서 어긋남 8이 없어져 지금 정의에는 없다) |
| 주황 상태 | 설계 증거 정의의 다섯 상태에 없는 잎(`definitions.LEAVES_NOT_IN_DESIGN`, 지금은 노드의 배포 성공에 밀려 종결 하나) |
| `(어긋남 N)` | 설계가 어긋남 N으로 적은 동작. 지금 정의에는 없다 |
| `(PR 2에서 바뀜)` | 설계 글의 줄이 옛 노드 규칙이나 PR 2에서 바꾼다고 적은 동작. 줄 번호는 전이 표 「설계와 다른 점」에 적는다(보내기 실패가 점검 필요를 켬은 design.md:429-430, W17은 design.md:125, W4는 design.md:382와 489, 늦은 성공의 판별은 design.md:123과 431) |
| `(설계에 없음)` | 설계에 그 전이가 없는 동작(W5 보류 되살림. 자기 갱신 영역은 둘째 걸음, design.md:489) |
| 가로 막대 | 한 묶음(열림, 닫힘)의 모든 잎에서 같은 사건과 조건으로 같은 곳에 가는 화살표를 한 줄로 모은 합류 막대 |

표시가 없는 전이는 설계 글이 받친다. 증거 정의의 실행 방식 조건(결과와 대조의 방식별 종결, design.md:79-80)과 증거 회수 성공(design.md:80)이 여기 든다.
열림과 닫힘 묶음은 Pele가 층을 건너는 화살표 때문에 상자를 깨뜨리므로 상자로 그리지 않고 잎 이름 뒤에 「(열림)」 꼴로 붙인다.

## 다시 만들기

```bash
# 저장소 루트에서. 정의를 고쳤으면 같은 PR에 다시 만든 생성물을 넣는다.
.venv/bin/python -m haniel_orch.statechart.docs --render   # .mmd, transitions.md, .svg
.venv/bin/python -m haniel_orch.statechart.docs            # .mmd와 transitions.md만
.venv/bin/python -m haniel_orch.statechart.docs --check    # 쓰지 않고 정의와 다르면 0이 아닌 코드
```

`--render`는 환경변수 `STATECHART_RENDER`가 가리키는 `render.py`를 먼저 찾고, 없으면
`AGENT_COMMON_FILES_DIR/skills/statechart/scripts/render.py`를 부르고,
각 `.svg` 끝에 원본 `.mmd`의 해시를 적는다(코드와 같은 순서). `--check`는 이 해시로 `.svg`가 `.mmd`와 같은지 본다.
표시 이름이 없는 상태, 사건, 조건이 있으면 생성이 실패한다(`statechart/labels.py`).

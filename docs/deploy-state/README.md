# 오케스트레이터 배포 상태 정의

오케스트레이터(`orch-server`)가 배포 요청 줄(`deploy_events`)과 시도 줄(`deploy_attempts`)의 상태를
바꾸는 길은 모두 코드의 상태 정의를 거친다(카드 #1259 PR 1이 지금 동작 그대로 옮겼고, PR 2a가 「점검 필요」 영역을,
PR 2b-1이 닫힌 시도의 늦은 글 규칙과 「닫힌 뒤 적용」 꼬리표 영역을 더했다).
정본은 `orch-server/src/haniel_orch/statechart/definitions.py`의 정의 둘이고, 여기 그림과 표는 거기서 뽑는다.

| 정의 | 단위 | 잎 칸 | 바깥 칸 |
|---|---|---|---|
| `OrchRequest` | 배포 요청 한 건. 영역 셋: 요청 상태, 점검 필요, 닫힌 뒤 적용 꼬리표 | `deploy_events.sm_status`(요청 상태), `deploy_events.sm_lock`(점검 필요), `deploy_events.sm_tag`(꼬리표) | `status`, `reject_reason`, 표시 줄(점검 필요의 바깥 칸은 `inspection_required`)과 계보 줄, 꼬리표의 `applied_after_close_at`과 `applied_after_close_attempt_id` |
| `AttemptEvidence` | 시도 한 건의 종결 증거 | `deploy_attempts.sm_evidence` | `result_status`, `settled_*`. 시도의 닫힘(`outcome`, `terminal_*`, `completed_at`)은 증거와 따로 움직이는 닫힘 칸이다 |

한 걸음은 저장소 변경 잠금과 트랜잭션 안에서 돈다(요청 줄은 `statechart/steps.py`, 시도 줄은 `statechart/attempt_steps.py`).
줄에서 새 인스턴스를 되살려 사건 하나를 넣고, 잎과 바깥 칸과 딸린 줄을 같은 트랜잭션에 쓴다. 전이가 없는 사건은
`NoTransition`으로 거절되고 줄에는 아무것도 쓰이지 않는다. 방송, 푸시, 타이머는 지금처럼 커밋 뒤 호출부에서 돈다.
기동 때 잎이 비었거나 바깥 칸과 어긋난 줄은 옛 칸에서 다시 계산하고(`statechart/backfill.py`), 빈 시도 순서 칸(`attempt_seq`)과
꼬리표 칸을 채운다.

시도의 닫힘과 증거 종결은 다른 것이다(design.md 79). 노드 글로 닫히는 시도(실패 결과, `deploy_attempt_terminal`)는 닫힘과
증거를 한 걸음에 끝내지만, 오케스트레이터가 스스로 내는 닫힘(실행 임대 마감, 보내기 실패)은 닫힘 칸만 쓰고 증거 잎은 열어 둔다.
그 뒤에 닿은 결과와 정착은 그 시도의 증거로 적히고(`late_evidence`), 성공으로 종결되면 「뒤 시도를 연 적이 없을 때만」(`attempt_seq`로
가린다) 열린 대기 요청을 성공으로 닫으며, 실패로 종결되면 점검 필요를 켠다. 실행 상한은 첫 실행 증거(진행 알림, 결과, 정착)의
시각 `execution_started_at`에서 잰다.

## 그림

그림은 `generated/` 아래에 있다. `.mmd`가 원본이고 `.svg`는 statechart 스킬의 `render.py`(Pele)로 만든다.

| 그림 | 내용 |
|---|---|
| [request-status](generated/request-status.svg) | 요청 상태. 설계 정의와 같은 전이 |
| [request-divergence](generated/request-divergence.svg) | 요청 상태 가운데 설계와 다른 전이(PR 2에서 바뀜, 설계에 없음) |
| [request-lock](generated/request-lock.svg) | 점검 필요 영역. 모든 시도의 닫힘(닫힌 시도의 늦은 실패 포함)이 켜고, 원천이 단건 승인인 승인이 시도를 열 때에만 내린다 |
| [request-tag](generated/request-tag.svg) | 닫힌 뒤 적용 꼬리표 영역. 거절이나 밀림으로 닫힌 요청에 성공 증거가 닿으면 서고, 내리지 않는다 |
| [attempt-evidence](generated/attempt-evidence.svg) | 시도 종결 증거 전체. 오케스트레이터가 스스로 내는 닫힘은 증거 잎을 옮기지 않으므로 여기 없다 |
| [transitions.md](generated/transitions.md) | 두 정의의 전이 표(함께 쓰는 것, 설계와 다른 점) |

범례(색은 그 그림에 그린 화살표와 이름으로 적어 둔 잎만으로 정한다):

| 표시 | 뜻 |
|---|---|
| 빨강 상태 | 그 그림에 그린 `(어긋남 N)` 화살표가 닿는 상태. 설계가 어긋남으로 적은 동작이다(PR 2a에서 어긋남 8이 없어져 지금 정의에는 없다) |
| 주황 상태 | 설계 증거 정의의 다섯 상태에 없는 잎(`definitions.LEAVES_NOT_IN_DESIGN`, 지금은 노드의 배포 성공에 밀려 종결 하나) |
| `(어긋남 N)` | 설계가 어긋남 N으로 적은 동작. 지금 정의에는 없다 |
| `(PR 2에서 바뀜)` | 설계 글의 줄이 옛 노드 규칙이나 PR 2에서 바꾼다고 적은 동작. 줄 번호는 전이 표 「설계와 다른 점」에 적는다(보내기 실패가 점검 필요를 켬은 design.md:429-430, W17은 design.md:125, W4는 design.md:382와 489, 늦은 성공이 「뒤 시도를 연 적이 없을 때만」 닫음은 design.md:123과 431) |
| `(설계에 없음)` | 설계에 그 전이가 없는 동작(W5 보류 되살림. 자기 갱신 영역은 둘째 걸음, design.md:489) |
| 가로 막대 | 한 묶음(열림, 닫힘)의 모든 잎에서 같은 사건과 조건으로 같은 곳에 가는 화살표를 한 줄로 모은 합류 막대 |

표시가 없는 전이는 설계 글이 받친다. 증거 정의의 실행 방식 조건(결과와 대조의 방식별 종결, design.md:79-80), 증거 회수 성공(design.md:80),
노드의 배포 성공이 도는 시도를 밀어냄(design.md:124)이 여기 든다.
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

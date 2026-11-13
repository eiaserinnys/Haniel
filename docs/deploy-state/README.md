# 오케스트레이터 배포 상태 정의

오케스트레이터(`orch-server`)가 배포 요청 줄(`deploy_events`)과 시도 줄(`deploy_attempts`)의 상태를
바꾸는 길은 모두 코드의 상태 정의를 거친다(카드 #1259 PR 1, 지금 동작 그대로).
정본은 `orch-server/src/haniel_orch/statechart/definitions.py`의 정의 둘이고, 여기 그림과 표는 거기서 뽑는다.

| 정의 | 단위 | 잎 칸 | 바깥 칸 |
|---|---|---|---|
| `OrchRequest` | 배포 요청 한 건 | `deploy_events.sm_status` | `status`, `reject_reason`, 표시 줄과 계보 줄 |
| `AttemptEvidence` | 시도 한 건의 종결 증거 | `deploy_attempts.sm_evidence` | `outcome`, `result_status`, `settled_*` |

한 걸음은 저장소 변경 잠금과 트랜잭션 안에서 돈다(`statechart/steps.py`). 줄에서 새 인스턴스를 되살려
사건 하나를 넣고, 잎과 바깥 칸과 딸린 줄을 같은 트랜잭션에 쓴다. 전이가 없는 사건은 `NoTransition`으로
거절되고 줄에는 아무것도 쓰이지 않는다. 방송, 푸시, 타이머는 지금처럼 커밋 뒤 호출부에서 돈다.
기동 때 잎이 비었거나 바깥 칸과 어긋난 줄은 옛 칸에서 다시 계산한다.

## 그림

그림은 `generated/` 아래에 있다. `.mmd`가 원본이고 `.svg`는 statechart 스킬의 `render.py`(Pele)로 만든다.

| 그림 | 내용 |
|---|---|
| [request-status](generated/request-status.svg) | 요청 상태. 설계 정의와 같은 전이 |
| [request-divergence](generated/request-divergence.svg) | 요청 상태 가운데 설계와 다른 전이(어긋남 8, PR 2에서 바뀜) |
| [attempt-evidence](generated/attempt-evidence.svg) | 시도 종결 증거 전체 |
| [transitions.md](generated/transitions.md) | 두 정의의 전이 표(함께 쓰는 것, 설계와 다른 점) |

범례: 빨강 상태(`거절`)는 어긋남 8(조건 없는 거절)이 닿는 곳이다. 주황 상태(`노드의 배포 성공에 밀려 종결`)는
설계 정의에 없는 잎이다. 화살표 끝의 `(PR 2에서 바뀜)`은 지금 동작을 그대로 옮겼으나 설계 정의에는 없는 전이다.
열림과 닫힘 묶음은 Pele가 층을 건너는 화살표 때문에 상자를 깨뜨리므로 상태 이름 앞에 단다.

## 다시 만들기

```bash
# 저장소 루트에서. 정의를 고쳤으면 같은 PR에 다시 만든 생성물을 넣는다.
.venv/bin/python -m haniel_orch.statechart.docs --render   # .mmd, transitions.md, .svg
.venv/bin/python -m haniel_orch.statechart.docs            # .mmd와 transitions.md만
.venv/bin/python -m haniel_orch.statechart.docs --check    # 쓰지 않고 정의와 다르면 0이 아닌 코드
```

`--render`는 `AGENT_COMMON_FILES_DIR/skills/statechart/scripts/render.py`(없으면 `STATECHART_RENDER`)를 부르고,
각 `.svg` 끝에 원본 `.mmd`의 해시를 적는다. `--check`는 이 해시로 `.svg`가 `.mmd`와 같은지 본다.
표시 이름이 없는 상태, 사건, 조건이 있으면 생성이 실패한다(`statechart/labels.py`).

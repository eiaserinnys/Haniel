"""오케스트레이터 배포 상태의 정의(python-statemachine 3.2.2).

정본은 이 패키지의 정의 둘이다.
- `OrchRequest`: 배포 요청 한 건(`deploy_events`)의 요청 상태
- `AttemptEvidence`: 시도 한 건(`deploy_attempts`)의 종결 증거

그림은 `python -m haniel_orch.statechart.docs`가 정의에서 뽑는다.
"""

# haniel-push-relay

Cloudflare Worker가 허브(orch-server)의 푸시 요청을 받아 iOS 앱 기기로 APNs 알림을 보낸다. 허브는 배포 대기가 새로 생길 때와 배포가 끝날 때 이 서버에 `POST {url}/v1/push`를 보낸다.

## 구성

| 엔드포인트 | 호출하는 쪽 | 하는 일 |
| --- | --- | --- |
| `POST /v1/push` | 허브 | 등록된 모든 기기에 알림을 보낸다 |
| `POST /v1/devices` | iOS 앱 | 기기 토큰을 등록한다 |
| `DELETE /v1/devices/{token}` | iOS 앱 | 기기 토큰을 지운다 |

모든 요청은 `Authorization: Bearer <INSTANCE_KEY>`로 인증한다. 기기 토큰은 KV 바인딩 `DEVICE_TOKENS`에 저장한다. APNs 호스트는 `api.push.apple.com`(production)으로 고정이다.

## Worker 비밀값 다섯

| 이름 | 내용 |
| --- | --- |
| `INSTANCE_KEY` | 허브와 앱이 중계 서버에 보이는 공유 키. 임의의 긴 문자열 |
| `APNS_KEY_ID` | Apple Developer에서 만든 APNs 키의 Key ID |
| `APNS_TEAM_ID` | Apple Developer Team ID |
| `APNS_PRIVATE_KEY` | APNs 키 `.p8` 파일의 PEM 전문 |
| `APNS_BUNDLE_ID` | 앱 번들 ID (`apns-topic`으로 쓴다) |

## 배포 순서

1. KV 네임스페이스를 하나만 만든다. `wrangler kv namespace create DEVICE_TOKENS`가 id를 알려 준다. 미리보기 네임스페이스는 만들지 않는다. 로컬 `wrangler dev`는 로컬 KV를 쓴다.
2. `wrangler.toml`의 `[[kv_namespaces]]`에 있는 `id`를 1에서 받은 값으로 둔다. 이 id는 비밀이 아니며 `wrangler.toml`에만 적는다.
3. 비밀값 다섯을 등록한다. 각 이름마다 `wrangler secret put <이름>`을 실행해 값을 붙여 넣는다. `INSTANCE_KEY`가 없거나 비어 있으면 중계 서버는 모든 요청을 401로 막는다.
4. `npm ci` 뒤 `npm run deploy`로 배포한다. 중계 서버 주소는 `wrangler.toml`의 `routes`에 적은 사용자 지정 도메인(`https://haniel-push.eiaserinnys.me`) 하나다. `workers_dev = false`라 workers.dev 주소는 열리지 않는다.

비밀값의 실제 값, 저장 위치, 계정 id는 이 저장소에 적지 않는다.

## 허브에서 켜기

허브 환경변수 두 개를 모두 설정하고 허브를 다시 시작한다. 허브는 다른 `ORCH_*` 값과 같은 방식으로 시작할 때 읽는다.

```
ORCH_PUSH_RELAY_URL=https://haniel-push.eiaserinnys.me
ORCH_PUSH_INSTANCE_KEY=<INSTANCE_KEY와 같은 값>
```

- 둘 다 비어 있지 않을 때만 푸시가 켜진다. URL 끝의 `/`는 떼고 쓴다.
- 둘 다 없으면 푸시 없이 뜬다. 시작 로그에 `Push relay disabled`가 남는다.
- 하나만 있으면 서버는 죽지 않고 푸시 없이 뜬다. 빠진 변수 이름을 담은 WARNING 한 줄이 남는다.
- 켜지면 시작 로그에 `Push relay enabled: <URL>`이 남는다. `ORCH_PUSH_INSTANCE_KEY` 값은 로그에 남지 않는다.
- 푸시 전송은 기다리지 않고 보내며, 실패해도 배포 흐름은 멈추지 않고 로그만 남는다.

## 앱에서 켜기

iOS 앱의 Settings에 두 값을 넣는다.

- Push Relay URL: 허브의 `ORCH_PUSH_RELAY_URL`과 같은 주소
- Instance Key: 허브의 `ORCH_PUSH_INSTANCE_KEY`와 같은 값

앱은 이 값으로 기기 토큰을 `POST /v1/devices`에 등록한다.

## 개발

```
npm install
npm test
npm run typecheck
```

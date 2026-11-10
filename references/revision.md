# 고치고 판 쌓기

수정 요청이 여러 번 오가는 시연·설명 영상은 `scripts/reel.py`로 만든다. 소스는 한 벌만 두고, 시각은 한 파일에서 관리하고, 바뀐 컷만 다시 렌더한다. 한 번에 끝나는 쇼릴은 `SKILL.md`의 기본 절차(`timeline.py` → `render.mjs` → `mix.py` → `finish.py`)로 충분하다.

## 목차

- 폴더 구조
- 처음 만들기
- cuts.json
- 수정 요청을 받았을 때
- 렌더 중에 확인하기
- 판 저장과 복원
- 검수
- 한계

## 폴더 구조

```
<root>/
  project/    작업 소스 한 벌. 판마다 복사하지 않는다
  cache/      컷 클립·믹스 결과·검수 시트. 지워도 다시 만들 수 있다(.gitignore)
  versions/   판마다 납품 파일과 기록(mp4, srt, 포스터, storyboard.md, feedback.md, qa_report.md, manifest.json)
  .store/     판의 소스를 내용 해시로 저장한다. 같은 파일은 판이 몇 개든 한 번만 저장된다
```

`project/`가 git으로 관리되지 않는 곳에 있어도 판을 복원할 수 있다. 복원에 git이 필요 없다.

## 처음 만들기

```bash
python3 $S/reel.py init <root>
cd <root>
python3 $S/reel.py add-cut c01 --length 12   # reel/c01.html, reel/scenes_c01.js 를 만들고 cuts.json 에 더한다
python3 $S/reel.py all --draft               # build → mix → render → join
python3 $S/reel.py all                       # 최종(모션 블러)
python3 $S/reel.py release v1 --note "첫 납품"   # 공유용 파일·검수가 없거나 낡았으면 먼저 하고 판을 남긴다
```

`<root>` 안에서 실행하면 위 폴더로 올라가며 `project/cuts.json`을 찾는다. 다른 곳에서는 `--root <root>`를 쓴다.

컷 페이지는 `engine.js` 앞에서 `timeline_<컷>.js`를 불러와야 한다. `add-cut`이 그렇게 만들고, `build`가 빠진 페이지를 ✗로 알린다. 컷 길이는 장면 코드에 숫자로 쓰지 않고 `REEL.cue('length')`로 읽는다.

## cuts.json

시각의 단일 원천이다. 장면 코드, 자막, 효과음, 음악이 모두 여기서 시각을 받는다.

```json
{
 "title": "제품_시연",
 "max_duration": 290,
 "narration": { "dir": "audio/narration/lines", "durations": "audio/narration/durations.json", "script": "audio/narration/script.json" },
 "cuts": [
  { "id": "c02", "page": "c02.html", "length": "cross.end+7.1",
    "cues": { "er.in": "line.c02_1.end-7.15", "gate.end": "er.in+13.8", "cross.end": "gate.end+24.2" } }
 ],
 "lines": [ { "id": "c02_1", "start": 0.6 }, { "id": "c02_2", "start": "gate.end+0.2" } ],
 "sfx": [ { "cut": "c02", "at": "er.in-0.2", "name": "counter", "gain_db": -22 } ],
 "music": { "spec": "audio/music/spec.json", "file": "audio/music/music.wav", "engine": "synth" },
 "mix": { "script": "audio/mix_project.py" },
 "syncs": [ { "line": "c02_2", "cue": "gate.end", "offset": 0.2, "tolerance": 0.5 } ],
 "snapshot_exclude": [ "assets/session.json" ]
}
```

| 항목 | 뜻 |
| --- | --- |
| `cues` | 컷 안의 시각 이름. 숫자, 다른 큐, `line.<줄>`, `line.<줄>.end`, `length`를 기준으로 쓴다. 이름에 `+`, `-`, 공백은 쓰지 않는다 |
| `length` | 컷 길이. 마지막 큐에 이어 두면 장면이 늘 때 컷도 같이 는다. 가장 가까운 프레임으로 맞춰지고, 컷 시작 시각도 프레임 수를 더해 계산하므로 컷이 많아져도 소리와 어긋나지 않는다 |
| `deps` | 사람이 적는 의존 파일·패턴(폰트, 데이터 파일 등). 자동 기록이 건드리지 않는다. 맞는 파일이 없으면 `build`가 알린다 |
| `deps_auto` | `reel.py deps`가 적는 목록. 직접 고치지 않는다. `deps`와 `deps_auto`가 모두 없는 컷은 모든 에셋을 본다 |
| `lines[].start` | 나레이션 시작이자 자막 시작. 끝은 나레이션 길이로 계산한다 |
| `sfx[].at` | 효과음 시각. **화면 사건의 큐에 묶는다**. 숫자로 두면 장면을 옮길 때 혼자 남는다 |
| `sfx[].ambient` | `true`면 검수에서 화면 변화와 비교하지 않는다(배경음) |
| `music.spec` | 음악 스펙. 시각을 `"c03"`, `"c03+50.4"`, `"total"`처럼 컷 기준으로 쓸 수 있다. `build`가 숫자로 풀어 `spec.resolved.json`에 쓴다 |
| `music.engine` | `synth`면 풀린 스펙이 바뀔 때 음악을 다시 합성한다. 없으면 다시 만들라고 알리기만 한다(ElevenLabs 크레딧 보호) |
| `mix.script` | 프로젝트 전용 믹스. `timeline.json` 경로를 인자로 받는다. 없으면 `mix.py` |
| `syncs` | 자막의 특정 구절과 맞아야 하는 강조 큐. `offset`은 자막 시작에서 그 구절까지의 초 |
| `snapshot_exclude` | 판 저장소에 넣지 않을 파일. **세션 쿠키·비밀값은 반드시 적는다** |

장면 코드에서는 숫자 대신 큐를 읽는다.

```js
const T_GATE_END = REEL.cue('gate.end');
this.counter.render(lt, REEL.cue('er.in'));
```

큐끼리 이어 두면 앞을 옮길 때 뒤가 따라온다. 첫 나레이션이 1초 길어지면 `line.c02_1.end`를 기준으로 삼은 장면, 자막, 효과음, 뒤 컷, 음악이 함께 1초 밀린다.

## 수정 요청을 받았을 때

1. 요청의 영상 시각을 찾는다. 시각은 **납품한 판 기준**이므로 `where`는 최근 판의 표를 먼저 쓴다.

   ```bash
   python3 $S/reel.py where 03:56             # 최근 판 기준
   python3 $S/reel.py where 03:56 --version v12
   python3 $S/reel.py where 03:56 --current   # 지금 소스 기준
   ```

   컷, 컷 안 시각, 장면 파일, 그 시각의 자막, 앞뒤 큐, 근처 효과음을 보여 준다.
2. 요청을 `project/feedback.md`에 적는다.
3. 시각은 `cuts.json`, 화면은 `reel/scenes_<컷>.js`, 문구는 나레이션 원고를 고친다. 나레이션을 고쳤으면 `reel.py build --tts`.
4. `reel.py all --draft`로 확인한 뒤 `reel.py all`. 바뀐 컷만 다시 렌더한다. 렌더가 끝난 컷부터 `cache/preview/`에서 볼 수 있다(아래 "렌더 중에 확인하기").
5. `reel.py release v14 --note "…"`.

### 바뀐 컷을 판단하는 방법

컷 페이지, 그 페이지가 불러오는 파일(공통 스크립트와 그 컷의 `timeline_<컷>.js` 포함), `deps`와 `deps_auto`의 에셋을 해시한다. 앞 컷의 길이가 바뀌어도 뒤 컷의 화면은 그대로이므로 다시 렌더하지 않는다. 공통 파일을 고치면 모든 컷을 렌더한다.

`deps_auto`는 컷을 렌더할 때마다 그 컷이 실제로 읽은 에셋으로 자동 갱신된다. 렌더하지 않은 컷의 목록은 건드리지 않는다. 바꾸면 그 컷의 해시가 달라져 다음 렌더에서 괜히 다시 렌더되기 때문이다. 렌더 없이 에셋 목록만 다시 잡으려면 `reel.py deps`. 페이지가 요청하지 않는 파일에 화면이 달라진다면 `deps`에 직접 적는다.

믹스와 이어 붙이기는 입력을 기록한다. 믹스는 시각표, 믹스 스크립트, 나레이션·효과음·음악 파일을, 이어 붙이기는 컷 순서, 클립, `mix.wav`, 시각표를 적는다. `join`은 믹스가 낡았거나 클립 프레임 수가 시각표와 다르면 멈춘다.

이미 있는 클립을 그대로 쓸 때(다른 구조에서 옮겨 왔을 때)는 `reel.py render --adopt`로 현재 소스의 결과로 기록한다. 소스와 클립이 정말 같은지 먼저 확인한다.

## 렌더 중에 확인하기

`all`은 믹스를 렌더 앞에서 한다. 믹스는 화면과 상관없이 시각표와 소리 파일로만 정해지기 때문이다. 그래서 컷 하나가 렌더되면 바로 그 구간의 소리를 입힌 `cache/preview/<컷>_final.mp4`(초안은 `_draft`)를 만들고 경로를 출력한다. 영상은 복사만 하므로 1~2초면 된다. 전체 렌더가 끝나기를 기다리지 않고 바뀐 컷부터 확인한다.

```bash
python3 $S/reel.py render c03        # 그 컷만 렌더 → cache/preview/c03_final.mp4
python3 $S/reel.py preview           # 지금 있는 클립과 mix.wav 를 검사 없이 잇는다(5분 영상에 약 7초)
```

`preview`는 납품 파일이 아니다. 믹스가 낡았으면 △로 알리고 그대로 잇는다.

`all`은 공유용 인코딩과 `qa`를 하지 않는다. 5분 영상에서 이 둘이 4분쯤 걸리는데, 수정을 확인할 때마다 필요하지는 않기 때문이다. `release`가 판을 남기기 전에 공유용 파일과 검수 보고가 없거나 최종 영상보다 오래됐으면 먼저 만든다. 수정 중에 검수까지 보고 싶으면 `reel.py all --full`이나 `reel.py qa`를 쓴다.

## 판 저장과 복원

```bash
python3 $S/reel.py release v14 --note "01:16 문구 수정"
python3 $S/reel.py versions
python3 $S/reel.py restore v12 --to <빈 폴더>
```

- `release`는 공유용 파일과 검수 보고가 없거나 최종 영상보다 오래됐으면 먼저 만든다. `--force`로 낡은 상태를 저장할 때는 만들지 않고 있는 그대로 저장한다.
- `release`는 이미 있는 판을 덮어쓰지 않는다. 장면 소스와 클립이 다르거나, 믹스·이어 붙이기 기록이 지금 상태와 다르면 거부한다(`--force`로 무시). 문서만 고쳤다면 다시 렌더하지 않고 저장할 수 있다.
- `manifest.json`에 파일 해시, 컷별 해시, 영상 시각 표가 남는다. `where --version`이 이 표를 쓴다.
- `restore`는 비어 있는 폴더에만 꺼낸다. 작업 소스를 덮어쓰지 않는다. 생성 파일(`timeline.json`, `timeline_*.js`, 믹스)은 저장하지 않으므로 꺼낸 뒤 `build`와 `mix`를 다시 실행한다.

## 검수

`reel.py qa`는 `qa_check.py`에 더해 `cut_checks.py`를 돌린다. 결과는 **확인할 후보**이고, 시각을 열어 보고 판단한다.

| 검사 | 찾는 것 | 잘 잡는 것 | 못 잡는 것 |
| --- | --- | --- | --- |
| 컷 경계 | 경계 ±0.5초에 앞뒤보다 밝거나 어두운 순간, 한 프레임에 화면이 튀는 곳 | 전환 중 드러나는 흰 바탕, 의도하지 않은 하드컷 | 화면 일부만 바뀌는 어긋남 |
| 효과음 | 소리 앞뒤 0.25초에 화면 변화가 거의 없는 곳 | 소리가 사건과 동떨어진 경우 | 작은 움직임이 계속되는 화면에서 어긋난 소리, 작은 글자 이동만 있는 화면(오탐) |
| 강조 | `syncs`의 큐와 자막 구절의 시각 차이 | 강조가 말보다 늦거나 빠른 경우 | `syncs`에 적지 않은 강조 |
| 자막 줄 | 한 줄에 문장 두 개, 70자 초과 | 줄을 나눠야 할 긴 자막 | 어색한 위치의 자동 줄바꿈 |

효과음 싱크는 검사보다 **큐에 묶는 것이 먼저**다. 큐에 묶은 소리는 장면과 어긋날 수 없다.

UI 화면이 중심인 시연 영상은 `qa_check.py`의 "멈춘 화면", "큰 움직임" 항목이 쇼릴 기준으로 많이 걸린다. 화면을 읽는 시간이 필요한 구간이면 의도한 정지로 보고 넘어가되, 설명 없이 비어 있는 구간은 고친다.

## 한계

- 큐로 옮기지 않은 장면 안의 숫자는 따라오지 않는다. 자주 고치는 컷부터 큐로 옮긴다.
- 큐는 시각만 옮긴다. 움직임의 길이(예: 0.8초 동안 축소)는 장면 코드에 남는다.
- `deps_auto`는 컷을 0.5초 간격으로 넘기며 기록한다. 페이지가 시작할 때 읽지 않고 그 사이 잠깐만 쓰는 에셋은 빠질 수 있다.
- 판 저장소는 지우지 않는 한 계속 쌓인다. 오래된 판 정리 기능은 아직 없다.

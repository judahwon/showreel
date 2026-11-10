# 판 관리(version.py)

기본 절차(`new_project.py` → `render.mjs` → `mix.py` → `finish.py`)로 만든 프로젝트의 판을 남긴다. 컷으로 나눈 수정 워크플로는 `reel.py release`를 쓴다(`revision.md`).

## 목차

- 왜 이렇게 나누나
- 폴더 구조
- 판에 넣는 파일
- 명령
- 수정 요청을 받았을 때
- 한계

## 왜 이렇게 나누나

판마다 프로젝트 폴더를 통째로 복사하면, 판끼리 거의 같은 이미지·음악·폰트가 판마다 수백 MB씩 쌓인다. 이전 시연 영상에서는 11개 판이 약 8GB였고, 대부분이 판마다 똑같은 `assets/`(약 200MB)·`audio/`(약 170MB)·`reel/`(44MB)였다. 그런데 판끼리 실제로 다른 것은 장면 코드 몇 개와 원고뿐이었다.

그래서 이렇게 나눈다.

- 판마다 중복되는 무거운 파일 → `shared/`에 한 벌
- 그 판의 영상을 만드는 데 필요한 작은 레시피 → 판 폴더의 `src/`
- 다시 만들 수 있는 중간 산출물(초안, 스틸, 검수 시트, 무음 렌더, 렌더 청크) → 판에 넣지 않음

## 폴더 구조

```
<프로젝트>/
  reel/ audio/ assets/ data/ …    지금 고치는 작업 폴더(한 벌)
  qa/                             검수 시트·스틸. 판에 넣지 않는다
  shared/                         판마다 중복되는 무거운 파일. shared/<경로>/<이름>.<해시 10자리><확장자>
  out/                            초안·무음 렌더·finish.py 결과가 생기는 곳
    video.mp4, *.log …            다시 만들 수 있는 작업 파일. 판에 넣지 않고 그대로 둔다
    versions/
      v1/                         release 가 out/ 의 납품 파일을 이 폴더로 옮긴다
        <제목>_v1.mp4             마스터
        <제목>_v1_공유용.mp4
        <제목>_v1_포스터.jpg
        <제목>_v1.srt             나레이션이 있을 때
        src/                      레시피: reel/scenes.js, reel/index.html, reel/data.js, reel/timeline.js,
                                  script.json, timeline.json, project.json, audio/music/*.json, data/…, *.md
        manifest.json             판에 필요한 모든 파일의 경로·해시·크기·역할·저장 위치, 납품 파일 해시, 이전 판 대비 바뀐 파일
        변경.md                    메모, 납품 파일, 이전 판 대비 바뀐 파일, 다시 만드는 방법
      v2/ …
```

납품 파일은 판 폴더로 옮겨지므로, `release` 뒤 `out/`에는 무음 렌더와 로그만 남는다. 다음 판은 다시 렌더·`finish.py`를 해야 남길 수 있다(같은 영상을 두 번 판으로 남기지 않는다). 예전 위치(`<프로젝트>/versions/vN`)에 남긴 판도 `list`·`show`·`diff`·`restore`가 읽는다.

`shared/`의 파일 이름에는 내용 해시가 붙는다. 같은 경로의 파일을 고치면 다른 이름으로 하나 더 저장되므로, 이전 판이 쓰던 내용이 덮어써지지 않는다. 내용이 같으면 판이 몇 개든 한 번만 저장된다.

## 판에 넣는 파일

| 역할 | 찾는 방법 | 저장 위치 |
| --- | --- | --- |
| render | `version_deps.mjs`가 렌더러와 같은 방식으로 `reel/index.html`을 열고 0.5초 간격으로 끝까지 넘기며 실제로 요청한 파일(HTML, 스크립트, 데이터, 이미지, 폰트, 국기 등) | 작은 텍스트는 `src/`, 그 밖은 `shared/` |
| audio | `timeline.json`이 가리키는 음악·효과음·나레이션 파일(`file`, `analysis`)과 `audio/mix.wav`(`finish.py` 입력) | `shared/`(분석 JSON은 `src/`) |
| recipe | `script.json`, `timeline.json`, `project.json`, `audio/music/*.json`, `data/**`, 프로젝트 루트의 `*.md`, `--include`로 더한 패턴 | `src/` |

- `src/`에는 확장자가 텍스트(`.js .html .css .json .md .csv` 등)이고 256KB 이하인 파일만 들어간다. 엔진(`reel/engine.js`, `reel/fx.js`), `reel/fonts/`, `reel/lib/`는 판과 상관없는 파일이라 크기와 무관하게 `shared/`에 둔다.
- 쓰지 않는 파일은 넣지 않는다. 버린 음악 테이크, 쓰지 않은 효과음, 원본 생성 이미지(`assets/gen`), 조사 원본(`research/raw`), 남은 보조 파일이 그렇다. 판에 꼭 남겨야 하는 파일은 `--include "research/*.md"`처럼 더한다.
- `.env`, `versions/`, `shared/`, `out/`, `qa/`는 레시피로 넣지 않는다. 납품 파일은 `out/`에서 판 폴더로 옮긴다.

## 명령

```bash
python3 $S/version.py release [vN] [--note "메모"] [--name 제목] [--include "패턴"] [--force]
python3 $S/version.py list
python3 $S/version.py show v2
python3 $S/version.py diff v1 v2 [--text]
python3 $S/version.py restore v1 --to <빈 폴더>
```

- 프로젝트 폴더 안 어디서 실행해도 `project.json`을 찾아 올라간다. 다른 곳에서는 `--project <폴더>`를 준다.
- `release`
  - 판 이름을 주지 않으면 다음 번호(v1, v2…)를 쓴다.
  - 납품 파일은 `--name`이 없으면 `out/`에서 가장 최근의 `<제목>_공유용.mp4`와 짝이 맞는 `<제목>.mp4`를 고른다.
  - 이미 있는 판은 덮어쓰지 않는다.
  - 판을 남기지 않는 경우가 둘 있다. 최종 영상보다 나중에 고친 화면·소리 입력이 있거나, `mix.wav`보다 나중에 고친 소리 입력이나 `timeline.json`이 있을 때다. 그 판의 레시피로 그 영상을 다시 만들 수 없기 때문이다. 다시 렌더·`mix.py`·`finish.py`를 하거나, `--force`로 지금 납품 파일 그대로 남긴다. `--force`로 남긴 판은 `manifest.json`의 `forced`와 `변경.md`에 이유가 적힌다.
- `show`는 판의 파일을 역할별로, `diff`는 두 판 사이의 추가(+)·삭제(-)·변경(~) 파일을 보여 준다.
- `restore`는 비어 있는 폴더에만 꺼낸다. 작업 폴더를 덮어쓰지 않는다. 꺼낸 폴더에서 `render.mjs` → `finish.py`로 그 판의 영상을 다시 만들 수 있다(소리를 고쳤으면 `mix.py`부터). 납품 파일도 `out/`에 함께 꺼낸다.

## 수정 요청을 받았을 때

1. 같은 프로젝트 폴더에서 고친다(장면 코드, `script.json`, 음악 등). 새 폴더로 복사하지 않는다.
2. 바뀐 것에 따라 `timeline.py` → `mix.py` → `render.mjs` → `finish.py` 중 필요한 것을 다시 실행한다. 소리만 바꿨다면 렌더 없이 `mix.py`와 `finish.py`만 돌린다.
3. 검수(`qa_check.py`)를 하고 시트를 본다.
4. `version.py release --note "요청 요약"`으로 다음 판을 남긴다.
5. 납품 보고에 판 폴더 경로와 `변경.md`의 바뀐 파일을 적는다.

## 한계

- 페이지가 0.5초 간격 사이에만 잠깐 읽는 파일은 기록에서 빠질 수 있다. 그런 파일은 `--include`로 더한다.
- 음악을 ffmpeg로 편집했다면 그 결과 파일은 판에 남지만, 편집 명령은 남지 않는다. 명령을 `제작노트.md`나 `audio/music/`의 메모에 적어 두면 레시피로 함께 남는다.
- `shared/`는 지우지 않는 한 계속 쌓인다. 오래된 판 정리 기능은 아직 없다.
- 판을 꺼내 다시 렌더하려면 렌더러(`render.mjs`)와 엔진 버전이 그 판 때와 같아야 한다. 엔진 파일은 판에 함께 저장되지만 렌더러 스크립트는 스킬 쪽에 있다.

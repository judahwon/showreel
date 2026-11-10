"""Windows 에서 스크립트를 UTF-8 모드로 다시 실행한다.

Windows 의 Python 은 기본 인코딩이 cp949 라 한글 JSON·SRT 를 읽고 쓰거나 ✓ 를 출력할 때 깨지거나 멈춘다.
스크립트 맨 위에서 `import _utf8` 만 하면 `PYTHONUTF8=1` 없이 같은 결과가 된다. macOS·Linux 에서는 아무 일도 하지 않는다.
이 폴더의 스크립트를 직접 실행했을 때만 다시 실행한다. 다른 프로그램이 이 폴더의 모듈을 import 할 때 그 프로그램까지 다시 돌리면 안 된다.
"""
import os
import subprocess
import sys
from pathlib import Path

if os.name == "nt" and not sys.flags.utf8_mode and sys.argv and sys.argv[0].endswith(".py"):
    if Path(sys.argv[0]).resolve().parent == Path(__file__).resolve().parent:
        # 이 스크립트가 부르는 프로젝트 전용 스크립트도 UTF-8 모드로 돌도록 환경 변수로 물려준다
        sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv], env={**os.environ, "PYTHONUTF8": "1"}))

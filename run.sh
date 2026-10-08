#!/usr/bin/env bash
# 실행 (Linux / macOS / WSL):  bash run.sh [옵션]
#   - 처음 실행하면 가상환경(.venv)을 만들고 필요한 패키지를 자동으로 설치합니다.
#   - 옵션 없이 실행하면 화면(앱 창)을 엽니다.  예) bash run.sh
#   - 옵션을 주면 그대로 main.py에 전달합니다.    예) bash run.sh --week
cd "$(dirname "$0")" || exit 1

if [ ! -f .venv/.installed ]; then
    PY=python3
    command -v "$PY" >/dev/null 2>&1 || PY=python
    if ! command -v "$PY" >/dev/null 2>&1; then
        echo "Python 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요."
        exit 1
    fi
    echo "처음 실행입니다. 필요한 패키지를 설치합니다. (1~3분)"
    [ -x .venv/bin/python ] || "$PY" -m venv .venv || exit 1
    # conda 등 다른 환경이 켜져 있어도 항상 .venv의 python으로 설치한다.
    .venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip --upgrade
    .venv/bin/python -m pip install -q -r requirements.txt || { echo "패키지 설치에 실패했습니다."; exit 1; }
    touch .venv/.installed
    echo "설치 완료."
fi

[ $# -eq 0 ] && set -- --app
exec .venv/bin/python main.py "$@"

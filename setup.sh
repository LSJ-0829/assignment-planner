#!/usr/bin/env bash
# 설치 확인용 (선택):  bash setup.sh
# 설치는 run.sh가 처음 실행될 때 자동으로 하므로, 이 스크립트는 설치 + 테스트(API 키 불필요)를 한 번에 확인할 때만 씁니다.
cd "$(dirname "$0")" || exit 1
bash run.sh --list-plans >/dev/null || exit 1
echo "설치 확인 테스트를 실행합니다. (API 키 불필요)"
.venv/bin/python -m pytest -q tests
echo
echo "준비 완료. 실행:  bash run.sh"

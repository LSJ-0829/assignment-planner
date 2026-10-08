@echo off
rem 설치 확인용 (선택):  setup.bat
rem 설치는 run.bat이 처음 실행될 때 자동으로 하므로, 이 스크립트는 설치 + 테스트(API 키 불필요)를 한 번에 확인할 때만 씁니다.
chcp 65001 >nul
cd /d "%~dp0"
call run.bat --list-plans >nul
if errorlevel 1 exit /b 1
echo 설치 확인 테스트를 실행합니다. (API 키 불필요)
.venv\Scripts\python.exe -m pytest -q tests
echo.
echo 준비 완료. 실행:  run.bat

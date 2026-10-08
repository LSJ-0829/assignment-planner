@echo off
rem 실행 (Windows):  run.bat [옵션]   또는 파일을 더블클릭
rem   - 처음 실행하면 가상환경(.venv)을 만들고 필요한 패키지를 자동으로 설치합니다.
rem   - 옵션 없이 실행하면 화면(앱 창)을 엽니다.
rem   - 옵션을 주면 그대로 main.py에 전달합니다.  예) run.bat --week
chcp 65001 >nul
cd /d "%~dp0"

if exist .venv\.installed goto run
where python >nul 2>&1
if errorlevel 1 (
    echo Python 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
    echo 설치할 때 "Add python.exe to PATH"에 체크하세요.
    pause
    exit /b 1
)
echo 처음 실행입니다. 필요한 패키지를 설치합니다. (1~3분)
if not exist .venv\Scripts\python.exe python -m venv .venv
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m pip --version >nul 2>&1 || .venv\Scripts\python.exe -m ensurepip --upgrade
.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if errorlevel 1 goto failed
echo ok> .venv\.installed
echo 설치 완료.

:run
if "%~1"=="" (
    .venv\Scripts\python.exe main.py --app
) else (
    .venv\Scripts\python.exe main.py %*
)
exit /b %errorlevel%

:failed
echo 패키지 설치에 실패했습니다.
pause
exit /b 1

:: This method is not recommended, and we recommend you use the `start.sh` file with WSL instead.
@echo off
SETLOCAL ENABLEDELAYEDEXPANSION

:: Get the directory of the current script
SET "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%" || exit /b

:: Add conditional Playwright browser installation
IF /I "%WEB_LOADER_ENGINE%" == "playwright" (
    IF "%PLAYWRIGHT_WS_URL%" == "" (
        echo Installing Playwright browsers...
        playwright install chromium
        playwright install-deps chromium
    )

    python -c "import nltk; nltk.download('punkt_tab')"
)

SET "KEY_FILE=.openlaunch_secret_key"
SET "LEGACY_KEY_FILE=.webui_secret_key"
IF NOT EXIST "%KEY_FILE%" IF EXIST "%LEGACY_KEY_FILE%" SET "KEY_FILE=%LEGACY_KEY_FILE%"
IF NOT "%OPENLAUNCH_SECRET_KEY_FILE%" == "" (
    SET "KEY_FILE=%OPENLAUNCH_SECRET_KEY_FILE%"
)

IF "%PORT%"=="" SET PORT=8080
IF "%HOST%"=="" SET HOST=0.0.0.0
IF "%FORWARDED_ALLOW_IPS%"=="" SET "FORWARDED_ALLOW_IPS='*'"
SET "OPENLAUNCH_SECRET_KEY=%OPENLAUNCH_SECRET_KEY%"
SET "OPENLAUNCH_JWT_SECRET_KEY=%OPENLAUNCH_JWT_SECRET_KEY%"
IF "%OPENLAUNCH_SECRET_KEY_LENGTH%" == "" (
    SET "OPENLAUNCH_SECRET_KEY_LENGTH=24"
)

:: Check if OPENLAUNCH_SECRET_KEY and OPENLAUNCH_JWT_SECRET_KEY are not set
IF "%OPENLAUNCH_SECRET_KEY% %OPENLAUNCH_JWT_SECRET_KEY%" == " " (
    echo Loading OPENLAUNCH_SECRET_KEY from file, not provided as an environment variable.

    IF NOT EXIST "%KEY_FILE%" (
        echo Generating OPENLAUNCH_SECRET_KEY
        :: Generate a random value to use as a OPENLAUNCH_SECRET_KEY in case the user didn't provide one
        SET /p OPENLAUNCH_SECRET_KEY=<nul
        FOR /L %%i IN (1,1,%OPENLAUNCH_SECRET_KEY_LENGTH%) DO SET /p OPENLAUNCH_SECRET_KEY=<!random!>>%KEY_FILE%
        echo OPENLAUNCH_SECRET_KEY generated
    )

    echo Loading OPENLAUNCH_SECRET_KEY from %KEY_FILE%
    SET /p OPENLAUNCH_SECRET_KEY=<%KEY_FILE%
)

:: Execute uvicorn
SET "OPENLAUNCH_SECRET_KEY=%OPENLAUNCH_SECRET_KEY%"
IF "%UVICORN_WORKERS%"=="" SET UVICORN_WORKERS=1
uvicorn openlaunch.main:app --host "%HOST%" --port "%PORT%" --forwarded-allow-ips %FORWARDED_ALLOW_IPS% --workers %UVICORN_WORKERS% --ws auto
:: For ssl user uvicorn openlaunch.main:app --host "%HOST%" --port "%PORT%" --forwarded-allow-ips '*' --ssl-keyfile "key.pem" --ssl-certfile "cert.pem" --ws auto

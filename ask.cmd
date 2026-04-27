@echo off
setlocal

rem ===========================================================================
rem  ask.cmd  -  Researcher-Agent-Wrapper
rem
rem  Verwendung:  ask "Deine Frage in Anfuehrungszeichen"
rem
rem  Was passiert (Reihenfolge):
rem    1. researcher ask "..."          -> Recherche + lokale Render-Updates
rem    2. git add + commit + push       -> DB landet im Repo
rem    3. gh workflow run pages.yml     -> Live-Site wird neu deployt
rem
rem  Funktioniert von ueberall - der Skript-Pfad wird automatisch ermittelt.
rem ===========================================================================

if "%~1"=="" goto :usage
if /I "%~1"=="--help" goto :usage
if /I "%~1"=="-h" goto :usage
if /I "%~1"=="/?" goto :usage

pushd "%~dp0" || (
  echo [FEHLER] Konnte Projektordner nicht wechseln.
  exit /b 1
)

echo.
echo === 1/3  Recherche laeuft ===
".venv\Scripts\researcher.exe" ask %1
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo [FEHLER] Recherche fehlgeschlagen ^(exit %RC%^).
  echo Tipp: Bei 0xC0000005-Crash einmal wiederholen.
  popd
  exit /b %RC%
)

echo.
echo === 2/3  DB committen und pushen ===
git add data/researcher.sqlite
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "ask: %~1"
  git push
  if errorlevel 1 (
    echo [WARNUNG] Push fehlgeschlagen. DB ist lokal committed; bitte 'git push' manuell wiederholen.
    popd
    exit /b 0
  )
) else (
  echo Keine DB-Aenderungen - ueberspringe Commit.
  popd
  exit /b 0
)

echo.
echo === 3/3  Pages-Deploy triggern ===
gh workflow run pages.yml --repo cavadind-davide/researcher-agent --ref main
if errorlevel 1 (
  echo [WARNUNG] Workflow-Trigger fehlgeschlagen. Live-Update kommt erst beim naechsten Cron ^(Mo 06 UTC^).
)

echo.
echo Live-Site (Build dauert ca. 1-4 min):
echo   https://cavadind-davide.github.io/researcher-agent/

popd
exit /b 0

:usage
echo.
echo Verwendung:  ask "Deine Frage in Anfuehrungszeichen"
echo.
echo Beispiele:
echo   ask "Welche TLS-Mindestkonfiguration empfiehlt das BSI fuer Webserver?"
echo   ask "Wie funktioniert Workload-Identity-Federation in Azure?"
echo   ask "Welche Detection-Use-Cases gegen Pass-the-PRT existieren?"
echo.
echo Optionen:
echo   --help, -h, /?    Diese Hilfe anzeigen
echo.
echo Ablauf:
echo   1. Researcher fuehrt die Recherche aus, schreibt das Topic in die DB
echo      und rendert das HTML neu.
echo   2. Die DB wird committed und gepusht.
echo   3. Der Pages-Workflow wird getriggert -^> Live-Deploy.
echo.
exit /b 1

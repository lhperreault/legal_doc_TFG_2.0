@echo off
echo ============================================
echo  Legal Pipeline Services
echo ============================================
echo.
echo Starting 3 services:
echo   1. Pipeline Worker (polls Supabase for jobs)
echo   2. Upload Server (http://localhost:8787)
echo   3. Folder Watcher (Dropbox/Legal Intake)
echo.
echo Press Ctrl+C in any window to stop that service.
echo ============================================
echo.

cd /d "c:\Users\lukep\Documents\Salle\Business\Senior Year\TFG\Projectfiles"

start "Pipeline Worker" cmd /k "cd /d \"c:\Users\lukep\Documents\Salle\Business\Senior Year\TFG\Projectfiles\" && .venv\Scripts\python.exe backend/05_INTAKE/pipeline_worker.py --poll 10"

start "Upload Server" cmd /k "cd /d \"c:\Users\lukep\Documents\Salle\Business\Senior Year\TFG\Projectfiles\" && .venv\Scripts\python.exe backend/05_INTAKE/upload_server.py"

start "Folder Watcher" cmd /k "cd /d \"c:\Users\lukep\Documents\Salle\Business\Senior Year\TFG\Projectfiles\" && .venv\Scripts\python.exe backend/05_INTAKE/folder_watcher.py --watch-dir \"C:\Users\lukep\Dropbox\Legal Intake\""

echo.
echo All services launched in separate windows.
echo.
pause

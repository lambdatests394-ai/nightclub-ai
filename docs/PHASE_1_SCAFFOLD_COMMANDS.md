# Phase 1 Scaffold Commands

Run these commands from `C:\Users\Rogelio\nightclub-ai` in PowerShell. They create the Python 3.12 virtual environment, install only the dependency source of truth (`requirements.txt`), run the Phase 1 health tests, and start the local FastAPI scaffold.

```powershell
$ErrorActionPreference = 'Stop'
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest backend\tests -q
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

These commands do not target, read, or depend on La Boutique resources. Real environment values must be supplied outside Git; `.env.example` contains only placeholders/default non-secret development values.

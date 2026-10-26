@echo off
set WZ_PORT=8788
set WZ_MAX_WORKERS=2
cd /d <PROJECT_ROOT>\pool_api
python -u app.py >> data\app.log 2>> data\app.err.log

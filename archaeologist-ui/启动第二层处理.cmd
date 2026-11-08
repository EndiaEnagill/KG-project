@echo off
cd /d "%~dp0"
python second_stage.py --source "data/rasbt__mlxtend/20261101-185034-cc52d0-git-5c8a9e" --output "data/rasbt__mlxtend_layer2" --workers 8
pause

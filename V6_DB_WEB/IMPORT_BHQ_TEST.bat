@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHON=%ROOT%erp_returns\20260807_six_month_sr\V6_DUAL_MODE_SR_ERP_TEST_20260806\runtime\python\python.exe"
set "SOURCE=C:\Users\USER\Downloads\202607ALL 訂單明細表.xls"
"%PYTHON%" -X utf8 "%ROOT%bhq_import.py" --source "%SOURCE%" --import-test --db "%ROOT%data\orders_bhq_test.db"
pause

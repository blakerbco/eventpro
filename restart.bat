@echo off
taskkill /F /IM python.exe 2>nul
timeout /t 2 /nobreak >nul
set PORT=5009
start "AuctionIntel" python app.py
echo Server starting on port 5009...

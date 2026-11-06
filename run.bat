@echo off
rem Starts Paper Trader with your real experiment data.
cd /d "%~dp0"
start "" pythonw -m paper_trader

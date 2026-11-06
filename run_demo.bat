@echo off
rem Starts Paper Trader in demo mode: fake data, stored separately. No API keys needed.
cd /d "%~dp0"
start "" pythonw -m paper_trader --demo

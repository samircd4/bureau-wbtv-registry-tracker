@echo off
title Bureau Wbtv Continuous Monitor
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo Starting Bureau Wbtv Continuous Monitor...
echo ============================================================
echo.

uv run monitor.py %*

echo.
echo ============================================================
echo Monitor stopped.
echo ============================================================
pause


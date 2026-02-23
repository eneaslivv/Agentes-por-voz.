@echo off
title Wispr Flow - Voice Dictation
cd /d "%~dp0apps\api"
python wispr_client.py
pause

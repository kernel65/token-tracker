#!/usr/bin/env pythonw
# Панель управления Token Tracker — двойной клик открывает окно запуска/остановки.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import launcher
launcher.run_gui()

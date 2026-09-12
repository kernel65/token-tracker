# -*- coding: utf-8 -*-
"""
Control panel for the Token Tracker.

  launcher.pyw / pythonw launcher.py  — graphical panel
  python launcher.py status|start|stop|restart|open|log|autostart|noautostart

The panel is unrelated to the server window: it starts server.py hidden
(pythonw, no console) and stops it via /api/shutdown — the server itself
saves data and exits cleanly.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(BASE, "server.py")
LOG = os.path.join(BASE, "data", "server.log")
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"

IS_WIN = sys.platform == "win32"
if IS_WIN:
    import ctypes

# ---------------------------------------------------------------- core ops


def is_running(timeout=1.5):
    """Is the server alive. /api/state exists in all versions — probe that."""
    try:
        req = urllib.request.Request(URL + "/api/state",
                                     headers={"User-Agent": "launcher"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        info = {"tokens": len(data.get("tokens") or []),
                "lastPoll": data.get("lastPoll"),
                "pollError": data.get("pollError") or ""}
        return True, info
    except Exception:
        return False, None


def start_server():
    """Launch pythonw server.py hidden; wait until the port answers."""
    alive, _ = is_running()
    if alive:
        return False, "уже запущен"
    pythonw = None
    if IS_WIN:
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(cand):
            import shutil
            cand = shutil.which("pythonw")
        pythonw = cand if cand and os.path.exists(cand) else None
    if pythonw:
        cmd = [pythonw, SERVER]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(cmd, cwd=BASE, creationflags=flags,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    elif IS_WIN:  # no pythonw — just windowless
        subprocess.Popen([sys.executable, SERVER], cwd=BASE,
                         creationflags=0x08000000,  # CREATE_NO_WINDOW
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    else:  # POSIX / no pythonw
        subprocess.Popen([sys.executable, SERVER], cwd=BASE,
                         start_new_session=True,
                         stdout=open(LOG + ".out", "a"),
                         stderr=subprocess.STDOUT)
    for _ in range(20):
        alive, _ = is_running()
        if alive:
            return True, "запущен"
        time.sleep(0.4)
    return False, "не поднялся за 8 секунд — проверь data/server.log"


def stop_server():
    """Graceful stop via API; fallback — kill the PID owning the port."""
    alive, _ = is_running()
    if not alive:
        return True, "уже остановлен"
    try:
        req = urllib.request.Request(URL + "/api/shutdown", method="POST", data=b"{}")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    for _ in range(15):
        alive, _ = is_running()
        if not alive:
            return True, "остановлен (данные сохранены)"
        time.sleep(0.4)
    # graceful didn't work — forceful cleanup by port
    kill_port_owner()
    time.sleep(0.6)
    alive, _ = is_running()
    return (not alive), ("остановлен принудительно" if not alive else "НЕ удалось остановить")


def pid_on_port():
    """PID of the process listening on the port (Windows — netstat, POSIX — lsof/fuser)."""
    try:
        if IS_WIN:
            out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                          text=True, errors="ignore")
            for ln in out.splitlines():
                parts = ln.split()
                # e.g. "TCP  127.0.0.1:8765  0.0.0.0:0  LISTENING  33004"
                if len(parts) >= 5 and parts[0].upper() == "TCP" \
                        and parts[3].upper() == "LISTENING":
                    host, _, p = parts[1].rpartition(":")
                    if p == str(PORT):
                        return int(parts[4])
        else:
            out = subprocess.check_output(
                ["sh", "-c", f"lsof -ti tcp:{PORT} -sTCP:LISTEN || true"], text=True)
            pid = out.strip().split("\n")[0]
            return int(pid) if pid else None
    except Exception:
        pass
    return None


def kill_port_owner():
    pid = pid_on_port()
    if not pid:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, 15)
    except Exception:
        pass


def open_dashboard():
    if IS_WIN:
        os.startfile(URL)
    else:
        subprocess.Popen(["xdg-open", URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tail_log(n=40):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except Exception as e:
        return f"(лог недоступен: {e})"


# ------------------------------------------------------------- autostart

def _startup_dir():
    if not IS_WIN:
        return None
    return os.path.join(os.environ.get("APPDATA", ""),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _autostart_lnk():
    d = _startup_dir()
    return os.path.join(d, "Token Tracker.lnk") if d else None


def autostart_enabled():
    p = _autostart_lnk()
    return bool(p and os.path.exists(p))


def set_autostart(enable):
    """Shortcut in Shell:Startup — runs the hidden launcher --autostart,
    which starts the server without a window and does not open a browser."""
    lnk = _autostart_lnk()
    if not lnk:
        return False, "только Windows"
    try:
        if enable:
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            exe = pythonw if os.path.exists(pythonw) else sys.executable
            ps = (f"$s=(New-Object -ComObject WScript.Shell)."
                  f"CreateShortcut('{lnk}');"
                  f"$s.TargetPath='{sys.executable if not os.path.exists(pythonw) else pythonw}';"
                  f"$s.Arguments='\"{os.path.abspath(__file__)}\" --autostart';"
                  f"$s.WorkingDirectory='{BASE}';"
                  f"$s.Description='Token Tracker';"
                  f"$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True, "автозапуск включён (запуск при входе в Windows, без окна)"
        else:
            if os.path.exists(lnk):
                os.remove(lnk)
            return True, "автозапуск выключен"
    except Exception as e:
        return False, f"ошибка: {e}"


# ------------------------------------------------------------------ GUI

def run_gui():
    if not IS_WIN:
        print("GUI только на Windows; используй команды: status/start/stop/restart/open/log/autostart")
        return
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("Token Tracker — управление")
    root.resizable(False, False)
    root.configure(bg="#0d1220")
    root.attributes("-topmost", True)
    root.update_idletasks()

    style = dict(font=("Segoe UI", 10), bg="#16203a", fg="#e8ecf5",
                 activebackground="#22315c", activeforeground="#ffffff",
                 relief="flat", bd=0, padx=14, pady=9)

    title = tk.Label(root, text="🪙  Token Tracker", font=("Segoe UI", 13, "bold"),
                     bg="#0d1220", fg="#e8ecf5")
    title.pack(anchor="w", padx=18, pady=(16, 2))
    status_lbl = tk.Label(root, text="…", font=("Segoe UI", 9),
                          bg="#0d1220", fg="#8b94ab")
    status_lbl.pack(anchor="w", padx=18)
    tokens_lbl = tk.Label(root, text="", font=("Segoe UI", 9),
                          bg="#0d1220", fg="#8b94ab")
    tokens_lbl.pack(anchor="w", padx=18)

    def do_start():
        ok, msg = start_server()
        if ok:
            refresh()
        else:
            messagebox.showwarning("Запуск", msg)

    def do_stop():
        ok, msg = stop_server()
        if not ok:
            messagebox.showerror("Остановка", msg)
        refresh()

    def do_restart():
        stop_server()
        ok, msg = start_server()
        if not ok:
            messagebox.showerror("Перезапуск", msg)
        refresh()

    def do_log():
        import tempfile
        txt = tail_log(200)
        p = os.path.join(tempfile.gettempdir(), "meme_tracker_log_view.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt)
        os.startfile(p)

    b1 = tk.Button(root, text="▶  Запустить", command=do_start, width=16, **style)
    b2 = tk.Button(root, text="■  Остановить", command=do_stop, width=16, **style)
    b3 = tk.Button(root, text="↻  Перезапустить", command=do_restart, width=16, **style)
    b4 = tk.Button(root, text="🌐  Открыть дашборд", command=open_dashboard, width=16, **style)
    b5 = tk.Button(root, text="📄  Последние логи", command=do_log, width=16, **style)
    for b in (b1, b2, b3, b4, b5):
        b.pack(fill="x", padx=18, pady=3)

    auto_var = tk.BooleanVar(value=autostart_enabled())

    def toggle_auto():
        ok, msg = set_autostart(auto_var.get())
        if not ok:
            messagebox.showwarning("Автозапуск", msg)
            auto_var.set(autostart_enabled())

    chk = tk.Checkbutton(root, text="Запускать при входе в Windows",
                         variable=auto_var, command=toggle_auto,
                         font=("Segoe UI", 9), bg="#0d1220", fg="#e8ecf5",
                         selectcolor="#16203a", activebackground="#0d1220",
                         activeforeground="#e8ecf5", highlightthickness=0)
    chk.pack(anchor="w", padx=16, pady=(10, 6))

    close_lbl = tk.Label(root, text="можно закрыть — трекер продолжит работать",
                         font=("Segoe UI", 8), bg="#0d1220", fg="#5b6478")
    close_lbl.pack(anchor="w", padx=18, pady=(2, 12))

    def refresh():
        alive, info = is_running()
        if alive:
            status_lbl.config(text="⬤ работает", fg="#34d399")
            toks = info.get("tokens")
            err = info.get("pollError")
            tokens_lbl.config(text=f"токенов: {toks}" + (f" · ошибка сети" if err else ""))
        else:
            status_lbl.config(text="⬤ остановлен", fg="#f87171")
            tokens_lbl.config(text="")

    refresh()
    root.after(4000, lambda: (refresh(), root.after(4000, refresh)))
    root.lift()
    root.attributes("-topmost", False)
    root.mainloop()


# ------------------------------------------------------------------- main

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--autostart":
        # autostart on login: server without a window, don't show the GUI
        if args and args[0] == "--autostart":
            alive, _ = is_running()
            if not alive:
                start_server()
            sys.exit(0)
        run_gui()
        sys.exit(0)
    cmd = args[0]
    if cmd == "status":
        alive, info = is_running()
        if alive:
            print(f"running: yes | tokens={info.get('tokens')} | pollError={info.get('pollError') or 'нет'}")
        else:
            pid = pid_on_port()
            print(f"running: no" + (f" (порт держит PID {pid} — зависший процесс?)"))
    elif cmd == "start":
        ok, msg = start_server()
        print("OK" if ok else "FAIL")
        print(msg)
        sys.exit(0 if ok else 1)
    elif cmd == "stop":
        ok, msg = stop_server()
        print("OK" if ok else "FAIL")
        print(msg)
        sys.exit(0 if ok else 1)
    elif cmd == "restart":
        ok1, m1 = stop_server()
        ok2, m2 = start_server()
        print("OK" if (ok1 and ok2) else "FAIL")
        print(m1 + " → " + m2)
        sys.exit(0 if ok2 else 1)
    elif cmd == "open":
        if is_running()[0]:
            open_dashboard()
            print("OK открыт браузер")
        else:
            print("FAIL сервер не запущен")
            sys.exit(1)
    elif cmd == "log":
        print(tail_log(int(args[1]) if len(args) > 1 else 40))
    elif cmd in ("autostart", "noautostart"):
        ok, msg = set_autostart(cmd == "autostart")
        print("OK" if ok else "FAIL")
        print(msg)
        sys.exit(0 if ok else 1)
    else:
        print("usage: status | start | stop | restart | open | log [N] | autostart | noautostart")
        sys.exit(1)

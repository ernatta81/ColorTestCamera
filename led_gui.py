#!/usr/bin/env python3
"""
Interfaccia grafica per il test della barra LED.

    python led_gui.py

Due schede:
  1. SETUP - telecamera, esposizione, numero di LED e definizione a mano
     delle zone di lettura (una per LED, aggiunte col mouse una alla volta).
  2. TEST  - esecuzione del test, esito PASS/FAIL, contatori, report.

La logica di analisi e la stessa di led_test.py.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def _fix_tcl_paths() -> None:
    """Rende Tk utilizzabile dentro un ambiente virtuale su Windows.

    I venv non copiano la cartella 'tcl', che resta nell'installazione base:
    senza queste variabili Tk fallisce con "Can't find a usable init.tcl".
    Va eseguito prima di importare tkinter.
    """
    if os.name != "nt" or os.environ.get("TCL_LIBRARY"):
        return
    tcl_root = os.path.join(getattr(sys, "base_prefix", sys.prefix), "tcl")
    if not os.path.isdir(tcl_root):
        return
    for var, prefix in (("TCL_LIBRARY", "tcl8."), ("TK_LIBRARY", "tk8.")):
        for name in sorted(os.listdir(tcl_root), reverse=True):
            if name.startswith(prefix) and os.path.isdir(os.path.join(tcl_root, name)):
                os.environ[var] = os.path.join(tcl_root, name)
                break


_fix_tcl_paths()

import tkinter as tk  # noqa: E402  (dopo _fix_tcl_paths, per forza)
from dataclasses import asdict  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

from led_test import (  # noqa: E402
    COLOR_NAMES,
    HERE,
    apply_wb,
    backend_flag,
    draw_overlay,
    evaluate,
    load_config,
    measure_zones,
    save_config,
    zones_from_roi,
)

VIEW_W, VIEW_H = 900, 520
CODES = ["R", "G", "B", "Y", "C", "M", "W"]
REPORT_DIR = os.path.join(HERE, "reports")
TEXT_ENTRY = (tk.Entry, ttk.Entry, ttk.Combobox, ttk.Spinbox)


class CameraThread(threading.Thread):
    """Legge la telecamera in background: la GUI non deve mai bloccarsi."""

    def __init__(self, cfg: dict):
        super().__init__(daemon=True)
        self.cfg = dict(cfg)
        self.cap = None
        self.lock = threading.Lock()
        self.frame = None
        self.error = None
        self.stop_evt = threading.Event()

    def run(self):
        cap = cv2.VideoCapture(self.cfg["cam"], backend_flag(self.cfg["backend"]))
        if not cap.isOpened():
            self.error = (f"Impossibile aprire la telecamera {self.cfg['cam']} "
                          f"(backend {self.cfg['backend']}).")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg["height"])
        if self.cfg["manual_exposure"]:
            # esposizione e bilanciamento del bianco fissi: senza questo lo
            # stesso LED misura colori diversi da un frame all'altro
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25 if os.name == "nt" else 1)
            cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        with self.lock:
            self.cap = cap
        while not self.stop_evt.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            with self.lock:
                self.frame = frame
        with self.lock:
            self.cap = None
        cap.release()

    def latest(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def set_exposure(self, value: float):
        with self.lock:
            if self.cap is not None:
                self.cap.set(cv2.CAP_PROP_EXPOSURE, float(value))

    def stop(self):
        self.stop_evt.set()
        self.join(timeout=2.0)


class LedGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.zones = [list(map(int, z)) for z in (self.cfg.get("zones") or [])]
        self.selected = None          # indice della zona selezionata
        self.cam_thread: CameraThread | None = None
        self.view_scale = 1.0
        self.view_off = (0, 0)
        self.drag_start = None
        self.drag_rect_id = None
        self.grid_mode = False        # prossimo trascinamento = griglia di N zone
        self.frozen = None            # risultato del test, congelato
        self.tk_img = None
        self.tick_count = 0
        self.n_pass = 0
        self.n_fail = 0

        root.title("LED Test - verifica colore e accensione barra LED")
        root.minsize(1300, 700)
        self._build_ui()
        self.root.bind("<F5>", lambda _e: self.run_test())
        self.root.bind("<Delete>", self.on_key_delete)
        for key in ("Left", "Right", "Up", "Down"):
            self.root.bind(f"<{key}>", self.on_key_nudge)
            self.root.bind(f"<Shift-{key}>", self.on_key_resize)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self._tick)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # --- colonna sinistra: anteprima
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, width=VIEW_W, height=VIEW_H, bg="#101010",
                                highlightthickness=1, highlightbackground="#555")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_drag_end)
        self.lbl_hint = ttk.Label(left, foreground="#0a5", text="")
        self.lbl_hint.pack(anchor="w", pady=(4, 0))

        # --- colonna destra: schede
        right = ttk.Frame(main, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="y")
        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.tab_setup = ttk.Frame(self.nb, padding=8)
        self.tab_test = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_setup, text="  1. Setup  ")
        self.nb.add(self.tab_test, text="  2. Test  ")
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self._update_hint())

        self._build_setup(self.tab_setup)
        self._build_test(self.tab_test)

        self.var_status = tk.StringVar(
            value="Pronto. Scheda Setup: scegli la telecamera e premi Collega.")
        ttk.Label(self.root, textvariable=self.var_status, relief="sunken",
                  anchor="w", padding=4).pack(fill="x", side="bottom")

    def _build_setup(self, parent):
        # 1) telecamera
        box = ttk.LabelFrame(parent, text="1. Telecamera", padding=8)
        box.pack(fill="x")
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Telecamera").pack(side="left")
        self.var_cam = tk.StringVar(value=str(self.cfg["cam"]))
        self.cmb_cam = ttk.Combobox(row, textvariable=self.var_cam, width=5,
                                    values=[str(i) for i in range(8)])
        self.cmb_cam.pack(side="left", padx=4)
        ttk.Button(row, text="Cerca", command=self.scan_cameras, width=7).pack(side="left")
        self.btn_connect = ttk.Button(row, text="Collega", command=self.toggle_camera, width=10)
        self.btn_connect.pack(side="right")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Backend").pack(side="left")
        self.var_backend = tk.StringVar(value=self.cfg["backend"])
        ttk.Combobox(row, textvariable=self.var_backend, width=7, state="readonly",
                     values=["auto", "dshow", "msmf", "v4l2", "any"]).pack(side="left", padx=4)
        ttk.Label(row, text="(se non trova la webcam: msmf)",
                  foreground="#666").pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Esposizione").pack(side="left")
        self.var_exposure = tk.DoubleVar(
            value=self.cfg["exposure"] if self.cfg["exposure"] is not None else -6)
        ttk.Scale(row, from_=-13, to=0, variable=self.var_exposure, length=150,
                  command=lambda _v: self.apply_exposure()).pack(side="left", padx=4)
        self.lbl_exposure = ttk.Label(row, width=4)
        self.lbl_exposure.pack(side="left")
        ttk.Label(box, text="Abbassala finche i LED non sono piu bruciati (bianchi).",
                  foreground="#666").pack(anchor="w")

        # 2) zone di lettura
        box = ttk.LabelFrame(parent, text="2. Zone di lettura (una per LED)", padding=8)
        box.pack(fill="both", expand=True, pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Numero di LED").pack(side="left")
        self.var_leds = tk.IntVar(value=int(self.cfg.get("leds", 8)))
        ttk.Spinbox(row, from_=0, to=256, width=5, textvariable=self.var_leds,
                    command=self.refresh_zone_list).pack(side="left", padx=4)
        self.lbl_count = tk.Label(row, text="", font=("Segoe UI", 9, "bold"))
        self.lbl_count.pack(side="left", padx=6)

        ttk.Label(box, foreground="#666", justify="left",
                  text="Trascina col mouse sull'anteprima per aggiungere una zona\n"
                       "alla volta, nell'ordine dei LED. Clic su una zona per\n"
                       "selezionarla; frecce = sposta, Shift+frecce = ridimensiona,\n"
                       "Canc = elimina.").pack(anchor="w", pady=(4, 2))

        self.lst_zones = tk.Listbox(box, height=8, exportselection=False,
                                    font=("Consolas", 9), activestyle="none")
        self.lst_zones.pack(fill="both", expand=True)
        self.lst_zones.bind("<<ListboxSelect>>", self.on_list_select)

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Elimina", command=self.delete_selected).pack(side="left")
        ttk.Button(row, text="Elimina ultima", command=self.delete_last).pack(side="left", padx=3)
        ttk.Button(row, text="Cancella tutte", command=self.clear_zones).pack(side="left")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        self.btn_grid = ttk.Button(row, text="Griglia automatica da un rettangolo",
                                   command=self.arm_grid_mode)
        self.btn_grid.pack(side="left")

        # 3) colori attesi
        box = ttk.LabelFrame(parent, text="3. Colori attesi", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.var_mode = tk.StringVar(value=self.cfg.get("mode", "none"))
        ttk.Radiobutton(box, text="Solo accensione (nessun colore atteso)",
                        variable=self.var_mode, value="none").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Radiobutton(row, text="Tutti dello stesso colore", variable=self.var_mode,
                        value="all").pack(side="left")
        self.var_all_color = tk.StringVar(value=self.cfg.get("all_color", "G"))
        ttk.Combobox(row, textvariable=self.var_all_color, width=4, state="readonly",
                     values=CODES).pack(side="left", padx=4)
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(2, 0))
        ttk.Radiobutton(row, text="Sequenza", variable=self.var_mode,
                        value="seq").pack(side="left")
        self.var_seq = tk.StringVar(value=self.cfg.get("seq", "R,G,B"))
        ttk.Entry(row, textvariable=self.var_seq, width=20).pack(side="left", padx=4)
        ttk.Label(box, foreground="#666",
                  text="R rosso  G verde  B blu  Y giallo  C ciano  M magenta  W bianco\n"
                       "La sequenza segue l'ordine delle zone e si ripete se piu corta.").pack(anchor="w")

        # 4) soglie
        box = ttk.LabelFrame(parent, text="4. Soglie e bilanciamento", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.var_off = tk.IntVar(value=self.cfg["off_value"])
        self.var_dim = tk.DoubleVar(value=self.cfg["dim_ratio"])
        self.var_sat = tk.IntVar(value=self.cfg["min_sat"])
        for label, var, frm, to, inc in (
            ("LED spento se V <", self.var_off, 5, 200, 5),
            ("LED debole se V < mediana x", self.var_dim, 0.05, 0.95, 0.05),
            ("Considera bianco se S <", self.var_sat, 10, 200, 5),
        ):
            row = ttk.Frame(box)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label).pack(side="left")
            ttk.Spinbox(row, from_=frm, to=to, increment=inc, width=6,
                        textvariable=var).pack(side="right")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="Calibra bianco", command=self.calibrate_wb).pack(side="left")
        self.lbl_wb = ttk.Label(row, foreground="#666", text="")
        self.lbl_wb.pack(side="left", padx=6)

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Salva impostazioni",
                   command=self.persist_config).pack(side="left")
        ttk.Button(row, text="Vai al test  >>",
                   command=lambda: self.nb.select(self.tab_test)).pack(side="right")

    def _build_test(self, parent):
        self.btn_test = tk.Button(parent, text="ESEGUI TEST  (F5)", command=self.run_test,
                                  font=("Segoe UI", 13, "bold"), height=2,
                                  bg="#1f6fb2", fg="white", activebackground="#17527f")
        self.btn_test.pack(fill="x")

        self.lbl_verdict = tk.Label(parent, text="--", font=("Segoe UI", 34, "bold"),
                                    bg="#333", fg="white", height=2)
        self.lbl_verdict.pack(fill="x", pady=(8, 0))

        cols = ("led", "letto", "atteso", "h", "s", "v", "esito")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", height=12)
        for col, text, width in (("led", "LED", 44), ("letto", "letto", 76),
                                 ("atteso", "atteso", 76), ("h", "H", 44),
                                 ("s", "S", 44), ("v", "V", 44), ("esito", "esito", 52)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="center")
        self.tree.tag_configure("ko", background="#ffd9d9")
        self.tree.pack(fill="both", expand=True, pady=(8, 0))

        self.txt_notes = tk.Text(parent, height=5, wrap="word", state="disabled",
                                 background="#f7f7f7", relief="flat")
        self.txt_notes.pack(fill="x", pady=(6, 0))

        box = ttk.LabelFrame(parent, text="Contatori", padding=6)
        box.pack(fill="x", pady=(8, 0))
        self.var_counters = tk.StringVar(value="eseguiti 0   PASS 0   FAIL 0")
        ttk.Label(box, textvariable=self.var_counters).pack(side="left")
        ttk.Button(box, text="Azzera", command=self.reset_counters).pack(side="right")

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        self.btn_resume = ttk.Button(row, text="Torna al live", command=self.resume_live,
                                     state="disabled")
        self.btn_resume.pack(side="left")
        ttk.Button(row, text="Salva report...", command=self.save_report).pack(side="left", padx=4)
        self.var_autosave = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="salva ogni test in reports/",
                        variable=self.var_autosave).pack(side="left", padx=4)

    def _update_hint(self):
        if self.nb.select() == str(self.tab_setup):
            if self.grid_mode:
                self.lbl_hint.config(
                    text="GRIGLIA: trascina un rettangolo che contenga tutti i LED.")
            else:
                self.lbl_hint.config(
                    text="SETUP: trascina col mouse per aggiungere una zona di lettura; "
                         "clic su una zona per selezionarla.")
        else:
            self.lbl_hint.config(text="TEST: premi ESEGUI TEST (o F5) con il pezzo in posizione.")

    # -------------------------------------------------------------- camera

    def toggle_camera(self):
        if self.cam_thread:
            self.cam_thread.stop()
            self.cam_thread = None
            self.btn_connect.config(text="Collega")
            self.status("Telecamera scollegata.")
            return
        self.sync_cfg()
        self.cam_thread = CameraThread(self.cfg)
        self.cam_thread.start()
        self.btn_connect.config(text="Scollega")
        self.status(f"Telecamera {self.cfg['cam']} in apertura...")
        self.root.after(600, self.apply_exposure)

    def apply_exposure(self):
        self.lbl_exposure.config(text=f"{self.var_exposure.get():.0f}")
        if self.cam_thread:
            self.cam_thread.set_exposure(self.var_exposure.get())

    def scan_cameras(self):
        """Enumera gli indici disponibili: operazione lenta, va in un thread."""
        if self.cam_thread:
            messagebox.showinfo("Ricerca", "Scollega prima la telecamera.")
            return
        self.status("Ricerca telecamere in corso...")
        backend = self.var_backend.get()

        def work():
            found = []
            for idx in range(8):
                cap = cv2.VideoCapture(idx, backend_flag(backend))
                if cap.isOpened():
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        found.append(idx)
                cap.release()
            self.root.after(0, lambda: self._scan_done(found))

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, found):
        if found:
            self.cmb_cam.config(values=[str(i) for i in found])
            self.var_cam.set(str(found[0]))
            self.status("Telecamere trovate: " + ", ".join(map(str, found)))
        else:
            self.status("Nessuna telecamera trovata. Controlla il cavo, le "
                        "impostazioni privacy di Windows e prova un altro backend.")

    # ---------------------------------------------------------------- zone

    def n_expected(self) -> int:
        try:
            return max(0, int(self.var_leds.get()))
        except (tk.TclError, ValueError):
            return 0

    def view_to_frame(self, x, y):
        ox, oy = self.view_off
        return (x - ox) / self.view_scale, (y - oy) / self.view_scale

    def on_drag_start(self, ev):
        self.drag_start = (ev.x, ev.y)
        if self.drag_rect_id:
            self.canvas.delete(self.drag_rect_id)
        self.drag_rect_id = self.canvas.create_rectangle(
            ev.x, ev.y, ev.x, ev.y, outline="#00ff00", width=2)

    def on_drag_move(self, ev):
        if self.drag_start and self.drag_rect_id:
            x0, y0 = self.drag_start
            self.canvas.coords(self.drag_rect_id, x0, y0, ev.x, ev.y)

    def on_drag_end(self, ev):
        if not self.drag_start:
            return
        x0, y0 = self.drag_start
        self.drag_start = None
        if self.drag_rect_id:
            self.canvas.delete(self.drag_rect_id)
            self.drag_rect_id = None

        # trascinamento minimo = clic: seleziona la zona sotto il puntatore
        if abs(ev.x - x0) < 6 and abs(ev.y - y0) < 6:
            self.select_zone_at(*self.view_to_frame(ev.x, ev.y))
            return

        fx0, fy0 = self.view_to_frame(min(x0, ev.x), min(y0, ev.y))
        fx1, fy1 = self.view_to_frame(max(x0, ev.x), max(y0, ev.y))
        rect = [max(0, int(fx0)), max(0, int(fy0)), int(fx1 - fx0), int(fy1 - fy0)]
        if rect[2] < 4 or rect[3] < 4:
            return

        if self.grid_mode:
            self.make_grid(rect)
            return
        limit = self.n_expected()
        if limit and len(self.zones) >= limit:
            self.status(f"Hai gia definito {limit} zone: aumenta il numero di LED "
                        f"oppure elimina una zona.")
            return
        self.zones.append(rect)
        self.selected = len(self.zones) - 1
        self.refresh_zone_list()
        self.status(f"Zona {len(self.zones)} aggiunta.")

    def arm_grid_mode(self):
        n = self.n_expected()
        if n <= 0:
            messagebox.showinfo("Griglia", "Imposta prima il numero di LED.")
            return
        self.grid_mode = True
        self._update_hint()
        self.status(f"Trascina un rettangolo intorno a tutti i {n} LED: "
                    f"verra diviso in {n} zone uguali.")

    def make_grid(self, rect):
        """Genera N zone uguali dentro il rettangolo trascinato."""
        n = self.n_expected()
        self.grid_mode = False
        self._update_hint()
        if n <= 0:
            return
        self.zones = [list(map(int, z)) for z in zones_from_roi(rect, n, (rect[3], rect[2]))]
        self.selected = None
        self.refresh_zone_list()
        self.status(f"Griglia creata: {n} zone. Puoi ritoccarle una a una "
                    f"(clic, frecce, Shift+frecce).")

    def select_zone_at(self, fx, fy):
        for i, (x, y, w, h) in enumerate(self.zones):
            if x <= fx <= x + w and y <= fy <= y + h:
                self.selected = i
                self.refresh_zone_list()
                self.status(f"Zona {i + 1} selezionata.")
                return
        self.selected = None
        self.refresh_zone_list()

    def on_list_select(self, _ev):
        sel = self.lst_zones.curselection()
        self.selected = sel[0] if sel else None

    def delete_selected(self):
        if self.selected is None or not (0 <= self.selected < len(self.zones)):
            self.status("Nessuna zona selezionata.")
            return
        n = self.selected + 1
        self.zones.pop(self.selected)
        self.selected = None
        self.refresh_zone_list()
        self.status(f"Zona {n} eliminata; le successive sono state rinumerate.")

    def delete_last(self):
        if not self.zones:
            return
        self.zones.pop()
        self.selected = None
        self.refresh_zone_list()
        self.status("Ultima zona eliminata.")

    def clear_zones(self):
        if not self.zones:
            return
        if not messagebox.askyesno("Cancella tutte", "Eliminare tutte le zone?"):
            return
        self.zones.clear()
        self.selected = None
        self.refresh_zone_list()
        self.status("Zone cancellate.")

    def on_key_delete(self, _ev):
        if isinstance(self.root.focus_get(), TEXT_ENTRY):
            return
        self.delete_selected()

    def _nudge(self, ev, resize: bool):
        if isinstance(self.root.focus_get(), TEXT_ENTRY):
            return
        if self.selected is None or not (0 <= self.selected < len(self.zones)):
            return
        step = 1 if not resize else 2
        dx = {"Left": -step, "Right": step}.get(ev.keysym, 0)
        dy = {"Up": -step, "Down": step}.get(ev.keysym, 0)
        z = self.zones[self.selected]
        if resize:
            z[2] = max(4, z[2] + dx)
            z[3] = max(4, z[3] + dy)
        else:
            z[0] = max(0, z[0] + dx)
            z[1] = max(0, z[1] + dy)
        self.refresh_zone_list()
        return "break"

    def on_key_nudge(self, ev):
        return self._nudge(ev, resize=False)

    def on_key_resize(self, ev):
        return self._nudge(ev, resize=True)

    def refresh_zone_list(self):
        self.lst_zones.delete(0, "end")
        for i, (x, y, w, h) in enumerate(self.zones):
            self.lst_zones.insert("end", f"{i + 1:>3}  x={x:<5} y={y:<5} {w}x{h}")
        if self.selected is not None and 0 <= self.selected < len(self.zones):
            self.lst_zones.selection_clear(0, "end")
            self.lst_zones.selection_set(self.selected)
            self.lst_zones.see(self.selected)
        n, k = self.n_expected(), len(self.zones)
        if n == 0:
            self.lbl_count.config(text=f"zone: {k} (nessun numero atteso)", fg="#666")
        elif k == n:
            self.lbl_count.config(text=f"zone: {k} / {n}", fg="#1b8a3a")
        else:
            self.lbl_count.config(text=f"zone: {k} / {n}", fg="#b32020")

    # ----------------------------------------------------------- impostazioni

    def persist_config(self):
        self.sync_cfg()
        save_config(self.cfg)
        self.status(f"Impostazioni salvate in config.json ({len(self.zones)} zone).")

    def sync_cfg(self):
        """Copia i valori dei widget nella configurazione."""
        try:
            self.cfg["cam"] = int(self.var_cam.get())
        except ValueError:
            self.cfg["cam"] = 0
        self.cfg["backend"] = self.var_backend.get()
        self.cfg["exposure"] = float(self.var_exposure.get())
        self.cfg["off_value"] = int(self.var_off.get())
        self.cfg["dim_ratio"] = float(self.var_dim.get())
        self.cfg["min_sat"] = int(self.var_sat.get())
        self.cfg["leds"] = self.n_expected()
        self.cfg["zones"] = [list(map(int, z)) for z in self.zones]
        self.cfg["mode"] = self.var_mode.get()
        self.cfg["all_color"] = self.var_all_color.get()
        self.cfg["seq"] = self.var_seq.get()

    # ------------------------------------------------------------- analisi

    def expected_list(self, n: int):
        mode = self.var_mode.get()
        if mode == "all":
            return [self.var_all_color.get().strip().upper()] * n
        if mode == "seq":
            seq = [c.strip().upper() for c in self.var_seq.get().split(",") if c.strip()]
            if not seq:
                return [None] * n
            if len(seq) == n:
                return seq
            if n % len(seq) == 0:
                return seq * (n // len(seq))
            return [None] * n
        return [None] * n

    def analyze_frame(self, frame):
        """Misura le zone definite a mano e restituisce esito e problemi."""
        self.sync_cfg()
        results = measure_zones(frame, self.zones, self.cfg)
        verdict = evaluate(results, self.expected_list(len(results)), self.cfg)
        issues = []
        n = self.n_expected()
        if n and len(self.zones) != n:
            issues.append(f"zone definite {len(self.zones)} su {n} LED attesi")
        if not self.zones:
            issues.append("nessuna zona di lettura definita")
        if issues:
            verdict = "FAIL"
        return results, verdict, issues

    def run_test(self):
        if not self.cam_thread:
            messagebox.showwarning("Test", "Collega prima la telecamera (scheda Setup).")
            return
        if not self.zones:
            messagebox.showwarning(
                "Test", "Definisci prima le zone di lettura nella scheda Setup:\n"
                        "trascina col mouse una zona per ogni LED.")
            self.nb.select(self.tab_setup)
            return
        self.nb.select(self.tab_test)
        frames = []
        deadline = time.time() + 1.2
        while len(frames) < 5 and time.time() < deadline:
            f = self.cam_thread.latest()
            if f is not None:
                frames.append(apply_wb(f, self.cfg["wb_gains"]))
            time.sleep(0.06)
        if not frames:
            messagebox.showerror("Test", "Nessun frame disponibile dalla telecamera.")
            return
        # mediana su piu frame: taglia il rumore del sensore e lo sfarfallio PWM
        frame = np.median(np.stack(frames).astype(np.float32), axis=0).astype(np.uint8)
        results, verdict, issues = self.analyze_frame(frame)

        self.frozen = (frame, results, verdict, issues)
        self.btn_resume.config(state="normal")
        self.show_results(results, verdict, issues)
        if verdict == "PASS":
            self.n_pass += 1
        else:
            self.n_fail += 1
        self.update_counters()
        self.status(f"Test eseguito su {len(frames)} frame - esito {verdict}")
        if self.var_autosave.get():
            self.autosave_report()

    def resume_live(self):
        self.frozen = None
        self.btn_resume.config(state="disabled")
        self.status("Anteprima live.")

    def reset_counters(self):
        self.n_pass = self.n_fail = 0
        self.update_counters()

    def update_counters(self):
        self.var_counters.set(f"eseguiti {self.n_pass + self.n_fail}   "
                              f"PASS {self.n_pass}   FAIL {self.n_fail}")

    def calibrate_wb(self):
        """Guadagni B/G/R su un riferimento bianco (LED bianco o R+G+B accesi)."""
        if not self.cam_thread:
            messagebox.showwarning("Calibrazione", "Collega prima la telecamera.")
            return
        frame = self.cam_thread.latest()
        if frame is None:
            return
        # usa i pixel delle zone definite; senza zone, tutta l'immagine
        if self.zones:
            fh, fw = frame.shape[:2]
            parts = []
            for x, y, w, h in self.zones:
                sub = frame[max(0, y):min(fh, y + h), max(0, x):min(fw, x + w)]
                if sub.size:
                    parts.append(sub.reshape(-1, 3))
            px_all = np.concatenate(parts) if parts else frame.reshape(-1, 3)
        else:
            px_all = frame.reshape(-1, 3)

        px_all = px_all.astype(np.float32)
        lum = px_all.max(axis=1)
        px = px_all[lum >= max(float(np.percentile(lum, self.cfg["bright_pct"])), 1.0)]
        px = px[px.max(axis=1) < 250.0]
        if len(px) < 20:
            messagebox.showerror(
                "Calibrazione",
                "Area troppo piccola o sovraesposta.\nRiduci l'esposizione, inquadra "
                "un riferimento bianco (LED bianco oppure R+G+B accesi) e riprova.")
            return
        mean = px.mean(axis=0)
        self.cfg["wb_gains"] = [round(float(g), 4) for g in mean.mean() / np.maximum(mean, 1e-3)]
        self.lbl_wb.config(text=f"guadagni B/G/R = {self.cfg['wb_gains']}")
        self.status("Bilanciamento del bianco calibrato.")

    def show_results(self, results, verdict, issues=None):
        self.tree.delete(*self.tree.get_children())
        for r in results:
            exp = COLOR_NAMES.get(r.expected, r.expected) if r.expected else "-"
            self.tree.insert("", "end", tags=() if r.ok else ("ko",), values=(
                r.index, r.name, exp, f"{r.hue:.0f}", f"{r.sat:.0f}",
                f"{r.val:.0f}", "OK" if r.ok else "KO"))
        self.lbl_verdict.config(text=verdict,
                                bg={"PASS": "#1b8a3a", "FAIL": "#b32020"}.get(verdict, "#333"))
        notes = [f"! {t}" for t in (issues or [])]
        notes += [f"LED {r.index}: {n}" for r in results for n in r.notes]
        self.txt_notes.config(state="normal")
        self.txt_notes.delete("1.0", "end")
        self.txt_notes.insert("1.0", "\n".join(notes) if notes else "Nessuna anomalia.")
        self.txt_notes.config(state="disabled")

    # -------------------------------------------------------------- report

    def _write_report(self, path):
        frame, results, verdict, issues = self.frozen
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "verdict": verdict,
            "camera": self.cfg["cam"],
            "leds_expected": self.n_expected(),
            "zones": [list(map(int, z)) for z in self.zones],
            "issues": issues,
            "wb_gains": self.cfg["wb_gains"],
            "leds": [asdict(r) for r in results],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        cv2.imwrite(os.path.splitext(path)[0] + ".png",
                    draw_overlay(frame, results, verdict, None, issues))

    def save_report(self):
        if not self.frozen:
            messagebox.showinfo("Report", "Esegui prima un test.")
            return
        path = filedialog.asksaveasfilename(
            title="Salva report", defaultextension=".json", initialdir=HERE,
            initialfile=time.strftime("report_%Y%m%d_%H%M%S.json"),
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        self._write_report(path)
        self.status(f"Report salvato: {os.path.basename(path)} (+ immagine annotata)")

    def autosave_report(self):
        os.makedirs(REPORT_DIR, exist_ok=True)
        name = time.strftime(f"%Y%m%d_%H%M%S_{self.frozen[2]}.json")
        self._write_report(os.path.join(REPORT_DIR, name))
        self.status(f"Test {self.frozen[2]} - report automatico in reports/{name}")

    # --------------------------------------------------------------- video

    def _tick(self):
        try:
            self._update_view()
        finally:
            self.root.after(40, self._tick)

    def _update_view(self):
        self.tick_count += 1
        if self.cam_thread and self.cam_thread.error:
            err = self.cam_thread.error
            self.cam_thread = None
            self.btn_connect.config(text="Collega")
            self.status(err)
            messagebox.showerror("Telecamera", err)
            return

        if self.frozen:
            frame, results, verdict, issues = self.frozen
            shown = draw_overlay(frame, results, verdict, None, issues)
        else:
            if not self.cam_thread:
                return
            raw = self.cam_thread.latest()
            if raw is None:
                return
            frame = apply_wb(raw, self.cfg["wb_gains"])
            if not self.zones:
                shown = frame.copy()
                cv2.putText(shown, "Definisci le zone di lettura (scheda Setup)",
                            (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                            (0, 200, 255), 2, cv2.LINE_AA)
            else:
                results, verdict, issues = self.analyze_frame(frame)
                shown = draw_overlay(frame, results, verdict, None, issues,
                                     highlight=self.selected)
                if self.tick_count % 8 == 0:   # tabella a ~3 Hz: evita lo sfarfallio
                    self.show_results(results, verdict, issues)
        self._draw(shown)

    def _draw(self, bgr):
        cw = max(self.canvas.winfo_width(), 320)
        ch = max(self.canvas.winfo_height(), 240)
        h, w = bgr.shape[:2]
        self.view_scale = min(cw / w, ch / h)
        dw, dh = max(int(w * self.view_scale), 1), max(int(h * self.view_scale), 1)
        self.view_off = ((cw - dw) // 2, (ch - dh) // 2)
        small = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA)
        self.tk_img = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
        self.canvas.delete("video")
        self.canvas.create_image(*self.view_off, anchor="nw", image=self.tk_img, tags="video")
        self.canvas.tag_lower("video")

    # ---------------------------------------------------------------- varie

    def status(self, text: str):
        self.var_status.set(text)

    def on_close(self):
        if self.cam_thread:
            self.cam_thread.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    gui = LedGui(root)
    gui.refresh_zone_list()
    gui.apply_exposure()
    gui._update_hint()
    root.mainloop()


if __name__ == "__main__":
    main()

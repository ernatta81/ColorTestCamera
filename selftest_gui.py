#!/usr/bin/env python3
"""Verifica l'interfaccia grafica senza telecamera: apre la finestra, simula
col mouse l'aggiunta delle zone una per una, la selezione, lo spostamento,
l'eliminazione e l'esecuzione del test su un'immagine sintetica.

    python selftest_gui.py
"""

import os
import sys
import tempfile
import tkinter as tk
from types import SimpleNamespace

import led_gui
from selftest import BAR_H, BAR_W, MARGIN, synth_bar

FAILURES = []


def check(label, cond, extra=""):
    print(f"{'OK  ' if cond else 'FAIL'} {label}{(' - ' + str(extra)) if extra else ''}")
    if not cond:
        FAILURES.append(label)


def main() -> int:
    root = tk.Tk()
    gui = led_gui.LedGui(root)
    gui.refresh_zone_list()
    gui.apply_exposure()
    gui._update_hint()
    root.update()

    # l'immagine sintetica stabilisce la mappatura schermo <-> frame
    img = synth_bar(["R", "G", "B"])
    gui._draw(img)
    root.update()

    def to_view(fx, fy):
        ox, oy = gui.view_off
        return ox + fx * gui.view_scale, oy + fy * gui.view_scale

    def drag(fx0, fy0, fx1, fy1):
        x0, y0 = to_view(fx0, fy0)
        x1, y1 = to_view(fx1, fy1)
        gui.on_drag_start(SimpleNamespace(x=int(x0), y=int(y0)))
        gui.on_drag_move(SimpleNamespace(x=int(x1), y=int(y1)))
        gui.on_drag_end(SimpleNamespace(x=int(x1), y=int(y1)))
        root.update()

    def click(fx, fy):
        x, y = to_view(fx, fy)
        gui.on_drag_start(SimpleNamespace(x=int(x), y=int(y)))
        gui.on_drag_end(SimpleNamespace(x=int(x), y=int(y)))
        root.update()

    step = BAR_W / 3
    centers = [(MARGIN + (i + 0.5) * step, MARGIN + BAR_H / 2 + 20) for i in range(3)]

    # --- 1) numero di LED e aggiunta manuale di una zona alla volta
    gui.var_leds.set(3)
    gui.zones.clear()
    gui.refresh_zone_list()
    for i, (cx, cy) in enumerate(centers, start=1):
        drag(cx - step * 0.3, cy - 35, cx + step * 0.3, cy + 35)
        check(f"aggiunta zona {i} col mouse", len(gui.zones) == i, gui.zones[-1])
    check("conteggio zone = 3/3", "3 / 3" in gui.lbl_count.cget("text"),
          gui.lbl_count.cget("text"))
    check("elenco aggiornato", gui.lst_zones.size() == 3)

    # --- 2) oltre il numero impostato non si aggiunge
    drag(600, 200, 660, 240)
    check("quarta zona rifiutata (limite 3)", len(gui.zones) == 3, gui.var_status.get())

    # --- 3) selezione con un clic e spostamento con le frecce
    click(*centers[1])
    check("clic seleziona la zona 2", gui.selected == 1, gui.selected)
    before = list(gui.zones[1])
    for _ in range(3):
        gui.on_key_nudge(SimpleNamespace(keysym="Right"))
    gui.on_key_resize(SimpleNamespace(keysym="Down"))
    root.update()
    check("frecce spostano la zona", gui.zones[1][0] == before[0] + 3, gui.zones[1])
    check("Shift+frecce ridimensionano", gui.zones[1][3] == before[3] + 2, gui.zones[1])
    gui.zones[1] = before

    # --- 4) test con la sequenza R,G,B sulle zone definite
    gui.var_mode.set("seq")
    gui.var_seq.set("R,G,B")
    results, verdict, issues = gui.analyze_frame(img)
    check("3 zone lette", len(results) == 3, [r.code for r in results])
    check("sequenza R,G,B riconosciuta -> PASS", verdict == "PASS",
          f"{verdict} {[r.code for r in results]} {issues}")

    # --- 5) un LED spento -> FAIL sulla zona giusta
    res_off, verdict_off, _ = gui.analyze_frame(synth_bar(["R", "G", "B"], off_index=2))
    check("LED 3 spento -> FAIL", verdict_off == "FAIL" and not res_off[2].ok,
          res_off[2].notes)

    # --- 6) eliminazione: il conteggio non torna e il test fallisce
    gui.selected = 0
    gui.delete_selected()
    check("zona eliminata", len(gui.zones) == 2)
    check("conteggio segnala 2/3", "2 / 3" in gui.lbl_count.cget("text"),
          gui.lbl_count.cget("text"))
    _, verdict_missing, issues_missing = gui.analyze_frame(img)
    check("zone mancanti -> FAIL", verdict_missing == "FAIL", issues_missing)
    check("motivo indicato", any("zone definite" in t for t in issues_missing),
          issues_missing)

    # --- 7) griglia automatica: riempie tutte le zone in un colpo
    gui.arm_grid_mode()
    drag(MARGIN, MARGIN, MARGIN + BAR_W, MARGIN + BAR_H)
    check("griglia genera 3 zone", len(gui.zones) == 3, gui.zones)
    results_grid, verdict_grid, _ = gui.analyze_frame(img)
    check("griglia -> PASS con R,G,B", verdict_grid == "PASS",
          [r.code for r in results_grid])

    # --- 8) disegno dell'overlay e salvataggio del report
    gui.frozen = (img, results_grid, verdict_grid, [])
    gui.show_results(results_grid, verdict_grid, [])
    for _ in range(2):
        gui._update_view()
        root.update()
    check("verdetto a schermo", gui.lbl_verdict.cget("text") == "PASS")
    check("tabella popolata", len(gui.tree.get_children()) == 3)

    out = os.path.join(tempfile.mkdtemp(), "report.json")
    gui._write_report(out)
    check("report json + png", os.path.exists(out) and os.path.exists(out[:-5] + ".png"))

    # --- 9) le zone finiscono nella configurazione salvabile
    gui.sync_cfg()
    check("zone in configurazione", gui.cfg["zones"] == [list(map(int, z)) for z in gui.zones],
          gui.cfg["zones"])
    check("numero LED in configurazione", gui.cfg["leds"] == 3)

    gui.on_close()
    print("\n" + ("TUTTI I TEST GUI OK" if not FAILURES
                  else f"TEST GUI FALLITI: {FAILURES}"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())

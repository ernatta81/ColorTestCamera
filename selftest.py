#!/usr/bin/env python3
"""Verifica la logica di analisi su immagini sintetiche, senza telecamera.

    python selftest.py
"""

import sys

import cv2
import numpy as np

from led_test import DEFAULTS, analyze, draw_overlay, print_report

CODE_BGR = {
    "R": (0, 0, 220), "G": (0, 200, 0), "B": (220, 0, 0),
    "Y": (0, 200, 210), "C": (200, 200, 0), "M": (200, 0, 200),
    "W": (230, 230, 230),
}

BAR_W, BAR_H = 800, 120
MARGIN = 40


def synth_bar(codes, off_index=None, dim_index=None, w=BAR_W, h=BAR_H):
    """Barra LED finta: un riquadro luminoso per LED su sfondo scuro."""
    img = np.full((h + 80, w + 80, 3), 12, np.uint8)
    step = w / len(codes)
    for i, code in enumerate(codes):
        x0 = int(MARGIN + i * step) + 6
        x1 = int(MARGIN + (i + 1) * step) - 6
        color = np.array(CODE_BGR[code], np.float32)
        if i == off_index:
            color *= 0.02          # LED guasto: spento
        elif i == dim_index:
            color *= 0.25          # LED debole
        cv2.rectangle(img, (x0, 46), (x1, 34 + h), color.tolist(), -1)
    # alone diffuso, come nella realta
    return cv2.GaussianBlur(img, (9, 9), 0)


def manual_zones(n, w=BAR_W, h=BAR_H):
    """Zone di lettura equivalenti a quelle che si disegnerebbero col mouse."""
    step = w / n
    return [[int(MARGIN + i * step) + 12, 60, max(4, int(step) - 24), h - 40]
            for i in range(n)]


def base_cfg(**over):
    cfg = dict(DEFAULTS)
    cfg["roi"] = [MARGIN, MARGIN, BAR_W, BAR_H]
    cfg.update(over)
    return cfg


def run(title, codes, expected, want, cfg=None, n_leds=None, **kw):
    img = synth_bar(codes, **kw)
    cfg = cfg or base_cfg()
    n = len(codes) if n_leds is None else n_leds
    results, verdict, zones, issues = analyze(img, cfg, n, expected)
    print(f"\n=== {title} ===")
    print_report(results, verdict, issues)
    ok = verdict == want
    if not ok:
        print(f"!! atteso verdetto {want}, ottenuto {verdict}")
        cv2.imwrite(f"selftest_fail_{title.replace(' ', '_')}.png",
                    draw_overlay(img, results, verdict, cfg.get("roi"), issues))
    return ok, results, issues


def main() -> int:
    all_ok = True

    # --- zone ricavate dividendo la ROI (--leds N)
    ok, res, _ = run("RGB corretti", ["R", "G", "B"], ["R", "G", "B"], "PASS")
    all_ok &= ok and [r.code for r in res] == ["R", "G", "B"]

    ok, _, _ = run("8 LED verdi", ["G"] * 8, ["G"] * 8, "PASS")
    all_ok &= ok

    ok, res, _ = run("un LED spento", ["G"] * 8, ["G"] * 8, "FAIL", off_index=3)
    all_ok &= ok and not res[3].ok

    ok, res, _ = run("un LED debole", ["B"] * 6, ["B"] * 6, "FAIL", dim_index=2)
    all_ok &= ok and not res[2].ok

    ok, res, _ = run("colore sbagliato", ["R", "G", "R"], ["R", "G", "B"], "FAIL")
    all_ok &= ok and res[2].code == "R"

    ok, _, _ = run("bianco (tutti i canali)", ["W"] * 4, ["W"] * 4, "PASS")
    all_ok &= ok

    ok, _, _ = run("secondari Y/C/M", ["Y", "C", "M"], ["Y", "C", "M"], "PASS")
    all_ok &= ok

    # --- zone definite a mano (quelle dell'interfaccia grafica)
    ok, res, _ = run("zone manuali, 5 LED blu", ["B"] * 5, ["B"] * 5, "PASS",
                     cfg=base_cfg(zones=manual_zones(5)))
    all_ok &= ok and [r.code for r in res] == ["B"] * 5

    ok, res, _ = run("zone manuali, LED 4 spento", ["G"] * 5, ["G"] * 5, "FAIL",
                     cfg=base_cfg(zones=manual_zones(5)), off_index=3)
    all_ok &= ok and not res[3].ok

    # il numero di zone deve coincidere con i LED dichiarati
    ok, res, issues = run("zone insufficienti (4 su 5)", ["G"] * 5, ["G"] * 5, "FAIL",
                          cfg=base_cfg(zones=manual_zones(5)[:4]), n_leds=5)
    all_ok &= ok and len(res) == 4 and any("zone" in t for t in issues)

    # zone in ordine invertito: la sequenza attesa segue l'ordine delle zone
    cfg = base_cfg(zones=list(reversed(manual_zones(3))))
    ok, res, _ = run("zone in ordine invertito", ["R", "G", "B"], ["B", "G", "R"], "PASS", cfg=cfg)
    all_ok &= ok and [r.code for r in res] == ["B", "G", "R"]

    # zona fuori dall'immagine: deve essere segnalata, non far crashare
    cfg = base_cfg(zones=manual_zones(3) + [[5000, 5000, 40, 40]])
    ok, res, _ = run("zona fuori dall'immagine", ["R", "G", "B"], [None] * 4, "FAIL",
                     cfg=cfg, n_leds=4)
    all_ok &= ok and "fuori" in " ".join(res[3].notes)

    print("\n" + ("TUTTI I TEST OK" if all_ok else "CI SONO TEST FALLITI"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

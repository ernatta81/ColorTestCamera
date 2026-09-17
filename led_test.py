#!/usr/bin/env python3
"""
LED bar tester - verifica via webcam USB il colore e l'accensione dei LED
di una barra LED RGB.

Ogni LED viene letto in una "zona" rettangolare. Le zone possono essere:
  - definite a mano una per una (interfaccia grafica led_gui.py, salvate in
    config.json alla voce "zones");
  - ricavate dividendo una ROI in parti uguali (--leds N);
  - rilevate automaticamente come blob luminosi (--leds 0).

Comandi:
    python led_test.py list                  elenca le telecamere disponibili
    python led_test.py roi                   seleziona l'area della barra LED
    python led_test.py wb                    calibra il bilanciamento del bianco
    python led_test.py live                  anteprima live con verdetto continuo
    python led_test.py test --expect R,G,B   test singolo -> exit code 0 = PASS, 1 = FAIL

Esempi:
    python led_test.py roi --cam 0
    python led_test.py live --leds 8
    python led_test.py test --leds 8 --expect-all G --save out
    python led_test.py test --leds 3 --expect R,G,B --json report.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, asdict, field

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

DEFAULTS = {
    "cam": 0,
    "width": 1280,
    "height": 720,
    "backend": "auto",            # auto | dshow | msmf | v4l2 | any
    "roi": None,                  # [x, y, w, h] area complessiva (opzionale)
    "zones": [],                  # [[x, y, w, h], ...] zone di lettura manuali
    "wb_gains": [1.0, 1.0, 1.0],  # guadagni B, G, R applicati al frame
    "exposure": None,             # valore CAP_PROP_EXPOSURE; None = non toccare
    "manual_exposure": True,      # disattiva auto-esposizione e auto-WB
    "off_value": 40,              # V medio sotto il quale il LED e considerato spento
    "min_sat": 60,                # S medio sotto il quale il colore e "bianco"
    "dim_ratio": 0.45,            # LED sospetto se luminosita < ratio * mediana
    "clip_warn": 0.35,            # quota di pixel saturi oltre cui l'esposizione e troppo alta
    "bright_pct": 70,             # percentile di luminosita per isolare l'area emittente
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except (OSError, ValueError) as exc:
            print(f"[warn] config.json non leggibile ({exc}), uso i default")
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"[ok] configurazione salvata in {CONFIG_PATH}")


# ---------------------------------------------------------------------------
# Telecamera
# ---------------------------------------------------------------------------

def backend_flag(name: str) -> int:
    if name == "auto":
        name = "dshow" if platform.system() == "Windows" else "v4l2"
    return {
        "dshow": cv2.CAP_DSHOW,
        "msmf": cv2.CAP_MSMF,
        "v4l2": cv2.CAP_V4L2,
        "any": cv2.CAP_ANY,
    }.get(name, cv2.CAP_ANY)


def open_camera(cfg: dict) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(cfg["cam"], backend_flag(cfg["backend"]))
    if not cap.isOpened():
        raise SystemExit(
            f"[errore] impossibile aprire la telecamera {cfg['cam']} "
            f"(backend {cfg['backend']}). Prova 'python led_test.py list'."
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])

    if cfg["manual_exposure"]:
        # Esposizione e bilanciamento del bianco devono essere fissi: se la
        # camera li regola da sola, lo stesso LED misura colori diversi.
        if platform.system() == "Windows":
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # 0.25 = manuale (DirectShow)
        else:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)      # 1 = manuale (V4L2), 3 = auto
        cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        if cfg["exposure"] is not None:
            cap.set(cv2.CAP_PROP_EXPOSURE, float(cfg["exposure"]))

    # scarta i primi frame: molte webcam partono con esposizione sbagliata
    for _ in range(8):
        cap.read()
    return cap


def grab(cap: cv2.VideoCapture) -> np.ndarray:
    ok, frame = cap.read()
    if not ok or frame is None:
        raise SystemExit("[errore] lettura frame fallita")
    return frame


def apply_wb(frame: np.ndarray, gains) -> np.ndarray:
    g = np.asarray(gains, dtype=np.float32).reshape(1, 1, 3)
    if np.allclose(g, 1.0):
        return frame
    return np.clip(frame.astype(np.float32) * g, 0, 255).astype(np.uint8)


def cmd_list(args) -> int:
    print("Scansione telecamere 0..7 ...")
    found = []
    for idx in range(8):
        cap = cv2.VideoCapture(idx, backend_flag(args.backend))
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
                print(f"  [{idx}] disponibile - {w}x{h}")
                found.append(idx)
        cap.release()
    if not found:
        print("  nessuna telecamera trovata")
        return 1
    return 0


# ---------------------------------------------------------------------------
# Misura del colore
# ---------------------------------------------------------------------------

COLOR_NAMES = {
    "R": "rosso", "G": "verde", "B": "blu",
    "Y": "giallo", "C": "ciano", "M": "magenta",
    "O": "arancio", "W": "bianco", "OFF": "spento",
}

# codice -> intervallo di tinta (hue) in scala OpenCV 0..179
HUE_RANGES = [
    ("R", 170, 180), ("R", 0, 10),
    ("O", 10, 20),
    ("Y", 20, 35),
    ("G", 35, 85),
    ("C", 85, 100),
    ("B", 100, 130),
    ("M", 130, 170),
]


@dataclass
class LedResult:
    index: int
    code: str
    name: str
    hue: float
    sat: float
    val: float
    bgr: list
    clipped: float
    zone: list = field(default_factory=list)   # [x, y, w, h] nel frame
    expected: str | None = None
    ok: bool = True
    notes: list = field(default_factory=list)


def classify(hue: float, sat: float, val: float, cfg: dict) -> str:
    if val < cfg["off_value"]:
        return "OFF"
    if sat < cfg["min_sat"]:
        return "W"
    for code, lo, hi in HUE_RANGES:
        if lo <= hue < hi:
            return code
    return "?"


def measure_patch(patch: np.ndarray, cfg: dict):
    """Colore rappresentativo di una zona.

    Usa solo i pixel piu luminosi (l'area che emette, non lo sfondo) e scarta
    i pixel saturi, che appaiono bianchi e falserebbero la tinta.
    """
    if patch.size == 0:
        return 0.0, 0.0, 0.0, [0.0, 0.0, 0.0], 0.0

    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]

    thr = max(int(np.percentile(v, cfg["bright_pct"])), 1)
    mask = v >= thr
    if mask.sum() < 8:
        mask = np.ones_like(v, dtype=bool)

    px = patch[mask].astype(np.float32)                 # BGR dei pixel emittenti
    clipped = float(np.mean(px.max(axis=1) >= 250.0))   # quota di pixel bruciati

    usable = px[px.max(axis=1) < 250.0]
    mean_bgr = (usable if len(usable) >= 8 else px).mean(axis=0)

    one = np.uint8([[[int(round(c)) for c in mean_bgr]]])
    h, s, _ = cv2.cvtColor(one, cv2.COLOR_BGR2HSV)[0, 0]
    val = float(v[mask].mean())
    return float(h), float(s), val, [round(float(c), 1) for c in mean_bgr], clipped


# ---------------------------------------------------------------------------
# Zone di lettura
# ---------------------------------------------------------------------------

def zones_from_roi(roi_box, n_leds: int, frame_shape) -> list:
    """Divide una ROI in n zone uguali lungo il lato piu lungo."""
    if roi_box:
        x, y, w, h = (int(v) for v in roi_box)
    else:
        h, w = frame_shape[:2]
        x = y = 0
    zones = []
    if w >= h:                                  # barra orizzontale
        step = w / n_leds
        pad = step * 0.20                       # margine: evita il bordo fra LED
        for i in range(n_leds):
            zones.append([int(x + i * step + pad), y,
                          max(1, int(step - 2 * pad)), h])
    else:                                       # barra verticale
        step = h / n_leds
        pad = step * 0.20
        for i in range(n_leds):
            zones.append([x, int(y + i * step + pad),
                          w, max(1, int(step - 2 * pad))])
    return zones


def detect_zones(frame: np.ndarray, cfg: dict) -> list:
    """Rileva automaticamente i LED accesi come blob luminosi (--leds 0)."""
    roi_box = cfg.get("roi")
    if roi_box:
        ox, oy, w, h = (int(v) for v in roi_box)
        roi = frame[oy:oy + h, ox:ox + w]
    else:
        ox = oy = 0
        roi = frame
    if roi.size == 0:
        return []

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    min_area = max(20.0, 0.0005 * roi.shape[0] * roi.shape[1])
    boxes = []
    for c in cnts:
        if cv2.contourArea(c) < min_area:
            continue
        x, y, w, h = cv2.boundingRect(c)
        boxes.append([ox + x, oy + y, w, h])
    horizontal = roi.shape[1] >= roi.shape[0]
    boxes.sort(key=lambda b: b[0] if horizontal else b[1])
    return boxes


def measure_zones(frame: np.ndarray, zones, cfg: dict) -> list:
    """Misura il colore di ogni zona; le zone sono in coordinate del frame."""
    fh, fw = frame.shape[:2]
    results = []
    for i, zone in enumerate(zones):
        zx, zy, zw, zh = (int(v) for v in zone)
        x0, y0 = max(0, zx), max(0, zy)
        x1, y1 = min(fw, zx + zw), min(fh, zy + zh)
        patch = frame[y0:y1, x0:x1]
        hue, sat, val, bgr, clipped = measure_patch(patch, cfg)
        code = classify(hue, sat, val, cfg) if patch.size else "OFF"
        res = LedResult(
            index=i + 1, code=code, name=COLOR_NAMES.get(code, code),
            hue=round(hue, 1), sat=round(sat, 1), val=round(val, 1),
            bgr=bgr, clipped=round(clipped, 3), zone=[zx, zy, zw, zh],
        )
        if patch.size == 0:
            res.notes.append("zona fuori dall'immagine")
        elif clipped > cfg["clip_warn"]:
            res.notes.append("sovraesposto: riduci l'esposizione o allontana la telecamera")
        results.append(res)
    return results


def evaluate(results, expected, cfg: dict) -> str:
    """Assegna esito e note a ogni LED e restituisce il verdetto complessivo."""
    expected = list(expected) if expected else []
    expected = (expected + [None] * len(results))[:len(results)]

    vals = [r.val for r in results]
    median_val = float(np.median(vals)) if vals else 0.0
    for r, exp in zip(results, expected):
        r.expected = exp
        if r.code == "OFF":
            r.ok = False
            r.notes.append("LED spento")
            continue
        if median_val > 0 and r.val < cfg["dim_ratio"] * median_val:
            r.ok = False
            r.notes.append(f"luminosita anomala ({r.val:.0f} vs mediana {median_val:.0f})")
        if exp and r.code != exp:
            r.ok = False
            r.notes.append(f"atteso {COLOR_NAMES.get(exp, exp)}, letto {r.name}")
    return "PASS" if results and all(r.ok for r in results) else "FAIL"


def analyze(frame: np.ndarray, cfg: dict, n_leds: int, expected, zones=None):
    """Analizza il frame.

    Le zone di lettura vengono prese, in ordine di priorita, da:
    l'argomento `zones`, `cfg["zones"]`, la divisione della ROI in `n_leds`
    parti, il rilevamento automatico dei blob luminosi.

    Restituisce (risultati, verdetto, zone, problemi_generali).
    """
    issues = []
    if zones is None:
        zones = list(cfg.get("zones") or [])
    zones = list(zones)

    if zones:
        if n_leds > 0 and len(zones) != n_leds:
            issues.append(f"definite {len(zones)} zone di lettura ma i LED "
                          f"attesi sono {n_leds}")
    elif n_leds > 0:
        zones = zones_from_roi(cfg.get("roi"), n_leds, frame.shape)
    else:
        zones = detect_zones(frame, cfg)
        if not zones:
            issues.append("nessun LED acceso rilevato")

    results = measure_zones(frame, zones, cfg)
    verdict = evaluate(results, expected, cfg)
    if issues:
        verdict = "FAIL"
    return results, verdict, zones, issues


def parse_expected(args, n: int):
    if args.expect_all:
        return [args.expect_all.strip().upper()] * n
    if args.expect:
        seq = [c.strip().upper() for c in args.expect.split(",") if c.strip()]
        if len(seq) == n:
            return seq
        if seq and n % len(seq) == 0:            # pattern ripetuto (R,G,B,R,G,B...)
            return seq * (n // len(seq))
        print(f"[warn] --expect ha {len(seq)} valori ma i LED sono {n}: "
              f"verifico solo l'accensione")
        return [None] * n
    return [None] * n


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

DRAW_BGR = {
    "R": (0, 0, 255), "G": (0, 255, 0), "B": (255, 0, 0),
    "Y": (0, 255, 255), "C": (255, 255, 0), "M": (255, 0, 255),
    "W": (255, 255, 255), "OFF": (90, 90, 90), "O": (0, 140, 255),
}


def draw_overlay(frame, results, verdict, roi_box=None, issues=None, highlight=None):
    """Disegna le zone con il colore letto, il numero e il verdetto."""
    out = frame.copy()
    if roi_box:
        rx, ry, rw, rh = (int(v) for v in roi_box)
        cv2.rectangle(out, (rx, ry), (rx + rw, ry + rh), (120, 120, 120), 1)

    for r in results:
        if not r.zone:
            continue
        x, y, w, h = (int(v) for v in r.zone)
        color = DRAW_BGR.get(r.code, (255, 255, 255))
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2 if r.ok else 3)
        label = f"{r.index}:{r.code}" if r.ok else f"{r.index}:{r.code}!"
        cv2.putText(out, label, (x, max(14, y - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
        if highlight is not None and r.index == highlight + 1:
            cv2.rectangle(out, (x - 3, y - 3), (x + w + 3, y + h + 3), (0, 255, 255), 2)

    vcolor = (0, 200, 0) if verdict == "PASS" else (0, 0, 255)
    cv2.putText(out, verdict, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.1, vcolor, 2, cv2.LINE_AA)
    for i, text in enumerate(issues or []):
        cv2.putText(out, text, (12, 60 + i * 22), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (0, 0, 255), 1, cv2.LINE_AA)
    return out


def print_report(results, verdict, issues=None) -> None:
    print(f"{'LED':>4} {'letto':>8} {'atteso':>8} {'H':>6} {'S':>6} {'V':>6}  esito")
    for r in results:
        exp = COLOR_NAMES.get(r.expected, r.expected) if r.expected else "-"
        flag = "OK" if r.ok else "KO"
        print(f"{r.index:>4} {r.name:>8} {exp:>8} {r.hue:>6.1f} {r.sat:>6.1f} "
              f"{r.val:>6.1f}  {flag}")
        for note in r.notes:
            print(f"       -> {note}")
    for text in issues or []:
        print(f"  [!] {text}")
    print(f"\nVERDETTO: {verdict}  ({len(results)} LED analizzati)")


# ---------------------------------------------------------------------------
# Comandi
# ---------------------------------------------------------------------------

def cmd_roi(args) -> int:
    cfg = load_config()
    if args.cam is not None:
        cfg["cam"] = args.cam
    if args.exposure is not None:
        cfg["exposure"] = args.exposure
    cap = open_camera(cfg)
    print("Inquadra la barra LED accesa, trascina il rettangolo sui soli LED,")
    print("poi premi INVIO o SPAZIO per confermare (c per annullare).")
    try:
        frame = apply_wb(grab(cap), cfg["wb_gains"])
        rect = cv2.selectROI("Seleziona la barra LED", frame, showCrosshair=False)
    finally:
        cap.release()
        cv2.destroyAllWindows()
    if rect[2] == 0 or rect[3] == 0:
        print("[annullato] nessuna ROI selezionata")
        return 1
    cfg["roi"] = [int(v) for v in rect]
    save_config(cfg)
    print(f"ROI = {cfg['roi']}")
    if cfg.get("zones"):
        print("[nota] in config.json ci sono zone di lettura manuali: hanno la "
              "precedenza sulla ROI. Cancellale dall'interfaccia grafica per "
              "tornare alla divisione automatica.")
    return 0


def cmd_wb(args) -> int:
    """Calibra i guadagni sul riferimento (LED bianco, o R+G+B tutti accesi)."""
    cfg = load_config()
    if args.cam is not None:
        cfg["cam"] = args.cam
    if args.exposure is not None:
        cfg["exposure"] = args.exposure
    cfg["wb_gains"] = [1.0, 1.0, 1.0]
    cap = open_camera(cfg)
    try:
        frame = grab(cap)
    finally:
        cap.release()

    roi_box = cfg.get("roi")
    roi = (frame[roi_box[1]:roi_box[1] + roi_box[3], roi_box[0]:roi_box[0] + roi_box[2]]
           if roi_box else frame)

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    mask = gray >= max(int(np.percentile(gray, cfg["bright_pct"])), 1)
    px = roi[mask].astype(np.float32)
    px = px[px.max(axis=1) < 250.0]
    if len(px) < 20:
        print("[errore] area troppo piccola o sovraesposta: riduci l'esposizione e riprova")
        return 1
    mean = px.mean(axis=0)
    gains = mean.mean() / np.maximum(mean, 1e-3)
    cfg["wb_gains"] = [round(float(g), 4) for g in gains]
    save_config(cfg)
    print(f"medie BGR misurate = {mean.round(1).tolist()} -> guadagni {cfg['wb_gains']}")
    return 0


def cmd_live(args) -> int:
    cfg = load_config()
    if args.cam is not None:
        cfg["cam"] = args.cam
    if args.exposure is not None:
        cfg["exposure"] = args.exposure
    cap = open_camera(cfg)
    n_expected = args.leds if args.leds > 0 else len(cfg.get("zones") or []) or 1
    expected = parse_expected(args, n_expected)
    print("Live: 'q' o ESC esce, 's' salva il frame e stampa il report, 'r' ri-seleziona la ROI.")
    try:
        while True:
            frame = apply_wb(grab(cap), cfg["wb_gains"])
            results, verdict, zones, issues = analyze(frame, cfg, args.leds, expected)
            cv2.imshow("LED test", draw_overlay(frame, results, verdict,
                                                cfg.get("roi"), issues))
            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("s"):
                name = time.strftime("led_%Y%m%d_%H%M%S.png")
                cv2.imwrite(os.path.join(HERE, name), frame)
                print(f"[ok] salvato {name}")
                print_report(results, verdict, issues)
            if key == ord("r"):
                rect = cv2.selectROI("LED test", frame, showCrosshair=False)
                if rect[2] and rect[3]:
                    cfg["roi"] = [int(v) for v in rect]
                    save_config(cfg)
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return 0


def cmd_test(args) -> int:
    cfg = load_config()
    if args.cam is not None:
        cfg["cam"] = args.cam
    if args.exposure is not None:
        cfg["exposure"] = args.exposure
    cap = open_camera(cfg)
    try:
        frames = [apply_wb(grab(cap), cfg["wb_gains"]) for _ in range(args.frames)]
    finally:
        cap.release()
    # mediana su piu frame: taglia il rumore del sensore e lo sfarfallio PWM
    frame = np.median(np.stack(frames).astype(np.float32), axis=0).astype(np.uint8)

    n_expected = args.leds if args.leds > 0 else len(cfg.get("zones") or []) or 1
    expected = parse_expected(args, n_expected)
    results, verdict, zones, issues = analyze(frame, cfg, args.leds, expected)
    print_report(results, verdict, issues)

    if args.save:
        cv2.imwrite(args.save + ".png",
                    draw_overlay(frame, results, verdict, cfg.get("roi"), issues))
        print(f"[ok] immagine annotata: {args.save}.png")
    if args.json:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "verdict": verdict,
            "camera": cfg["cam"],
            "roi": cfg.get("roi"),
            "zones": zones,
            "issues": issues,
            "leds": [asdict(r) for r in results],
        }
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"[ok] report JSON: {args.json}")
    return 0 if verdict == "PASS" else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Test colore e accensione di una barra LED via webcam USB")
    sub = p.add_subparsers(dest="cmd", metavar="comando")

    def common(sp):
        sp.add_argument("--cam", type=int, default=None, help="indice telecamera")
        sp.add_argument("--exposure", type=float, default=None,
                        help="esposizione manuale (es. -6 su Windows, 100 su V4L2)")

    sp = sub.add_parser("list", help="elenca le telecamere")
    sp.add_argument("--backend", default="auto",
                    choices=["auto", "dshow", "msmf", "v4l2", "any"])
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("roi", help="seleziona l'area della barra LED")
    common(sp)
    sp.set_defaults(func=cmd_roi)

    sp = sub.add_parser("wb", help="calibra il bilanciamento del bianco")
    common(sp)
    sp.set_defaults(func=cmd_wb)

    for name, fn, helptext in (("live", cmd_live, "anteprima live"),
                               ("test", cmd_test, "test singolo con exit code")):
        sp = sub.add_parser(name, help=helptext)
        common(sp)
        sp.add_argument("--leds", type=int, default=0,
                        help="numero di LED attesi (0 = usa le zone salvate, "
                             "altrimenti rilevamento automatico)")
        sp.add_argument("--expect", default=None,
                        help="colori attesi in sequenza, es. R,G,B (ripetuta se serve)")
        sp.add_argument("--expect-all", default=None,
                        help="colore atteso per tutti i LED, es. G")
        if name == "test":
            sp.add_argument("--frames", type=int, default=5,
                            help="frame da mediare per ridurre il rumore")
            sp.add_argument("--save", default=None, help="prefisso immagine annotata")
            sp.add_argument("--json", default=None, help="file report JSON")
        sp.set_defaults(func=fn)

    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        # lanciato senza argomenti (tipico con F5 in VS Code): mostra l'aiuto.
        # Esce con 0 per non far fermare il debugger su SystemExit.
        p.print_help()
        print()
        print("Esempio per iniziare:  python led_test.py list")
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""Human-readable interpretation of a stitch report (`NAME_report.json`).

Shared by the GUI's results browser and the "export report" option. Works with reports
written by older versions (fields that are missing are simply left out).
"""

import html
import json
import os
from datetime import datetime

from .engine import output_paths
from .options import Options, option_fields


def load(path):
    with open(path) as fh:
        r = json.load(fh)
    r.setdefault("section", os.path.basename(path).removesuffix("_report.json"))
    r["_path"] = path
    r["_outputs"] = output_paths(os.path.dirname(path), r["section"])
    return r


def find_reports(output_dir):
    """All reports in an output folder, newest first."""
    if not output_dir or not os.path.isdir(output_dir):
        return []
    found = []
    for f in os.listdir(output_dir):
        if f.endswith("_report.json"):
            try:
                found.append(load(os.path.join(output_dir, f)))
            except (OSError, ValueError):
                continue
    found.sort(key=lambda r: r["section"].lower())
    return found


def photo_width(r):
    return (r.get("photo_size_px") or [1024])[0]


def relative_error(r):
    """Typical error per 1000 px of photo width: comparable across cameras."""
    return r["median_error_px"] / photo_width(r) * 1000


def verdict(r):
    """(short label, sentence, level) where level is good/ok/warn/bad."""
    e = relative_error(r)
    placed, total = len(r["images"]), r.get("n_photos", len(r["images"]) + len(r.get("unplaced", [])))
    if e < 0.7:
        label, level = "Excellent alignment", "good"
        text = "Photos line up to within a fraction of a pixel; seams should be invisible."
    elif e < 1.2:
        label, level = "Good alignment", "good"
        text = "Photos line up to around a pixel; seams are unlikely to be noticeable."
    elif e < 2.5:
        label, level = "Fair alignment", "warn"
        text = "Small misalignments may be visible at some seams. Check the worst overlaps."
    else:
        label, level = "Poor alignment", "bad"
        text = "Photos disagree by several pixels. Check the layout for misplaced photos."
    if placed < total:
        text += f" {total - placed} of {total} photos could not be placed."
        if level == "good":
            level = "ok"
    return label, text, level


def fmt_int(n):
    return f"{n:,}"


def fmt_duration(s):
    if s is None:
        return None
    s = int(round(s))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def px_to_um(r, px):
    um = r.get("um_per_px")
    return None if not um else px * um


def size_text(r):
    w, h = r["width_px"], r["height_px"]
    text = f"{fmt_int(w)} × {fmt_int(h)} px"
    um = r.get("um_per_px")
    if um:
        text += f" ({w * um / 1000:.1f} × {h * um / 1000:.1f} mm)"
    return text


def error_text(r, px):
    um = px_to_um(r, px)
    return f"{px:.2f} px" + (f" ({um:.1f} µm)" if um else "")


def flagged_overlaps(r):
    """Overlaps that align noticeably worse than the rest of the mosaic."""
    med = r["median_error_px"]
    limit = max(3 * med, photo_width(r) / 1000 * 1.5)
    return [o for o in r.get("overlaps", []) if o["error_px"] > limit]


def gain_spread_pct(r):
    gains = [sum(i["gain_bgr"]) / 3 for i in r["images"] if "gain_bgr" in i]
    if not gains:
        return None
    return (max(gains) / min(gains) - 1) * 100


def facts(r):
    """[(label, value)] summary rows."""
    rows = []
    total = r.get("n_photos", len(r["images"]) + len(r.get("unplaced", [])))
    placed = f"{len(r['images'])} of {total}"
    if r.get("excluded"):
        placed += f" ({len(r['excluded'])} more left out for different camera settings)"
    rows.append(("Photos placed", placed))
    rows.append(("Mosaic size", size_text(r)))
    if r.get("um_per_px"):
        rows.append(("Resolution", f"{r['um_per_px']:.3f} µm per pixel (from microscope metadata)"))
    else:
        rows.append(("Resolution", "No calibration found: sizes are in pixels only"))
    rows.append(("Typical misalignment", error_text(r, r["median_error_px"])))
    ov = r.get("overlaps", [])
    if ov:
        worst = max(ov, key=lambda o: o["error_px"])
        rows.append(("Worst overlap", f"{error_text(r, worst['error_px'])} between "
                                      f"{worst['a']} and {worst['b']}"))
        rows.append(("Overlaps used", str(len(ov))))
    opts = r.get("options", {})
    if "lens_max_shift_px" in r:
        if opts.get("perspective", True):
            rows.append(("Camera and lens correction", f"up to {r['lens_max_shift_px']:.1f} px at the photo edges"))
        else:
            rows.append(("Camera and lens correction", "off"))
    if "illumination_range_pct" in r:
        if opts.get("gain", True) and opts.get("flat", True):
            rows.append(("Illumination correction",
                         f"lighting varied by {r['illumination_range_pct']:.0f}% across each photo (corrected)"))
        else:
            rows.append(("Illumination correction", "off"))
    spread = gain_spread_pct(r)
    if spread is not None and opts.get("gain", True):
        rows.append(("Exposure differences", f"up to {spread:.0f}% between photos (evened out)"))
    if opts:
        rows.append(("Blending", "smooth feathering" if opts.get("blend") == "feather" else "hard seams"))
    if r.get("created"):
        try:
            when = datetime.fromisoformat(r["created"]).strftime("%d %b %Y, %H:%M")
        except ValueError:
            when = r["created"]
        took = fmt_duration(r.get("elapsed_s"))
        rows.append(("Stitched", when + (f", took {took}" if took else "")))
    rows.append(("Source folder", r.get("folder", "")))
    return rows


def warnings(r):
    """[(title, detail)] things worth the user's attention."""
    out = []
    if r.get("unplaced"):
        out.append(("Photos left out",
                    f"{', '.join(r['unplaced'])} had no reliable overlap with the rest and were not "
                    "used. They are often stray shots, e.g. a different area or lighting. If one belongs "
                    "in the mosaic, retake it with more overlap (about 30%)."))
    if r.get("excluded"):
        reasons = {}
        for e in r["excluded"]:
            reasons.setdefault(e["reason"], []).append(e["file"])
        detail = " ".join(f"{', '.join(files)}: {reason}." for reason, files in reasons.items())
        out.append(("Photos with different camera settings",
                    detail + " They can't be combined with the rest without changing the scale, so "
                    "they were not used. Stitch them separately (in their own folder) if needed."))
    bad = flagged_overlaps(r)
    if bad:
        pairs = ", ".join(f"{o['a']}/{o['b']}" for o in bad[:5]) + (" …" if len(bad) > 5 else "")
        out.append(("Overlaps to check",
                    f"{len(bad)} overlap(s) align noticeably worse than the rest ({pairs}). Inspect "
                    "these areas in the full-resolution image."))
    if not r.get("full_resolution_written", True) or not os.path.exists(r["_outputs"]["tif"]):
        out.append(("No full-resolution image",
                    "Only the preview was written. Stitch again without 'Preview only' to get the TIFF."))
    if not r.get("um_per_px"):
        out.append(("No calibration", "No microscope calibration was found, so sizes are in pixels only."))
    return out


def changed_settings(r):
    """[(label, value)] for settings that differ from the defaults."""
    opts = r.get("options")
    if not opts:
        return []
    base = Options().to_dict()
    rows = []
    for f in option_fields():
        if f.name in ("output", "workers", "skip_existing") or f.name not in opts:
            continue
        if opts[f.name] != base.get(f.name):
            v = opts[f.name]
            if isinstance(v, bool):
                v = "on" if v else "off"
            elif isinstance(v, list):
                v = "; ".join(v) or "(none)"
            rows.append((f.metadata["label"], str(v)))
    return rows


def photo_rows(r):
    """Per-photo table rows: file, x mm/px, y mm/px, rotation, brightness %, overlaps, worst error."""
    um = r.get("um_per_px")
    count, worst = {}, {}
    for o in r.get("overlaps", []):
        for k in (o["a"], o["b"]):
            count[k] = count.get(k, 0) + 1
            worst[k] = max(worst.get(k, 0), o["error_px"])
    rows = []
    for i in r["images"]:
        x, y = i["centre_xy"]
        if um:
            x, y = x * um / 1000, y * um / 1000
        g = sum(i.get("gain_bgr", [1, 1, 1])) / 3
        rows.append({"file": i["file"], "x": x, "y": y, "rotation": i["rotation_deg"],
                     "brightness": (g - 1) * 100, "overlaps": count.get(i["file"], 0),
                     "worst": worst.get(i["file"], 0.0)})
    return rows


# ----------------------------------------------------------------------------- HTML

def _callout(c, level, body):
    """A box with a coloured bar on the left (built from table cells so Qt's rich text renders it)."""
    return (f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-bottom:10px"><tr>'
            f'<td width="4" bgcolor="{c[level]}"></td>'
            f'<td bgcolor="{c["card"]}" style="padding:9px 12px">{body}</td></tr></table>')


def to_html(r, colors=None):
    """A self-contained HTML summary (used in the GUI and for export)."""
    c = {"text": "#1d2327", "muted": "#6b7280", "line": "#e3e6e8", "good": "#1a7f4b",
         "ok": "#1f6f8b", "warn": "#a86100", "bad": "#b42318", "card": "#f6f8f9"}
    c.update(colors or {})
    e = html.escape
    label, text, level = verdict(r)
    parts = [f"""<div style="color:{c['text']}">
<h2 style="margin:0 0 2px 0">{e(r['section'])}</h2>
<p style="margin:0 0 12px 0; color:{c['muted']}">{e(size_text(r))}</p>
{_callout(c, level, f'<b style="color:{c[level]}; font-size:15px">{e(label)}</b><br>{e(text)}')}"""]
    for title, detail in warnings(r):
        parts.append(_callout(c, "warn", f'<b>{e(title)}</b><br><span style="color:{c["muted"]}">{e(detail)}</span>'))
    parts.append('<h3 style="margin:16px 0 6px 0">Summary</h3><table cellpadding="5" cellspacing="0" width="100%">')
    for k, v in facts(r):
        parts.append(f'<tr><td style="color:{c["muted"]}; border-bottom:1px solid {c["line"]}; white-space:nowrap" width="34%">{e(k)}</td>'
                     f'<td style="border-bottom:1px solid {c["line"]}">{e(v)}</td></tr>')
    parts.append("</table>")
    changed = changed_settings(r)
    parts.append('<h3 style="margin:16px 0 6px 0">Settings</h3>')
    if changed:
        parts.append(f'<p style="margin:0 0 4px 0; color:{c["muted"]}">Changed from the defaults:</p>'
                     '<table cellpadding="5" cellspacing="0" width="100%">')
        for k, v in changed:
            parts.append(f'<tr><td style="color:{c["muted"]}; border-bottom:1px solid {c["line"]}" width="34%">{e(k)}</td>'
                         f'<td style="border-bottom:1px solid {c["line"]}">{e(v)}</td></tr>')
        parts.append("</table>")
    elif r.get("options"):
        parts.append(f'<p style="color:{c["muted"]}">All settings were at their defaults.</p>')
    else:
        parts.append(f'<p style="color:{c["muted"]}">Not recorded (made with an older version).</p>')
    parts.append(f'<p style="color:{c["muted"]}; margin-top:16px">{e(r.get("software", ""))}</p></div>')
    return "\n".join(parts)


def export_html(r, path):
    """Standalone HTML page with the summary, per-photo and per-overlap tables."""
    e = html.escape
    um = bool(r.get("um_per_px"))
    unit = "mm" if um else "px"
    photos = "".join(
        f"<tr><td>{e(p['file'])}</td><td>{p['x']:.2f}</td><td>{p['y']:.2f}</td><td>{p['rotation']:.2f}</td>"
        f"<td>{p['brightness']:+.1f}%</td><td>{p['overlaps']}</td><td>{p['worst']:.2f}</td></tr>"
        for p in photo_rows(r))
    flagged = {(o["a"], o["b"]) for o in flagged_overlaps(r)}
    overlaps = "".join(
        f"<tr{' class=flag' if (o['a'], o['b']) in flagged else ''}><td>{e(o['a'])}</td><td>{e(o['b'])}</td>"
        f"<td>{o['matches']}</td><td>{o['error_px']:.2f}</td>"
        f"<td>{'' if not um else f'{o['error_px'] * r['um_per_px']:.1f}'}</td></tr>"
        for o in sorted(r.get("overlaps", []), key=lambda o: -o["error_px"]))
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(r['section'])} stitch report</title>
<style>body{{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#1d2327}}
table.data{{border-collapse:collapse;width:100%;margin-bottom:24px}}table.data td,table.data th{{border-bottom:1px solid #e3e6e8;padding:4px 8px;text-align:left}}
table.data th{{color:#6b7280;font-weight:600}}tr.flag td{{background:#fff4e5}}</style></head><body>
{to_html(r)}
<h3>Photos</h3><table class="data"><tr><th>Photo</th><th>Centre x ({unit})</th><th>Centre y ({unit})</th><th>Rotation (°)</th>
<th>Brightness correction</th><th>Overlaps</th><th>Worst error (px)</th></tr>{photos}</table>
<h3>Overlaps</h3><table class="data"><tr><th>Photo A</th><th>Photo B</th><th>Matches</th><th>Error (px)</th><th>Error (µm)</th></tr>{overlaps}</table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)

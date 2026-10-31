
"""
EPUB Colorizer - single-file Flet app for Android (built with "flet build apk").

Pick any novel EPUB, choose colors for system elements (fieldset boxes,
bracketed system text, prophet quotes, novel titles) and for any number of
character names, then tap "Color it!". The app rewrites the EPUB's internal
HTML so those elements appear in the chosen colors and saves a new .epub
next to the original. Works for ANY novel - nothing is hardcoded.

Dependencies: Python standard library + flet. Nothing else.
"""

import io
import json
import os
import re
import shutil
import tempfile
import threading
import traceback
import zipfile
from datetime import datetime

import flet as ft

APP_NAME = "EPUB Colorizer"
STORAGE_KEY = "epub_colorizer.charlist.v1"
MIMETYPE = "application/epub+zip"
HTML_EXTS = (".xhtml", ".html", ".htm")
HEARTBEAT_EVERY = 25
HEX_RE = re.compile(r"#?([0-9a-fA-F]{6})")

PALETTE = [
    "#FFFFFF", "#C0C0C0", "#808080", "#404040", "#000000",
    "#FF4444", "#E53935", "#B71C1C", "#FF6666", "#FF8A80",
    "#FF9800", "#FFB74D", "#FFC107", "#FFD700", "#FFD54F",
    "#FFEB3B", "#FDD835", "#FFF176", "#C6FF00", "#AEEA00",
    "#66BB6A", "#4CAF50", "#2E7D32", "#00E676", "#44CC44",
    "#00E5FF", "#00BCD4", "#00ACC1", "#26C6DA", "#80DEEA",
    "#42A5F5", "#1E88E5", "#0D47A1", "#5C6BC0", "#3949AB",
    "#7E57C2", "#9575CD", "#B57EDC", "#673AB7", "#5E35B1",
    "#EC407A", "#E91E63", "#D81B60", "#AD1457", "#F48FB1",
    "#8D6E63", "#6D4C41", "#795548", "#A1887F", "#5D4037",
]

# Defaults per spec: fieldset ON, system text ON, quotes OFF, titles OFF.
SYSTEM_ELEMENTS = [
    {"key": "fieldset", "label": "Fieldset boxes", "on": True, "color": "#00E5FF"},
    {"key": "sys", "label": "System text [ ... ]", "on": True, "color": "#00E5FF"},
    {"key": "quote", "label": "Prophet quotes 「 ... 」", "on": False, "color": "#B57EDC"},
    {"key": "title", "label": "Novel titles 『 ... 』", "on": False, "color": "#FFD700"},
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _icon(name):
    """Icon constant across flet versions (ft.Icons / ft.icons)."""
    ns = getattr(ft, "Icons", None)
    if ns is None:
        ns = getattr(ft, "icons", None)
    return getattr(ns, name)


def fieldset_style(color):
    """The exact style string required for colored fieldset boxes."""
    return (
        "border: 2px solid " + color + "; "
        "background-color: rgba(0,229,255,0.06); "
        "padding: 12px 16px; border-radius: 8px; margin: 16px 0;"
    )


# ---------------------------------------------------------------------------
# HTML transformation (strict order: fieldset, brackets, quotes, titles, names)
# ---------------------------------------------------------------------------

def transform_html(text, opts):
    """Apply all enabled colorings to one HTML document, in strict order."""
    # 1. Fieldset boxes that have not been styled yet.
    if opts["fieldset_on"]:
        color = opts["fieldset_color"]
        style = fieldset_style(color)

        def _fieldset_repl(m):
            tag = m.group(0)
            probe = tag.lower()
            if "style=" in probe or "data-colored" in probe:
                return tag
            if probe.endswith("/>"):
                head = tag[:-2].rstrip()
                return head + ' data-colored="1" style="' + style + '"/>'
            head = tag[:-1].rstrip()
            return head + ' data-colored="1" style="' + style + '">'

        text = re.sub(r"<fieldset\b[^>]*>", _fieldset_repl, text, flags=re.IGNORECASE)

    # 2. Bracketed system text: [TEXT] (TEXT contains no [ or ]).
    if opts["sys_on"]:
        color = opts["sys_color"]

        def _bracket_repl(m):
            return '<span style="color: ' + color + ';">[' + m.group(1) + "]</span>"

        text = re.sub(r"\[([^\[\]]+)\]", _bracket_repl, text)

    # 3. Prophet quotes: 「TEXT」.
    if opts["quote_on"]:
        color = opts["quote_color"]

        def _quote_repl(m):
            return '<span style="color: ' + color + ';">「' + m.group(1) + "」</span>"

        text = re.sub(r"「([^「」]+)」", _quote_repl, text)

    # 4. Novel titles: 『TEXT』.
    if opts["title_on"]:
        color = opts["title_color"]

        def _title_repl(m):
            return '<span style="color: ' + color + ';">『' + m.group(1) + "』</span>"

        text = re.sub(r"『([^『』]+)』", _title_repl, text)

    # 5. Character names: ONE compiled regex, names sorted by length
    #    DESCENDING, each escaped, single re.sub pass with a repl function.
    names = opts["names"]
    if names:
        by_name = {n["name"]: n["color"] for n in names}
        ordered = sorted(by_name.keys(), key=len, reverse=True)
        pattern = "|".join(re.escape(n) for n in ordered)
        rex = re.compile(pattern)

        def _name_repl(m):
            word = m.group(0)
            color = by_name.get(word)
            if not color:
                return word
            return '<span style="color: ' + color + ';">' + word + "</span>"

        text = rex.sub(_name_repl, text)

    return text


# ---------------------------------------------------------------------------
# EPUB pipeline
# ---------------------------------------------------------------------------

def deliver_file(src, dest):
    """Copy src to dest.

    Written to a temp file and renamed where the folder allows it. Android's
    save dialog hands back paths such as "/document/995" where only that exact
    document is writable (its folder is not a real directory), so a failed
    temp file falls back to writing straight to dest.
    """
    tmp = dest + ".epubcolorizer.tmp"
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            while True:
                chunk = fsrc.read(1024 * 1024)
                if not chunk:
                    break
                fdst.write(chunk)
        os.replace(tmp, dest)
        return
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
        while True:
            chunk = fsrc.read(1024 * 1024)
            if not chunk:
                break
            fdst.write(chunk)


def is_private_copy(path):
    """True when the path is an app-private temporary copy (Android/iOS picker)."""
    p = (path or "").lower().replace(os.sep, "/")
    return "/cache/" in p or "/caches/" in p or "/data/data/" in p or "/data/user/" in p


def fallback_save_dirs(input_path):
    """Folders the app can always write to, tried if the chosen one fails."""
    dirs = []
    for d in (os.path.dirname(input_path), tempfile.gettempdir(), os.path.expanduser("~")):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    return dirs


def process_epub(input_path, out_path, opts, log, fallback):
    """Extract -> transform one HTML file at a time -> repack -> deliver.

    Returns a dict with the final path and counters, or None if the user
    cancelled / the save failed. All save problems are logged as warnings
    (never crashes) with the exception type and message.
    """
    with tempfile.TemporaryDirectory(prefix="epubcolor_") as workdir:
        log("Extracting EPUB...")
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(workdir)

        # One file at a time: read, transform, write, free.
        total = 0
        changed = 0
        failed = 0
        log("Processing HTML files one at a time...")
        for root, _dirs, files in os.walk(workdir):
            for fname in sorted(files):
                if not fname.lower().endswith(HTML_EXTS):
                    continue
                total += 1
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8-sig") as f:
                        original = f.read()
                    recolored = transform_html(original, opts)
                    if recolored != original:
                        changed += 1
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(recolored)
                    del original, recolored
                except Exception as ex:
                    failed += 1
                    log("Failed on " + fname + ": " + type(ex).__name__ + ": " + str(ex), error=True)
                if total % HEARTBEAT_EVERY == 0:
                    log("heartbeat: " + str(total) + " files processed so far")
        log("Transform done: " + str(total) + " HTML files, " + str(changed) + " recolored, " + str(failed) + " failed.")

        # Repack: "mimetype" FIRST and ZIP_STORED, everything else deflated.
        produced = os.path.join(workdir, "__colored_output__.epub")
        log("Repacking EPUB (mimetype first, stored)...")
        with zipfile.ZipFile(produced, "w") as zout:
            mime_path = os.path.join(workdir, "mimetype")
            if os.path.isfile(mime_path):
                with open(mime_path, "rb") as f:
                    zout.writestr("mimetype", f.read(), compress_type=zipfile.ZIP_STORED)
            else:
                zout.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
            for root, _dirs, files in os.walk(workdir):
                for fname in sorted(files):
                    rel = os.path.relpath(os.path.join(root, fname), workdir).replace(os.sep, "/")
                    if rel == "mimetype" or rel == "__colored_output__.epub":
                        continue
                    zout.write(os.path.join(root, fname), rel, compress_type=zipfile.ZIP_DEFLATED)

        private_copy = is_private_copy(input_path)
        if not private_copy:
            log("Saving next to the input file: " + out_path + " ...")
            try:
                deliver_file(produced, out_path)
                log("Saved: " + out_path)
                return {"path": out_path, "total": total, "changed": changed, "failed": failed}
            except (PermissionError, OSError) as ex:
                # Fall back to a save dialog - logged as a WARNING, not an error.
                log("WARNING: cannot save to " + out_path + ": " + type(ex).__name__ + ": " + str(ex), warn=True)
        else:
            log("WARNING: the system handed the app a temporary copy of your book, so it cannot write beside the original.", warn=True)
            log("Choose the folder your book lives in - the output is saved there.", warn=True)

        log("Opening a save dialog - the output goes in the folder you pick.", warn=True)
        chosen = None
        try:
            chosen = fallback(os.path.basename(out_path), None if private_copy else os.path.dirname(out_path))
        except Exception as ex2:
            log("Save dialog failed: " + type(ex2).__name__ + ": " + str(ex2), warn=True)
        if not chosen:
            log("Save cancelled")
            return None
        if os.path.abspath(chosen) == os.path.abspath(input_path):
            log("ERROR: refusing to overwrite the input file - pick another name.", error=True)
            return None
        try:
            deliver_file(produced, chosen)
            log("Saved: " + chosen)
            return {"path": chosen, "total": total, "changed": changed, "failed": failed}
        except Exception as ex3:
            log("WARNING: cannot save to " + chosen + ": " + type(ex3).__name__ + ": " + str(ex3), warn=True)

        # The chosen path was not writable (common with Android's document
        # picker) - save somewhere the app always can, so nothing is lost.
        name = os.path.basename(chosen) or os.path.basename(out_path)
        if not name.lower().endswith(".epub"):
            name += ".epub"
        for folder in fallback_save_dirs(input_path):
            try:
                alt = os.path.join(folder, name)
                deliver_file(produced, alt)
                log("Saved to " + alt + " instead - pick another folder next time if you want it elsewhere.", warn=True)
                return {"path": alt, "total": total, "changed": changed, "failed": failed}
            except Exception as ex4:
                log("WARNING: cannot save to " + folder + ": " + type(ex4).__name__ + ": " + str(ex4), warn=True)
        log("ERROR: save failed: no writable folder was found.", error=True)
        return None


# ---------------------------------------------------------------------------
# Self-test: build a tiny in-memory EPUB, run the full pipeline, PASS/FAIL.
# ---------------------------------------------------------------------------

TEST_HTML = (
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>'
    "<fieldset>Quest Board</fieldset>"
    "<p>[System: Level Up]</p>"
    "<p>「The prophecy is near.」</p>"
    "<p>『Record of the Ancients』</p>"
    "<p>Alice met Bob at the gate.</p>"
    "</body></html>\n"
)


def build_test_opts():
    return {
        "fieldset_on": True, "fieldset_color": "#00E5FF",
        "sys_on": True, "sys_color": "#00E5FF",
        "quote_on": True, "quote_color": "#B57EDC",
        "title_on": True, "title_color": "#FFD700",
        "names": [
            {"name": "Alice", "color": "#FF4444"},
            {"name": "Bob", "color": "#44CC44"},
        ],
    }


def run_self_test(log):
    """Full pipeline on a tiny in-memory EPUB. Returns (ok, message)."""
    tmpdir = tempfile.mkdtemp(prefix="epubcolor_selftest_")
    try:
        src = os.path.join(tmpdir, "selftest_in.epub")
        dst = os.path.join(tmpdir, "selftest_in_colored.epub")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("mimetype", MIMETYPE, compress_type=zipfile.ZIP_STORED)
            zf.writestr("OEBPS/content.xhtml", TEST_HTML, compress_type=zipfile.ZIP_DEFLATED)
            zf.writestr("OEBPS/style.css", "body{}", compress_type=zipfile.ZIP_DEFLATED)
        with open(src, "wb") as f:
            f.write(buf.getvalue())
        del buf

        opts = build_test_opts()
        res = process_epub(src, dst, opts, log, fallback=lambda default_name, folder=None: None)
        if not res or res.get("path") != dst:
            return False, "pipeline did not save the output file"

        with zipfile.ZipFile(dst) as zf:
            names = zf.namelist()
            content = zf.read("OEBPS/content.xhtml").decode("utf-8")
            mime_stored = zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED

        checks = [
            ("mimetype first in zip", names[0] == "mimetype"),
            ("mimetype stored uncompressed", mime_stored),
            ("fieldset styled", 'data-colored="1"' in content and "border: 2px solid #00E5FF" in content),
            ("system text colored", '<span style="color: #00E5FF;">[System: Level Up]</span>' in content),
            ("quotes colored", '<span style="color: #B57EDC;">「The prophecy is near.」</span>' in content),
            ("titles colored", '<span style="color: #FFD700;">『Record of the Ancients』</span>' in content),
            ("Alice colored", '<span style="color: #FF4444;">Alice</span>' in content),
            ("Bob colored", '<span style="color: #44CC44;">Bob</span>' in content),
        ]
        failed_names = [name for name, ok in checks if not ok]
        if failed_names:
            return False, "failed: " + ", ".join(failed_names)
        return True, "all " + str(len(checks)) + " checks passed"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Flet UI
# ---------------------------------------------------------------------------

def main(page: ft.Page):
    """Entry point. The whole startup is wrapped so any exception is visible."""
    try:
        _start(page)
    except Exception as ex:
        try:
            page.controls.clear()
            page.add(
                ft.Text(
                    "STARTUP ERROR\n\n"
                    + type(ex).__name__ + ": " + str(ex)
                    + "\n\n" + traceback.format_exc(),
                    color="#FF5252",
                    selectable=True,
                )
            )
            page.update()
        except Exception:
            pass


def _start(page: ft.Page):
    page.title = APP_NAME
    page.padding = 16
    page.theme_mode = ft.ThemeMode.DARK
    page.horizontal_alignment = ft.CrossAxisAlignment.START

    state = {"input": None, "busy": False}
    chars = []
    sys_map = {elem["key"]: elem for elem in SYSTEM_ELEMENTS}

    # ---- status log (read-only, monospace) ----
    log_column = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO, expand=True)
    log_box = ft.Container(
        content=log_column,
        height=280,
        bgcolor="#111318",
        border_radius=10,
        padding=10,
        border=ft.border.all(1, "#2A2F3A"),
    )

    def safe_update():
        try:
            page.update()
        except Exception:
            pass

    def log(msg, error=False, warn=False):
        ts = datetime.now().strftime("%H:%M:%S")
        color = "#FF6B6B" if error else ("#FFD166" if warn else "#8FD18F")
        prefix = "ERROR " if error else ("WARN  " if warn else "")
        try:
            log_column.controls.append(
                ft.Text(
                    ts + "  " + prefix + msg,
                    size=12, color=color,
                    font_family="monospace", selectable=True,
                )
            )
            if len(log_column.controls) > 400:
                del log_column.controls[:200]
        except Exception:
            pass
        safe_update()

    # ---- storage permission ----
    perm_denied_text = ft.Text(
        "Storage permission was denied. Picking and saving may fail - grant storage permission in Android Settings > Apps > EPUB Colorizer.",
        color="#FFD166", size=13, visible=False,
    )

    def request_storage_permission():
        try:
            fn = getattr(page, "request_permissions", None)
            if not callable(fn):
                log("Permission API not available on this platform - continuing.", warn=True)
                return
            granted = fn()
            if granted:
                log("Storage permission: granted.")
            else:
                log("Storage permission: DENIED.", warn=True)
                perm_denied_text.visible = True
        except Exception as ex:
            log("Permission request failed: " + type(ex).__name__ + ": " + str(ex), warn=True)

    # ---- character list persistence (JSON via client_storage) ----
    def save_charlist_to_storage():
        try:
            page.client_storage.set(STORAGE_KEY, json.dumps(chars))
        except Exception as ex:
            log("Could not persist character list: " + type(ex).__name__ + ": " + str(ex), warn=True)

    def load_charlist_from_storage():
        try:
            raw = page.client_storage.get(STORAGE_KEY)
            if not raw:
                return None
            data = json.loads(raw)
            if not isinstance(data, list):
                return None
            out = []
            for item in data:
                if isinstance(item, dict):
                    out.append({
                        "name": str(item.get("name", "") or ""),
                        "color": str(item.get("color", "") or "#00E5FF"),
                    })
            return out
        except Exception as ex:
            log("Could not load saved character list: " + type(ex).__name__ + ": " + str(ex), warn=True)
            return None

    # ---- color palette dialog ----
    def open_color_dialog(current, apply_color):
        preview = ft.Container(
            width=44, height=44, bgcolor=current, border_radius=8,
            border=ft.border.all(1, "#555"),
        )
        hex_field = ft.TextField(label="#RRGGBB", value=current, width=170, dense=True)

        grid = ft.GridView(expand=True, max_extent=44, child_aspect_ratio=1.0, spacing=6, run_spacing=6)
        for col in PALETTE:
            grid.controls.append(
                ft.Container(
                    width=44, height=44, bgcolor=col, border_radius=8,
                    border=ft.border.all(1, "#3A3F4A"),
                )
            )
        # wire clicks after creation so close() is bound
        def pick_from_palette(e):
            apply_color(e.control.bgcolor)
            close()
        for sw in grid.controls:
            sw.on_click = pick_from_palette

        def apply_hex(e):
            val = (hex_field.value or "").strip()
            m = HEX_RE.fullmatch(val)
            if not m:
                hex_field.error_text = "Use #RRGGBB (6 hex digits)."
                safe_update()
                return
            digits = m.group(1)
            color = "#" + digits if val.startswith("#") else "#" + digits
            apply_color(color)
            close()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Choose a color", size=16),
            content=ft.Column(
                tight=True, spacing=12,
                controls=[
                    ft.Row([preview, hex_field], spacing=12),
                    ft.Container(height=300, content=grid, bgcolor="#0E1014", border_radius=8, padding=6),
                ],
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: close()),
                ft.TextButton("Use this hex", on_click=apply_hex),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def close():
            try:
                page.close(dlg)
            except Exception:
                dlg.open = False
                safe_update()

        try:
            page.open(dlg)
        except Exception:
            try:
                page.dialog = dlg
                dlg.open = True
                safe_update()
            except Exception:
                pass

    # ---- system elements section ----
    def build_system_section():
        rows = []
        for elem in SYSTEM_ELEMENTS:
            swatch = ft.Container(
                width=34, height=34, bgcolor=elem["color"], border_radius=8,
                border=ft.border.all(1, "#4A4F5A"), tooltip="Change color",
            )

            def make_applier(elem, swatch):
                def apply_color(c):
                    elem["color"] = c
                    swatch.bgcolor = c
                    safe_update()
                return apply_color

            applier = make_applier(elem, swatch)
            swatch.on_click = lambda e, el=elem, ap=applier: open_color_dialog(el["color"], ap)

            def make_toggle(elem):
                def on_toggle(e):
                    try:
                        elem["on"] = bool(e.control.value)
                    except Exception:
                        pass
                return on_toggle

            chk = ft.Checkbox(label=elem["label"], value=elem["on"], on_change=make_toggle(elem))
            rows.append(ft.Row(spacing=10, controls=[ft.Container(chk, expand=True), swatch]))
        return ft.Column(rows, spacing=4)

    # ---- character rows ----
    char_list_col = ft.Column(spacing=8)

    def make_char_row(ch):
        name_field = ft.TextField(
            hint_text="Character name", value=ch.get("name", ""),
            expand=True, dense=True,
        )

        def on_name(e, ch=ch):
            try:
                ch["name"] = e.control.value or ""
            except Exception:
                try:
                    ch["name"] = e.data or ""
                except Exception:
                    pass
        name_field.on_change = on_name

        swatch = ft.Container(
            width=34, height=34, bgcolor=ch.get("color", "#00E5FF"), border_radius=8,
            border=ft.border.all(1, "#4A4F5A"), tooltip="Change color",
        )

        def make_applier(ch, swatch):
            def apply_color(c):
                ch["color"] = c
                swatch.bgcolor = c
                safe_update()
            return apply_color

        applier = make_applier(ch, swatch)
        swatch.on_click = lambda e, cc=ch, ap=applier: open_color_dialog(cc["color"], ap)

        def delete(e, ch=ch):
            try:
                chars.remove(ch)
            except ValueError:
                pass
            rebuild_chars()
            safe_update()

        return ft.Row(
            spacing=8,
            controls=[
                name_field,
                swatch,
                ft.IconButton(icon=_icon("CLOSE"), tooltip="Remove", on_click=delete),
            ],
        )

    def rebuild_chars():
        char_list_col.controls = [make_char_row(ch) for ch in chars]

    def add_char(e=None):
        default = PALETTE[(len(chars) * 7) % len(PALETTE)]
        chars.append({"name": "", "color": default})
        rebuild_chars()
        safe_update()

    def save_list(e=None):
        try:
            payload = [c for c in chars if (c.get("name") or "").strip()]
            if not payload:
                log("Save list: nothing to save (no character names entered).", warn=True)
                return
            page.client_storage.set(STORAGE_KEY, json.dumps(payload))
            log("Character list saved (" + str(len(payload)) + " names).")
        except Exception as ex:
            log("Save list failed: " + type(ex).__name__ + ": " + str(ex), error=True)

    def load_list(e=None):
        try:
            data = load_charlist_from_storage()
            if not data:
                log("Load list: no saved list found.", warn=True)
                return
            chars[:] = [c for c in data if (c.get("name") or "").strip()]
            rebuild_chars()
            safe_update()
            log("Character list loaded (" + str(len(chars)) + " names).")
        except Exception as ex:
            log("Load list failed: " + type(ex).__name__ + ": " + str(ex), error=True)

    def clear_all(e=None):
        chars[:] = []
        rebuild_chars()
        safe_update()
        log("Character list cleared.")

    # ---- file pickers ----
    chosen_file_text = ft.Text("No EPUB chosen yet.", italic=True, color="#9AA3B2", size=13)
    out_name_field = ft.TextField(label="Output filename", value="", hint_text="mybook_colored.epub", dense=True)

    def on_pick_result(e):
        try:
            if e.files and len(e.files) > 0:
                f = e.files[0]
                path = getattr(f, "path", None)
                if not path:
                    log("ERROR: the file picker returned no path (web upload?). Use the app on a device.", error=True)
                    return
                if not path.lower().endswith(".epub"):
                    log("WARNING: chosen file is not a .epub - trying anyway.", warn=True)
                state["input"] = path
                base = os.path.splitext(os.path.basename(path))[0]
                chosen_file_text.value = "Chosen: " + os.path.basename(path)
                out_name_field.value = base + "_colored.epub"
                log("Input: " + path)
                if is_private_copy(path):
                    log("Note: the picker gave the app a temporary copy, so the output goes to the folder you choose when saving.", warn=True)
                else:
                    log("Output folder: " + os.path.dirname(path) + " (same folder as the input file)")
            else:
                log("File selection cancelled.")
            safe_update()
        except Exception as ex:
            log("ERROR in file picker: " + type(ex).__name__ + ": " + str(ex), error=True)

    file_picker = ft.FilePicker(on_result=on_pick_result)

    def pick_epub(e=None):
        try:
            file_picker.pick_files(allowed_extensions=["epub"], dialog_title="Choose an EPUB")
        except Exception as ex:
            log("Filtered picker failed (" + type(ex).__name__ + ": " + str(ex) + ") - reopening without filter.", warn=True)
            try:
                file_picker.pick_files(dialog_title="Choose an EPUB")
            except Exception as ex2:
                log("ERROR: could not open file picker: " + type(ex2).__name__ + ": " + str(ex2), error=True)

    save_target = {"path": None}
    save_wait = {"event": None}
    save_picker = ft.FilePicker(on_result=None)

    def on_save_result(e):
        try:
            save_target["path"] = e.path if getattr(e, "path", None) else None
        except Exception:
            save_target["path"] = None
        finally:
            ev = save_wait["event"]
            if ev is not None:
                ev.set()

    save_picker.on_result = on_save_result

    try:
        page.overlay.append(file_picker)
        page.overlay.append(save_picker)
    except Exception:
        try:
            page.add(file_picker, save_picker)
        except Exception as ex:
            log("Could not register file pickers: " + type(ex).__name__ + ": " + str(ex), error=True)

    def fallback_save_dialog(default_name, folder=None):
        """Ask the user where to save, starting in the input folder. Returns the path or None."""
        save_target["path"] = None
        ev = threading.Event()
        save_wait["event"] = ev
        try:
            if folder:
                save_picker.save_file(
                    dialog_title="Save colored EPUB as",
                    file_name=default_name,
                    initial_directory=folder,
                )
            else:
                save_picker.save_file(dialog_title="Save colored EPUB as", file_name=default_name)
        except TypeError:
            # Older flet builds have no initial_directory argument.
            try:
                save_picker.save_file(dialog_title="Save colored EPUB as", file_name=default_name)
            except Exception as ex:
                log("Save dialog could not open: " + type(ex).__name__ + ": " + str(ex), warn=True)
                ev.set()
                return None
        except Exception as ex:
            log("Save dialog could not open: " + type(ex).__name__ + ": " + str(ex), warn=True)
            ev.set()
            return None
        ev.wait(timeout=600)
        return save_target["path"]

    # ---- options & output path ----
    def gather_opts():
        return {
            "fieldset_on": sys_map["fieldset"]["on"], "fieldset_color": sys_map["fieldset"]["color"],
            "sys_on": sys_map["sys"]["on"], "sys_color": sys_map["sys"]["color"],
            "quote_on": sys_map["quote"]["on"], "quote_color": sys_map["quote"]["color"],
            "title_on": sys_map["title"]["on"], "title_color": sys_map["title"]["color"],
            "names": [
                {"name": c["name"].strip(), "color": c["color"]}
                for c in chars
                if (c.get("name") or "").strip() and c.get("color")
            ],
        }

    def compute_output_path(inp):
        out_name = (out_name_field.value or "").strip()
        folder = os.path.dirname(inp) or "."
        if not out_name:
            out_name = os.path.splitext(os.path.basename(inp))[0] + "_colored.epub"
        if not out_name.lower().endswith(".epub"):
            out_name += ".epub"
        out_path = os.path.join(folder, out_name)
        if os.path.abspath(out_path) == os.path.abspath(inp):
            out_name = os.path.splitext(out_name)[0] + "_colored.epub"
            out_path = os.path.join(folder, out_name)
        return out_path

    # ---- processing ----
    color_it_btn = ft.ElevatedButton(
        "✨ Color it!", bgcolor="#00E5FF", color="#06232B",
        height=48, on_click=None,
    )
    selftest_btn = ft.OutlinedButton("Self-test", icon=_icon("SCIENCE"), on_click=None)

    def processing_worker(inp, out_path, opts):
        try:
            log("Processing: " + os.path.basename(inp))
            log("Output: " + out_path)
            log(
                "Enabled: fieldset=" + str(opts["fieldset_on"])
                + ", system-text=" + str(opts["sys_on"])
                + ", quotes=" + str(opts["quote_on"])
                + ", titles=" + str(opts["title_on"])
                + ", characters=" + str(len(opts["names"]))
            )
            res = process_epub(inp, out_path, opts, log, fallback_save_dialog)
            if res:
                tail = ", " + str(res["failed"]) + " failed" if res["failed"] else ""
                log("DONE - " + str(res["changed"]) + " of " + str(res["total"]) + " HTML files recolored" + tail + ".")
                log("Saved: " + res["path"])
        except Exception as ex:
            log("ERROR: processing failed: " + type(ex).__name__ + ": " + str(ex), error=True)
            log(traceback.format_exc(), error=True)
        finally:
            state["busy"] = False
            color_it_btn.disabled = False
            safe_update()

    def on_color_it(e=None):
        try:
            if state["busy"]:
                return
            inp = state["input"]
            if not inp or not os.path.isfile(inp):
                log("ERROR: pick an EPUB first.", error=True)
                return
            opts = gather_opts()
            anything = (
                opts["fieldset_on"] or opts["sys_on"] or opts["quote_on"]
                or opts["title_on"] or len(opts["names"]) > 0
            )
            if not anything:
                log("ERROR: nothing is enabled - tick a system element or add a character name first.", error=True)
                return
            out_path = compute_output_path(inp)
            state["busy"] = True
            color_it_btn.disabled = True
            safe_update()
            # persist the last-used list
            try:
                page.client_storage.set(STORAGE_KEY, json.dumps(opts["names"]))
            except Exception:
                pass
            threading.Thread(target=processing_worker, args=(inp, out_path, opts), daemon=True).start()
        except Exception as ex:
            log("ERROR while starting: " + type(ex).__name__ + ": " + str(ex), error=True)
            log(traceback.format_exc(), error=True)
            state["busy"] = False
            color_it_btn.disabled = False
            safe_update()

    color_it_btn.on_click = on_color_it

    def on_self_test(e=None):
        if state["busy"]:
            return
        state["busy"] = True
        selftest_btn.disabled = True
        safe_update()

        def worker():
            try:
                log("Self-test: building a tiny in-memory EPUB and running the full pipeline...")
                ok, message = run_self_test(log)
                if ok:
                    log("Self-test: PASS - " + message)
                else:
                    log("Self-test: FAIL - " + message, error=True)
            except Exception as ex:
                log("Self-test crashed: " + type(ex).__name__ + ": " + str(ex), error=True)
                log(traceback.format_exc(), error=True)
            finally:
                state["busy"] = False
                selftest_btn.disabled = False
                safe_update()

        threading.Thread(target=worker, daemon=True).start()

    selftest_btn.on_click = on_self_test

    # ---- assemble UI ----
    file_section = ft.Container(
        bgcolor="#161A22", border_radius=12, padding=14, border=ft.border.all(1, "#2A2F3A"),
        content=ft.Column(spacing=10, controls=[
            ft.Row(spacing=10, controls=[
                ft.OutlinedButton("Choose EPUB", icon=_icon("FILE_OPEN"), on_click=pick_epub),
                ft.Container(chosen_file_text, expand=True),
            ]),
            out_name_field,
        ]),
    )

    system_section = ft.Container(
        bgcolor="#161A22", border_radius=12, padding=14, border=ft.border.all(1, "#2A2F3A"),
        content=ft.Column(spacing=4, controls=[
            ft.Text("System elements", size=15, weight=ft.FontWeight.BOLD),
            build_system_section(),
        ]),
    )

    char_section = ft.Container(
        bgcolor="#161A22", border_radius=12, padding=14, border=ft.border.all(1, "#2A2F3A"),
        content=ft.Column(spacing=10, controls=[
            ft.Text("Character names", size=15, weight=ft.FontWeight.BOLD),
            ft.Text(
                "Any name, any color - works for every novel. The list is saved and restored automatically.",
                size=12, color="#9AA3B2",
            ),
            char_list_col,
            ft.Row(wrap=True, spacing=8, controls=[
                ft.OutlinedButton("+ Add character", icon=_icon("ADD"), on_click=add_char),
                ft.OutlinedButton("Save list", icon=_icon("SAVE"), on_click=save_list),
                ft.OutlinedButton("Load list", icon=_icon("FOLDER_OPEN"), on_click=load_list),
                ft.OutlinedButton("Clear all", icon=_icon("DELETE"), on_click=clear_all),
            ]),
        ]),
    )

    main_col = ft.Column(
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        spacing=14,
        controls=[
            ft.Column(spacing=2, controls=[
                ft.Text(APP_NAME, size=26, weight=ft.FontWeight.BOLD),
                ft.Text("Recolor system elements and character names in any novel EPUB.", size=13, color="#9AA3B2"),
            ]),
            perm_denied_text,
            file_section,
            system_section,
            char_section,
            color_it_btn,
            selftest_btn,
            ft.Text("Status log", size=13, weight=ft.FontWeight.BOLD),
            log_box,
        ],
    )

    # ---- startup sequence ----
    try:
        saved = load_charlist_from_storage()
        if saved:
            chars.extend(saved)
    except Exception as ex:
        log("Could not restore character list: " + type(ex).__name__ + ": " + str(ex), warn=True)
    rebuild_chars()

    page.add(main_col)
    log("EPUB Colorizer started.")
    request_storage_permission()
    log("Ready. Pick an EPUB to begin.")


if __name__ == "__main__":
    if hasattr(ft, "run"):
        ft.run(main)
    else:
        ft.app(target=main)

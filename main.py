"""EPUB Colorizer: locally colorize system markers and character names in EPUB books."""

import asyncio
import json
import os
import queue
import re
import sys
import tempfile
import threading
import zipfile

import flet as ft


PALETTE = [
    "#F44336", "#E91E63", "#9C27B0", "#673AB7", "#3F51B5", "#2196F3",
    "#03A9F4", "#00BCD4", "#009688", "#4CAF50", "#8BC34A", "#CDDC39",
    "#FFEB3B", "#FFC107", "#FF9800", "#FF5722", "#795548", "#607D8B",
    "#FFFFFF", "#B57EDC", "#FFD700", "#00E5FF", "#FF5733", "#33A1FF",
]


def private_data_dir():
    """Return a writable app-private directory for saved character lists."""
    home = os.path.expanduser("~")
    if not home:
        home = os.path.dirname(os.path.abspath(sys.argv[0]))
    path = os.path.join(home, ".epub_colorizer")
    os.makedirs(path, exist_ok=True)
    return path


def valid_color(value):
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is not None


def transform_html(text, options, characters, character_pattern):
    """Apply the five requested transformations in order, once per HTML document."""
    if options["fieldset"]["enabled"]:
        color = options["fieldset"]["color"]
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        rgba = f"rgba({rgb[0]},{rgb[1]},{rgb[2]},0.06)"
        style = (
            f'border: 2px solid {color}; background-color: {rgba}; '
            "padding: 12px 16px; border-radius: 8px; margin: 16px 0;"
        )

        def style_fieldset(match):
            tag = match.group(0)
            if re.search(r"\bstyle\s*=|\bdata-colored\s*=", tag, re.IGNORECASE):
                return tag
            opening = re.match(r"<fieldset\b", tag, re.IGNORECASE).group(0)
            attributes = tag[len(opening):-1]
            return f'{opening} data-colored="1" style="{style}"{attributes}>'

        text = re.sub(r"<fieldset\b[^>]*>", style_fieldset, text, flags=re.IGNORECASE)

    # Bracket spans are created before character spans so names are nested inside them.
    if options["brackets"]["enabled"]:
        color = options["brackets"]["color"]
        text = re.sub(
            r"\[[^\[\]]*\]",
            lambda m: f'<span style="color: {color};">{m.group(0)}</span>',
            text,
        )

    if options["quotes"]["enabled"]:
        color = options["quotes"]["color"]
        text = re.sub(
            r"「[^「」]*」",
            lambda m: f'<span style="color: {color};">{m.group(0)}</span>',
            text,
        )

    if options["titles"]["enabled"]:
        color = options["titles"]["color"]
        text = re.sub(
            r"『[^『』]*』",
            lambda m: f'<span style="color: {color};">{m.group(0)}</span>',
            text,
        )

    # A single longest-first regex pass handles overlapping names and punctuation safely.
    if character_pattern is not None:
        color_by_name = {item["name"]: item["color"] for item in characters}
        text = character_pattern.sub(
            lambda match: f'<span style="color: {color_by_name[match.group(0)]};">'
            f'{match.group(0)}</span>',
            text,
        )
    return text


def safe_extract(archive, destination):
    """Extract ZIP members without permitting paths to escape the temporary folder."""
    root = os.path.realpath(destination)
    for info in archive.infolist():
        member_path = os.path.realpath(os.path.join(root, *info.filename.replace("\\", "/").split("/")))
        if member_path != root and not member_path.startswith(root + os.sep):
            raise ValueError(f"Unsafe ZIP entry path: {info.filename}")
        if info.is_dir():
            os.makedirs(member_path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(member_path), exist_ok=True)
        with archive.open(info, "r") as source, open(member_path, "wb") as target:
            while True:
                chunk = source.read(1024 * 128)
                if not chunk:
                    break
                target.write(chunk)


def process_epub(input_path, output_path, options, characters, message_queue):
    """Extract, transform each HTML file individually, and repack a valid EPUB."""
    try:
        message_queue.put(("log", "📦 Reading EPUB..."))
        compiled_names = None
        if characters:
            ordered = sorted(characters, key=lambda item: len(item["name"]), reverse=True)
            compiled_names = re.compile("|".join(re.escape(item["name"]) for item in ordered))

        with tempfile.TemporaryDirectory(prefix="epub_colorizer_") as work_dir:
            with zipfile.ZipFile(input_path, "r") as source_zip:
                safe_extract(source_zip, work_dir)

            html_files = []
            for current_root, _, filenames in os.walk(work_dir):
                for filename in filenames:
                    if filename.lower().endswith((".xhtml", ".html", ".htm")):
                        html_files.append(os.path.join(current_root, filename))
            html_files.sort()
            total = len(html_files)
            message_queue.put(("log", f"🔎 Found {total} HTML files"))

            modified = 0
            for index, file_path in enumerate(html_files, start=1):
                # Read, transform, and write one document at a time to bound memory use.
                with open(file_path, "r", encoding="utf-8-sig") as html_file:
                    original = html_file.read()
                transformed = transform_html(original, options, characters, compiled_names)
                if transformed != original:
                    modified += 1
                with open(file_path, "w", encoding="utf-8", newline="") as html_file:
                    html_file.write(transformed)
                if index % 50 == 0 or index == total:
                    message_queue.put(("log", f"   ... {index}/{total}"))
                    message_queue.put(("progress", (index, total)))

            mimetype_path = os.path.join(work_dir, "mimetype")
            if not os.path.isfile(mimetype_path):
                raise ValueError("This ZIP does not contain the required EPUB mimetype file.")

            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            with zipfile.ZipFile(output_path, "w") as output_zip:
                with open(mimetype_path, "rb") as mimetype_file:
                    output_zip.writestr("mimetype", mimetype_file.read(), compress_type=zipfile.ZIP_STORED)
                for current_root, _, filenames in os.walk(work_dir):
                    for filename in filenames:
                        full_path = os.path.join(current_root, filename)
                        relative = os.path.relpath(full_path, work_dir).replace(os.sep, "/")
                        if relative == "mimetype":
                            continue
                        output_zip.write(full_path, relative, compress_type=zipfile.ZIP_DEFLATED)

        message_queue.put(("log", f"✅ Modified {modified} files"))
        message_queue.put(("log", f"🎉 Saved: {output_path}"))
        message_queue.put(("done", None))
    except Exception as error:
        message_queue.put(("log", f"❌ Error: {error}"))
        message_queue.put(("done", error))


def main(page: ft.Page):
    page.title = "EPUB Colorizer"
    page.scroll = None
    page.padding = 16

    state = {
        "input_path": None,
        "input_name": None,
        "staged_input_dir": None,
        "characters": [],
        "log_lines": [],
    }
    default_options = {
        "fieldset": {"label": "Fieldset boxes (system windows)", "enabled": True, "color": "#00E5FF"},
        "brackets": {"label": "System text [ ... ]", "enabled": True, "color": "#00E5FF"},
        "quotes": {"label": "Prophet quotes 「 ... 」", "enabled": False, "color": "#B57EDC"},
        "titles": {"label": "Novel titles 『 ... 』", "enabled": False, "color": "#FFD700"},
    }
    options = {key: value.copy() for key, value in default_options.items()}

    file_picker = ft.FilePicker()
    status_log = ft.TextField(
        label="Status / log", multiline=True, min_lines=8, max_lines=12,
        read_only=True, text_style=ft.TextStyle(font_family="monospace", size=12),
    )
    filename_label = ft.Text("No EPUB selected")
    output_field = ft.TextField(label="Output filename", value="")
    progress_bar = ft.ProgressBar(value=0)
    progress_label = ft.Text("Ready")
    character_rows = ft.Column(controls=[])
    empty_hint = ft.Text("No characters yet — tap '+ Add character' or 'Load list'.")
    color_button = ft.Button(content="✨ Color it!", on_click=None, disabled=True)

    def add_log(message):
        state["log_lines"].append(message)
        status_log.value = "\n".join(state["log_lines"])
        page.update()

    def set_character_controls():
        character_rows.controls = [row["control"] for row in state["characters"]]
        empty_hint.visible = len(state["characters"]) == 0
        page.update()

    def save_last_characters():
        try:
            with open(os.path.join(private_data_dir(), "last_characters.json"), "w", encoding="utf-8") as saved:
                json.dump(get_characters(), saved, ensure_ascii=False, indent=2)
        except Exception as error:
            add_log(f"⚠️ Could not persist character list: {error}")

    def get_characters():
        result = []
        for row in state["characters"]:
            name = (row["name_field"].value or "").strip()
            if name and valid_color(row["color"]):
                result.append({"name": name, "color": row["color"]})
        return result

    def make_character_row(name="", color="#FF5733"):
        item = {"name_field": None, "color": color, "control": None}
        name_field = ft.TextField(value=name, label="Character name", expand=True)
        item["name_field"] = name_field
        swatch = ft.Container(width=36, height=36, bgcolor=color, on_click=lambda e: open_palette(lambda c: set_row_color(item, c)))

        def remove_row():
            state["characters"] = [entry for entry in state["characters"] if entry is not item]
            set_character_controls()
            save_last_characters()

        delete_button = ft.TextButton(content="×", on_click=lambda e: remove_row())
        item["control"] = ft.Row(controls=[name_field, swatch, delete_button])
        name_field.on_change = lambda e: save_last_characters()
        item["swatch"] = swatch
        return item

    def set_row_color(item, color):
        item["color"] = color
        item["swatch"].bgcolor = color
        page.update()
        save_last_characters()

    def open_palette(on_pick):
        rows = []
        for start in range(0, len(PALETTE), 6):
            swatches = []
            for color in PALETTE[start:start + 6]:
                swatches.append(
                    ft.Container(
                        width=38, height=38, bgcolor=color,
                        on_click=lambda e, selected=color: (
                            page.pop_dialog(), on_pick(selected)
                        ),
                    )
                )
            rows.append(ft.Row(controls=swatches))
        dialog = ft.AlertDialog(
            title=ft.Text("Choose a color"),
            content=ft.Column(controls=rows, tight=True, spacing=8),
            actions=[ft.TextButton(content="Cancel", on_click=lambda e: page.pop_dialog())],
        )
        page.show_dialog(dialog)

    def append_loaded_characters(entries, announce=True):
        state["characters"] = []
        skipped = 0
        for entry in entries:
            if not isinstance(entry, dict):
                skipped += 1
                continue
            name = entry.get("name")
            color = entry.get("color")
            if not isinstance(name, str) or not name.strip() or not valid_color(color):
                skipped += 1
                continue
            state["characters"].append(make_character_row(name.strip(), color.upper()))
        set_character_controls()
        save_last_characters()
        if announce and skipped:
            add_log(f"⚠️ Skipped {skipped} invalid character entr{'y' if skipped == 1 else 'ies'}.")
        if announce:
            add_log(f"✅ Loaded {len(state['characters'])} character entr{'y' if len(state['characters']) == 1 else 'ies'}.")

    def add_character():
        state["characters"].append(make_character_row())
        set_character_controls()
        save_last_characters()

    def clear_characters():
        state["characters"] = []
        set_character_controls()
        save_last_characters()

    async def choose_epub():
        try:
            selected = await file_picker.pick_files(
                dialog_title="Choose an EPUB", file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["epub"], allow_multiple=False, with_data=True,
            )
            if not selected:
                return
            chosen = selected[0]
            if state["staged_input_dir"]:
                import shutil
                shutil.rmtree(state["staged_input_dir"], ignore_errors=True)
                state["staged_input_dir"] = None
            selected_path = chosen.path
            if not selected_path and chosen.bytes is not None:
                stage_dir = tempfile.mkdtemp(prefix="epub_selected_")
                selected_path = os.path.join(stage_dir, chosen.name)
                with open(selected_path, "wb") as staged:
                    staged.write(chosen.bytes)
                state["staged_input_dir"] = stage_dir
            if not selected_path:
                raise ValueError("The file picker did not provide an accessible file path or file data.")
            state["input_path"] = selected_path
            state["input_name"] = os.path.basename(chosen.name or selected_path)
            filename_label.value = state["input_name"]
            base = os.path.splitext(state["input_name"])[0]
            output_field.value = f"{base}_colored.epub"
            color_button.disabled = False
            progress_label.value = "Ready"
            page.update()
            add_log(f"📘 Selected: {state['input_name']}")
        except Exception as error:
            add_log(f"❌ File selection error: {error}")

    async def save_character_list():
        try:
            entries = get_characters()
            if not entries:
                add_log("⚠️ There are no named characters to save.")
                return
            path = None
            try:
                path = await file_picker.save_file(
                    dialog_title="Save character list", file_name="characters.json",
                    file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["json"],
                )
            except Exception as error:
                add_log(f"⚠️ Save picker unavailable; using app storage: {error}")
            if not path:
                path = os.path.join(private_data_dir(), "characters.json")
                add_log("ℹ️ Save picker was unavailable or canceled; saving in app data.")
            if not path.lower().endswith(".json"):
                path += ".json"
            with open(path, "w", encoding="utf-8") as saved:
                json.dump(entries, saved, ensure_ascii=False, indent=2)
            add_log(f"✅ Character list saved: {path}")
        except Exception as error:
            add_log(f"❌ Could not save character list: {error}")

    async def load_character_list():
        try:
            selected = await file_picker.pick_files(
                dialog_title="Load character list", file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"], allow_multiple=False, with_data=True,
            )
            if not selected:
                return
            chosen = selected[0]
            if chosen.path:
                with open(chosen.path, "r", encoding="utf-8-sig") as source:
                    entries = json.load(source)
            elif chosen.bytes is not None:
                entries = json.loads(chosen.bytes.decode("utf-8-sig"))
            else:
                raise ValueError("The selected JSON file could not be read.")
            if not isinstance(entries, list):
                raise ValueError("Character list JSON must contain an array.")
            append_loaded_characters(entries)
        except Exception as error:
            add_log(f"❌ Could not load character list: {error}")

    def make_option_row(key):
        option = options[key]
        checkbox = ft.Checkbox(label=option["label"], value=option["enabled"], on_change=lambda e: change_option(key, e.control.value))
        swatch = ft.Container(
            width=36, height=30, bgcolor=option["color"],
            on_click=lambda e: open_palette(lambda c: change_option_color(key, c)),
        )
        option["checkbox"] = checkbox
        option["swatch"] = swatch
        return ft.Row(controls=[checkbox, swatch], wrap=True)

    def change_option(key, enabled):
        options[key]["enabled"] = bool(enabled)
        page.update()

    def change_option_color(key, color):
        options[key]["color"] = color
        options[key]["swatch"].bgcolor = color
        page.update()

    async def colorize():
        input_path = state["input_path"]
        if not input_path:
            add_log("❌ Choose an EPUB file first.")
            return
        characters = get_characters()
        if not characters and not any(options[key]["enabled"] for key in options):
            add_log("❌ Enable at least one system element or add a character before processing.")
            return
        output_name = os.path.basename((output_field.value or "").strip())
        if not output_name:
            output_name = f"{os.path.splitext(state['input_name'])[0]}_colored.epub"
        if not output_name.lower().endswith(".epub"):
            output_name += ".epub"
        if output_name.casefold() == state["input_name"].casefold():
            output_name = f"{os.path.splitext(output_name)[0]}_colored.epub"
        output_path = os.path.join(os.path.dirname(os.path.abspath(input_path)), output_name)
        if os.path.abspath(output_path) == os.path.abspath(input_path):
            output_path = os.path.join(os.path.dirname(os.path.abspath(input_path)), f"{os.path.splitext(output_name)[0]}_colored.epub")

        color_button.disabled = True
        progress_bar.value = 0
        progress_label.value = "Starting..."
        page.update()
        messages = queue.Queue()
        worker = threading.Thread(
            target=process_epub,
            args=(input_path, output_path, {key: {"enabled": value["enabled"], "color": value["color"]} for key, value in options.items()}, characters, messages),
            daemon=True,
        )
        worker.start()
        try:
            while worker.is_alive() or not messages.empty():
                try:
                    while True:
                        kind, payload = messages.get_nowait()
                        if kind == "log":
                            state["log_lines"].append(payload)
                            status_log.value = "\n".join(state["log_lines"])
                        elif kind == "progress":
                            current, total = payload
                            progress_bar.value = current / total if total else 1
                            progress_label.value = f"Processing {current}/{total} HTML files"
                except queue.Empty:
                    pass
                page.update()
                await asyncio.sleep(0.1)
            worker.join()
            while not messages.empty():
                kind, payload = messages.get_nowait()
                if kind == "log":
                    state["log_lines"].append(payload)
                elif kind == "progress":
                    current, total = payload
                    progress_bar.value = current / total if total else 1
                    progress_label.value = f"Processing {current}/{total} HTML files"
            if state["log_lines"]:
                status_log.value = "\n".join(state["log_lines"])
            progress_label.value = "Finished"
        except Exception as error:
            add_log(f"❌ Processing error: {error}")
        finally:
            color_button.disabled = False
            page.update()

    color_button.on_click = lambda e: asyncio.create_task(colorize())

    option_rows = [make_option_row(key) for key in options]
    page.add(
        ft.Column(
            controls=[
                ft.Text("🎨 EPUB Colorizer"),
                ft.Text("Runs fully on your device. Nothing is uploaded."),
                ft.Row(controls=[ft.Button(content="Choose EPUB", on_click=lambda e: asyncio.create_task(choose_epub())), filename_label], wrap=True),
                output_field,
                ft.Text("System elements", weight=ft.FontWeight.BOLD),
                *option_rows,
                ft.Text("Character names", weight=ft.FontWeight.BOLD),
                empty_hint,
                character_rows,
                ft.Row(controls=[
                    ft.Button(content="+ Add character", on_click=lambda e: add_character()),
                    ft.Button(content="Save list", on_click=lambda e: asyncio.create_task(save_character_list())),
                    ft.Button(content="Load list", on_click=lambda e: asyncio.create_task(load_character_list())),
                    ft.Button(content="Clear all", on_click=lambda e: clear_characters()),
                ], wrap=True),
                color_button,
                progress_bar,
                progress_label,
                status_log,
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=12,
        )
    )

    try:
        last_path = os.path.join(private_data_dir(), "last_characters.json")
        if os.path.isfile(last_path):
            with open(last_path, "r", encoding="utf-8-sig") as saved:
                saved_entries = json.load(saved)
            if isinstance(saved_entries, list):
                append_loaded_characters(saved_entries, announce=False)
            else:
                add_log("⚠️ Saved character list is not a JSON array; starting empty.")
    except Exception as error:
        add_log(f"⚠️ Could not restore the last character list: {error}")

    page.update()


ft.run(main)

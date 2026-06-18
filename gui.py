#!/usr/bin/env python3
"""
Reverse Image Search — Desktop GUI
Double-click this file (or run: python gui.py) to open the app.
No command line needed.
"""

import os
import json
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

from reverse_image_search import (
    search_reverse_image,
    upload_and_search,
    parse_results,
    export_csv,
    export_excel,
    export_html,
)

CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(data: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Reverse Image Search")
        self.minsize(680, 560)
        self.resizable(True, True)

        # Use a more modern theme on Windows
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        style.configure("Header.TLabel", font=("Segoe UI", 13, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 9, "bold"), foreground="#555")
        style.configure("Run.TButton", font=("Segoe UI", 10, "bold"), padding=6)

        self._config = load_config()
        self._build_ui()
        self._restore_saved()

    # ── UI Construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        pad = {"padx": 16, "pady": 6}

        root_frame = ttk.Frame(self)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(1, weight=1)

        r = 0

        # ── Header ──
        ttk.Label(root_frame, text="Reverse Image Search", style="Header.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", padx=16, pady=(14, 2)
        )
        r += 1
        ttk.Label(
            root_frame,
            text="Find everywhere an image appears online, then export a visual report.",
            foreground="#666",
        ).grid(row=r, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 10))
        r += 1
        ttk.Separator(root_frame, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=2
        )
        r += 1

        # ── Input image ──
        ttk.Label(root_frame, text="IMAGE INPUT", style="Section.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", **pad
        )
        r += 1

        ttk.Label(root_frame, text="Image URL").grid(row=r, column=0, sticky="w", padx=(16, 8), pady=4)
        self.url_var = tk.StringVar()
        ttk.Entry(root_frame, textvariable=self.url_var).grid(
            row=r, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=4
        )
        r += 1

        ttk.Label(root_frame, text="— or —", foreground="#888").grid(
            row=r, column=0, columnspan=3, pady=2
        )
        r += 1

        ttk.Label(root_frame, text="Local image file").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.file_var = tk.StringVar()
        file_row = ttk.Frame(root_frame)
        file_row.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=4)
        file_row.columnconfigure(0, weight=1)
        ttk.Entry(file_row, textvariable=self.file_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(file_row, text="Browse…", command=self._browse_image).grid(
            row=0, column=1, padx=(6, 0)
        )
        r += 1

        ttk.Separator(root_frame, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=6
        )
        r += 1

        # ── API key ──
        ttk.Label(root_frame, text="API KEY", style="Section.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", **pad
        )
        r += 1

        ttk.Label(root_frame, text="SerpAPI key").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.key_var = tk.StringVar()
        self._key_entry = ttk.Entry(root_frame, textvariable=self.key_var, show="•")
        self._key_entry.grid(row=r, column=1, sticky="ew", padx=(0, 6), pady=4)
        self._show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            root_frame, text="Show", variable=self._show_key,
            command=lambda: self._key_entry.config(show="" if self._show_key.get() else "•"),
        ).grid(row=r, column=2, padx=(0, 16))
        r += 1

        self._save_key = tk.BooleanVar(value=True)
        ttk.Checkbutton(root_frame, text="Remember key on this computer", variable=self._save_key).grid(
            row=r, column=1, sticky="w", padx=(0, 16), pady=(0, 6)
        )
        r += 1

        ttk.Separator(root_frame, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=6
        )
        r += 1

        # ── Output settings ──
        ttk.Label(root_frame, text="OUTPUT", style="Section.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", **pad
        )
        r += 1

        ttk.Label(root_frame, text="Save folder").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.outdir_var = tk.StringVar(value=str(Path(__file__).parent))
        outdir_row = ttk.Frame(root_frame)
        outdir_row.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=4)
        outdir_row.columnconfigure(0, weight=1)
        ttk.Entry(outdir_row, textvariable=self.outdir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(outdir_row, text="Browse…", command=self._browse_outdir).grid(
            row=0, column=1, padx=(6, 0)
        )
        r += 1

        ttk.Label(root_frame, text="Filename prefix").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.prefix_var = tk.StringVar(value="results")
        ttk.Entry(root_frame, textvariable=self.prefix_var, width=28).grid(
            row=r, column=1, sticky="w", padx=(0, 16), pady=4
        )
        r += 1

        ttk.Label(root_frame, text="Export formats").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        fmt_row = ttk.Frame(root_frame)
        fmt_row.grid(row=r, column=1, columnspan=2, sticky="w", padx=(0, 16), pady=4)
        self._do_csv   = tk.BooleanVar(value=True)
        self._do_excel = tk.BooleanVar(value=True)
        self._do_html  = tk.BooleanVar(value=True)
        ttk.Checkbutton(fmt_row, text="CSV",                        variable=self._do_csv).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(fmt_row, text="Excel (with thumbnails)",    variable=self._do_excel).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(fmt_row, text="HTML (view in browser)",     variable=self._do_html).pack(side="left")
        r += 1

        ttk.Separator(root_frame, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=6
        )
        r += 1

        # ── Action buttons ──
        btn_row = ttk.Frame(root_frame)
        btn_row.grid(row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=(0, 6))
        self._search_btn = ttk.Button(
            btn_row, text="  Search  ", style="Run.TButton", command=self._run_search
        )
        self._search_btn.pack(side="left")
        self._open_btn = ttk.Button(
            btn_row, text="Open results folder", command=self._open_outdir, state="disabled"
        )
        self._open_btn.pack(side="left", padx=(12, 0))
        r += 1

        # ── Progress log ──
        ttk.Label(root_frame, text="Progress", style="Section.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", padx=16
        )
        r += 1
        self._log_widget = scrolledtext.ScrolledText(
            root_frame, height=9, state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", relief="flat",
        )
        self._log_widget.grid(
            row=r, column=0, columnspan=3, sticky="nsew", padx=16, pady=(2, 12)
        )
        root_frame.rowconfigure(r, weight=1)

    # ── Saved config helpers ───────────────────────────────────────────────────

    def _restore_saved(self):
        if "api_key" in self._config:
            self.key_var.set(self._config["api_key"])
        if "output_dir" in self._config:
            self.outdir_var.set(self._config["output_dir"])

    # ── Browse helpers ─────────────────────────────────────────────────────────

    def _browse_image(self):
        path = filedialog.askopenfilename(
            title="Select image file",
            filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.gif *.webp *.bmp *.tiff"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.file_var.set(path)
            self.url_var.set("")

    def _browse_outdir(self):
        path = filedialog.askdirectory(title="Choose folder to save results")
        if path:
            self.outdir_var.set(path)

    def _open_outdir(self):
        path = self.outdir_var.get()
        if os.path.isdir(path):
            os.startfile(path)

    # ── Logging ────────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        self._log_widget.config(state="normal")
        self._log_widget.insert("end", msg + "\n")
        self._log_widget.see("end")
        self._log_widget.config(state="disabled")

    def _clear_log(self):
        self._log_widget.config(state="normal")
        self._log_widget.delete("1.0", "end")
        self._log_widget.config(state="disabled")

    # ── Search ─────────────────────────────────────────────────────────────────

    def _run_search(self):
        url        = self.url_var.get().strip()
        local_file = self.file_var.get().strip()
        api_key    = self.key_var.get().strip()
        outdir     = self.outdir_var.get().strip()
        prefix     = self.prefix_var.get().strip() or "results"

        if not url and not local_file:
            messagebox.showerror("Missing input", "Please paste an image URL or choose a local file.")
            return
        if url and local_file:
            messagebox.showerror(
                "Ambiguous input",
                "Please provide either a URL or a local file — not both.",
            )
            return
        if not api_key:
            messagebox.showerror("Missing API key", "Please enter your SerpAPI key.")
            return
        if not outdir or not os.path.isdir(outdir):
            messagebox.showerror("Invalid folder", "Please choose a valid output folder.")
            return
        if not (self._do_csv.get() or self._do_excel.get() or self._do_html.get()):
            messagebox.showerror("No format selected", "Please select at least one export format.")
            return

        if self._save_key.get():
            self._config["api_key"]    = api_key
            self._config["output_dir"] = outdir
            save_config(self._config)

        self._clear_log()
        self._search_btn.config(state="disabled")
        self._open_btn.config(state="disabled")

        threading.Thread(
            target=self._worker,
            args=(url or None, local_file or None, api_key, outdir, prefix),
            daemon=True,
        ).start()

    def _worker(self, url, local_file, api_key, outdir, prefix):
        source_label = url or local_file
        try:
            self.after(0, self._log, f"Searching for: {source_label}\n")

            if local_file:
                self.after(0, self._log, "Uploading local file to SerpAPI (Google Lens)…")
                data = upload_and_search(local_file, api_key)
            else:
                self.after(0, self._log, "Querying SerpAPI (Google Reverse Image Search)…")
                data = search_reverse_image(url, api_key)

            results = parse_results(data)

            if not results:
                self.after(0, self._log, "No results returned.")
                self.after(0, self._log, "The image may not be publicly indexed, or the URL may be inaccessible to Google.")
                return

            self.after(0, self._log, f"Found {len(results)} results.\n")

            out_prefix = os.path.join(outdir, prefix)

            if self._do_csv.get():
                path = f"{out_prefix}.csv"
                export_csv(results, path)
                self.after(0, self._log, f"CSV saved:   {path}")

            if self._do_excel.get():
                self.after(0, self._log, "Downloading thumbnails for Excel — this may take 30–60 seconds…")
                path = f"{out_prefix}.xlsx"
                export_excel(results, path)
                self.after(0, self._log, f"Excel saved: {path}")

            if self._do_html.get():
                path = f"{out_prefix}.html"
                export_html(results, path, source_label)
                self.after(0, self._log, f"HTML saved:  {path}")

            self.after(0, self._log, "\nAll done. Click 'Open results folder' to view your files.")
            self.after(0, lambda: self._open_btn.config(state="normal"))

        except Exception as e:
            self.after(0, self._log, f"\nError: {e}")
            self.after(0, self._log, traceback.format_exc())

        finally:
            self.after(0, lambda: self._search_btn.config(state="normal"))


if __name__ == "__main__":
    App().mainloop()

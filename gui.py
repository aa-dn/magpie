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
        self.minsize(720, 540)
        self.resizable(True, True)

        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

        style.configure("Header.TLabel",    font=("Segoe UI", 13, "bold"))
        style.configure("Section.TLabel",   font=("Segoe UI", 9, "bold"), foreground="#555")
        style.configure("Run.TButton",      font=("Segoe UI", 10, "bold"), padding=6)
        style.configure("ColHeader.TLabel", font=("Segoe UI", 8, "bold"), foreground="#555")

        self._config = load_config()
        self._results = []
        self._result_vars = []
        self._source_label = ""
        self._file_paths = []
        self._build_ui()
        self._restore_saved()

    # ── UI layout ──────────────────────────────────────────────────────────────
    #
    # root_frame rows (only row 4 expands):
    #   0 — scrollable form canvas   (fixed height, scrolls internally)
    #   1 — scrollbar for the form   (same row, column 1)
    #   2 — action buttons           (always visible)
    #   3 — progress log             (always visible, 3 lines)
    #   4 — results panel            (weight=1, fills rest of window)
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root_frame = ttk.Frame(self)
        root_frame.pack(fill="both", expand=True)
        root_frame.columnconfigure(0, weight=1)
        root_frame.rowconfigure(4, weight=1)   # results expands

        # ── Scrollable form ────────────────────────────────────────────────────
        # Fixed pixel height so it never pushes the Search button off-screen.
        # Users can mousewheel-scroll to reach Output settings.
        self._form_canvas = tk.Canvas(root_frame, height=310, highlightthickness=0)
        form_vsb = ttk.Scrollbar(root_frame, orient="vertical",
                                  command=self._form_canvas.yview)
        self._form_canvas.configure(yscrollcommand=form_vsb.set)
        self._form_canvas.grid(row=0, column=0, sticky="ew")
        form_vsb.grid(row=0, column=1, sticky="ns")

        form = ttk.Frame(self._form_canvas)
        form.columnconfigure(1, weight=1)
        _form_win = self._form_canvas.create_window((0, 0), window=form, anchor="nw")

        form.bind("<Configure>",
                  lambda e: self._form_canvas.configure(
                      scrollregion=self._form_canvas.bbox("all")))
        self._form_canvas.bind("<Configure>",
                               lambda e: self._form_canvas.itemconfig(
                                   _form_win, width=e.width))
        self._form_canvas.bind("<MouseWheel>",
                               lambda e: self._form_canvas.yview_scroll(
                                   int(-1 * (e.delta / 120)), "units"))

        self._populate_form(form)

        # ── Separator ─────────────────────────────────────────────────────────
        ttk.Separator(root_frame, orient="horizontal").grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 4)
        )

        # ── Action buttons (always on-screen) ─────────────────────────────────
        btn_row = ttk.Frame(root_frame)
        btn_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 6))
        self._search_btn = ttk.Button(
            btn_row, text="  Search  ", style="Run.TButton", command=self._run_search
        )
        self._search_btn.pack(side="left")
        self._export_btn = ttk.Button(
            btn_row, text="Export Selected", command=self._export_selected, state="disabled"
        )
        self._export_btn.pack(side="left", padx=(12, 0))
        self._open_btn = ttk.Button(
            btn_row, text="Open results folder", command=self._open_outdir, state="disabled"
        )
        self._open_btn.pack(side="left", padx=(12, 0))

        # ── Progress log ───────────────────────────────────────────────────────
        self._log_widget = scrolledtext.ScrolledText(
            root_frame, height=3, state="disabled",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", relief="flat",
        )
        self._log_widget.grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 4)
        )

        # ── Results panel (expands to fill remaining space) ────────────────────
        results_outer = ttk.LabelFrame(
            root_frame, text="Results — tick the rows you want to export"
        )
        results_outer.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=16, pady=(0, 10))
        results_outer.columnconfigure(0, weight=1)
        results_outer.rowconfigure(1, weight=1)

        ctrl_row = ttk.Frame(results_outer)
        ctrl_row.grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        ttk.Button(ctrl_row, text="Select all",  command=self._select_all).pack(side="left", padx=(0, 6))
        ttk.Button(ctrl_row, text="Select none", command=self._select_none).pack(side="left")
        self._result_count_label = ttk.Label(ctrl_row, text="No results yet", foreground="#888")
        self._result_count_label.pack(side="left", padx=(14, 0))

        container = ttk.Frame(results_outer)
        container.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(container, bg="#f8f8f8", highlightthickness=0)
        vsb = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self._rows_frame = ttk.Frame(self._canvas)
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._rows_frame, anchor="nw"
        )
        self._rows_frame.bind("<Configure>", self._on_rows_configure)
        self._canvas.bind("<Configure>",     self._on_canvas_configure)
        self._canvas.bind("<MouseWheel>",    self._on_mousewheel)
        self._rows_frame.bind("<MouseWheel>", self._on_mousewheel)

    def _populate_form(self, form):
        """Fill the scrollable form frame with input widgets."""
        r = 0

        # Header
        ttk.Label(form, text="Reverse Image Search", style="Header.TLabel").grid(
            row=r, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 2)
        )
        r += 1
        ttk.Label(
            form,
            text="Find everywhere an image appears online, then export a visual report.",
            foreground="#666",
        ).grid(row=r, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 6))
        r += 1
        ttk.Separator(form, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=2
        )
        r += 1

        # Image URL
        ttk.Label(form, text="Image URL").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.url_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.url_var).grid(
            row=r, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=4
        )
        r += 1

        ttk.Label(form, text="— or —", foreground="#888").grid(
            row=r, column=0, columnspan=3, pady=(2, 0)
        )
        r += 1

        # Multi-file listbox
        ttk.Label(form, text="Local image files").grid(
            row=r, column=0, sticky="nw", padx=(16, 8), pady=(6, 2)
        )

        file_area = ttk.Frame(form)
        file_area.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=(4, 2))
        file_area.columnconfigure(0, weight=1)

        lb_frame = ttk.Frame(file_area)
        lb_frame.grid(row=0, column=0, sticky="ew")
        lb_frame.columnconfigure(0, weight=1)

        self._file_listbox = tk.Listbox(
            lb_frame, height=4, selectmode=tk.EXTENDED,
            font=("Segoe UI", 9), activestyle="none",
            relief="sunken", borderwidth=1,
        )
        lb_vsb = ttk.Scrollbar(lb_frame, orient="vertical",
                                command=self._file_listbox.yview)
        self._file_listbox.configure(yscrollcommand=lb_vsb.set)
        self._file_listbox.grid(row=0, column=0, sticky="ew")
        lb_vsb.grid(row=0, column=1, sticky="ns")
        # Mousewheel over the file list shouldn't scroll the form
        self._file_listbox.bind("<MouseWheel>", lambda e: "break")

        lb_btns = ttk.Frame(file_area)
        lb_btns.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Button(lb_btns, text="Add files…",      command=self._browse_images).pack(side="left", padx=(0, 6))
        ttk.Button(lb_btns, text="Remove selected", command=self._remove_selected_files).pack(side="left", padx=(0, 6))
        ttk.Button(lb_btns, text="Clear all",       command=self._clear_files).pack(side="left")
        r += 1

        ttk.Separator(form, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=6
        )
        r += 1

        # API key
        ttk.Label(form, text="SerpAPI key").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.key_var = tk.StringVar()
        self._key_entry = ttk.Entry(form, textvariable=self.key_var, show="•")
        self._key_entry.grid(row=r, column=1, sticky="ew", padx=(0, 6), pady=4)
        self._show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            form, text="Show", variable=self._show_key,
            command=lambda: self._key_entry.config(
                show="" if self._show_key.get() else "•"),
        ).grid(row=r, column=2, padx=(0, 16))
        r += 1

        self._save_key = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            form, text="Remember key on this computer", variable=self._save_key
        ).grid(row=r, column=1, sticky="w", padx=(0, 16), pady=(0, 4))
        r += 1

        ttk.Separator(form, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", padx=16, pady=6
        )
        r += 1

        # Output settings
        ttk.Label(form, text="Save folder").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.outdir_var = tk.StringVar(value=str(Path(__file__).parent))
        outdir_row = ttk.Frame(form)
        outdir_row.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=4)
        outdir_row.columnconfigure(0, weight=1)
        ttk.Entry(outdir_row, textvariable=self.outdir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(outdir_row, text="Browse…", command=self._browse_outdir).grid(
            row=0, column=1, padx=(6, 0)
        )
        r += 1

        ttk.Label(form, text="Filename prefix").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=4
        )
        self.prefix_var = tk.StringVar(value="results")
        ttk.Entry(form, textvariable=self.prefix_var, width=28).grid(
            row=r, column=1, sticky="w", padx=(0, 16), pady=4
        )
        r += 1

        ttk.Label(form, text="Export formats").grid(
            row=r, column=0, sticky="w", padx=(16, 8), pady=(4, 8)
        )
        fmt_row = ttk.Frame(form)
        fmt_row.grid(row=r, column=1, columnspan=2, sticky="w", padx=(0, 16), pady=(4, 8))
        self._do_csv   = tk.BooleanVar(value=True)
        self._do_excel = tk.BooleanVar(value=True)
        self._do_html  = tk.BooleanVar(value=True)
        ttk.Checkbutton(fmt_row, text="CSV",                     variable=self._do_csv).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(fmt_row, text="Excel (with thumbnails)", variable=self._do_excel).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(fmt_row, text="HTML (view in browser)",  variable=self._do_html).pack(side="left")

    def _on_rows_configure(self, event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── Config ─────────────────────────────────────────────────────────────────

    def _restore_saved(self):
        if "api_key" in self._config:
            self.key_var.set(self._config["api_key"])
        if "output_dir" in self._config:
            self.outdir_var.set(self._config["output_dir"])

    # ── File list helpers ──────────────────────────────────────────────────────

    def _browse_images(self):
        paths = filedialog.askopenfilenames(
            title="Select image files — hold Ctrl or Shift to pick multiple",
            filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.gif *.webp *.bmp *.tiff"),
                ("All files", "*.*"),
            ],
        )
        if paths:
            for p in paths:
                if p not in self._file_paths:
                    self._file_paths.append(p)
                    self._file_listbox.insert("end", os.path.basename(p))
            self._file_listbox.see("end")
            self.url_var.set("")

    def _remove_selected_files(self):
        for i in reversed(self._file_listbox.curselection()):
            self._file_listbox.delete(i)
            del self._file_paths[i]

    def _clear_files(self):
        self._file_paths.clear()
        self._file_listbox.delete(0, "end")

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

    # ── Results display ────────────────────────────────────────────────────────

    def _show_results(self, results: list):
        self._results = results
        self._result_vars = []

        for w in self._rows_frame.winfo_children():
            w.destroy()

        if not results:
            self._result_count_label.config(text="No results")
            self._export_btn.config(state="disabled")
            return

        has_source = any(r.get("source_image") for r in results)
        n_cols = 6 if has_source else 5

        self._rows_frame.columnconfigure(2, weight=1)
        self._rows_frame.columnconfigure(3, weight=2)

        ttk.Label(self._rows_frame, text="",      width=2).grid(row=0, column=0, padx=(4, 0))
        ttk.Label(self._rows_frame, text="#",     width=3, style="ColHeader.TLabel").grid(row=0, column=1, sticky="w", padx=4)
        ttk.Label(self._rows_frame, text="Title",          style="ColHeader.TLabel").grid(row=0, column=2, sticky="w", padx=4)
        ttk.Label(self._rows_frame, text="URL",            style="ColHeader.TLabel").grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(self._rows_frame, text="Source",         style="ColHeader.TLabel").grid(row=0, column=4, sticky="w", padx=4)
        if has_source:
            ttk.Label(self._rows_frame, text="From", style="ColHeader.TLabel").grid(row=0, column=5, sticky="w", padx=4)

        ttk.Separator(self._rows_frame, orient="horizontal").grid(
            row=1, column=0, columnspan=n_cols, sticky="ew", pady=2
        )

        for i, result in enumerate(results):
            row = i + 2

            var = tk.BooleanVar(value=True)
            self._result_vars.append(var)

            cb = ttk.Checkbutton(self._rows_frame, variable=var,
                                 command=self._update_result_count)
            cb.grid(row=row, column=0, padx=(6, 0), pady=1, sticky="w")
            cb.bind("<MouseWheel>", self._on_mousewheel)

            ttk.Label(self._rows_frame, text=str(i + 1), foreground="#888", width=3).grid(
                row=row, column=1, sticky="w", padx=4, pady=1
            )

            title = result.get("title", "") or "—"
            if len(title) > 60:
                title = title[:57] + "…"
            t = ttk.Label(self._rows_frame, text=title, anchor="w")
            t.grid(row=row, column=2, sticky="ew", padx=4, pady=1)
            t.bind("<MouseWheel>", self._on_mousewheel)

            url = result.get("url", "") or "—"
            url_d = url if len(url) <= 80 else url[:77] + "…"
            u = ttk.Label(self._rows_frame, text=url_d, foreground="#1a0dab", anchor="w")
            u.grid(row=row, column=3, sticky="ew", padx=4, pady=1)
            u.bind("<MouseWheel>", self._on_mousewheel)

            source = result.get("source", "") or "—"
            if len(source) > 25:
                source = source[:22] + "…"
            s = ttk.Label(self._rows_frame, text=source, foreground="#555", width=20)
            s.grid(row=row, column=4, sticky="w", padx=(4, 8), pady=1)
            s.bind("<MouseWheel>", self._on_mousewheel)

            if has_source:
                ft = result.get("source_image", "") or "—"
                if len(ft) > 28:
                    ft = ft[:25] + "…"
                f = ttk.Label(self._rows_frame, text=ft, foreground="#555", width=24)
                f.grid(row=row, column=5, sticky="w", padx=(4, 8), pady=1)
                f.bind("<MouseWheel>", self._on_mousewheel)

        self._update_result_count()
        self._export_btn.config(state="normal")

    def _select_all(self):
        for var in self._result_vars:
            var.set(True)
        self._update_result_count()

    def _select_none(self):
        for var in self._result_vars:
            var.set(False)
        self._update_result_count()

    def _update_result_count(self):
        if not self._result_vars:
            self._result_count_label.config(text="No results yet")
            return
        n     = sum(1 for v in self._result_vars if v.get())
        total = len(self._result_vars)
        self._result_count_label.config(text=f"{n} of {total} selected")

    # ── Search ─────────────────────────────────────────────────────────────────

    def _run_search(self):
        url     = self.url_var.get().strip()
        files   = self._file_paths.copy()
        api_key = self.key_var.get().strip()

        if not url and not files:
            messagebox.showerror("Missing input", "Please paste an image URL or add local files.")
            return
        if url and files:
            messagebox.showerror("Ambiguous input", "Please use a URL or local files — not both.")
            return
        if not api_key:
            messagebox.showerror("Missing API key", "Please enter your SerpAPI key.")
            return

        if self._save_key.get():
            self._config["api_key"] = api_key
            save_config(self._config)

        self._clear_log()
        self._search_btn.config(state="disabled")
        self._export_btn.config(state="disabled")
        self._open_btn.config(state="disabled")

        for w in self._rows_frame.winfo_children():
            w.destroy()
        self._results = []
        self._result_vars = []
        n = len(files) if files else 1
        self._result_count_label.config(
            text=f"Searching {n} image{'s' if n > 1 else ''}…"
        )

        threading.Thread(
            target=self._worker,
            args=(url or None, files or None, api_key),
            daemon=True,
        ).start()

    def _worker(self, url, files, api_key):
        if url:
            sources = [("url", url)]
            self._source_label = url
        else:
            sources = [("file", f) for f in files]
            self._source_label = ", ".join(os.path.basename(f) for f in files)

        multi = len(sources) > 1
        all_results = []

        try:
            for i, (kind, src) in enumerate(sources, 1):
                label  = os.path.basename(src) if kind == "file" else src
                prefix = f"[{i}/{len(sources)}] " if multi else ""
                self.after(0, self._log, f"{prefix}Searching: {label}")

                data    = upload_and_search(src, api_key) if kind == "file" \
                          else search_reverse_image(src, api_key)
                results = parse_results(data)

                if multi:
                    for res in results:
                        res["source_image"] = os.path.basename(src)

                all_results.extend(results)
                self.after(0, self._log,
                           f"  → {len(results)} result{'s' if len(results) != 1 else ''}")

            if not all_results:
                self.after(0, self._log, "\nNo results returned.")
                self.after(0, lambda: self._result_count_label.config(text="No results"))
                return

            total = len(all_results)
            msg   = f"\nFound {total} result{'s' if total != 1 else ''}"
            if multi:
                msg += f" across {len(sources)} images"
            msg += ". Tick the ones you want, then click 'Export Selected'."
            self.after(0, self._log, msg)
            self.after(0, self._show_results, all_results)

        except Exception as e:
            self.after(0, self._log, f"\nError: {e}")
            self.after(0, self._log, traceback.format_exc())
            self.after(0, lambda: self._result_count_label.config(text="Error — see log"))

        finally:
            self.after(0, lambda: self._search_btn.config(state="normal"))

    # ── Export ─────────────────────────────────────────────────────────────────

    def _export_selected(self):
        selected = [r for r, v in zip(self._results, self._result_vars) if v.get()]

        if not selected:
            messagebox.showerror("Nothing selected", "Please tick at least one result.")
            return

        outdir = self.outdir_var.get().strip()
        prefix = self.prefix_var.get().strip() or "results"

        if not outdir or not os.path.isdir(outdir):
            messagebox.showerror("Invalid folder", "Please choose a valid output folder.")
            return
        if not (self._do_csv.get() or self._do_excel.get() or self._do_html.get()):
            messagebox.showerror("No format", "Please select at least one export format.")
            return

        self._config["output_dir"] = outdir
        save_config(self._config)

        self._export_btn.config(state="disabled")
        self._open_btn.config(state="disabled")

        threading.Thread(
            target=self._export_worker,
            args=(selected, outdir, prefix),
            daemon=True,
        ).start()

    def _export_worker(self, results, outdir, prefix):
        try:
            out_prefix = os.path.join(outdir, prefix)

            if self._do_csv.get():
                path = f"{out_prefix}.csv"
                export_csv(results, path)
                self.after(0, self._log, f"CSV saved:   {path}")

            if self._do_excel.get():
                self.after(0, self._log,
                           "Downloading thumbnails for Excel — this may take 30–60 s…")
                path = f"{out_prefix}.xlsx"
                export_excel(results, path)
                self.after(0, self._log, f"Excel saved: {path}")

            if self._do_html.get():
                path = f"{out_prefix}.html"
                export_html(results, path, self._source_label)
                self.after(0, self._log, f"HTML saved:  {path}")

            self.after(0, self._log,
                       f"\nExported {len(results)} results. "
                       "Click 'Open results folder' to view.")
            self.after(0, lambda: self._open_btn.config(state="normal"))

        except Exception as e:
            self.after(0, self._log, f"\nExport error: {e}")
            self.after(0, self._log, traceback.format_exc())

        finally:
            self.after(0, lambda: self._export_btn.config(state="normal"))


if __name__ == "__main__":
    App().mainloop()

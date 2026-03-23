#!/usr/bin/env python3
"""
Auction Intel — 6-Stream Batch Runner (Tkinter GUI)

Control panel for running up to 6 parallel search streams via the API.
Each stream uses a different admin API key → routes to its own Poe bot.

Usage:
  1. Paste API keys into the key fields (one per stream)
  2. Load a domains file (one domain per line)
  3. Click "Split & Launch" — domains are divided evenly across active streams
  4. Watch real-time progress for each stream
"""

import json
import os
import random
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

import requests

API_BASE = "https://auctionintel.app"
CONFIG_FILE = "batch_runner_config.json"

STREAM_LABELS = ["blake", "blake1", "blake2", "blake3", "blake4", "blake5"]
STREAM_COLORS = ["#22c55e", "#3b82f6", "#f59e0b", "#ef4444", "#a855f7", "#06b6d4"]


def load_config():
    """Load saved API keys from config file."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"api_keys": [""] * 6}


def save_config(api_keys):
    """Save API keys to config file."""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"api_keys": api_keys}, f, indent=2)
    except Exception as e:
        print(f"Failed to save config: {e}")


class StreamPanel(ttk.LabelFrame):
    """One panel per stream showing progress."""

    def __init__(self, parent, index, **kwargs):
        label = f"  {STREAM_LABELS[index]}@  "
        super().__init__(parent, text=label, **kwargs)
        self.index = index
        self.job_id = None
        self.api_key = None
        self.running = False

        # API key entry
        key_frame = ttk.Frame(self)
        key_frame.pack(fill="x", padx=5, pady=(5, 2))
        ttk.Label(key_frame, text="API Key:").pack(side="left")
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(key_frame, textvariable=self.key_var, width=28, show="*")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # EQ bars (music visualizer style)
        self.eq_canvas = tk.Canvas(self, height=40, bg="#0a0a0a", highlightthickness=0)
        self.eq_canvas.pack(fill="x", padx=5, pady=2)
        self.eq_bars = []
        self.eq_heights = [0] * 8
        bar_width = 8
        gap = 4
        for i in range(8):
            x = i * (bar_width + gap) + gap
            bar = self.eq_canvas.create_rectangle(
                x, 40, x + bar_width, 40,
                fill=STREAM_COLORS[index % len(STREAM_COLORS)],
                outline=""
            )
            self.eq_bars.append(bar)
        self.eq_animating = False

        # Progress bar
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(self, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", padx=5, pady=2)

        # Stats
        stats_frame = ttk.Frame(self)
        stats_frame.pack(fill="x", padx=5, pady=2)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(stats_frame, textvariable=self.status_var, foreground="#888").pack(side="left")

        self.count_var = tk.StringVar(value="")
        ttk.Label(stats_frame, textvariable=self.count_var).pack(side="right")

        # Decision maker count
        self.dm_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.dm_var, foreground="#22c55e", font=("Segoe UI", 9, "bold")).pack(padx=5, pady=(0, 2))

        # Domains assigned
        self.domains_var = tk.StringVar(value="0 domains")
        ttk.Label(self, textvariable=self.domains_var, foreground="#666").pack(padx=5, pady=(0, 2))

        # Stop button
        self.stop_btn = ttk.Button(self, text="Stop", command=self.stop_job, state="disabled")
        self.stop_btn.pack(padx=5, pady=(0, 5))

    def start_job(self, domains):
        """Submit search via API and start polling."""
        key = self.key_var.get().strip()
        if not key:
            return
        self.api_key = key
        self.running = True
        self.stop_btn.config(state="normal")
        self.status_var.set("Submitting...")
        self.progress_var.set(0)
        self.count_var.set("")
        self.domains_var.set(f"{len(domains)} domains")

        # Start EQ animation
        self.eq_animating = True
        threading.Thread(target=self._animate_eq, daemon=True).start()

        threading.Thread(target=self._run, args=(domains,), daemon=True).start()

    def _animate_eq(self):
        """Animate EQ bars while job is running."""
        while self.eq_animating:
            for i, bar in enumerate(self.eq_bars):
                # Random height between 5 and 35
                target_height = random.randint(5, 35)
                # Smooth transition
                self.eq_heights[i] += (target_height - self.eq_heights[i]) * 0.3
                y_top = 40 - self.eq_heights[i]
                x1, _, x2, _ = self.eq_canvas.coords(bar)
                self.eq_canvas.coords(bar, x1, y_top, x2, 40)
            time.sleep(0.05)
        # Reset bars to 0 when stopped
        for bar in self.eq_bars:
            x1, _, x2, _ = self.eq_canvas.coords(bar)
            self.eq_canvas.coords(bar, x1, 40, x2, 40)

    def _run(self, domains):
        """Submit job and poll status."""
        try:
            resp = requests.post(
                f"{API_BASE}/api/v1/search",
                json={"domains": domains},
                headers={"X-API-Key": self.api_key},
                timeout=30,
            )
            if resp.status_code != 200:
                self.status_var.set(f"Error: {resp.status_code} — {resp.text[:80]}")
                self.running = False
                self.eq_animating = False
                self.stop_btn.config(state="disabled")
                return

            data = resp.json()
            self.job_id = data["job_id"]
            total = data["total_domains"]
            self.status_var.set(f"Running — {self.job_id}")

            # Poll status
            while self.running:
                time.sleep(5)
                try:
                    sr = requests.get(
                        f"{API_BASE}/api/v1/status/{self.job_id}",
                        headers={"X-API-Key": self.api_key},
                        timeout=15,
                    )
                    if sr.status_code != 200:
                        continue
                    sd = sr.json()
                    processed = sd.get("processed", 0)
                    found = sd.get("found", 0)
                    dm = sd.get("decision_makers", 0)
                    status = sd.get("status", "unknown")
                    eta = sd.get("eta_seconds")

                    pct = (processed / total * 100) if total > 0 else 0
                    self.progress_var.set(pct)
                    self.count_var.set(f"{processed}/{total}  |  {found} found")
                    if dm > 0:
                        self.dm_var.set(f">> {dm} Decision Makers")

                    eta_str = ""
                    if eta and eta > 0:
                        mins = int(eta // 60)
                        secs = int(eta % 60)
                        eta_str = f" — ~{mins}m {secs}s left"

                    if status == "complete":
                        self.status_var.set(f"Complete — {found} found")
                        self.progress_var.set(100)
                        self.running = False
                        self.eq_animating = False
                        self.stop_btn.config(state="disabled")
                        break
                    elif status == "error":
                        self.status_var.set(f"Error — {processed}/{total} processed, {found} found")
                        self.running = False
                        self.eq_animating = False
                        self.stop_btn.config(state="disabled")
                        break
                    else:
                        self.status_var.set(f"Running{eta_str}")

                except Exception as e:
                    self.status_var.set(f"Poll error: {e}")

        except Exception as e:
            self.status_var.set(f"Submit error: {e}")
            self.running = False
            self.eq_animating = False
            self.stop_btn.config(state="disabled")

    def stop_job(self):
        """Stop running job."""
        if self.job_id and self.api_key:
            self.running = False
            self.eq_animating = False
            self.stop_btn.config(state="disabled")
            self.status_var.set("Stopping...")
            try:
                requests.post(
                    f"{API_BASE}/api/v1/stop/{self.job_id}",
                    headers={"X-API-Key": self.api_key},
                    timeout=10,
                )
                self.status_var.set("Stopped")
            except Exception as e:
                self.status_var.set(f"Stop error: {e}")


class BatchRunner(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Auction Intel — Batch Runner")
        self.geometry("1100x700")
        self.configure(bg="#1a1a2e")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background="#1a1a2e", foreground="#e0e0e0")
        style.configure("TLabelframe", background="#16213e", foreground="#fbbf24")
        style.configure("TLabelframe.Label", background="#16213e", foreground="#fbbf24", font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#16213e", foreground="#e0e0e0")
        style.configure("TButton", background="#334155", foreground="#e0e0e0")
        style.configure("TEntry", fieldbackground="#0f172a", foreground="#e0e0e0")
        style.configure("TFrame", background="#16213e")
        style.configure("Horizontal.TProgressbar", background="#fbbf24", troughcolor="#0f172a")

        # Top controls
        ctrl_frame = ttk.Frame(self)
        ctrl_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(ctrl_frame, text="Domains File:", font=("Segoe UI", 10)).pack(side="left")
        self.file_var = tk.StringVar(value="No file loaded")
        ttk.Label(ctrl_frame, textvariable=self.file_var).pack(side="left", padx=(5, 10))
        ttk.Button(ctrl_frame, text="Load File", command=self.load_file).pack(side="left", padx=2)

        self.domain_count_var = tk.StringVar(value="")
        ttk.Label(ctrl_frame, textvariable=self.domain_count_var, foreground="#fbbf24").pack(side="left", padx=10)

        ttk.Button(ctrl_frame, text="Split & Launch All", command=self.launch_all).pack(side="right", padx=2)
        ttk.Button(ctrl_frame, text="Stop All", command=self.stop_all).pack(side="right", padx=2)

        # Exclude file
        excl_frame = ttk.Frame(self)
        excl_frame.pack(fill="x", padx=10, pady=(0, 5))
        ttk.Label(excl_frame, text="Exclude File (optional):", font=("Segoe UI", 9)).pack(side="left")
        self.exclude_var = tk.StringVar(value="")
        ttk.Label(excl_frame, textvariable=self.exclude_var).pack(side="left", padx=(5, 10))
        ttk.Button(excl_frame, text="Load Exclude", command=self.load_exclude).pack(side="left", padx=2)
        self.exclude_count_var = tk.StringVar(value="")
        ttk.Label(excl_frame, textvariable=self.exclude_count_var, foreground="#ef4444").pack(side="left", padx=10)

        # Stream panels — 2 rows x 3 columns
        grid_frame = ttk.Frame(self)
        grid_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.streams = []
        for i in range(6):
            row, col = divmod(i, 3)
            panel = StreamPanel(grid_frame, i)
            panel.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")
            # Add callback to save keys when changed
            panel.key_var.trace_add("write", lambda *args: self.save_api_keys())
            self.streams.append(panel)

        for c in range(3):
            grid_frame.columnconfigure(c, weight=1)
        for r in range(2):
            grid_frame.rowconfigure(r, weight=1)

        # Summary bar
        self.summary_var = tk.StringVar(value="Ready")
        summary_label = ttk.Label(self, textvariable=self.summary_var, font=("Segoe UI", 10, "bold"), foreground="#fbbf24")
        summary_label.pack(pady=(5, 10))

        self.domains = []
        self.exclude_domains = set()

        # Load saved API keys
        self.load_api_keys()

    def load_api_keys(self):
        """Load saved API keys from config file."""
        config = load_config()
        api_keys = config.get("api_keys", [""] * 6)
        for i, key in enumerate(api_keys[:6]):
            if i < len(self.streams):
                self.streams[i].key_var.set(key)

    def save_api_keys(self):
        """Save current API keys to config file."""
        api_keys = [s.key_var.get() for s in self.streams]
        save_config(api_keys)

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv"), ("All", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        lines = [l.strip() for l in raw.replace(",", "\n").split("\n") if l.strip()]
        # Basic domain cleanup
        cleaned = []
        for line in lines:
            d = line.lower().replace("http://", "").replace("https://", "").strip("/").strip()
            if d and "." in d:
                cleaned.append(d)
        self.domains = cleaned
        self.file_var.set(os.path.basename(path))
        self._update_counts()

    def load_exclude(self):
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv"), ("All", "*.*")])
        if not path:
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        lines = [l.strip().lower().replace("http://", "").replace("https://", "").strip("/") for l in raw.replace(",", "\n").split("\n") if l.strip()]
        self.exclude_domains = set(lines)
        self.exclude_var.set(os.path.basename(path))
        self._update_counts()

    def _update_counts(self):
        total = len(self.domains)
        excluded = sum(1 for d in self.domains if d in self.exclude_domains)
        remaining = total - excluded
        self.domain_count_var.set(f"{total} domains loaded")
        if excluded > 0:
            self.exclude_count_var.set(f"{excluded} excluded → {remaining} remaining")
        else:
            self.exclude_count_var.set("")

    def launch_all(self):
        # Filter excluded domains
        domains = [d for d in self.domains if d not in self.exclude_domains]
        if not domains:
            messagebox.showwarning("No Domains", "Load a domains file first.")
            return

        # Find streams with API keys
        active = [s for s in self.streams if s.key_var.get().strip()]
        if not active:
            messagebox.showwarning("No API Keys", "Enter at least one API key.")
            return

        # Split domains evenly across active streams
        n = len(active)
        chunk_size = len(domains) // n
        remainder = len(domains) % n

        start = 0
        for i, stream in enumerate(active):
            size = chunk_size + (1 if i < remainder else 0)
            batch = domains[start:start + size]
            start += size
            stream.start_job(batch)

        self.summary_var.set(f"Launched {n} streams — {len(domains)} domains total")

        # Start summary updater
        threading.Thread(target=self._update_summary, daemon=True).start()

    def _update_summary(self):
        total_found = 0
        total_dm = 0
        while any(s.running for s in self.streams):
            time.sleep(3)
            total_found = 0
            total_processed = 0
            total_domains = 0
            total_dm = 0
            for s in self.streams:
                count_text = s.count_var.get()
                if "/" in count_text:
                    try:
                        parts = count_text.split("|")
                        prog = parts[0].strip().split("/")
                        total_processed += int(prog[0])
                        total_domains += int(prog[1])
                        if len(parts) > 1:
                            found_part = parts[1].strip().split()[0]
                            total_found += int(found_part)
                    except (ValueError, IndexError):
                        pass
                dm_text = s.dm_var.get()
                if dm_text:
                    try:
                        total_dm += int(dm_text.split()[1])
                    except (ValueError, IndexError):
                        pass

            self.summary_var.set(f"Total: {total_processed}/{total_domains} processed — {total_found} found — {total_dm} Decision Makers")

        self.summary_var.set(f"All streams finished — {total_found} found — {total_dm} Decision Makers")

    def stop_all(self):
        for s in self.streams:
            if s.running:
                s.stop_job()
        self.summary_var.set("All streams stopped")


if __name__ == "__main__":
    app = BatchRunner()
    app.mainloop()

"""TPMS dashboard simulator and live monitoring UI.

Features:
- Connects to a serial TPMS source at 115200 baud using pyserial.
- Displays four wheel cards with live pressure/temperature/status.
- Shows a live alert banner and event log.
- Includes live matplotlib charts for pressure and temperature trends.
- Includes a car diagram, gauges, and bar chart in a dark theme.
- Supplies a demo mode that simulates realistic data without hardware.

Install dependencies:
    pip install pyserial matplotlib

Run:
    python tpms_dashboard.py
"""

import csv
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime

import serial
from serial.tools import list_ports
import tkinter as tk
from tkinter import ttk, filedialog

try:
    import winsound
except ImportError:  # pragma: no cover
    winsound = None

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import Rectangle, Wedge


NODE_NAMES = ["FL", "FR", "RL", "RR"]
STATUS_COLORS = {
    "NORMAL": "#2ecc71",
    "LOW": "#f39c12",
    "HIGH": "#e74c3c",
    "OVERHEAT": "#e74c3c",
    "NO SIGNAL": "#7f8c8d",
}
STATUS_TEXT = {
    "NORMAL": "NORMAL",
    "LOW": "LOW",
    "HIGH": "HIGH",
    "OVERHEAT": "OVERHEAT",
    "NO SIGNAL": "NO SIGNAL",
}

MAX_LOG_ENTRIES = 50
MAX_HISTORY = 800
LOW_THRESHOLD = 1.5
HIGH_THRESHOLD = 4.5
TEMP_OVERHEAT = 45.0


class TPMSDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TPMS Dashboard")
        self.geometry("1200x820")
        self.minsize(900, 650)
        self.configure(bg="#0b1220")

        # Runtime state.
        self.serial_lock = threading.Lock()
        self.serial_conn = None
        self.serial_thread = None
        self.serial_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.connected = False
        self.demo_mode = tk.BooleanVar(value=False)
        self.muted = tk.BooleanVar(value=False)
        self.paused = tk.BooleanVar(value=False)
        self.window_seconds = tk.IntVar(value=60)
        self.window_options = [30, 60, 300]

        self.state = {node: {"pressure": None, "temp": None, "status": "NO SIGNAL", "timestamp": None, "last_status": None, "last_seen": 0.0} for node in NODE_NAMES}
        self.pressure_history = {node: deque(maxlen=MAX_HISTORY) for node in NODE_NAMES}
        self.temp_history = deque(maxlen=MAX_HISTORY)
        self.sample_history = deque(maxlen=10000)
        self.event_log = deque(maxlen=MAX_LOG_ENTRIES)
        self.alert_signature = ()

        self.configure_theme()
        self.build_ui()
        self.update_port_list()
        self.bind("<Configure>", self.handle_resize, add="+")
        self.after(150, self.poll_serial_queue)
        self.after(400, self.demo_tick)
        self.after(250, self.watchdog_tick)

    def configure_theme(self):
        # Dark modern look with sans-serif styling.
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#0b1220")
        style.configure("TLabel", background="#0b1220", foreground="#e5edf7", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10, "bold"))
        style.configure("TCheckbutton", background="#0b1220", foreground="#e5edf7", font=("Segoe UI", 10))
        style.configure("TCombobox", fieldbackground="#111827", background="#111827", foreground="#e5edf7")

    def build_ui(self):
        self.header = tk.Frame(self, bg="#111827", padx=12, pady=10)
        self.header.pack(fill="x")

        self.title_label = tk.Label(self.header, text="TPMS Dashboard", bg="#111827", fg="#f8fafc",
                                    font=("Segoe UI", 20, "bold"))
        self.title_label.pack(side="left", padx=(0, 18))

        self.port_label = tk.Label(self.header, text="COM Port", bg="#111827", fg="#dfe7f3", font=("Segoe UI", 11))
        self.port_label.pack(side="left")

        self.port_combo = ttk.Combobox(self.header, width=15, state="readonly", justify="center")
        self.port_combo.pack(side="left", padx=(8, 12))

        self.connect_btn = ttk.Button(self.header, text="Connect", command=self.toggle_connection)
        self.connect_btn.pack(side="left", padx=(0, 12))

        self.status_dot = tk.Label(self.header, text="●", font=("Segoe UI", 18), fg="#d11a2a", bg="#111827")
        self.status_dot.pack(side="left", padx=(0, 8))

        self.demo_checkbox = ttk.Checkbutton(self.header, text="Demo mode", variable=self.demo_mode, command=self.toggle_demo_mode)
        self.demo_checkbox.pack(side="left", padx=(12, 0))

        self.mute_checkbox = ttk.Checkbutton(self.header, text="Mute", variable=self.muted)
        self.mute_checkbox.pack(side="left", padx=(12, 0))

        # Content area.
        self.main = ttk.Frame(self)
        self.main.pack(fill="both", expand=True, padx=12, pady=(12, 8))

        self.banner = tk.Label(self.main, text="ALL SYSTEMS NORMAL", bg="#1f8f4e", fg="white",
                               font=("Segoe UI", 14, "bold"), pady=10, anchor="center")
        self.banner.pack(fill="x", pady=(0, 12))

        self.cards_frame = tk.Frame(self.main, bg="#0b1220")
        self.cards_frame.pack(fill="x")

        self.card_widgets = {}
        for idx, node in enumerate(NODE_NAMES):
            card = tk.Frame(self.cards_frame, bg="#111827", bd=1, relief="flat")
            card.grid(row=idx // 2, column=idx % 2, padx=10, pady=10, sticky="nsew")
            self.cards_frame.grid_columnconfigure(idx % 2, weight=1)
            self.cards_frame.grid_rowconfigure(idx // 2, weight=1)
            self.card_widgets[node] = self.create_tire_card(card, node)

        self.notebook = ttk.Notebook(self.main)
        self.notebook.pack(fill="both", expand=True, pady=(12, 0))

        self.overview_tab = ttk.Frame(self.notebook)
        self.live_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.overview_tab, text="Overview")
        self.notebook.add(self.live_tab, text="Live Data")

        # Overview tab contains a compact summary of the car plus charts.
        self.overview_summary = tk.Label(self.overview_tab, text="", bg="#0b1220", fg="#e5edf7", justify="left",
                                         font=("Segoe UI", 10))
        self.overview_summary.pack(fill="both", expand=True, padx=12, pady=12)

        # Live Data tab with two matplotlib canvases.
        self.live_top = tk.Frame(self.live_tab, bg="#0b1220")
        self.live_top.pack(fill="both", expand=True)

        self.chart_figure = Figure(figsize=(10, 5), dpi=100, facecolor="#0b1220")
        self.chart_ax1 = self.chart_figure.add_subplot(211)
        self.chart_ax2 = self.chart_figure.add_subplot(212)
        self.chart_canvas = FigureCanvasTkAgg(self.chart_figure, master=self.live_top)
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(10, 5))

        self.visual_figure = Figure(figsize=(10, 5), dpi=100, facecolor="#0b1220")
        self.visual_ax = self.visual_figure.add_subplot(111)
        self.visual_canvas = FigureCanvasTkAgg(self.visual_figure, master=self.live_top)
        self.visual_canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=(5, 10))

        self.create_chart_line_art()

        # Bottom log and controls.
        self.footer = tk.Frame(self, bg="#0b1220")
        self.footer.pack(fill="x", padx=12, pady=(0, 12))

        self.control_bar = tk.Frame(self.footer, bg="#0b1220")
        self.control_bar.pack(fill="x")

        self.pause_btn = ttk.Button(self.control_bar, text="Pause", command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=(0, 8))

        self.clear_btn = ttk.Button(self.control_bar, text="Clear history", command=self.clear_history)
        self.clear_btn.pack(side="left", padx=(0, 8))

        self.window_combo = ttk.Combobox(self.control_bar, values=["30 s", "60 s", "5 min"], state="readonly", width=8)
        self.window_combo.set("60 s")
        self.window_combo.bind("<<ComboboxSelected>>", self.on_window_change)
        self.window_combo.pack(side="left", padx=(0, 8))

        self.export_btn = ttk.Button(self.control_bar, text="Export CSV", command=self.export_csv)
        self.export_btn.pack(side="left")

        self.log_frame = tk.Frame(self.footer, bg="#111827", bd=1, relief="flat")
        self.log_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.log_label = tk.Label(self.log_frame, text="Event log", bg="#111827", fg="#dfe7f3",
                                  font=("Segoe UI", 11, "bold"), anchor="w")
        self.log_label.pack(fill="x", padx=(10, 0), pady=(8, 0))

        self.log_widget = tk.Text(self.log_frame, height=8, state="disabled", bg="#131d2d", fg="#e5edf7",
                                  insertbackground="#e5edf7", wrap="word", font=("Segoe UI", 10))
        self.log_widget.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    def create_tire_card(self, master, node):
        frame = tk.Frame(master, bg="#111827", padx=12, pady=10)
        frame.pack(fill="both", expand=True)

        self.set_widget_font(frame)

        title = tk.Label(frame, text=node, bg="#111827", fg="#e5edf7", font=("Segoe UI", 12, "bold"), anchor="w")
        title.pack(fill="x")

        pressure_value = tk.Label(frame, text="-- kPa", bg="#111827", fg="#f8fafc",
                                  font=("Segoe UI", 20, "bold"))
        pressure_value.pack(anchor="w", pady=(8, 4))

        temp_label = tk.Label(frame, text="Temp: -- C", bg="#111827", fg="#bac8dd", font=("Segoe UI", 10))
        temp_label.pack(anchor="w")

        badge = tk.Label(frame, text="NORMAL", bg=STATUS_COLORS["NORMAL"], fg="white",
                         font=("Segoe UI", 9, "bold"), padx=8, pady=4)
        badge.pack(anchor="w", pady=(10, 8))

        bar = tk.Canvas(frame, width=180, height=18, bg="#0f172a", highlightthickness=0)
        bar.pack(fill="x")

        # Build the colored channel for low / safe / high ranges.
        bar.create_rectangle(0, 0, 180, 18, fill="#1f2937", outline="")
        low_x = 180 * (LOW_THRESHOLD / 10.0)
        high_x = 180 * (HIGH_THRESHOLD / 10.0)
        bar.create_rectangle(0, 0, low_x, 18, fill="#f39c12", outline="")
        bar.create_rectangle(low_x, 0, high_x, 18, fill="#2ecc71", outline="")
        bar.create_rectangle(high_x, 0, 180, 18, fill="#e74c3c", outline="")
        bar.create_line(low_x, 0, low_x, 18, fill="white", width=2)
        bar.create_line(high_x, 0, high_x, 18, fill="white", width=2)
        bar.create_rectangle(0, 0, 0, 18, fill="#f8fafc", outline="")

        values = {
            "title": title,
            "pressure": pressure_value,
            "temp": temp_label,
            "badge": badge,
            "bar": bar,
            "fill": None,
        }
        return values

    def set_widget_font(self, frame):
        # Keep all widgets in this card consistently styled.
        for child in frame.winfo_children():
            if isinstance(child, tk.Label):
                child.configure(font=("Segoe UI", 10))

    def toggle_connection(self):
        if self.connected:
            self.disconnect_serial()
            return
        port = self.port_combo.get().strip()
        if not port:
            self.update_status_message("Please select a COM port.")
            return
        try:
            self.serial_conn = serial.Serial(port, 115200, timeout=0.1)
            self.connected = True
            self.stop_event.clear()
            self.start_serial_reader()
            self.status_dot.configure(fg="#1ccf60")
            self.connect_btn.configure(text="Disconnect")
            self.update_status_message(f"Connected to {port}")
        except Exception as exc:  # pragma: no cover
            self.connected = False
            self.status_dot.configure(fg="#d11a2a")
            self.update_status_message(f"Connection failed: {exc}")

    def disconnect_serial(self):
        self.connected = False
        self.stop_event.set()
        if self.serial_conn is not None:
            try:
                self.serial_conn.close()
            except Exception:
                pass
            self.serial_conn = None
        self.connect_btn.configure(text="Connect")
        self.status_dot.configure(fg="#d11a2a")

    def update_status_message(self, msg):
        self.overview_summary.configure(text=msg)

    def start_serial_reader(self):
        if self.serial_thread is not None and self.serial_thread.is_alive():
            return
        self.serial_thread = threading.Thread(target=self.serial_worker, daemon=True)
        self.serial_thread.start()

    def serial_worker(self):
        while not self.stop_event.is_set():
            if self.serial_conn is None or not self.serial_conn.is_open:
                time.sleep(0.05)
                continue
            try:
                line = self.serial_conn.readline()
                if not line:
                    time.sleep(0.02)
                    continue
                decoded = line.decode("utf-8", errors="ignore").strip()
                if decoded:
                    self.serial_queue.put(decoded)
            except (serial.SerialException, OSError):
                break
            except Exception:
                continue

    def poll_serial_queue(self):
        while not self.serial_queue.empty():
            try:
                line = self.serial_queue.get_nowait()
                self.process_serial_line(line)
            except queue.Empty:
                break

        self.refresh_cards()
        self.update_alert_banner()
        self.update_event_log_widget()
        self.update_live_charts()
        self.after(120, self.poll_serial_queue)

    def process_serial_line(self, line):
        try:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 4:
                return
            node, pressure_txt, temp_txt, status_txt = parts
            node = node.upper()
            if node not in NODE_NAMES:
                return
            pressure = float(pressure_txt)
            temp = float(temp_txt)
            status = status_txt.upper()
            if status not in STATUS_TEXT:
                status = self.compute_status_from_values(pressure, temp)
            else:
                status = self.compute_status_from_values(pressure, temp, status)

            self.update_wheel_state(node, pressure, temp, status)
            ts = datetime.now().strftime("%H:%M:%S")
            self.sample_history.append((ts, node, pressure, temp, status))
        except Exception:
            # Ignore malformed lines without crashing the UI.
            return

    def compute_status_from_values(self, pressure, temp, explicit_status=None):
        if explicit_status in STATUS_TEXT and explicit_status != "NO SIGNAL":
            if explicit_status == "OVERHEAT" or temp > TEMP_OVERHEAT:
                return "OVERHEAT"
            if explicit_status == "LOW" or pressure < LOW_THRESHOLD:
                return "LOW"
            if explicit_status == "HIGH" or pressure > HIGH_THRESHOLD:
                return "HIGH"
            return "NORMAL"
        if temp > TEMP_OVERHEAT:
            return "OVERHEAT"
        if pressure < LOW_THRESHOLD:
            return "LOW"
        if pressure > HIGH_THRESHOLD:
            return "HIGH"
        return "NORMAL"

    def update_wheel_state(self, node, pressure, temp, status):
        now = time.monotonic()
        previous = self.state[node]["status"]
        self.state[node].update({
            "pressure": pressure,
            "temp": temp,
            "status": status,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "last_seen": now,
        })

        # Keep history in a rolling buffer.
        self.pressure_history[node].append((time.time(), pressure))
        self.temp_history.append((time.time(), temp))

        # Only record true status changes in the event log.
        if previous != status and previous is not None:
            self.event_log.append(f"{datetime.now().strftime('%H:%M:%S')}  {node}  {previous} -> {status}")

    def toggle_demo_mode(self):
        if self.demo_mode.get():
            self.update_status_message("Demo mode active: synthetic TPMS data running.")
        else:
            self.update_status_message("Demo mode disabled.")

    def demo_tick(self):
        if self.demo_mode.get() and not self.paused.get():
            current_time = time.time()
            temp = 26.5 + 8.5 * (1 + (0.5 * (1 + __import__("math").sin(current_time / 9))))
            temp += 10 if int(current_time) % 18 < 6 else 0
            for index, node in enumerate(NODE_NAMES):
                phase = current_time / (6.5 + index)
                base_pressure = 3.2 + 0.6 * __import__("math").sin(phase)
                if node == "FL":
                    base_pressure -= 0.09 * ((current_time * 1.3) % 30) / 30.0
                    base_pressure = max(0.5, base_pressure)
                elif node == "FR":
                    base_pressure += 0.15 * __import__("math").sin(phase * 1.7)
                elif node == "RL":
                    base_pressure += 0.2 * __import__("math").cos(phase * 1.4)
                else:
                    base_pressure += 0.1 * __import__("math").sin(phase * 1.2)
                pressure = max(0.2, min(9.8, base_pressure))
                status = self.compute_status_from_values(pressure, temp)
                self.update_wheel_state(node, round(pressure, 2), round(temp, 2), status)
                self.sample_history.append((datetime.now().strftime("%H:%M:%S"), node, round(pressure, 2), round(temp, 2), status))

            # Touch a shared temp signal and force a temp spike.
            self.temp_history.append((current_time, round(temp, 2)))

        self.after(600, self.demo_tick)

    def watchdog_tick(self):
        # If a wheel has not reported data in 3 seconds, mark it NO SIGNAL.
        now_monotonic = time.monotonic()
        for node in NODE_NAMES:
            last_seen = self.state[node]["last_seen"]
            if (now_monotonic - last_seen) > 3.0 and self.state[node]["status"] != "NO SIGNAL":
                self.state[node]["status"] = "NO SIGNAL"
                self.event_log.append(f"{datetime.now().strftime('%H:%M:%S')}  {node}  {self.state[node]['status']} -> NO SIGNAL")
                self.state[node]["timestamp"] = datetime.now().strftime("%H:%M:%S")
        self.after(300, self.watchdog_tick)

    def refresh_cards(self):
        for node in NODE_NAMES:
            state = self.state[node]
            pressure = state["pressure"]
            temp = state["temp"]
            status = state["status"]
            if pressure is None:
                pressure_text = "-- kPa"
            else:
                pressure_text = f"{pressure:.2f} kPa"
            if temp is None:
                temp_text = "Temp: -- C"
            else:
                temp_text = f"Temp: {temp:.1f} C"

            card = self.card_widgets[node]
            card["pressure"].configure(text=pressure_text, fg="#f8fafc")
            card["temp"].configure(text=temp_text, fg="#bac8dd")
            card["badge"].configure(text=status, bg=STATUS_COLORS.get(status, "#7f8c8d"))

            # Update pressure bar fill based on the current value.
            canvas = card["bar"]
            bar_width = 180
            low_x = int(bar_width * (LOW_THRESHOLD / 10.0))
            high_x = int(bar_width * (HIGH_THRESHOLD / 10.0))
            current_x = int((bar_width * min(max((pressure or 0.0), 0.0), 10.0) / 10.0)) if pressure is not None else 0

            # Remove the previous indicator.
            try:
                canvas.delete("indicator")
            except Exception:
                pass
            canvas.create_rectangle(0, 2, current_x, 16, fill="#dfe7f3", outline="", tags="indicator")

    def create_chart_line_art(self):
        # Pressure chart axes setup.
        self.chart_ax1.set_facecolor("#111827")
        self.chart_ax1.set_title("Pressure vs Time", color="#e5edf7", fontsize=11)
        self.chart_ax1.set_ylabel("Pressure (kPa)", color="#e5edf7")
        self.chart_ax1.set_ylim(0, 10)
        self.chart_ax1.set_xlim(-60, 0)
        self.chart_ax1.grid(True, color="#2a3443", linestyle="--", linewidth=0.7)
        self.chart_ax1.axhspan(LOW_THRESHOLD, HIGH_THRESHOLD, color="#78d68d", alpha=0.18)
        self.chart_ax1.axhline(LOW_THRESHOLD, color="#f6c453", linestyle="--", linewidth=1.2)
        self.chart_ax1.axhline(HIGH_THRESHOLD, color="#f28b82", linestyle="--", linewidth=1.2)
        self.chart_ax1.tick_params(axis="x", colors="#dfe7f3")
        self.chart_ax1.tick_params(axis="y", colors="#dfe7f3")
        self.chart_ax1.spines["top"].set_color("#27364c")
        self.chart_ax1.spines["right"].set_color("#27364c")
        self.chart_ax1.spines["left"].set_color("#27364c")
        self.chart_ax1.spines["bottom"].set_color("#27364c")

        self.chart_ax2.set_facecolor("#111827")
        self.chart_ax2.set_title("Temperature vs Time", color="#e5edf7", fontsize=11)
        self.chart_ax2.set_ylabel("Temperature (C)", color="#e5edf7")
        self.chart_ax2.set_xlabel("Time (s)", color="#e5edf7")
        self.chart_ax2.set_ylim(0, 80)
        self.chart_ax2.set_xlim(-60, 0)
        self.chart_ax2.grid(True, color="#2a3443", linestyle="--", linewidth=0.7)
        self.chart_ax2.axhspan(TEMP_OVERHEAT, 80, color="#ff6b6b", alpha=0.18)
        self.chart_ax2.axhline(TEMP_OVERHEAT, color="#ff6b6b", linestyle="--", linewidth=1.2)
        self.chart_ax2.tick_params(axis="x", colors="#dfe7f3")
        self.chart_ax2.tick_params(axis="y", colors="#dfe7f3")
        self.chart_ax2.spines["top"].set_color("#27364c")
        self.chart_ax2.spines["right"].set_color("#27364c")
        self.chart_ax2.spines["left"].set_color("#27364c")
        self.chart_ax2.spines["bottom"].set_color("#27364c")

        self.pressure_lines = {}
        for node, color in zip(NODE_NAMES, ["#4cc9f0", "#f72585", "#ffd166", "#56f39a"]):
            line, = self.chart_ax1.plot([], [], lw=2, color=color, label=node)
            self.pressure_lines[node] = line
        self.chart_ax1.legend(loc="upper right", facecolor="#111827", edgecolor="#111827", labelcolor="#e5edf7")

        self.temp_line, = self.chart_ax2.plot([], [], lw=2, color="#ff9966")
        self.chart_figure.tight_layout()
        self.chart_canvas.draw()

        # Visual panel: top-view car + gauges + bar chart.
        self.visual_ax.clear()
        self.visual_ax.set_facecolor("#111827")
        self.visual_ax.set_xlim(0, 10)
        self.visual_ax.set_ylim(0, 6)
        self.visual_ax.axis("off")

        # Car body.
        body = Rectangle((1.8, 1.5), 6.5, 3.0, facecolor="#1e293b", edgecolor="#cbd5e1", linewidth=2)
        self.visual_ax.add_patch(body)
        self.visual_ax.add_patch(Rectangle((2.8, 2.4), 4.4, 1.8, facecolor="#2d3748", edgecolor="#cbd5e1", linewidth=1.5))

        self.tire_patches = {}
        tire_positions = {
            "FL": (2.6, 3.6),
            "FR": (6.1, 3.6),
            "RL": (2.6, 1.6),
            "RR": (6.1, 1.6),
        }
        for node, pos in tire_positions.items():
            x, y = pos
            patch = Rectangle((x, y), 1.0, 0.8, facecolor="#7f8c8d", edgecolor="#cbd5e1", linewidth=1.5)
            self.visual_ax.add_patch(patch)
            self.tire_patches[node] = patch
            self.visual_ax.text(x + 0.5, y + 0.4, node, ha="center", va="center", color="white", fontsize=10)

        self.visual_canvas.draw()

    def update_live_charts(self):
        # Update pressure and temperature charts efficiently using set_data.
        if self.paused.get():
            return

        now = time.time()
        window = self.window_seconds.get()
        cutoff = now - window

        # Pressure lines.
        for node in NODE_NAMES:
            points = self.pressure_history[node]
            xs = [p[0] - now for p in points]
            ys = [p[1] for p in points]
            self.pressure_lines[node].set_data(xs, ys)
            self.chart_ax1.set_xlim(-window, 0)
            if points:
                self.chart_ax1.set_ylim(0, max(10.0, max(ys) + 1.0))

        # Temperature line.
        temp_points = [(p[0] - now, p[1]) for p in self.temp_history]
        ts = [p[0] for p in temp_points]
        vals = [p[1] for p in temp_points]
        self.temp_line.set_data(ts, vals)
        self.chart_ax2.set_xlim(-window, 0)
        if vals:
            self.chart_ax2.set_ylim(0, max(80.0, max(vals) + 10.0))
        self.chart_canvas.draw_idle()

        # Update car diagram, gauges, and bar chart in the second canvas.
        self.update_visual_panel()

    def update_visual_panel(self):
        self.visual_ax.clear()
        self.visual_ax.set_facecolor("#111827")
        self.visual_ax.set_xlim(0, 10)
        self.visual_ax.set_ylim(0, 6)
        self.visual_ax.axis("off")

        # Car body.
        body = Rectangle((1.8, 1.5), 6.5, 3.0, facecolor="#1e293b", edgecolor="#cbd5e1", linewidth=2)
        self.visual_ax.add_patch(body)
        self.visual_ax.add_patch(Rectangle((2.8, 2.4), 4.4, 1.8, facecolor="#2d3748", edgecolor="#cbd5e1", linewidth=1.5))

        tire_positions = {
            "FL": (2.6, 3.6),
            "FR": (6.1, 3.6),
            "RL": (2.6, 1.6),
            "RR": (6.1, 1.6),
        }
        for node, pos in tire_positions.items():
            x, y = pos
            status = self.state[node]["status"]
            color = STATUS_COLORS.get(status, "#7f8c8d")
            patch = Rectangle((x, y), 1.0, 0.8, facecolor=color, edgecolor="#cbd5e1", linewidth=1.5)
            self.visual_ax.add_patch(patch)
            pressure = self.state[node]["pressure"]
            text = "--" if pressure is None else f"{pressure:.1f}"
            self.visual_ax.text(x + 0.5, y + 0.4, text, ha="center", va="center", color="white", fontsize=10)
            self.visual_ax.text(x + 0.5, y + 0.9, node, ha="center", va="center", color="white", fontsize=9)

        # Gauges: 2x2 arrangement under the car diagram.
        gauge_positions = [(1.2, 0.2), (5.5, 0.2), (1.2, -1.5), (5.5, -1.5)]
        for idx, node in enumerate(NODE_NAMES):
            cx, cy = gauge_positions[idx]
            gauge_ax = self.visual_ax.inset_axes([cx, cy, 2.0, 1.6], transform=self.visual_ax.transData)
            gauge_ax.set_aspect("equal")
            gauge_ax.set_axis_off()
            gauge_ax.set_xlim(-1, 1)
            gauge_ax.set_ylim(-1, 1)

            gauge_ax.add_patch(Wedge((0, 0), 1.0, 180, 0, width=0.28, facecolor="#2ecc71", edgecolor="none"))
            gauge_ax.add_patch(Wedge((0, 0), 1.0, 0, -60, width=0.28, facecolor="#f39c12", edgecolor="none"))
            gauge_ax.add_patch(Wedge((0, 0), 1.0, -60, -180, width=0.28, facecolor="#e74c3c", edgecolor="none"))
            gauge_ax.add_patch(Wedge((0, 0), 0.72, 180, 0, width=0.3, facecolor="#0b1220", edgecolor="none"))

            pressure = self.state[node]["pressure"] if self.state[node]["pressure"] is not None else 0.0
            angle_deg = 180 - (pressure / 10.0) * 180.0
            theta = __import__("math").radians(angle_deg)
            x = 0.72 * __import__("math").cos(theta)
            y = 0.72 * __import__("math").sin(theta)
            gauge_ax.plot([0, x], [0, y], color="#f8fafc", linewidth=2)
            gauge_ax.add_patch(Wedge((0, 0), 0.08, 0, 360, facecolor="#f8fafc", edgecolor="none"))
            gauge_ax.text(0, -1.25, node, ha="center", va="center", color="white", fontsize=9)
            gauge_ax.text(0, 1.15, f"{pressure:.1f}", ha="center", va="center", color="white", fontsize=8)

        # Bar chart comparison.
        bar_values = [self.state[node]["pressure"] if self.state[node]["pressure"] is not None else 0.0 for node in NODE_NAMES]
        colors = [STATUS_COLORS.get(self.state[node]["status"], "#7f8c8d") for node in NODE_NAMES]
        ax_bar = self.visual_ax.inset_axes([0.15, 0.2, 7.5, 1.5], transform=self.visual_ax.transData)
        ax_bar.bar(NODE_NAMES, bar_values, color=colors, edgecolor="#e5edf7", linewidth=1)
        ax_bar.set_ylim(0, 10)
        ax_bar.set_ylabel("kPa", color="#e5edf7")
        ax_bar.set_facecolor("#111827")
        ax_bar.tick_params(axis="x", colors="#e5edf7")
        ax_bar.tick_params(axis="y", colors="#e5edf7")
        for spine in ax_bar.spines.values():
            spine.set_color("#27364c")

        self.visual_canvas.draw_idle()

    def update_alert_banner(self):
        active_faults = []
        for node in NODE_NAMES:
            status = self.state[node]["status"]
            if status != "NORMAL":
                active_faults.append((node, status))

        if not active_faults:
            self.banner.configure(text="ALL SYSTEMS NORMAL", bg="#1f8f4e", fg="white")
            self.alert_signature = ()
            return

        text = "  ".join([f"{node}: {status}" for node, status in active_faults])
        signature = tuple(active_faults)
        if signature != self.alert_signature:
            self.alert_signature = signature
            if not self.muted.get() and winsound is not None:
                try:
                    winsound.Beep(880, 180)
                except Exception:
                    pass
        self.banner.configure(text=text, bg="#b91c1c", fg="white")
        self.flash_banner()

    def flash_banner(self):
        current = self.banner.cget("bg")
        if current == "#b91c1c":
            self.banner.configure(bg="#d72626")
        else:
            self.banner.configure(bg="#b91c1c")
        self.after(500, self.flash_banner)

    def update_event_log_widget(self):
        if not self.event_log:
            return
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        for entry in list(self.event_log)[-MAX_LOG_ENTRIES:]:
            self.log_widget.insert("end", entry + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def on_window_change(self, event):
        selection = self.window_combo.get()
        if selection == "30 s":
            self.window_seconds.set(30)
        elif selection == "5 min":
            self.window_seconds.set(300)
        else:
            self.window_seconds.set(60)

    def toggle_pause(self):
        self.paused.set(not self.paused.get())
        self.pause_btn.configure(text="Resume" if self.paused.get() else "Pause")

    def clear_history(self):
        for node in NODE_NAMES:
            self.pressure_history[node].clear()
        self.temp_history.clear()
        self.sample_history.clear()
        self.event_log.clear()
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")
        self.update_alert_banner()

    def export_csv(self):
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile="tpms_log.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return
        with open(filename, "w", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["timestamp", "node", "pressure", "temp", "status"])
            for row in self.sample_history:
                writer.writerow(row)
        self.update_status_message(f"Exported CSV to {os.path.basename(filename)}")

    def update_port_list(self):
        ports = [p.device for p in list_ports.comports()]
        if not ports:
            ports = ["COM3", "COM4", "/dev/ttyACM0", "/dev/ttyUSB0"]
        self.port_combo["values"] = ports
        if ports:
            self.port_combo.set(ports[0])

    def handle_resize(self, event):
        try:
            self.chart_canvas.draw_idle()
            self.visual_canvas.draw_idle()
        except Exception:
            pass


def main():
    app = TPMSDashboard()
    app.mainloop()


if __name__ == "__main__":
    main()

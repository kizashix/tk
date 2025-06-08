import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from datetime import datetime
import hashlib
import json
import os
import threading
import time

try:
    import requests
except ImportError:  # pragma: no cover - optional dependency
    requests = None
    print("\u26a0\ufe0f 'requests' library not found. Install with: pip install requests")


# Allow overriding defaults via environment variables
DEFAULT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TARGET_CHAT_ID")
AI_PROCESSOR_API_BASE = os.getenv("AI_PROCESSOR_API_BASE", "http://localhost:1234/v1")

GROUPS_CONFIG_FILE = "telegram_groups.json"


class AIProcessor:
    """Communicate with a local LLM for extracting dosing data."""

    def __init__(self, api_base_url=AI_PROCESSOR_API_BASE):
        self.api_base_url = api_base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}

    def _get_llm_response(self, system_prompt, user_prompt):
        url = f"{self.api_base_url}/chat/completions"
        payload = {
            "model": "local-model",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        if requests is None:
            print("ERROR: 'requests' library is required for LLM communication.")
            return None
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=45)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            print(
                f"ERROR: Could not connect to LLM API at {url}. Is the server running? Error: {exc}"
            )
            return None
        except json.JSONDecodeError:
            print(
                f"ERROR: Failed to decode JSON from LLM response. Response text: {response.text}"
            )
            return None

    def extract_dosing_info(self, message_text):
        system_prompt = (
            """
You are a precision medical data extraction tool. Your task is to analyze the user's message and extract any mention of medications, dosages, frequencies, and side effects.
Respond ONLY with a JSON object with the following structure:
{
  "medication": "string or null",
  "dosage": "string (e.g., '50mg') or null",
  "frequency": "string (e.g., 'once daily') or null",
  "side_effects": ["list of strings or empty list"],
  "is_dosing_related": boolean
}
If no dosing information is present, set "is_dosing_related" to false and the other fields to null or empty.
"""
        )

        llm_response = self._get_llm_response(system_prompt, message_text)

        if not llm_response or "choices" not in llm_response or not llm_response["choices"]:
            return None

        try:
            content = llm_response["choices"][0]["message"]["content"]
            if content.startswith("```json"):
                content = content.strip("```json").strip("`").strip()
            parsed_json = json.loads(content)
            if "is_dosing_related" in parsed_json:
                return parsed_json
            return None
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"ERROR: Could not parse LLM JSON response. Error: {exc}, Response: {content}")
            return None


class TelegramGroup:
    """Configuration for a monitored Telegram group."""

    def __init__(self, name, chat_id, bot_token=None, auto_save=False):
        self.name = name
        self.chat_id = str(chat_id)
        self.bot_token = bot_token
        self.auto_save = auto_save
        self.is_monitoring = False
        self.monitor_instance = None
        self.id = hashlib.md5(f"{self.name}-{self.chat_id}".encode()).hexdigest()

    def to_dict(self):
        return {
            "name": self.name,
            "chat_id": self.chat_id,
            "bot_token": self.bot_token,
            "auto_save": self.auto_save,
        }


class TelegramChatParser:
    """Parses Telegram JSON export files."""

    def __init__(self):
        self.columns = [
            "msg_id",
            "sender",
            "sender_id",
            "reply_to_msg_id",
            "date",
            "date_unixtime",
            "msg_type",
            "msg_content",
            "forwarded_from",
            "action",
        ]

    def process_message(self, message):
        if message.get("type") != "message":
            return None

        msg_content_parts = message.get("text", "")
        if isinstance(message.get("text_entities"), list):
            msg_content_parts = "".join(part["text"] for part in message["text_entities"])

        return {
            "msg_id": message.get("id"),
            "sender": message.get("from", "Unknown Sender"),
            "sender_id": message.get("from_id", "Unknown_ID"),
            "reply_to_msg_id": message.get("reply_to_message_id", ""),
            "date": message.get("date"),
            "date_unixtime": message.get("date_unixtime"),
            "msg_type": message.get("media_type", "text"),
            "msg_content": str(msg_content_parts).replace("\n", " ").strip(),
            "forwarded_from": message.get("forwarded_from", ""),
            "action": message.get("action", ""),
        }

    def process_chat(self, jdata):
        chat_name = jdata.get("name", f"Chat_ID_{jdata.get('id', 'Unknown')}")
        rows = [
            processed
            for msg in jdata.get("messages", [])
            if (processed := self.process_message(msg))
        ]
        return {"chat": chat_name, "rows": rows}

    def process(self, chat_history_json_path):
        with open(chat_history_json_path, "r", encoding="utf-8-sig") as input_file:
            jdata = json.load(input_file)
        if "chats" in jdata and "list" in jdata["chats"]:
            return [self.process_chat(chat_data) for chat_data in jdata["chats"]["list"]]
        return [self.process_chat(jdata)]


class TelegramMonitor:
    """Polls the Telegram API and forwards messages to callbacks."""

    def __init__(self, bot_token, chat_id, group_id, message_callback, status_callback):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.group_id = group_id
        self.message_callback = message_callback
        self.status_callback = status_callback
        self.running = False
        self.last_update_id = 0
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.last_update_id = 0
        self.thread = threading.Thread(target=self._poll_updates, daemon=True)
        self.thread.start()
        self.status_callback("Monitoring started successfully.", "success", self.group_id)

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        self.status_callback("Monitoring stopped.", "info", self.group_id)

    def _poll_updates(self):
        self.status_callback(
            f"Polling for updates from chat ID: {self.chat_id}...",
            "info",
            self.group_id,
        )
        if requests is None:
            self.status_callback("'requests' library missing. Install with: pip install requests", "error", self.group_id)
            return
        while self.running:
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
                params = {
                    "offset": self.last_update_id + 1,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                }
                response = requests.get(url, params=params, timeout=35)
                if not self.running:
                    break
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") and data.get("result"):
                        for update in data["result"]:
                            self.last_update_id = update["update_id"]
                            if (
                                "message" in update
                                and str(update["message"].get("chat", {}).get("id"))
                                == self.chat_id
                            ):
                                self.message_callback(update["message"], self.group_id)
                    elif not data.get("ok"):
                        self.status_callback(
                            f"API Error: {data.get('description')}",
                            "error",
                            self.group_id,
                        )
                        time.sleep(10)
                elif response.status_code == 401:
                    self.status_callback(
                        "API Error: Unauthorized. Check bot token.",
                        "error",
                        self.group_id,
                    )
                    self.stop()
                    break
                else:
                    self.status_callback(
                        f"HTTP Error {response.status_code}", "error", self.group_id
                    )
                    time.sleep(10)
            except requests.exceptions.RequestException as exc:
                self.status_callback(
                    f"Network Error: {exc}", "error", self.group_id
                )
                time.sleep(15)
            except Exception as exc:  # noqa: BLE001
                self.status_callback(
                    f"Polling Error: {exc}", "error", self.group_id
                )
                time.sleep(10)
            time.sleep(1)


class AdvancedTelegramParserGUI:
    """Main application window."""

    def __init__(self, root_tk: tk.Tk):
        self.root = root_tk
        self.root.title("Advanced Telegram Parser v1.5 - AI & Analytics")
        self.root.geometry("1100x900")

        self.parser = TelegramChatParser()
        self.ai_processor = AIProcessor()
        self.telegram_groups = {}
        self.default_bot_token = tk.StringVar(value=DEFAULT_BOT_TOKEN)

        self.input_file_path = tk.StringVar()
        self.output_dir_path = tk.StringVar()
        self.current_parsed_data = None
        self.analyzed_chart_data = []

        self._setup_styles()
        self._setup_gui_layout()
        self.load_group_configurations()

    # ------------------------------------------------------------------
    # Logging helpers
    def _log_to_gui(self, text_widget, message, level="INFO"):
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        colors = {
            "INFO": "blue",
            "SUCCESS": "green",
            "WARNING": "orange",
            "ERROR": "red",
            "AI": "#8A2BE2",
        }
        tag_name = level.lower()
        text_widget.tag_configure(tag_name, foreground=colors.get(level, "black"))

        text_widget.configure(state="normal")
        text_widget.insert(
            tk.END, f"[{timestamp_str}] [{level}] ", (tag_name, "bold_tag")
        )
        text_widget.insert(tk.END, f"{message}\n")
        text_widget.configure(state="disabled")
        text_widget.see(tk.END)

    def _log_to_results_feed(self, message, level="INFO"):
        self._log_to_gui(self.results_text_area, message, level)

    def _log_to_monitor_feed(self, message, level="INFO"):
        self._log_to_gui(self.live_monitor_feed_area, message, level)

    # ------------------------------------------------------------------
    # GUI setup
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TNotebook.Tab", padding=[10, 5], font=("Arial", 10, "bold"))

    def _setup_gui_layout(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self._create_parse_files_tab(notebook)
        self._create_group_monitor_tab(notebook)
        self._create_analytics_tab(notebook)

    # ------------------------------------------------------------------
    def _create_parse_files_tab(self, notebook):
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="\U0001F4C1 Step 1: Parse Files")

        fs_frame = ttk.LabelFrame(tab, text="Input/Output", padding=10)
        fs_frame.pack(fill=tk.X, padx=10, pady=5)

        row1 = ttk.Frame(fs_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="JSON Export File:").pack(side=tk.LEFT, padx=(0, 5))
        self.input_file_label = ttk.Label(
            row1, text="No file selected.", width=60, relief="sunken", anchor="w"
        )
        self.input_file_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row1, text="Browse...", command=self._select_input_file_dialog).pack(
            side=tk.RIGHT
        )

        row2 = ttk.Frame(fs_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="Output Directory:").pack(side=tk.LEFT, padx=(0, 5))
        self.output_dir_label = ttk.Label(
            row2, text="No directory selected.", width=60, relief="sunken", anchor="w"
        )
        self.output_dir_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(row2, text="Browse...", command=self._select_output_dir_dialog).pack(
            side=tk.RIGHT
        )

        action_frame = ttk.Frame(tab)
        action_frame.pack(pady=15)
        self.parse_file_button = ttk.Button(
            action_frame, text="1. Start Parsing JSON", command=self._trigger_parsing_thread
        )
        self.parse_file_button.pack(side=tk.LEFT, padx=10)

        self.analyze_ai_button = ttk.Button(
            action_frame,
            text="2. \U0001F9E0 Analyze with AI (for Charts)",
            command=self._trigger_ai_analysis_thread,
            state="disabled",
        )
        self.analyze_ai_button.pack(side=tk.LEFT, padx=10)

        self.parse_progress_bar = ttk.Progressbar(tab, mode="indeterminate", length=300)
        self.parse_progress_bar.pack(pady=5)

        self.results_text_area = scrolledtext.ScrolledText(
            tab, height=20, wrap=tk.WORD, state="disabled"
        )
        self.results_text_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self._log_to_results_feed(
            "Welcome! Select a JSON file and output directory, then click 'Start Parsing'.",
            "INFO",
        )

    # ------------------------------------------------------------------
    def _create_group_monitor_tab(self, notebook):
        tab = ttk.Frame(notebook)
        notebook.add(tab, text="\U0001F4E1 Live Monitor")

        group_mgmt_frame = ttk.LabelFrame(tab, text="Group Configuration & Control", padding=10)
        group_mgmt_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        list_frame = ttk.Frame(group_mgmt_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        columns = ("name", "chat_id", "status")
        self.group_tree = ttk.Treeview(list_frame, columns=columns, show="headings")
        self.group_tree.heading("name", text="Group Name")
        self.group_tree.heading("chat_id", text="Chat ID")
        self.group_tree.heading("status", text="Status")
        self.group_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.group_tree.yview)
        self.group_tree.configure(yscroll=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        btn_frame = ttk.Frame(group_mgmt_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="Add Group", command=self._show_group_dialog).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="Edit Selected", command=self._edit_selected_group).pack(
            side=tk.LEFT, padx=5
        )
        ttk.Button(btn_frame, text="Remove Selected", command=self._remove_selected_group).pack(
            side=tk.LEFT, padx=5
        )

        monitor_ctrl_frame = ttk.Frame(group_mgmt_frame)
        monitor_ctrl_frame.pack(fill=tk.X, pady=5)
        ttk.Button(
            monitor_ctrl_frame, text="\u25B6\uFE0F Start Selected", command=self._start_selected_monitoring
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            monitor_ctrl_frame, text="\u23F9\uFE0F Stop Selected", command=self._stop_selected_monitoring
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            monitor_ctrl_frame, text="\u25B6\uFE0F Start All", command=self._start_all_monitoring
        ).pack(side=tk.LEFT, padx=(15, 5))
        ttk.Button(
            monitor_ctrl_frame, text="\u23F9\uFE0F Stop All", command=self._stop_all_monitoring
        ).pack(side=tk.LEFT, padx=5)

        token_frame = ttk.LabelFrame(tab, text="Default Bot Token", padding=10)
        token_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Entry(token_frame, textvariable=self.default_bot_token, width=80, show="*").pack(
            fill=tk.X, expand=True
        )

        self.live_monitor_feed_area = scrolledtext.ScrolledText(
            tab, height=15, wrap=tk.WORD, state="disabled"
        )
        self.live_monitor_feed_area.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 5))
        self._log_to_monitor_feed("Add groups to begin monitoring.", "INFO")

    # ------------------------------------------------------------------
    def _create_analytics_tab(self, notebook):
        self.analytics_tab = ttk.Frame(notebook)
        notebook.add(self.analytics_tab, text="\U0001F4CA Step 2: Visualize Data")

        try:
            import matplotlib

            matplotlib.use("TkAgg")
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            ttk.Label(
                self.analytics_tab,
                text=(
                    "Matplotlib library not found. Please install it (\`pip install matplotlib\`) "
                    "to enable charts."
                ),
            ).pack(pady=50)
            self.matplotlib_available = False
            return

        self.matplotlib_available = True
        ctrl_frame = ttk.Frame(self.analytics_tab)
        ctrl_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(ctrl_frame, text="Chart Type:").pack(side=tk.LEFT, padx=5)
        chart_types = ["Dosing Timeline", "Side Effect Correlation"]
        self.chart_type_var = tk.StringVar(value=chart_types[0])
        chart_dropdown = ttk.Combobox(
            ctrl_frame, textvariable=self.chart_type_var, values=chart_types, state="readonly"
        )
        chart_dropdown.pack(side=tk.LEFT, padx=5)
        chart_dropdown.bind("<<ComboboxSelected>>", self._update_chart)

        self.fig = Figure(figsize=(10, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, self.analytics_tab)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self._update_chart()

    # ------------------------------------------------------------------
    def _select_input_file_dialog(self):
        path = filedialog.askopenfilename(
            title="Select Telegram Export JSON", filetypes=[("JSON files", "*.json")]
        )
        if path:
            self.input_file_path.set(path)
            self.input_file_label.config(text=os.path.basename(path))
            self._log_to_results_feed(f"Input file set: {os.path.basename(path)}", "INFO")

    def _select_output_dir_dialog(self):
        path = filedialog.askdirectory(title="Select Output Directory for Exports")
        if path:
            self.output_dir_path.set(path)
            self.output_dir_label.config(text=path)
            self._log_to_results_feed(f"Output directory set: {path}", "INFO")

    # ------------------------------------------------------------------
    # Parsing logic
    def _trigger_parsing_thread(self):
        if not self.input_file_path.get():
            messagebox.showerror("Input Missing", "Please select an input JSON file.")
            return
        self.parse_file_button.config(state="disabled")
        self.analyze_ai_button.config(state="disabled")
        self.parse_progress_bar.start()
        self._log_to_results_feed("Parsing started...", "INFO")
        threading.Thread(target=self._execute_parsing, daemon=True).start()

    def _execute_parsing(self):
        try:
            data = self.parser.process(self.input_file_path.get())
            self.current_parsed_data = data
            self.root.after(0, self._parsing_finished, data)
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, self._parsing_failed, exc)

    def _parsing_finished(self, data):
        self.parse_progress_bar.stop()
        self.parse_file_button.config(state="normal")
        self.analyze_ai_button.config(state="normal")
        num_messages = sum(len(chat.get("rows", [])) for chat in data)
        msg = (
            f"Parsing complete! Processed {num_messages} messages. Ready for AI Analysis."
        )
        self._log_to_results_feed(msg, "SUCCESS")
        messagebox.showinfo("Parsing Complete", msg)

    def _parsing_failed(self, error):
        self.parse_progress_bar.stop()
        self.parse_file_button.config(state="normal")
        self._log_to_results_feed(f"Parsing failed: {error}", "ERROR")

    # ------------------------------------------------------------------
    # AI bulk analysis
    def _trigger_ai_analysis_thread(self):
        if not self.current_parsed_data:
            messagebox.showerror(
                "No Data", "Please parse a file first before running AI analysis."
            )
            return
        self.analyze_ai_button.config(state="disabled")
        self.parse_progress_bar.start()
        self._log_to_results_feed(
            "Starting AI analysis on all messages... This may take a while.", "INFO"
        )
        threading.Thread(target=self._execute_ai_analysis, daemon=True).start()

    def _execute_ai_analysis(self):
        self.analyzed_chart_data = []
        all_rows = [row for chat in self.current_parsed_data for row in chat["rows"]]

        for i, row in enumerate(all_rows):
            content = row.get("msg_content")
            if not content:
                continue

            if i % 10 == 0:
                self.root.after(
                    0,
                    lambda i=i: self._log_to_results_feed(
                        f"AI processing message {i+1}/{len(all_rows)}...", "INFO"
                    ),
                )

            extracted_info = self.ai_processor.extract_dosing_info(content)
            if extracted_info and extracted_info.get("is_dosing_related"):
                extracted_info["date"] = row.get("date")
                extracted_info["sender"] = row.get("sender")
                self.analyzed_chart_data.append(extracted_info)

        self.root.after(0, self._ai_analysis_finished)

    def _ai_analysis_finished(self):
        self.parse_progress_bar.stop()
        self.analyze_ai_button.config(state="normal")
        msg = (
            f"AI Analysis complete! Found {len(self.analyzed_chart_data)} dosing-related "
            "entries. You can now use the 'Analytics' tab."
        )
        self._log_to_results_feed(msg, "SUCCESS")
        messagebox.showinfo("AI Analysis Complete", msg)
        self._update_chart()

    # ------------------------------------------------------------------
    # Live message processing
    def _handle_new_live_message(self, message_data, group_id):
        self.root.after(
            0, self._process_and_display_live_message, message_data, group_id
        )

    def _process_and_display_live_message(self, message_data, group_id):
        group_name = self.telegram_groups.get(
            group_id, TelegramGroup("Unknown", "0")
        ).name
        sender = message_data.get("from", {}).get("first_name", "Unknown")
        text = message_data.get("text", "[Non-text message or empty]")
        msg_id = message_data.get("message_id", "N/A")

        log_msg = (
            f"\U0001F4AC [{group_name}] From: {sender} (ID: {msg_id}):\n    {text}"
        )
        self._log_to_monitor_feed(log_msg, "INFO")

        if text and text != "[Non-text message or empty]":
            threading.Thread(
                target=self._run_ai_analysis_on_live_message,
                args=(text, group_name, msg_id),
                daemon=True,
            ).start()

    def _run_ai_analysis_on_live_message(self, text, group_name, msg_id):
        extracted_data = self.ai_processor.extract_dosing_info(text)
        if extracted_data and extracted_data.get("is_dosing_related"):
            self.root.after(
                0, self._display_ai_results, extracted_data, group_name, msg_id
            )

    def _display_ai_results(self, data, group_name, msg_id):
        log_msg = (
            f"\U0001F9E0 AI Insight [{group_name}] (ID: {msg_id}): Med: {data.get('medication')}, "
            f"Dose: {data.get('dosage')}"
        )
        if data.get("side_effects"):
            log_msg += f", Side Effects: {', '.join(data['side_effects'])}"
        self._log_to_monitor_feed(log_msg, "AI")

    # ------------------------------------------------------------------
    # Charting helpers
    def _update_chart(self, event=None):
        if not getattr(self, "matplotlib_available", False):
            return
        chart_type = self.chart_type_var.get()
        self.fig.clear()

        if chart_type == "Dosing Timeline":
            self._generate_dosing_timeline()
        elif chart_type == "Side Effect Correlation":
            self._generate_side_effect_correlation()

        self.canvas.draw()

    def _generate_dosing_timeline(self):
        import matplotlib.dates as mdates

        ax = self.fig.add_subplot(111)
        ax.set_title("Dosing Timeline (based on AI Analysis)")
        if not self.analyzed_chart_data:
            ax.text(
                0.5,
                0.5,
                "No data. Parse a file and run AI Analysis.",
                ha="center",
                va="center",
            )
            return

        events = [
            {
                "time": datetime.fromisoformat(e["date"]),
                "label": f"{e.get('medication','?')} {e.get('dosage','?')}",
            }
            for e in self.analyzed_chart_data
            if e.get("medication") or e.get("dosage")
        ]
        if not events:
            ax.text(
                0.5,
                0.5,
                "No medication/dosage entries found by AI.",
                ha="center",
                va="center",
            )
            return

        event_times = [e["time"] for e in events]
        ax.plot(event_times, [1] * len(events), "o", markersize=8, color="green", alpha=0.7)
        for event in events:
            ax.text(
                event["time"],
                1.01,
                event["label"],
                rotation=30,
                ha="left",
                va="bottom",
                fontsize=8,
            )
        ax.set_ylim(0.95, 1.05)
        ax.set_yticks([])
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        self.fig.autofmt_xdate()

    def _generate_side_effect_correlation(self):
        import matplotlib.dates as mdates

        ax = self.fig.add_subplot(111)
        ax.set_title("Medication vs. Side Effect Timeline (based on AI Analysis)")
        if not self.analyzed_chart_data:
            ax.text(
                0.5,
                0.5,
                "No data. Parse a file and run AI Analysis.",
                ha="center",
                va="center",
            )
            return

        med_events = [
            {
                "time": datetime.fromisoformat(e["date"]),
                "label": f"{e.get('medication','?')} {e.get('dosage','?')}",
            }
            for e in self.analyzed_chart_data
            if e.get("medication") or e.get("dosage")
        ]
        side_effect_events = [
            {
                "time": datetime.fromisoformat(e["date"]),
                "label": ", ".join(e["side_effects"]),
            }
            for e in self.analyzed_chart_data
            if e.get("side_effects")
        ]

        if not med_events and not side_effect_events:
            ax.text(0.5, 0.5, "No medication or side effect data found by AI.", ha="center")
            return

        if med_events:
            med_times = [e["time"] for e in med_events]
            ax.plot(
                med_times,
                [1] * len(med_times),
                "o",
                markersize=10,
                color="blue",
                alpha=0.7,
                label="Medication Taken",
            )
            for event in med_events:
                ax.text(
                    event["time"],
                    1.01,
                    event["label"],
                    rotation=45,
                    ha="left",
                    va="bottom",
                    fontsize=9,
                )

        if side_effect_events:
            se_times = [e["time"] for e in side_effect_events]
            ax.plot(
                se_times,
                [1] * len(se_times),
                "X",
                markersize=12,
                color="red",
                alpha=0.9,
                label="Side Effect Reported",
            )
            for event in side_effect_events:
                ax.text(
                    event["time"],
                    0.99,
                    event["label"],
                    rotation=-45,
                    ha="right",
                    va="top",
                    fontsize=9,
                    color="darkred",
                )

        ax.set_ylim(0.95, 1.05)
        ax.set_yticks([])
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax.legend(loc="upper right")
        self.fig.autofmt_xdate()

    # ------------------------------------------------------------------
    # Group management helpers
    def _show_group_dialog(self, group_to_edit=None):
        """Prompt the user to add or edit a Telegram group."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Edit Group" if group_to_edit else "Add Group")
        dlg.resizable(False, False)

        name_var = tk.StringVar(value=getattr(group_to_edit, "name", ""))
        chat_var = tk.StringVar(value=getattr(group_to_edit, "chat_id", ""))
        token_var = tk.StringVar(
            value=getattr(group_to_edit, "bot_token", self.default_bot_token.get())
        )

        ttk.Label(dlg, text="Group Name:").grid(row=0, column=0, padx=5, pady=5)
        ttk.Entry(dlg, textvariable=name_var, width=40).grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(dlg, text="Chat ID:").grid(row=1, column=0, padx=5, pady=5)
        ttk.Entry(dlg, textvariable=chat_var, width=40).grid(row=1, column=1, padx=5, pady=5)
        ttk.Label(dlg, text="Bot Token (optional):").grid(row=2, column=0, padx=5, pady=5)
        ttk.Entry(dlg, textvariable=token_var, width=40, show="*").grid(row=2, column=1, padx=5, pady=5)

        def save_action():
            name = name_var.get().strip()
            chat_id = chat_var.get().strip()
            token = token_var.get().strip() or None
            if not name or not chat_id:
                messagebox.showerror("Missing Data", "Group name and chat ID are required.")
                return
            if group_to_edit:
                group_to_edit.name = name
                group_to_edit.chat_id = chat_id
                group_to_edit.bot_token = token
                self.group_tree.item(
                    group_to_edit.id,
                    values=(name, chat_id, "Running" if group_to_edit.is_monitoring else "Stopped"),
                )
            else:
                new_group = TelegramGroup(name, chat_id, token)
                self.telegram_groups[new_group.id] = new_group
                self.group_tree.insert("", tk.END, iid=new_group.id, values=(name, chat_id, "Stopped"))
            self.save_group_configurations()
            dlg.destroy()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=(5, 10))
        ttk.Button(btn_frame, text="Save", command=save_action).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT, padx=5)

    def _edit_selected_group(self):
        sel = self.group_tree.selection()
        if not sel:
            return
        group = self.telegram_groups.get(sel[0])
        if group:
            self._show_group_dialog(group)

    def _remove_selected_group(self):
        sel = self.group_tree.selection()
        if not sel:
            return
        gid = sel[0]
        group = self.telegram_groups.pop(gid, None)
        if group:
            if group.monitor_instance:
                group.monitor_instance.stop()
            self.group_tree.delete(gid)
            self.save_group_configurations()

    def _start_selected_monitoring(self):
        sel = self.group_tree.selection()
        if not sel:
            return
        gid = sel[0]
        group = self.telegram_groups.get(gid)
        if group and not group.is_monitoring:
            token = group.bot_token or self.default_bot_token.get()
            monitor = TelegramMonitor(
                token, group.chat_id, gid, self._handle_new_live_message, self._log_to_monitor_feed
            )
            group.monitor_instance = monitor
            monitor.start()
            group.is_monitoring = True
            self.group_tree.set(gid, "status", "Running")

    def _stop_selected_monitoring(self):
        sel = self.group_tree.selection()
        if not sel:
            return
        gid = sel[0]
        group = self.telegram_groups.get(gid)
        if group and group.is_monitoring and group.monitor_instance:
            group.monitor_instance.stop()
            group.is_monitoring = False
            self.group_tree.set(gid, "status", "Stopped")

    def _start_all_monitoring(self):
        for gid in self.telegram_groups:
            self.group_tree.selection_set(gid)
            self._start_selected_monitoring()
        self.group_tree.selection_remove(*self.group_tree.selection())

    def _stop_all_monitoring(self):
        for gid in self.telegram_groups:
            self.group_tree.selection_set(gid)
            self._stop_selected_monitoring()
        self.group_tree.selection_remove(*self.group_tree.selection())

    def load_group_configurations(self):
        """Load saved group configs from disk."""
        if not os.path.exists(GROUPS_CONFIG_FILE):
            return
        try:
            with open(GROUPS_CONFIG_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for entry in raw:
                grp = TelegramGroup(
                    entry.get("name", "Unknown"),
                    entry.get("chat_id", ""),
                    entry.get("bot_token"),
                    entry.get("auto_save", False),
                )
                self.telegram_groups[grp.id] = grp
                self.group_tree.insert("", tk.END, iid=grp.id, values=(grp.name, grp.chat_id, "Stopped"))
        except (OSError, json.JSONDecodeError):
            pass

    def save_group_configurations(self):
        data = [grp.to_dict() for grp in self.telegram_groups.values()]
        try:
            with open(GROUPS_CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass

    def on_closing(self):
        self.root.destroy()


def main():
    root = tk.Tk()
    app = AdvancedTelegramParserGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()


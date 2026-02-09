import argparse
import importlib.util
import json
import os
import queue
import random
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Iterable, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright

pyperclip = None
if importlib.util.find_spec("pyperclip"):
    import pyperclip  # type: ignore[assignment]

winsound = None
if importlib.util.find_spec("winsound"):
    import winsound  # type: ignore[assignment]


DEFAULT_DEBUG_URL = "http://localhost:9222"
DEFAULT_MODEL_URL = "https://gemini.google.com/app"
DEFAULT_CHATGPT_URL = "https://chat.openai.com"
DEFAULT_HISTORY_FILE = "eagle_history.json"
DEFAULT_PROMPTS_FILE = "eagle_prompts.json"
DEFAULT_BATCH = 30
DEFAULT_POLL_DELAY = 2.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_REFRESH_RETRIES = 2
DEFAULT_ENTER_GUARD_S = 1.2
DEFAULT_HUMAN_DELAY = (0.18, 0.45)

SACRED_PROMPT = """SYSTEM MODE: EXPERT TECHNICAL INSTRUCTOR & PROFESSIONAL C PROGRAMMER TRANSLATOR.
TARGET LANGUAGE: ARABIC (Professional, Academic, RTL-Optimized).

⚠️ OBJECTIVE: Translate the transcript to Arabic using the "English-First Dual-Anchor Protocol" while maintaining strict SRT structure.

🛑 TERMINOLOGY RULES (THE HYBRID STANDARD):
1. RULE 1: ENGLISH IS MASTER (The Core):
   - All technical terms (e.g., programming concepts, C keywords, memory management terms) MUST remain in English inside double quotes.
   - Example: "Stack", "Heap", "Pointer", "Memory Leak".

2. RULE 2: THE EXPLANATION (The Educational Support):
   - Immediately after the English term, provide the Arabic translation/explanation inside parentheses.
   - FORMAT: "English Term" (الترجمة العربية).
   - ✅ CORRECT EXAMPLES:
     * "Stack" (الذاكرة المكدسة).
     * "Pointer" (المؤشر).
     * "Memory Leak" (تسرب الذاكرة).

🛑 VISUAL & FORMAT RULES (THE STEEL STRUCTURE):
1. RTL ANCHOR (CRITICAL): Every single subtitle line MUST start with an Arabic word to ensure correct Right-to-Left alignment.
   - ❌ Bad: "Pointer" هو متغير...
   - ✅ Good: يعتبر الـ "Pointer" (المؤشر) متغيراً...

2. BLOCK INTEGRITY (NO MERGING):
   - Translate block-by-block.
   - STRICTLY FORBIDDEN to merge lines or combine IDs.
   - Keep timestamps exactly as they are.

3. OUTPUT FORMAT:
   - Return full SRT blocks (ID -> Time -> Text).
   - Must be inside a Markdown code block (```srt).
   - DATA ONLY. No conversational filler or preambles.

INPUT BLOCKS:
"""


def read_clipboard_text() -> str:
    if pyperclip:
        try:
            return pyperclip.paste()
        except Exception:
            pass
    try:
        root = tk.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text
    except Exception:
        return ""


class SoundPlayer:
    @staticmethod
    def play_success() -> None:
        threading.Thread(target=SoundPlayer._success_sound, daemon=True).start()

    @staticmethod
    def play_error() -> None:
        threading.Thread(target=SoundPlayer._error_sound, daemon=True).start()

    @staticmethod
    def _success_sound() -> None:
        if winsound:
            winsound.Beep(880, 120)
            winsound.Beep(1040, 120)
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.bell()
            root.destroy()
        except Exception:
            return

    @staticmethod
    def _error_sound() -> None:
        if winsound:
            winsound.Beep(440, 300)
            winsound.Beep(330, 300)
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.bell()
            root.bell()
            root.destroy()
        except Exception:
            return


@dataclass
class Config:
    debug_url: str = DEFAULT_DEBUG_URL
    model_url: str = DEFAULT_MODEL_URL
    chatgpt_url: str = DEFAULT_CHATGPT_URL
    history_file: str = DEFAULT_HISTORY_FILE
    batch_size: int = DEFAULT_BATCH
    poll_delay: float = DEFAULT_POLL_DELAY
    max_retries: int = DEFAULT_MAX_RETRIES
    refresh_retries: int = DEFAULT_REFRESH_RETRIES
    min_match_ratio: float = 0.8
    auto_copy: bool = True
    use_gui: bool = True
    model_choice: str = "Gemini"
    enter_guard_s: float = DEFAULT_ENTER_GUARD_S
    human_delay: tuple[float, float] = DEFAULT_HUMAN_DELAY


@dataclass
class ModelProfile:
    name: str
    base_url: str
    input_selectors: List[str]
    response_selectors: List[str]
    stop_button_names: List[str]
    regenerate_button_names: List[str]
    send_button_names: List[str]


MODEL_PROFILES = {
    "Gemini": ModelProfile(
        name="Gemini",
        base_url=DEFAULT_MODEL_URL,
        input_selectors=["[role='textbox']", "textarea", "div[contenteditable='true']"],
        response_selectors=["model-response", "div[role='article']", "div[data-response-id]"],
        stop_button_names=["stop responding", "stop generating"],
        regenerate_button_names=["regenerate", "retry", "try again"],
        send_button_names=["send", "submit", "send message"],
    ),
    "ChatGPT": ModelProfile(
        name="ChatGPT",
        base_url=DEFAULT_CHATGPT_URL,
        input_selectors=["textarea", "div[role='textbox']"],
        response_selectors=["div[data-message-author-role='assistant']", "div[role='article']"],
        stop_button_names=["stop generating", "stop"],
        regenerate_button_names=["regenerate", "retry", "try again"],
        send_button_names=["send", "submit", "send message"],
    ),
}


class EagleLog:
    def __init__(self, write_func: Callable[[str], None]) -> None:
        self.write_func = write_func

    def info(self, message: str) -> None:
        self.write_func(f"🦅 Eagle: {message}")

    def warn(self, message: str) -> None:
        self.write_func(f"⚠️ Eagle: {message}")

    def error(self, message: str) -> None:
        self.write_func(f"❌ Eagle: {message}")


class PromptManager:
    def __init__(self, path: str) -> None:
        self.path = path
        self._prompts = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self._prompts, handle, indent=2, ensure_ascii=False)

    def list_names(self) -> List[str]:
        return sorted(self._prompts.keys())

    def get_prompt(self, name: str) -> str:
        return self._prompts.get(name, "")

    def upsert_prompt(self, name: str, text: str) -> None:
        self._prompts[name] = text
        self._save()

    def delete_prompt(self, name: str) -> None:
        if name in self._prompts:
            del self._prompts[name]
            self._save()

class HistoryManager:
    def __init__(self, path: str) -> None:
        self.path = path
        self._history = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def mark_done(self, file_path: str) -> None:
        self._history[os.path.abspath(file_path)] = {
            "status": "DONE",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_status(self, file_path: str) -> Optional[dict]:
        return self._history.get(os.path.abspath(file_path))

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(self._history, handle, indent=2, ensure_ascii=False)
        except Exception:
            pass


class TranscriptParser:
    @staticmethod
    def natural_sort_key(text: str) -> List:
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]

    @staticmethod
    def read_and_parse_file(file_path: str, logger: EagleLog) -> List[dict]:
        try:
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except UnicodeDecodeError:
                with open(file_path, "r", encoding="latin-1") as handle:
                    content = handle.read()

            if not content.strip():
                return []

            if file_path.lower().endswith(".vtt"):
                content = re.sub(r"WEBVTT.*?\n", "", content, flags=re.IGNORECASE)
                content = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", content)

            regex = (
                r"(\d+)\s*\n?(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
                r"\d{2}:\d{2}:\d{2}[,.]\d{3}).*?\n([\s\S]*?)(?=\n\n\d|\Z)"
            )
            matches = list(re.finditer(regex, content, re.MULTILINE))

            blocks = []
            if not matches and "-->" in content:
                regex_time = (
                    r"(\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
                    r"\d{2}:\d{2}:\d{2}[,.]\d{3}).*?\n([\s\S]*?)(?=\n\n|\Z)"
                )
                for i, match in enumerate(re.finditer(regex_time, content, re.MULTILINE)):
                    blocks.append(
                        {
                            "id": str(i + 1),
                            "time": match.group(1).replace(".", ","),
                            "text": match.group(2).strip(),
                        }
                    )
            else:
                for match in matches:
                    blocks.append(
                        {
                            "id": match.group(1).strip(),
                            "time": match.group(2).replace(".", ","),
                            "text": match.group(3).strip(),
                        }
                    )
            return [block for block in blocks if block.get("text")]
        except Exception as exc:
            logger.error(f"Error parsing {os.path.basename(file_path)}: {exc}")
            return []

    @staticmethod
    def extract_srt_blocks(text: str) -> List[str]:
        if not text:
            return []
        clean_text = text.replace("```srt", "").replace("```", "").strip()
        regex_block = (
            r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n(.*?)(?=\n\s*\d+\s*\n\d{2}:\d{2}|$)"
        )
        found = re.findall(regex_block, clean_text, re.DOTALL)
        cleaned = []
        for match in found:
            text_block = match[3].strip()
            if not text_block:
                continue
            cleaned.append(f"{match[0]}\n{match[1]} --> {match[2]}\n{text_block}")
        return cleaned

    @staticmethod
    def get_resume_point(output_path: str) -> int:
        if not os.path.exists(output_path):
            return 0
        try:
            with open(output_path, "r", encoding="utf-8") as handle:
                content = handle.read()
            return len(re.findall(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", content))
        except Exception:
            return 0

    @staticmethod
    def extract_first_last_ids(text: str) -> tuple[Optional[str], Optional[str]]:
        matches = TranscriptParser.extract_srt_blocks(text)
        if not matches:
            return None, None
        first_id = matches[0].splitlines()[0].strip()
        last_id = matches[-1].splitlines()[0].strip()
        return first_id, last_id

    @staticmethod
    def extract_first_last_times(text: str) -> tuple[Optional[str], Optional[str]]:
        clean_text = text.replace("```srt", "").replace("```", "").strip()
        regex_time = r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
        matches = re.findall(regex_time, clean_text)
        if not matches:
            return None, None
        first_time = matches[0][0].replace(".", ",")
        last_time = matches[-1][0].replace(".", ",")
        return first_time, last_time


class AIClient:
    def __init__(self, config: Config, logger: EagleLog, prompt_provider: Callable[[], str]) -> None:
        self.config = config
        self.logger = logger
        self.prompt_provider = prompt_provider
        self._last_sent = ""
        self._last_enter_time = 0.0
        self._playwright = None
        self._browser = None
        self._profile = MODEL_PROFILES.get(config.model_choice, MODEL_PROFILES["Gemini"])

    def connect(self) -> object:
        self.logger.info(f"Connecting to {self._profile.name}...")
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(self.config.debug_url)
        except Exception:
            self.logger.warn("CDP connect failed. Launching new browser...")
            self._browser = self._playwright.chromium.launch(headless=False)

        base_url = self.config.model_url if self._profile.name == "Gemini" else self.config.chatgpt_url
        page = None
        for ctx in self._browser.contexts:
            for ctx_page in ctx.pages:
                if base_url in ctx_page.url:
                    page = ctx_page
                    page.bring_to_front()
                    break
            if page:
                break

        if not page:
            page = self._browser.contexts[0].new_page()
            page.goto(base_url)
            self.logger.warn("Opened model page. Please ensure you are logged in.")
        return page

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _find_input(self, page):
        for selector in self._profile.input_selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0 and locator.first.is_visible():
                    return locator.first
            except Exception:
                continue
        return None

    def _find_send_button(self, page):
        for name in self._profile.send_button_names:
            try:
                locator = page.get_by_role("button", name=re.compile(name, re.I))
                if locator.count() > 0 and locator.first.is_visible():
                    return locator.first
            except Exception:
                continue
        return None

    def _human_pause(self) -> None:
        low, high = self.config.human_delay
        time.sleep(random.uniform(low, high))

    def _wait_for_response_start(self, page) -> bool:
        selector = ", ".join(self._profile.response_selectors)
        start = time.time()
        while time.time() - start < 8:
            try:
                count = page.locator(selector).count()
                if count > 0:
                    return True
            except Exception:
                pass
            try:
                for name in self._profile.stop_button_names:
                    stop = page.get_by_role("button", name=re.compile(name, re.I))
                    if stop.count() > 0 and stop.first.is_visible():
                        return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def _submit_message(self, page) -> bool:
        now = time.time()
        if now - self._last_enter_time < self.config.enter_guard_s:
            self.logger.warn("Send guard active, skipping extra Enter.")
            return False
        self._last_enter_time = now

        send_button = self._find_send_button(page)
        if send_button:
            try:
                if send_button.is_enabled():
                    send_button.click()
                    self._human_pause()
                    return True
            except Exception:
                pass

        try:
            page.keyboard.press("Enter")
            self._human_pause()
            return True
        except Exception:
            return False

    def _collect_response_text(self, page) -> str:
        selector = ", ".join(self._profile.response_selectors)
        script = f"""() => {{
            const candidates = Array.from(document.querySelectorAll('{selector}'));
            if (!candidates.length) return '';
            const last = candidates[candidates.length - 1];
            return last.innerText || '';
        }}"""
        try:
            return page.evaluate(script)
        except Exception:
            return ""

    def _collect_response_html(self, page) -> str:
        selector = ", ".join(self._profile.response_selectors)
        script = f"""() => {{
            const candidates = Array.from(document.querySelectorAll('{selector}'));
            if (!candidates.length) return '';
            const last = candidates[candidates.length - 1];
            return last.innerHTML || '';
        }}"""
        try:
            return page.evaluate(script)
        except Exception:
            return ""

    def _find_response_with_times(self, page, start_time: str, end_time: str) -> str:
        selector = ", ".join(self._profile.response_selectors)
        script = f"""(data) => {{
            const nodes = Array.from(document.querySelectorAll('{selector}'));
            for (let i = nodes.length - 1; i >= 0; i--) {{
                const text = nodes[i].innerText || '';
                if (text.includes(data.start) && text.includes(data.end)) {{
                    return text;
                }}
            }}
            return '';
        }}"""
        try:
            return page.evaluate(script, {"start": start_time, "end": end_time})
        except Exception:
            return ""

    def send_batch(self, page, blocks: List[dict]) -> bool:
        text_payload = "".join(f"{b['id']}\n{b['time']}\n{b['text']}\n\n" for b in blocks)
        msg = self.prompt_provider().strip() + "\n" + text_payload.strip()

        if self._last_sent == msg:
            self.logger.warn("Duplicate payload detected. Skipping send.")
            return False

        textarea = self._find_input(page)
        if not textarea:
            self.logger.error("Input box not found. Refreshing...")
            self.refresh_page(page)
            textarea = self._find_input(page)
            if not textarea:
                return False

        try:
            textarea.click(force=True)
            self._human_pause()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            if pyperclip:
                pyperclip.copy(msg)
                page.keyboard.press("Control+V")
            else:
                textarea.fill(msg)
            self._human_pause()
            if not self._submit_message(page):
                return False
            if not self._wait_for_response_start(page):
                self.logger.warn("No response detected after send. Retrying submit.")
                time.sleep(self.config.enter_guard_s)
                if self._submit_message(page):
                    if not self._wait_for_response_start(page):
                        return False
            self._last_sent = msg
            return True
        except Exception as exc:
            self.logger.error(f"Failed to send batch: {exc}")
            return False

    def wait_for_completion(self, page, start_time: str, end_time: str) -> bool:
        self.logger.info("Waiting for model response...")
        stop_locators = [page.get_by_role("button", name=re.compile(name, re.I)) for name in self._profile.stop_button_names]
        regen_locators = [page.get_by_role("button", name=re.compile(name, re.I)) for name in self._profile.regenerate_button_names]

        try:
            for stop in stop_locators:
                expect(stop).to_be_hidden(timeout=20000)
                return True
        except PlaywrightTimeoutError:
            pass

        try:
            for regen in regen_locators:
                expect(regen).to_be_visible(timeout=20000)
                return True
        except PlaywrightTimeoutError:
            pass

        last_len = 0
        stable = 0
        start_wait = time.time()
        while time.time() - start_wait < 120:
            text = self._find_response_with_times(page, start_time, end_time) or self._collect_response_text(page)
            current_len = len(text)
            if current_len and current_len == last_len:
                stable += 1
            else:
                stable = 0
                last_len = current_len
            if stable >= 3 and TranscriptParser.extract_srt_blocks(text):
                return True
            time.sleep(self.config.poll_delay)

        self.logger.warn("Timeout waiting for model output.")
        return False

    def fetch_response_with_times(self, page, start_time: str, end_time: str) -> str:
        text = self._find_response_with_times(page, start_time, end_time)
        if text.strip():
            return text
        text = self._collect_response_text(page)
        if text.strip():
            return text
        html = self._collect_response_html(page)
        if html.strip():
            return re.sub(r"<[^>]+>", "", html)
        for _ in range(2):
            clipboard_text = self._copy_response_via_keyboard(page)
            if clipboard_text.strip():
                return clipboard_text
            time.sleep(0.4)
        return ""

    def refresh_page(self, page) -> bool:
        try:
            page.reload()
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)
            return True
        except Exception:
            return False

    def _copy_response_via_keyboard(self, page) -> str:
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Control+C")
            time.sleep(0.5)
            return read_clipboard_text()
        except Exception:
            return ""


class ManualDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        prompt: str,
        expected_first: str,
        expected_last: str,
        expected_first_time: str,
        expected_last_time: str,
        last_saved_id: str,
    ) -> None:
        super().__init__(parent)
        self.title("Manual Handover Required")
        self.geometry("700x450")
        self.result_text = ""
        self.expected_first = expected_first
        self.expected_last = expected_last
        self.last_saved_id = last_saved_id
        self.expected_first_time = expected_first_time
        self.expected_last_time = expected_last_time
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        ttk.Label(
            self,
            text=prompt,
            font=("Segoe UI", 10, "bold"),
            foreground="red",
            wraplength=650,
        ).pack(pady=10)

        info_frame = ttk.LabelFrame(self, text="Expected Context", padding=10)
        info_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(
            info_frame,
            text=(
                f"Target IDs: {expected_first} -> {expected_last}\n"
                f"Target Times: {expected_first_time} -> {expected_last_time}\n"
                f"Last Saved: ID {last_saved_id}"
            ),
            justify="left",
        ).pack(side="left")

        self.text_area = scrolledtext.ScrolledText(self, height=12, wrap="word")
        self.text_area.pack(fill="both", expand=True, padx=10, pady=10)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", pady=10)
        ttk.Button(btn_frame, text="✅ Confirm & Save", command=self._on_confirm).pack(
            side="right", padx=10
        )
        ttk.Button(btn_frame, text="❌ Skip/Abort", command=self._on_close).pack(side="right")

    def _on_confirm(self) -> None:
        pasted = self.text_area.get("1.0", "end").strip()
        first_id, last_id = TranscriptParser.extract_first_last_ids(pasted)
        first_time, last_time = TranscriptParser.extract_first_last_times(pasted)

        if (first_id and last_id) or (first_time and last_time):
            self.result_text = pasted
            self.destroy()
            return

        SoundPlayer.play_error()
        messagebox.showwarning(
            "Invalid Format",
            "Could not detect valid SRT blocks. Please make sure you copied the correct part.",
        )

    def _on_close(self) -> None:
        self.result_text = ""
        self.destroy()


class EagleProcessor:
    def __init__(
        self,
        config: Config,
        logger: EagleLog,
        manual_provider: Callable[..., str],
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
        refresh_event: Optional[threading.Event] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.history = HistoryManager(config.history_file)
        self.manual_provider = manual_provider
        self.last_saved_id = "N/A"
        self.stop_event = stop_event or threading.Event()
        self.pause_event = pause_event or threading.Event()
        self.refresh_event = refresh_event or threading.Event()

    def _wait_if_paused(self) -> bool:
        while self.pause_event.is_set():
            if self.stop_event.is_set():
                return False
            time.sleep(0.2)
        return not self.stop_event.is_set()

    def _check_refresh(self, bot: AIClient, page) -> None:
        if self.refresh_event.is_set():
            self.logger.warn("Manual refresh requested.")
            bot.refresh_page(page)
            self.refresh_event.clear()

    def get_all_files(self, root_path: str) -> List[str]:
        all_files = []
        for root, dirs, files in os.walk(root_path):
            dirs.sort(key=TranscriptParser.natural_sort_key)
            files.sort(key=TranscriptParser.natural_sort_key)
            for filename in files:
                if filename.endswith((".srt", ".vtt")) and "_AR" not in filename:
                    all_files.append(os.path.join(root, filename))
        return all_files

    def build_status(self, file_path: str) -> str:
        out_path = file_path.replace(".srt", "_AR.srt").replace(".vtt", "_AR.srt")
        if os.path.exists(out_path):
            done_blocks = TranscriptParser.get_resume_point(out_path)
            if done_blocks > 0:
                return f"🔄 RESUME ({done_blocks})"

        history = self.history.get_status(file_path)
        if history and history.get("status") == "DONE":
            return "✅ DONE"
        return "⬜ TODO"

    def process(self, file_paths: Iterable[str], bot: AIClient, page) -> None:
        for file_path in file_paths:
            if self.stop_event.is_set():
                return
            if not self._wait_if_paused():
                return
            self.process_file(file_path, bot, page)

    def process_file(self, file_path: str, bot: AIClient, page) -> None:
        self.logger.info(f"Processing: {os.path.basename(file_path)}")
        total_blocks = TranscriptParser.read_and_parse_file(file_path, self.logger)
        if not total_blocks:
            return

        out_path = file_path.replace(".srt", "_AR.srt").replace(".vtt", "_AR.srt")
        start_index = TranscriptParser.get_resume_point(out_path)

        if start_index >= len(total_blocks):
            self.logger.info("File already finished.")
            self.history.mark_done(file_path)
            return

        i = start_index
        while i < len(total_blocks):
            if self.stop_event.is_set():
                self.logger.warn("Stop requested. Halting.")
                return
            if not self._wait_if_paused():
                return
            self._check_refresh(bot, page)

            chunk = total_blocks[i : i + self.config.batch_size]
            response_text = self._run_with_retries(bot, page, chunk)

            if not response_text:
                self.logger.warn("Automation failed. Requesting manual assistance...")
                SoundPlayer.play_error()
                response_text = self.manual_provider(
                    "The AI failed to provide a valid response after several attempts. Please copy manually.",
                    chunk[0]["id"],
                    chunk[-1]["id"],
                    chunk[0]["time"],
                    chunk[-1]["time"],
                    self.last_saved_id,
                )

            if not response_text:
                self.logger.error("No translation provided. Skipping file.")
                return

            srt_matches = TranscriptParser.extract_srt_blocks(response_text)
            required = max(1, int(len(chunk) * self.config.min_match_ratio))

            if len(srt_matches) >= required:
                self._save_chunk(out_path, srt_matches, i == 0)
                i += len(chunk)
                self.last_saved_id = chunk[-1]["id"]
                self.logger.info(f"Progress: {i}/{len(total_blocks)} blocks saved.")
            else:
                self.logger.warn("Mismatch detected. Waiting for manual confirmation.")
                response_text = self.manual_provider(
                    "Format mismatch detected. Please paste the correct SRT output.",
                    chunk[0]["id"],
                    chunk[-1]["id"],
                    chunk[0]["time"],
                    chunk[-1]["time"],
                    self.last_saved_id,
                )
                srt_matches = TranscriptParser.extract_srt_blocks(response_text)
                if srt_matches:
                    self._save_chunk(out_path, srt_matches, i == 0)
                    i += len(chunk)
                    self.last_saved_id = chunk[-1]["id"]
                else:
                    self.logger.error("Manual input invalid. Aborting file.")
                    return

        self.history.mark_done(file_path)
        self.logger.info(f"Finished: {os.path.basename(out_path)}")
        SoundPlayer.play_success()

    def _save_chunk(self, path: str, matches: List[str], is_first: bool) -> None:
        mode = "w" if is_first and not os.path.exists(path) else "a"
        with open(path, mode, encoding="utf-8-sig") as handle:
            if mode == "a" and os.path.getsize(path) > 0:
                handle.write("\n\n")
            handle.write("\n\n".join(matches))

    def _run_with_retries(self, bot: AIClient, page, chunk: List[dict]) -> str:
        for attempt in range(1, self.config.max_retries + 1):
            if self.stop_event.is_set():
                return ""
            if not self._wait_if_paused():
                return ""
            self._check_refresh(bot, page)
            self.logger.info(f"Attempt {attempt}/{self.config.max_retries}...")
            if not bot.send_batch(page, chunk):
                bot.refresh_page(page)
                continue
            if bot.wait_for_completion(page, chunk[0]["time"], chunk[-1]["time"]):
                response_text = bot.fetch_response_with_times(
                    page, chunk[0]["time"], chunk[-1]["time"]
                )
                if response_text.strip() and TranscriptParser.extract_srt_blocks(response_text):
                    return response_text

            self.logger.warn("Attempt failed to get valid SRT.")
            if attempt <= self.config.refresh_retries:
                bot.refresh_page(page)
            time.sleep(2)

        return ""


class EagleGUI:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = tk.Tk()
        self.root.title("🦅 Eagle V3 - Improved Subtitle Keeper")
        self.root.geometry("1150x880")
        self.root.configure(bg="#f4f5f7")

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=25, font=("Segoe UI", 10))
        style.configure("TLabel", background="#f4f5f7", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f4f5f7")
        style.configure("TLabelframe", background="#f4f5f7")
        style.configure("TLabelframe.Label", background="#f4f5f7", font=("Segoe UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

        self.files: List[str] = []
        self.selected_dir = tk.StringVar()
        self.auto_copy_var = tk.BooleanVar(value=config.auto_copy)
        self.batch_var = tk.IntVar(value=config.batch_size)
        self.model_choice_var = tk.StringVar(value=config.model_choice)
        self.status_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.refresh_event = threading.Event()
        self.prompt_manager = PromptManager(DEFAULT_PROMPTS_FILE)
        self.selected_prompt = tk.StringVar(value="Default")

        self._build_ui()

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=15)
        header.pack(fill="x")
        ttk.Label(header, text="🦅 Eagle V3", font=("Segoe UI", 20, "bold")).pack(
            side="left"
        )
        ttk.Label(
            header,
            text="Industrial AI Automation",
            font=("Segoe UI", 11, "italic"),
        ).pack(side="left", padx=15, pady=5)

        controls = ttk.LabelFrame(self.root, text=" Control Panel ", padding=10)
        controls.pack(fill="x", padx=10, pady=5)

        ttk.Button(controls, text="📂 Select Folder", command=self._choose_folder).pack(
            side="left", padx=5
        )
        ttk.Entry(controls, textvariable=self.selected_dir, width=50).pack(
            side="left", padx=5
        )

        ttk.Label(controls, text="Batch:").pack(side="left", padx=(15, 2))
        ttk.Spinbox(controls, from_=5, to=150, textvariable=self.batch_var, width=5).pack(
            side="left"
        )

        ttk.Label(controls, text="Model:").pack(side="left", padx=(15, 2))
        ttk.Combobox(
            controls,
            textvariable=self.model_choice_var,
            values=list(MODEL_PROFILES.keys()),
            width=10,
            state="readonly",
        ).pack(side="left")

        ttk.Checkbutton(controls, text="Auto-copy", variable=self.auto_copy_var).pack(
            side="left", padx=15
        )

        self.start_btn = ttk.Button(
            controls, text="🚀 START", command=self._start, style="Primary.TButton"
        )
        self.start_btn.pack(side="left", padx=5)

        self.pause_btn = ttk.Button(controls, text="⏸ Pause", command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=5)

        self.refresh_btn = ttk.Button(controls, text="🔄 Refresh", command=self._manual_refresh)
        self.refresh_btn.pack(side="left", padx=5)

        self.stop_btn = ttk.Button(controls, text="🛑 STOP", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=5)

        prompt_frame = ttk.LabelFrame(self.root, text=" Prompt (Editable) ", padding=10)
        prompt_frame.pack(fill="both", padx=10, pady=5)
        prompt_controls = ttk.Frame(prompt_frame)
        prompt_controls.pack(fill="x", pady=(0, 6))
        ttk.Label(prompt_controls, text="Preset:").pack(side="left")
        self.prompt_combo = ttk.Combobox(
            prompt_controls,
            textvariable=self.selected_prompt,
            values=[],
            width=18,
            state="readonly",
        )
        self.prompt_combo.pack(side="left", padx=6)
        self.prompt_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_prompt())
        ttk.Button(prompt_controls, text="New", command=self._new_prompt).pack(
            side="left", padx=4
        )
        ttk.Button(prompt_controls, text="Save/Update", command=self._save_prompt).pack(
            side="left", padx=4
        )
        self.delete_prompt_btn = ttk.Button(
            prompt_controls, text="Delete", command=self._delete_prompt
        )
        self.delete_prompt_btn.pack(side="left", padx=4)
        self.prompt_text = scrolledtext.ScrolledText(prompt_frame, height=8, wrap="word")
        self.prompt_text.pack(fill="both", expand=True)
        self._refresh_prompt_list()

        paned = ttk.PanedWindow(self.root, orient="vertical")
        paned.pack(fill="both", expand=True, padx=10, pady=5)

        list_frame = ttk.Frame(paned)
        paned.add(list_frame, weight=3)
        self.tree = ttk.Treeview(list_frame, columns=("status", "path"), show="headings")
        self.tree.heading("status", text="Status")
        self.tree.heading("path", text="File Path")
        self.tree.column("status", width=150, anchor="center")
        self.tree.column("path", width=820)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        manual_frame = ttk.LabelFrame(paned, text=" Manual Paste Area ", padding=5)
        paned.add(manual_frame, weight=1)
        self.manual_text = scrolledtext.ScrolledText(
            manual_frame, height=5, wrap="word", font=("Consolas", 10)
        )
        self.manual_text.pack(fill="both", expand=True)

        log_frame = ttk.LabelFrame(paned, text=" Activity Log ", padding=5)
        paned.add(log_frame, weight=2)
        self.log_box = tk.Text(
            log_frame,
            height=8,
            state="disabled",
            wrap="word",
            font=("Consolas", 10),
            bg="#f0f0f0",
        )
        self.log_box.pack(fill="both", expand=True)

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.selected_dir.set(folder)
            self._scan_files()

    def _scan_files(self) -> None:
        self.tree.delete(*self.tree.get_children())
        processor = EagleProcessor(self.config, EagleLog(self._log), self._manual_dialog)
        self.files = processor.get_all_files(self.selected_dir.get())
        for file_path in self.files:
            status = processor.build_status(file_path)
            rel_path = os.path.relpath(file_path, self.selected_dir.get())
            self.tree.insert("", "end", values=(status, rel_path))
        self._log(f"Found {len(self.files)} files to process.")

    def _start(self) -> None:
        if not self.selected_dir.get():
            messagebox.showwarning("Warning", "Please select a folder first.")
            return

        self.config.batch_size = self.batch_var.get()
        self.config.auto_copy = self.auto_copy_var.get()
        self.config.model_choice = self.model_choice_var.get()
        self.stop_event.clear()
        self.pause_event.clear()
        self.refresh_event.clear()
        self.pause_btn.config(text="⏸ Pause")

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        self.worker_thread = threading.Thread(target=self._run_processing, daemon=True)
        self.worker_thread.start()
        self.root.after(100, self._poll_status)

    def _toggle_pause(self) -> None:
        if not self.worker_thread or not self.worker_thread.is_alive():
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="⏸ Pause")
            self._log("Resumed processing.")
        else:
            self.pause_event.set()
            self.pause_btn.config(text="▶ Resume")
            self._log("Paused processing.")

    def _stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.refresh_event.clear()
        self.pause_btn.config(text="⏸ Pause")
        self._log("Stop signal sent. Finishing current batch...")
        self.stop_btn.config(state="disabled")

    def _manual_refresh(self) -> None:
        if not self.worker_thread or not self.worker_thread.is_alive():
            messagebox.showinfo("Refresh", "Start processing first to refresh the page.")
            return
        self.refresh_event.set()
        self._log("Manual refresh requested.")

    def _run_processing(self) -> None:
        logger = EagleLog(self.status_queue.put)
        processor = EagleProcessor(
            self.config,
            logger,
            self._manual_dialog,
            self.stop_event,
            self.pause_event,
            self.refresh_event,
        )
        bot = AIClient(self.config, logger, self._current_prompt)

        try:
            page = bot.connect()
            todo_files = [f for f in self.files if "DONE" not in processor.build_status(f)]

            for file_path in todo_files:
                if self.stop_event.is_set():
                    break
                processor.process_file(file_path, bot, page)
                self.status_queue.put(
                    f"UI_UPDATE|{file_path}|{processor.build_status(file_path)}"
                )
        except Exception as exc:
            logger.error(f"Critical error: {exc}")
        finally:
            bot.close()
            self.status_queue.put("PROCESS_FINISHED")

    def _poll_status(self) -> None:
        try:
            while True:
                message = self.status_queue.get_nowait()
                if message == "PROCESS_FINISHED":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self._log("Process finished.")
                elif message.startswith("UI_UPDATE|"):
                    _, path, status = message.split("|")
                    self._update_tree_status(path, status)
                else:
                    self._log(message)
        except queue.Empty:
            pass

        if self.worker_thread and self.worker_thread.is_alive() or not self.status_queue.empty():
            self.root.after(100, self._poll_status)

    def _update_tree_status(self, file_path: str, new_status: str) -> None:
        rel_path = os.path.relpath(file_path, self.selected_dir.get())
        for item in self.tree.get_children():
            if self.tree.item(item)["values"][1] == rel_path:
                self.tree.item(item, values=(new_status, rel_path))
                break

    def _manual_dialog(self, prompt: str, *args: str) -> str:
        manual_text = self.manual_text.get("1.0", "end").strip()
        if manual_text and TranscriptParser.extract_srt_blocks(manual_text):
            self.manual_text.delete("1.0", "end")
            return manual_text

        dialog = ManualDialog(self.root, prompt, *args)
        self.root.wait_window(dialog)
        return dialog.result_text

    def _refresh_prompt_list(self) -> None:
        names = ["Default"] + self.prompt_manager.list_names()
        self.prompt_combo.configure(values=names)
        if self.selected_prompt.get() not in names:
            self.selected_prompt.set("Default")
        self._load_prompt()

    def _load_prompt(self) -> None:
        name = self.selected_prompt.get()
        if name == "Default":
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", SACRED_PROMPT)
            self.delete_prompt_btn.config(state="disabled")
        else:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", self.prompt_manager.get_prompt(name))
            self.delete_prompt_btn.config(state="normal")

    def _new_prompt(self) -> None:
        self.selected_prompt.set("Custom")
        self.prompt_text.delete("1.0", "end")
        self.delete_prompt_btn.config(state="disabled")

    def _save_prompt(self) -> None:
        text = self.prompt_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Prompt", "Prompt text cannot be empty.")
            return
        name = self.selected_prompt.get()
        if name in ("Default", "", "Custom"):
            name = tk.simpledialog.askstring("Prompt Name", "Enter a name for this prompt:")
            if not name:
                return
        self.prompt_manager.upsert_prompt(name, text)
        self.selected_prompt.set(name)
        self._refresh_prompt_list()

    def _delete_prompt(self) -> None:
        name = self.selected_prompt.get()
        if name == "Default":
            return
        if messagebox.askyesno("Delete Prompt", f"Delete '{name}' prompt?"):
            self.prompt_manager.delete_prompt(name)
            self.selected_prompt.set("Default")
            self._refresh_prompt_list()

    def _current_prompt(self) -> str:
        return self.prompt_text.get("1.0", "end").strip() or SACRED_PROMPT

    def _log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.configure(state="disabled")
        self.log_box.see("end")

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Eagle V3 - Improved Subtitle Translator")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--model", choices=list(MODEL_PROFILES.keys()), default="Gemini")
    args = parser.parse_args()

    config = Config(batch_size=args.batch, model_choice=args.model)
    app = EagleGUI(config)
    app.run()


if __name__ == "__main__":
    main()

"""Guided Windows desktop entry point; no input files or configuration required."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import tkinter as tk
from concurrent.futures import Future
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable

from .connections import ConnectionStore, ConnectionStoreError
from .desktop_session import ModelChoice, ModelDiscoveryError, discover_models, run_session, safe_error, validate_choice
from .report import render_markdown
from .schemas import CouncilOutcome, ProductBrief
from .word_report import save_word

PROVIDERS = {"OpenAI": "openai", "Anthropic": "anthropic", "Azure OpenAI": "azure_openai"}


class SessionCancelled(Exception):
    pass


class DesktopApp:
    def __init__(self, root: tk.Tk, *, store: ConnectionStore | None = None) -> None:
        self.root = root
        root.title("Product Council")
        root.geometry("1000x780")
        root.minsize(760, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.pending: Future[str] | None = None
        self.busy = False
        self.closed = False
        self.choice: ModelChoice | None = None
        self.store = store if store is not None else ConnectionStore()
        self.outcome: CouncilOutcome | None = None
        self.idea = ""
        self.brief_draft = ("", "", "")
        self.content = ttk.Frame(root, padding=20)
        self.content.pack(fill="both", expand=True)
        self.credentials_page()
        self.poll_id = root.after(100, self.poll)
        self.resume_id = root.after_idle(self.resume_connection)

    def page(self, title: str, description: str) -> None:
        for widget in self.content.winfo_children():
            widget.destroy()
        ttk.Label(self.content, text=title, font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(self.content, text=description, wraplength=900).pack(anchor="w", pady=(0, 14))

    def entry(self, label: str, *, secret: bool = False) -> ttk.Entry:
        ttk.Label(self.content, text=label).pack(anchor="w", pady=(8, 4))
        entry = ttk.Entry(self.content, show="*" if secret else "", width=90)
        entry.pack(fill="x")
        return entry

    def text_field(self, label: str, height: int) -> ScrolledText:
        ttk.Label(self.content, text=label).pack(anchor="w", pady=(8, 4))
        text = ScrolledText(self.content, height=height, wrap="word", font=("Segoe UI", 10))
        text.pack(fill="both", expand=True)
        return text

    def credentials_page(self) -> None:
        self.remembered_provider: str | None = None
        self.page(
            "1 of 5 - Connect your AI model",
            "Choose a provider, enter your API key, and load available models. A model is preselected; "
            "you do not need to look up its ID. All eight agents and the moderator use "
            "this model with distinct roles. Keys are session-only unless you opt into Windows Credential Manager.\n\n"
            "Validation sends a small test request, which may incur a provider charge. Later, your idea, "
            "answers, and supplied evidence will be sent to this provider. Only share data you are authorized to use.",
        )
        self.provider = tk.StringVar(value="OpenAI")
        ttk.Label(self.content, text="Provider").pack(anchor="w")
        provider = ttk.Combobox(self.content, values=list(PROVIDERS), textvariable=self.provider, state="readonly")
        self.provider_picker = provider
        provider.pack(anchor="w", pady=4)
        self.key = self.entry("API key", secret=True)
        self.key_value = tk.StringVar()
        self.key.configure(textvariable=self.key_value)
        self.endpoint = self.entry("Azure OpenAI HTTPS resource endpoint (Azure only)")
        self.endpoint_value = tk.StringVar()
        self.endpoint.configure(textvariable=self.endpoint_value)
        self.endpoint.configure(state="disabled")
        self.load_button = ttk.Button(self.content, text="Load available models", command=self.load_models)
        self.load_button.pack(anchor="w", pady=12)
        self.manual = tk.BooleanVar(value=False)
        self.manual_button = ttk.Checkbutton(
            self.content, text="Enter model/deployment manually (advanced; required for Azure)",
            variable=self.manual, command=self.reset_models,
        )
        self.manual_button.pack(anchor="w")
        ttk.Label(self.content, text="AI model").pack(anchor="w", pady=(8, 4))
        self.model = ttk.Combobox(self.content, state="readonly", width=85)
        self.model.pack(fill="x")
        self.status = ttk.Label(self.content, text="Enter your API key, then click Load available models.",
                                wraplength=900)
        self.status.pack(anchor="w", pady=12)
        self.remember = tk.BooleanVar(value=False)
        self.remember_button = ttk.Checkbutton(
            self.content, text="Remember this connection on this Windows account",
            variable=self.remember,
        )
        self.remember_button.pack(anchor="w")
        self.forget_button = ttk.Button(self.content, text="Forget saved connection for this provider",
                                       command=self.forget_connection)
        self.forget_button.pack(anchor="w", pady=6)
        self.connect_button = ttk.Button(self.content, text="Validate key and continue", command=self.connect)
        self.connect_button.pack(anchor="e", pady=12)
        provider.bind("<<ComboboxSelected>>", self.provider_changed)
        self.key_value.trace_add("write", lambda *_: self.reset_models())
        self.endpoint_value.trace_add("write", lambda *_: self.reset_models())
        self.key.focus_set()

    def resume_connection(self) -> None:
        try:
            choice = self.store.load()
        except ConnectionStoreError as exc:
            messagebox.showerror("Saved connection unavailable", str(exc), parent=self.root)
            return
        if choice:
            self.fill_connection(choice)
            self.connect()

    def fill_connection(self, choice: ModelChoice) -> None:
        self.remembered_provider = choice.provider
        self.provider.set(next(label for label, provider in PROVIDERS.items() if provider == choice.provider))
        self.endpoint.configure(state="normal")
        self.key_value.set(choice.api_key)
        self.endpoint_value.set(choice.endpoint)
        self.manual.set(choice.provider == "azure_openai")
        self.reset_models()
        self.model.configure(values=[choice.model])
        self.model.set(choice.model)
        if choice.provider != "azure_openai":
            self.endpoint.configure(state="disabled")
        self.remember.set(True)
        self.status.configure(text="Saved connection loaded. Validate to use it, or update the connection details.")

    def forget_connection(self) -> None:
        provider = PROVIDERS[self.provider.get()]
        if not messagebox.askyesno("Forget saved connection?", "Remove this provider's saved connection from "
                                  "Windows Credential Manager? This does not revoke the key at the provider.",
                                  parent=self.root):
            return
        try:
            self.store.forget(provider)
        except ConnectionStoreError as exc:
            messagebox.showerror("Could not forget connection", str(exc), parent=self.root)
            return
        self.remember.set(False)
        self.remembered_provider = None
        self.key_value.set("")
        self.endpoint_value.set("")
        self.choice = None
        self.status.configure(text="Saved connection removed. Enter a key to connect again.")

    def provider_changed(self, _: Any = None) -> None:
        self.remembered_provider = None
        self.key_value.set("")
        self.endpoint_value.set("")
        self.remember.set(False)
        azure = self.provider.get() == "Azure OpenAI"
        self.endpoint.configure(state="normal" if azure else "disabled")
        self.manual.set(azure)
        self.reset_models()
        try:
            saved = self.store.load(PROVIDERS[self.provider.get()])
        except ConnectionStoreError as exc:
            messagebox.showerror("Saved connection unavailable", str(exc), parent=self.root)
            return
        if saved:
            self.fill_connection(saved)

    def reset_models(self) -> None:
        self.model.set("")
        self.model.configure(values=())
        azure = self.provider.get() == "Azure OpenAI"
        if azure:
            self.manual.set(True)
        self.model.configure(state="normal" if self.manual.get() else "readonly")
        self.load_button.configure(state="disabled" if azure or self.manual.get() else "normal")
        self.status.configure(text=(
            "Azure deployment names are organization-specific. Ask your resource administrator for yours."
            if azure else "Enter a model ID supplied by your provider or administrator."
            if self.manual.get() else "Click Load available models to preselect a chat model for this key."
        ))

    def credentials_busy(self, busy: bool) -> None:
        for control in (self.provider_picker, self.key, self.endpoint, self.model, self.load_button,
                        self.manual_button, self.connect_button, self.remember_button, self.forget_button):
            control.configure(state="disabled" if busy else "normal")
        if not busy:
            self.provider_picker.configure(state="readonly")
            self.model.configure(state="normal" if self.manual.get() else "readonly")
            if self.provider.get() != "Azure OpenAI":
                self.endpoint.configure(state="disabled")
            if self.manual.get():
                self.load_button.configure(state="disabled")

    def load_models(self) -> None:
        choice = ModelChoice(PROVIDERS[self.provider.get()], "", self.key.get().strip(), self.endpoint.get().strip())
        try:
            choice.spec(require_model=False)
        except ValueError as exc:
            messagebox.showerror("Connection details required", str(exc), parent=self.root)
            return
        self.credentials_busy(True)
        self.status.configure(text="Loading the provider's model catalog...")
        self.work(lambda: discover_models(choice), "models_loaded")

    def connect(self) -> None:
        choice = ModelChoice(PROVIDERS[self.provider.get()], self.model.get().strip(),
                             self.key.get().strip(), self.endpoint.get().strip())
        try:
            choice.spec()
        except ValueError as exc:
            messagebox.showerror("Connection details required", str(exc), parent=self.root)
            return
        self.choice = choice
        self.credentials_busy(True)
        self.status.configure(text="Validating the key and access to your chosen model...")
        self.work(lambda: validate_choice(choice), "connected")

    def idea_page(self) -> None:
        self.page("2 of 5 - Your productization idea",
                  "Describe the customer or market gap, who experiences it, and what you are considering building.")
        self.idea_text = self.text_field("Productization idea", 14)
        self.idea_text.insert("1.0", self.idea)
        ttk.Button(self.content, text="Manage connections", command=self.manage_connections).pack(anchor="w", pady=8)
        ttk.Button(self.content, text="Continue", command=self.accept_idea).pack(anchor="e", pady=12)

    def manage_connections(self) -> None:
        self.idea = self.idea_text.get("1.0", "end").strip()
        choice = self.choice
        self.credentials_page()
        if choice:
            self.fill_connection(choice)
            try:
                saved = self.store.load(choice.provider)
                self.remember.set(saved is not None)
                self.remembered_provider = choice.provider if saved else None
            except ConnectionStoreError as exc:
                self.remember.set(False)
                messagebox.showerror("Saved connection unavailable", str(exc), parent=self.root)

    def accept_idea(self) -> None:
        self.idea = self.idea_text.get("1.0", "end").strip()
        if not self.idea:
            messagebox.showerror("Idea required", "Enter your productization idea.", parent=self.root)
            return
        self.brief_page()

    def brief_page(self) -> None:
        self.page("3 of 5 - Define the desired outcome",
                  "Define the hypothesis and targets before discussion. These are goals, not proof of demand or success.")
        self.hypothesis = self.text_field("Hypothesis: what change will address the gap, and why?", 4)
        self.expected = self.text_field("Expected results: what should happen after release?", 4)
        self.metrics = self.text_field("Success criteria: one metric per line, with a target, baseline, and timeframe", 4)
        for control, value in zip((self.hypothesis, self.expected, self.metrics), self.brief_draft):
            control.insert("1.0", value)
        buttons = ttk.Frame(self.content)
        buttons.pack(fill="x", pady=12)
        ttk.Button(buttons, text="Back", command=self.back_to_idea).pack(side="left")
        ttk.Button(buttons, text="Start council discussion", command=self.start_council).pack(side="right")

    def back_to_idea(self) -> None:
        self.brief_draft = tuple(control.get("1.0", "end").strip()
                                 for control in (self.hypothesis, self.expected, self.metrics))
        self.idea_page()

    def start_council(self) -> None:
        brief = ProductBrief(
            self.hypothesis.get("1.0", "end").strip(),
            self.expected.get("1.0", "end").strip(),
            [line.strip() for line in self.metrics.get("1.0", "end").splitlines() if line.strip()],
        )
        try:
            brief.validate()
        except ValueError as exc:
            messagebox.showerror("All fields are required", str(exc), parent=self.root)
            return
        assert self.choice is not None
        choice = self.choice
        self.page("4 of 5 - Council discussion",
                  "The council will ask for clarification or evidence when needed. You may leave a follow-up "
                  "blank if unknown; the council may then report no consensus. This app does not browse the web "
                  "or use fictional sample evidence.")
        self.progress = ttk.Progressbar(self.content, mode="indeterminate")
        self.progress.pack(fill="x", pady=8)
        self.progress.start()
        self.log = self.text_field("Live progress", 20)
        self.log.configure(state="disabled")

        async def clarify(questions: list[str]) -> dict[str, str]:
            return {q: await self.ask(f"Clarifying question {i} of {len(questions)}", q)
                    for i, q in enumerate(questions, 1)}

        async def research(agenda: str) -> str:
            return await self.ask("Additional evidence needed",
                                  agenda + "\n\nSupply facts, source names, dates, or interview notes. "
                                  "Leave blank if you have no additional evidence.")

        self.work(lambda: run_session(choice, self.idea, brief, clarify=clarify, ask_research=research,
                                      on_event=lambda kind, data: self.events.put(("progress", (kind, data)))),
                  "result")

    async def ask(self, title: str, prompt: str) -> str:
        future: Future[str] = Future()
        self.events.put(("question", (title, prompt, future)))
        return await asyncio.wrap_future(future)

    def question_dialog(self, title: str, prompt: str, future: Future[str]) -> None:
        self.pending = future
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("760x570")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        question = ScrolledText(frame, height=12, wrap="word")
        question.pack(fill="both", expand=True)
        question.insert("1.0", prompt)
        question.configure(state="disabled")
        ttk.Label(frame, text="Your answer (blank means unknown / no additional evidence)").pack(anchor="w", pady=8)
        answer = ScrolledText(frame, height=8, wrap="word")
        answer.pack(fill="both", expand=True)

        def submit() -> None:
            if not future.done():
                future.set_result(answer.get("1.0", "end").strip())
            dialog.destroy()

        def cancel() -> None:
            if messagebox.askyesno("Cancel council?", "Stop this session without saving a report?", parent=dialog):
                if not future.done():
                    future.set_exception(SessionCancelled())
                dialog.destroy()

        ttk.Button(frame, text="Submit answer / continue", command=submit).pack(anchor="e", pady=10)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.grab_set()
        answer.focus_set()
        self.root.wait_window(dialog)
        self.pending = None

    def work(self, factory: Callable[[], Any], success: str) -> None:
        self.busy = True

        def worker() -> None:
            try:
                result = asyncio.run(factory())
            except SessionCancelled:
                self.events.put(("cancelled", None))
            except ModelDiscoveryError as exc:
                self.events.put(("connection_failed", str(exc)))
            except Exception as exc:
                self.events.put(("connection_failed" if success in ("connected", "models_loaded") else "failed",
                                 safe_error(exc)))
            else:
                self.events.put((success, result))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self) -> None:
        try:
            while not self.closed:
                event, data = self.events.get_nowait()
                if event == "question":
                    self.question_dialog(*data)
                elif event == "progress":
                    kind, payload = data
                    self.log.configure(state="normal")
                    self.log.insert("end", f"{kind}: {json.dumps(payload, ensure_ascii=False)}\n\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif event == "connected":
                    self.busy = False
                    assert self.choice is not None
                    try:
                        if self.remember.get():
                            self.store.save(self.choice)
                        elif self.remembered_provider == self.choice.provider:
                            self.store.forget(self.choice.provider)
                    except ConnectionStoreError as exc:
                        self.credentials_busy(False)
                        messagebox.showerror("Connection storage failed", str(exc), parent=self.root)
                        continue
                    self.key.configure(state="normal")
                    self.key.delete(0, "end")
                    self.idea_page()
                elif event == "models_loaded":
                    self.busy = False
                    self.model.configure(values=data)
                    self.model.set(data[0])
                    self.credentials_busy(False)
                    self.status.configure(text=(
                        f"Loaded {len(data)} chat model candidates. The newest listed candidate is preselected. "
                        "You may choose another. Model availability and JSON support will be validated next; "
                        "newest listed does not guarantee the latest or best model. Provider charges vary."
                    ))
                elif event == "connection_failed":
                    self.busy = False
                    self.choice = None
                    self.credentials_busy(False)
                    self.status.configure(text=data)
                    messagebox.showerror("Connection unsuccessful", data, parent=self.root)
                elif event == "result":
                    self.busy = False
                    self.progress.stop()
                    self.outcome = data
                    self.choice = None
                    self.preview_page()
                elif event in ("failed", "cancelled"):
                    self.busy = False
                    self.choice = None
                    messagebox.showerror("Session stopped", data or "Cancelled. No report was saved.", parent=self.root)
                    self.credentials_page()
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_id = self.root.after(100, self.poll)

    def preview_page(self) -> None:
        assert self.outcome is not None
        self.page("5 of 5 - Review and approve",
                  "Review the proposal, dissent, forecast, and evidence. No document has been saved. "
                  "Approval authorizes export only, not product release. No consensus is a valid outcome.")
        tabs = ttk.Notebook(self.content)
        tabs.pack(fill="both", expand=True)
        for title, text in (
            ("Proposal preview", render_markdown(self.outcome)),
            ("Full audit details", json.dumps(self.outcome.to_dict(), indent=2, ensure_ascii=False)),
        ):
            panel = ttk.Frame(tabs)
            tabs.add(panel, text=title)
            preview = ScrolledText(panel, wrap="word", font=("Segoe UI", 10))
            preview.pack(fill="both", expand=True)
            preview.insert("1.0", text)
            preview.configure(state="disabled")
        buttons = ttk.Frame(self.content)
        buttons.pack(fill="x", pady=12)
        ttk.Button(buttons, text="Discard without saving", command=self.discard).pack(side="left")
        ttk.Button(buttons, text="Approve preview and save Word document", command=self.export).pack(side="right")

    def export(self) -> None:
        assert self.outcome is not None
        filename = filedialog.asksaveasfilename(
            parent=self.root, title="Save approved proposal", defaultextension=".docx",
            initialfile="Product Council Proposal.docx", filetypes=[("Microsoft Word document", "*.docx")],
        )
        if not filename:
            return
        try:
            save_word(self.outcome, Path(filename), approved=True)
        except (OSError, ValueError, ImportError) as exc:
            messagebox.showerror("Export failed", f"The Word document could not be saved: {exc}\n"
                                 "Your preview is still available; choose another location and try again.",
                                 parent=self.root)
            return
        messagebox.showinfo("Word document saved", f"Saved to:\n{filename}", parent=self.root)

    def discard(self) -> None:
        if messagebox.askyesno("Discard preview?", "Discard the result without generating a document?", parent=self.root):
            self.outcome = None
            self.idea = ""
            self.brief_draft = ("", "", "")
            self.credentials_page()

    def close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "Exit Product Council?", "Stop the session? No report will be saved. Requests already sent "
            "to your provider may still be charged.", parent=self.root,
        ):
            return
        self.closed = True
        self.root.after_cancel(self.poll_id)
        self.root.after_cancel(self.resume_id)
        if self.pending and not self.pending.done():
            self.pending.set_exception(SessionCancelled())
        self.choice = None
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

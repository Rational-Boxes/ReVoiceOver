"""Voice, synthesized-take and QA actions for one MainFrame (kept out of main_frame.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import wx

from narration.gui.panels.qa_panel import QACallbacks, QAPanel
from narration.gui.panels.voice_panel import VoiceCallbacks, VoicePanel
from narration.stages.synth import resynth

if TYPE_CHECKING:
    from narration.gui.main_frame import MainFrame

FIELD_MSG = 0


class ReviewActions:
    def __init__(self, frame: MainFrame) -> None:
        self.frame = frame

    # -- tabs --------------------------------------------------------------
    def make_voice_panel(self, parent: wx.Window) -> VoicePanel:
        # The timeline is created after the tabs, so look it up at call time.
        self.voice_panel = VoicePanel(parent, VoiceCallbacks(
            play_file=lambda p: self.frame.timeline.play_file(p, show_playhead=False),
            play_span=lambda a, b: self.frame.timeline.play_span(a, b),
            use_span=lambda a, b: self.set_voice({"ref_span": [a, b], "ref": ""}),
            use_file=lambda p: self.set_voice({"ref": str(p), "ref_span": None}),
            use_auto=lambda: self.set_voice({"ref": "", "ref_span": None})))
        return self.voice_panel

    def make_qa_panel(self, parent: wx.Window) -> QAPanel:
        self.qa_panel = QAPanel(parent, QACallbacks(
            play_tts=self.play_tts, new_take=self.new_take, edit=self.edit_unit,
            accept=self.accept_qa, select=self.frame.select_unit))
        return self.qa_panel

    # -- results -----------------------------------------------------------
    def refresh_results(self) -> None:
        """Reload synth/QA results into the script, QA tab and timeline marks."""
        script = self.frame.script
        script.reload_results()
        self.qa_panel.load(script.model)
        marks = {}
        for u in script.model.units:
            flags = script.model.all_flags(u)
            if flags:
                severe = "dropped-content" in flags or any(f.startswith("qa-") for f in flags)
                marks[u.id] = "error" if severe else "warn"
        self.frame.timeline.set_marks(marks)

    def set_busy(self, busy: bool) -> None:
        self.qa_panel.set_busy(busy)

    # -- actions -----------------------------------------------------------
    def play_tts(self, unit_id: str) -> None:
        frame = self.frame
        clip = frame.script.clips.get(unit_id)
        if clip is not None and frame.project is not None:
            # The clip is the unit's whole speech group, so play it from the group start.
            frame.timeline.play_tts(unit_id, frame.project.work_dir / clip["file"],
                                    start=clip.get("start"))

    def new_take(self, unit_id: str) -> None:
        """Re-synthesize one unit in the background, then play it."""
        frame = self.frame
        unit = frame.script.model.get(unit_id)
        if frame.project is None or unit is None or frame.runner.running or not unit.clean_text:
            return
        if not (frame.project.work_dir / "voice.json").exists():
            frame.infobar.ShowMessage("Run the Voice stage first (Approve & Continue).",
                                      wx.ICON_INFORMATION)
            return
        clip = frame.script.clips.get(unit_id)
        # Clips from before "text" was recorded count as unchanged text.
        same_text = clip is not None and clip.get("text", unit.clean_text) == unit.clean_text
        seed = int(clip["seed"]) + 1 if clip is not None and same_text else None
        snapshot = type(unit).from_dict(unit.to_dict())  # the worker gets its own copy

        def done(_result: object, error: BaseException | None) -> None:
            wx.CallAfter(self._on_take_done, unit_id, error)

        frame.runner.start_job(frame.project, lambda ctx: resynth(ctx, snapshot, seed), done)
        frame.SetStatusText(f"Synthesizing {unit_id}…", FIELD_MSG)
        frame.update_ui()

    def _on_take_done(self, unit_id: str, error: BaseException | None) -> None:
        frame = self.frame
        if not frame:
            return
        frame.runner.join(timeout=2)  # the worker reports just before its thread exits
        frame.update_ui()
        if error is not None:
            frame.SetStatusText(f"{unit_id}: synthesis failed", FIELD_MSG)
            frame.infobar.ShowMessage(f"Could not synthesize {unit_id}: {error}", wx.ICON_ERROR)
            return
        frame.SetStatusText(f"{unit_id}: new take ready — run Quality check to verify it",
                            FIELD_MSG)
        frame.refresh_status()
        self.refresh_results()
        self.play_tts(unit_id)

    def edit_unit(self, unit_id: str) -> None:
        frame = self.frame
        frame.notebook.SetSelection(frame.notebook.FindPage(frame.script))
        frame.select_unit(unit_id)
        frame.script.editor.focus_text()

    def accept_qa(self, unit_id: str) -> None:
        self.frame.script.model.accept_qa(unit_id)
        self.refresh_results()

    def set_voice(self, overrides: dict[str, object]) -> None:
        """Change the voice reference for this video, then re-run the Voice stage."""
        frame = self.frame
        if frame.project is None or frame.runner.running:
            return
        frame.project.apply_overrides({"voice": overrides})
        frame.run_only("voice")

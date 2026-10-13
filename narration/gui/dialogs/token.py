"""DeepInfra token dialog: store or remove the token in the system keyring."""

from __future__ import annotations

import wx

from narration import credentials

SOURCE_TEXT = {
    "env": f"In use: ${credentials.ENV_VAR} from the environment (it overrides the keyring).",
    "keyring": "In use: token stored in the system keyring.",
    "none": "No token set. Stages that call DeepInfra will fail until one is stored.",
}


class TokenDialog(wx.Dialog):
    def __init__(self, parent: wx.Window | None) -> None:
        super().__init__(parent, title="DeepInfra Token")
        self.status = wx.StaticText(self)
        self.entry = wx.TextCtrl(self, style=wx.TE_PASSWORD, size=wx.Size(380, -1))
        self.entry.SetHint("Paste a new token to store it")
        note = wx.StaticText(self, label="The token is kept only in the system keyring — never in "
                                         "project files, config files or logs.")
        note.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        note.Wrap(420)
        self.save_btn = wx.Button(self, label="Save to Keyring")
        self.remove_btn = wx.Button(self, label="Remove from Keyring")
        close_btn = wx.Button(self, wx.ID_CLOSE)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.remove_btn)
        buttons.AddStretchSpacer()
        buttons.Add(self.save_btn, 0, wx.RIGHT, 6)
        buttons.Add(close_btn)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.status, 0, wx.EXPAND | wx.ALL, 12)
        sizer.Add(self.entry, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        sizer.Add(note, 0, wx.EXPAND | wx.ALL, 12)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()
        self.SetEscapeId(wx.ID_CLOSE)

        self.save_btn.Bind(wx.EVT_BUTTON, self._on_save)
        self.remove_btn.Bind(wx.EVT_BUTTON, self._on_remove)
        close_btn.Bind(wx.EVT_BUTTON, lambda _e: self.EndModal(wx.ID_CLOSE))
        self.entry.Bind(wx.EVT_TEXT, lambda _e: self._refresh())
        self._refresh()

    def _refresh(self) -> None:
        _, source = credentials.get_token()
        self.status.SetLabel(SOURCE_TEXT[source])
        self.save_btn.Enable(bool(self.entry.GetValue().strip()))
        self.remove_btn.Enable(credentials.keyring_token() is not None)
        self.Layout()

    def _on_save(self, _event: wx.CommandEvent) -> None:
        try:
            credentials.set_keyring_token(self.entry.GetValue())
        except Exception as exc:
            wx.MessageBox(f"Could not store the token in the keyring:\n{exc}", "Keyring error",
                          wx.OK | wx.ICON_ERROR, self)
            return
        self.entry.Clear()
        self._refresh()

    def _on_remove(self, _event: wx.CommandEvent) -> None:
        try:
            credentials.delete_keyring_token()
        except Exception as exc:
            wx.MessageBox(f"Could not remove the token:\n{exc}", "Keyring error",
                          wx.OK | wx.ICON_ERROR, self)
        self._refresh()

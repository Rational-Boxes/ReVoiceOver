# Releases

The packaging scripts in [`packaging/`](../packaging) write finished installers here:

| File | Made by |
|---|---|
| `ReVoiceOver-<version>-linux-<arch>.flatpak` | `packaging/flatpak/build.sh` |
| `ReVoiceOver-<version>-macos-<arch>.dmg` | `packaging/macos/build.sh` |
| `ReVoiceOver-<version>-windows-x64-setup.exe` | `packaging\windows\build.ps1` |

Installers are not committed to git: they are large, and GitHub rejects files over
100 MB. They are published as assets on the
[releases page](https://github.com/Rational-Boxes/ReVoiceOver/releases),
which the Package workflow drafts for every `v*` tag.

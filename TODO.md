# TODO

Open work-items, lowest-effort first. Strikethrough or delete entries as they ship.

## macOS KNOWN_ISSUES audit pass
Document macOS-specific quirks that the May 2026 macOS-support pass didn't investigate:
- Gatekeeper / quarantine behavior on un-notarized VST bundles
- universal2 vs arm64-only plugin builds (Rosetta implications)
- The recurring `attempt to map invalid URI` JUCE stderr noise on every plugin load

Output: new entries in `KNOWN_ISSUES.md`.

## Add CI job that installs from git URL
README documents `pip install git+https://github.com/wiillownet/vst-render.git` but CI only exercises the editable install. Add a job that runs the git URL install and confirms `vst-render --help` works — catches packaging regressions.

## VST3 preset support (`.vstpreset`) — blocked on separate Serum 2 investigation
Move beyond legacy VST2 `.fxp` to `.vstpreset` so Serum 2 (VST3-only) and any post-2024 plugin works. Touches `presets.py`, `worker.py`, CLI plugin-path validation, README, CLAUDE.md.

## Add Vital as second supported plugin
Vital is free + cross-platform, so adding it as a second supported plugin both proves the architecture isn't Serum-specific and unlocks a real integration test in CI (Serum can't ship on CI runners; Vital can). Note Vital uses `.vital` preset format, not `.fxp`.

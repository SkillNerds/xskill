# Dashboard light and dark themes

**Goal:** Add the approved sun/moon segmented control to the global dashboard header; keep light as the default and remember an explicit choice in this browser.

**Architecture:** A small inline head script applies the saved `.dark` class before CSS paints and binds accessible buttons after DOM readiness. Existing Tailwind components use explicit `dark:` variants. Custom CSS and SVG colors use theme variables so existing and newly rendered diagrams switch without refetching. Keep the frontend self-contained and the existing two static routes.

**Scope:** All nine dashboard sections, skill/trajectory/atom details, dialogs, notification surfaces and graphs. No backend preference, system mode, new runtime dependency or unrelated user edits. Work in the existing checkout on `codex/dashboard-theme`; defer all commits until local verification is complete, as requested.

- [x] Add regression tests in `tests/test_dashboard_theme.py`: default/invalid preference, saved dark at first paint, switching and persistence, inaccessible storage, cross-tab preference, accessible button markup and compiled CSS parity. Confirm all seven tests fail for missing functionality before implementation.
- [x] Add the inline initializer and sun/moon control to `static/index.html`; verify behavior tests pass. Catch storage read/write failures without disabling the toggle.
- [x] Enable manual dark mode in `static/tailwind.config.js`; add variants to static and JS-rendered components. Add custom color variables and replace diagram literals in `static/app.js` without altering data flow. Rebuild Tailwind 3.4.19 and inline the exact `tw.css` content following `BUILD.md`.
- [x] Run frontend and dashboard regression tests plus the repository's unit-test command (Windows equivalent of `make test`). Run JavaScript syntax and whitespace checks.
- [x] Verify both themes in the local browser: toggle, route changes, refresh persistence, keyboard, login dialog, pipeline, skill/trajectory/atom details and graph colors. Verify administrator surfaces using isolated fixture data and save light/dark screenshots outside the repo.
- [x] Review the complete diff. An independent read-only review found no actionable issues. Keep the commit restricted to theme-related files.

## Verification results

- Theme and frontend smoke tests: 17 passed.
- Dashboard regression run: 230 passed; five failures were caused by Git 2.27 lacking `git init -b`. Both affected test files were rerun with the bundled Git 2.56: all 30 tests passed, including those five cases.
- Full suite attempt with the newer Git: 39 passed, 6 skipped, then an unrelated setup error in `tests/e2e/test_team_cs_e2e.py::test_team_cs_full_loop`. The existing local daemon prevented the test server from starting. The running daemon was left untouched; the full suite is not claimed to pass.
- `node --check`, `git diff --check`, compiled/inlined CSS parity, and the check for undefined theme variables passed.
- Browser verification confirmed SVG computed colors change while graph DOM remains unchanged, and trajectory input/detail state survives theme switching.

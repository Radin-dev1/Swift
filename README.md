# Swift - fast app control for Claude (Windows)

Lets Claude work in any app on your PC without the screenshot-think-click loop.

## Install
1. Unzip this folder somewhere permanent (e.g. `C:\Users\you\Projects\swift`).
2. Double-click `install.bat`. It installs one package (`comtypes`) and adds
   Swift to Claude's config (a backup of the config is kept).
3. Fully quit Claude (tray icon > Quit) and reopen it.

Then ask: "use swift to open Notepad and type hello".

## How it works
- Claude starts every task with `look`: one screenshot of the app plus the list
  of its controls. If it isn't sure what a button does, it searches the web for
  that app first, then sends the whole plan as a single batch.
- `see` reads the window's buttons, fields and menus as text through Windows
  UI Automation, in one call. No screenshot.
- `act` runs many steps in one call and returns the new window state.
- Clicks go straight to the control when the app allows it, so your own mouse
  stays where it is. An orange "Claude" cursor shows where Claude is working.
- `screenshot` is the fallback for apps with no readable UI (games, Blender's
  3D viewport, canvases).

## Working in the background
Claude works in one app at a time and does not bring it to the front, move
your mouse or use your keyboard, so you can watch a video or work in another
app meanwhile. Keep Claude's app open (it can be behind other windows; if it
is minimized, Swift un-minimizes it behind your window).

Limits: some apps ignore background typing/clicks (many Electron and
browser-based apps, games). Shortcuts like Ctrl+S don't work in the background,
so Claude uses menus and buttons instead. If something can only be done in the
front, Claude should ask you before switching to foreground mode.

## Controls
- **Ctrl+Alt+P** pauses / resumes Claude instantly.
- Password fields are never typed into.
- Hide the Claude cursor: set the environment variable `SWIFT_CURSOR=0`.

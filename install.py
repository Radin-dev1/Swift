"""Adds Swift to the Claude desktop app's MCP config (keeps a backup)."""
import glob
import json
import os
import shutil
import sys

here = os.path.dirname(os.path.abspath(__file__))
entry = {"command": sys.executable, "args": [os.path.join(here, "server.py")]}

candidates = [os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude_desktop_config.json")]
candidates += glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*",
                                     "LocalCache", "Roaming", "Claude", "claude_desktop_config.json"))
path = next((p for p in candidates if os.path.isfile(p)), None)

snippet = json.dumps({"mcpServers": {"swift": entry}}, indent=2)
if not path:
    print("Could not find claude_desktop_config.json.")
    print("In Claude: Settings > Developer > Edit Config, then add this:\n")
    print(snippet)
    sys.exit(0)

with open(path, "r", encoding="utf-8") as f:
    cfg = json.load(f)
shutil.copyfile(path, path + ".swift-backup")
cfg.setdefault("mcpServers", {})["swift"] = entry
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
print("Added 'swift' to", path)
print("Backup saved as", path + ".swift-backup")
print("Now fully quit Claude (tray icon > Quit) and open it again.")

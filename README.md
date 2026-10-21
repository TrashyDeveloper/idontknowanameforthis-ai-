# Aria

A self-learning chat assistant for **SCP: Secret Laboratory** that runs in your browser.

- Answers questions about the game: SCPs, classes, items, zones, strategies.
- Understands casual sentences, slang and typos: *"yo can u make me a custum item exmple in exild"* works.
- Learns from you: 👍 teaches her how you ask, 👎 corrects her, and she remembers what you tell her.
- Helps write **C# server plugins** (EXILED or LabAPI). She knows the whole EXILED API, and copies the
  style of the working examples you give her.

## Quick start (Windows)

1. Install [Python](https://www.python.org/downloads/) and tick **"Add python.exe to PATH"** in the installer.
2. Download this project and unzip it.
3. Double-click **`start-aria.bat`**. The first start sets everything up, then Aria opens in your browser.

Keep the black window open while you use Aria; close it (or press Ctrl+C) to stop her.

## Quick start (any system)

You need [Python 3.10+](https://www.python.org/downloads/).

```bash
# 1. Get the code
git clone https://github.com/TrashyDeveloper/idontknowanameforthis-ai-.git
cd idontknowanameforthis-ai-

# 2. Install what it needs (in a virtual environment)
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

# 3. Run it
python app.py
```

Aria opens **http://127.0.0.1:5000** in your browser by itself (set `ARIA_NO_BROWSER=1` to turn that off).

## Two modes

| | Offline mode | Claude mode |
|---|---|---|
| Needs | Nothing | An Anthropic API key (paid per use) |
| Answers | Shows what she knows that matches best | Written by Claude using what she knows |
| Plugins | Shows the closest example she knows | Writes new plugins based on what she knows |

The badge in the top right of the page shows the current mode.

### Turning on Claude mode

1. Create an API key at [console.anthropic.com](https://console.anthropic.com/) (you need to add credit).
2. Save the key so Aria can find it:

   ```bash
   # Windows (PowerShell), once; then open a new window
   setx ANTHROPIC_API_KEY "sk-ant-..."
   # macOS/Linux
   export ANTHROPIC_API_KEY="sk-ant-..."

   python app.py
   ```

Never put your key in the code or commit it to GitHub.

Optional settings (environment variables):

- `ARIA_MODEL`: which Claude model to use (default `claude-opus-5-5`).
- `ARIA_EFFORT`: how hard it thinks: `low`, `medium` (default) or `high`. Lower is faster and cheaper.
- `PORT`: the web port (default `5000`).

## How she understands you

Offline, Aria works with words rather than true understanding, so she's built to be forgiving:

- **Filler is ignored:** "I need you Aria to do an example about…" is read as "example about…".
- **Synonyms:** "damage" also finds "hurt", "demo" finds "example", "gun" finds "weapon", and so on.
- **Typos are fixed** against the words she knows: "exild" → exiled, "custum" → custom.
- **Nicknames:** "peanut" is SCP-173, "larry" is SCP-106, "shy guy" is SCP-096, "nuke" is the warhead.
- **Code names** are split up: `ChangingLeverStatus` matches "changing lever status".
- **What you want:** "make/write/give me … example/plugin" gets code; "show me / what do you know about / list"
  gets everything related; when she isn't sure, she shows the closest thing she knows.

In Claude mode, Claude reads your message as written, so any phrasing works.

## How she learns

| You do | She learns |
|---|---|
| Press 👍 on an answer | Trusts that knowledge more, **and links the words you used to it**, so asking the same way finds it straight away |
| Press 👎 and type the right answer | Saves your answer and trusts the old knowledge less |
| Say `my favorite SCP is 106` or `our server has 30 slots` | Remembers it ("my/our … is/are/has …") |
| Say `remember: the server restarts at 6pm` | Remembers it |
| Say `tp means teleport` (or use **Teach → Teach a word**) | A new word: "tp" now also searches for "teleport" |
| Say `forget that my favorite SCP is 106` | Forgets it |
| **Teach** tab → *Teach a fact* | Saves a fact or strategy |
| **Teach** tab → *Add a plugin code example* | Saves C# code she uses as a model for new plugins |
| **Knowledge** tab → Delete / Hide / Restore | Deletes what you taught, hides starter knowledge you think is wrong, or brings it back |

Things you teach rank above the starter knowledge. Everything she learns is saved in
`data/learned.json`. That file is kept out of git so your learned data stays on your PC.
To back it up, use **Export learned** in the Knowledge tab, or copy the file.

### Teaching it plugins

The best way to get good plugin code is to give her **working examples from your own server**
(Teach tab → *Add a plugin code example*). The EXILED starter examples (welcome message, spawn items,
heal command, custom role, custom item, custom ability) were checked against the EXILED 9.15.2 source,
but not compiled, because that needs the game's server files. When you find code that compiles and
works, add it as an example.

## Knowledge packs

Aria also loads **knowledge packs** from `data/packs/`. These are reference material generated from real
source code, used when you ask about plugins or code (gameplay questions don't use them).

**EXILED** (`data/packs/exiled.json`) was generated from the
[EXILED repository](https://github.com/ExMod-Team/EXILED) and contains:

- all 292 events: what each does, what `ev` contains, whether it can be cancelled, which game method it hooks
  into, and how to subscribe
- the public API (`Player`, `Server`, `Map`, `Room`, `Door`, items, roles, ...), enums (`DamageType`, `EffectType`, ...)
  and the event-args interfaces
- the **CustomRoles** and **CustomItems** APIs (custom roles, abilities and items)
- real source code: the official example plugin, the CustomRoles/CustomItems commands and event handlers,
  and Exiled.Events' commands, features, internal handlers, config and project file
- all 321 Harmony patches: which game method each changes and which events it fires (the prefix/postfix
  ones are kept in full as Harmony examples)

To update it when EXILED releases a new version (needs internet):

```bash
python tools/learn_exiled.py
```

Then restart Aria. The EXILED pack is derived from EXILED by the ExMod Team and is shared under the
same [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/) license.

## Project layout

```
app.py                     web server (Flask) and API
start-aria.bat             double-click to start on Windows
aria/language.py           understanding: filler words, synonyms, nicknames, word endings, what you're asking for
aria/knowledge.py          knowledge base: storage, search, typo fixing, learning
aria/brain.py              builds answers (offline or with Claude)
data/seed_knowledge.json   starter knowledge about the game and plugins
data/packs/                knowledge packs (e.g. the EXILED API)
data/learned.json          what it learned from you (created automatically)
tools/learn_exiled.py      rebuilds the EXILED pack from EXILED's source code
templates/, static/        the web page
tests/                     automated tests: run with `pytest`
```

## Note

The game gets updated often, so some starter facts may be out of date. If you find one, correct it
with 👎 or hide it in the Knowledge tab. This is a fan project and is not affiliated with Northwood Studios.

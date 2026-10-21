# SCP-BOT

A self-learning chat assistant for **SCP: Secret Laboratory** that runs in your browser.

- Answers questions about the game: SCPs, classes, items, zones, strategies.
- Learns from you: correct it with 👎, teach it facts, or type `remember: ...` in the chat.
- Helps write **C# server plugins** (LabAPI or EXILED). Give it working code examples and it copies their style and framework.

## Quick start

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

Then open **http://127.0.0.1:5000** in your browser.

## Two modes

| | Offline mode | Claude mode |
|---|---|---|
| Needs | Nothing | An Anthropic API key (paid per use) |
| Answers | Straight from what it knows, word for word | Written by Claude using what it knows |
| Plugins | Shows the closest example it was taught | Writes new plugins based on your examples |

The badge in the top right of the page shows the current mode.

### Turning on Claude mode

1. Create an API key at [console.anthropic.com](https://console.anthropic.com/) (you need to add credit).
2. Set the key before you start the app:

   ```bash
   # Windows (PowerShell)
   $env:ANTHROPIC_API_KEY = "sk-ant-..."
   # macOS/Linux
   export ANTHROPIC_API_KEY="sk-ant-..."

   python app.py
   ```

Never put your key in the code or commit it to GitHub.

Optional settings (environment variables):

- `SCPBOT_MODEL`: which Claude model to use (default `claude-opus-5-5`).
- `SCPBOT_EFFORT`: how hard it thinks: `low`, `medium` (default) or `high`. Lower is faster and cheaper.
- `PORT`: the web port (default `5000`).

## How it learns

| You do | It learns |
|---|---|
| Press 👍 on an answer | Trusts the knowledge it used more |
| Press 👎 and type the right answer | Saves your answer and trusts the old knowledge less |
| Type `remember: the server restarts at 6pm` | Saves that fact |
| **Teach** tab → *Teach a fact* | Saves a fact or strategy |
| **Teach** tab → *Add a plugin code example* | Saves C# code it uses as a model for new plugins |
| **Knowledge** tab → Delete / Hide | Forgets something that's wrong |

Things you teach always rank above the starter knowledge. Everything it learns is saved in
`data/learned.json`. That file is kept out of git so your learned data stays on your PC.
To back it up, use **Export learned** in the Knowledge tab, or copy the file.

### Teaching it plugins

The best way to get good plugin code is to give it **working examples from your own server**
(Teach tab → *Add a plugin code example*). The starter examples in `data/seed_knowledge.json`
were written from memory and are not tested against the latest game version, so check
them before relying on them. When you find code that compiles and works, add it as an example.

## Project layout

```
app.py                     web server (Flask) and API
scpbot/knowledge.py        knowledge base: storage, search, learning
scpbot/brain.py            builds answers (offline or with Claude)
data/seed_knowledge.json   starter knowledge about the game and plugins
data/learned.json          what it learned from you (created automatically)
templates/, static/        the web page
tests/                     automated tests: run with `pytest`
```

## Note

The game gets updated often, so some starter facts may be out of date. If you find one, correct it
with 👎 or hide it in the Knowledge tab. This is a fan project and is not affiliated with Northwood Studios.

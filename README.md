# iphone-use

English | [中文](README.zh-CN.md)

An AI agent that controls a real iPhone through macOS iPhone Mirroring.

Given a natural-language goal, the agent repeatedly observes the current screen, reasons about the next step, executes an action, and verifies progress until the task is complete.

**iphone-use can drive long-horizon mobile workflows with a multimodal small model such as Qwen3.5-35B-A3B, including cross-app tasks, multi-step navigation, and explicit completion checks. It is designed to work with private or self-hosted OpenAI-compatible model providers, without depending on closed-source frontier models.**

## Highlights

- **Milestone-supervised GUI agent**: a decompose -> act -> verify/replan loop, plus app reconnaissance and a page knowledge base, reliably running long-horizon mobile tasks on a small model like Qwen3.5-35B. At this stage input was **preemptive** — it drove the Mac's mouse and keyboard.
- **Zero-preempt input**: actions go straight to the mirror window, never grabbing your mouse, keyboard, or foreground focus, so the agent works the phone in the background while you keep using your computer.

## Demos

The UI and agent traces are in Chinese because the demos use real Chinese apps, but the task patterns are general mobile GUI workflows. Each clip notes its own playback speed.

### Chat Mode

**Information workflow**: read and summarize information inside an app, such as bills, order history, or message threads.

**Zero-preempt input** is an important feature of this project. Most GUI / computer-use agents are *preemptive* — they take over your mouse and keyboard, so you can't use the computer while they run. Here, actions are delivered **directly to the mirror window**, without grabbing your cursor or foreground focus, so the agent can work the phone in the background while you keep using your computer.

Below is the same WeChat bill query, run both ways:

Zero-preempt:

> "How much did I spend via WeChat Pay from the 21st to the 28th last month?"
>
> Set a custom date range in the picker -> scroll-collect the matching transactions -> summarize the spending. The cursor never leaves your hands.

https://github.com/user-attachments/assets/6805dd78-fd8c-4b23-9f85-4409851882e7

*This demo runs at real-time speed (1x, not accelerated).*

Preemptive:

> "How much did I spend via WeChat Pay last week?"
>
> Open the payment bill page -> filter/read last week's transactions -> summarize the spending.

https://github.com/user-attachments/assets/2deb4026-97e9-4689-bfa7-30472544d3df

*This demo is played at 2x speed.*

**Action workflow**: execute concrete actions such as sending a message, placing an order, or changing a setting.

> "Share the baby formula I recently ordered from the shopping app with Lao Be, and say it is good value for money."
>
> E-commerce app -> find a recent order -> share to a messaging app contact. This is a cross-app mobile workflow.

https://github.com/user-attachments/assets/3b10c74a-99ae-4bbb-a983-767857b62136

*This demo is played at 2x speed.*

### Recon Mode

> Automatically explore the page structure of a marketplace app and generate a reusable page knowledge base.

https://github.com/user-attachments/assets/183b80fd-ba0f-4f14-b599-b7ef3efc4a79

*This demo is played at 2x speed.*

## Architecture

```mermaid
flowchart TD
    User([User natural language]) --> Router[Router\nGoal extraction]
    Router -- No phone action needed --> Output([Natural-language reply])
    Router -- Missing information --> Clarify[Ask clarification]
    Clarify -- User answers --> User
    Router -- Structured goal --> Supervisor[Supervisor\nManage milestone list]

    subgraph turn[" Every turn "]
        Checker["Checker\nScreenshot + Milestone -> done?"]
        Planner["Planner\nScreenshot + instruction -> next step"]
        AP["Action Policy\nScreenshot + instruction -> coordinate action"]
        Executor["Executor\nCoordinates -> touch events"]
        Checker -- Not done --> Planner
        Planner -- Operation instruction --> AP
        AP -- tap/type/scroll --> Executor
        Executor -- Screenshot --> Checker
    end

    Supervisor -- Current milestone --> Checker
    Checker -- Milestone done --> Supervisor
    Supervisor -- All done --> Output

    Knowledge[(Knowledge base\nApp page structure)]
    Recon[Recon mode\nAutomatic app exploration] --> Knowledge
    Knowledge -- Page structure + navigation graph --> Supervisor
    Knowledge -- Element function + operation hints --> Planner

    Memory[(User memory\nPreferences / habits)]
    Output -- Extract preferences --> Memory
    Memory -- Inject context --> Router
```

**Router** parses the user message and decides whether the phone needs to be operated. If yes, it extracts the concrete goal and target app, then passes them to the Supervisor. If no phone action is needed, it replies directly. If key information is missing, it asks a clarification question.

**Supervisor** decomposes a goal into dependent milestone tasks and executes them one by one with verification. It supports different completion strategies, including single-state confirmation, scroll-based collection, and repeat-until-satisfied workflows. When the agent gets stuck, it can trigger replanning.

**Planner** reads the current screenshot and the active operation instruction, optionally using the page knowledge base, and decides the next concrete step.

**Action Policy** converts a high-level operation instruction into screen-coordinate actions such as `tap`, `type`, `scroll`, and `drag`.

**Executor** dispatches coordinate actions to the phone. In `silent` mode it uses the built-in `mirror_daemon` backend (zero-preemption, no cursor movement or window focus changes). In `standard` mode it uses Quartz events through mirroir-mcp.

**Output** generates the final user-facing response after the task completes. For information workflows, it returns the extracted data or summary.

## Robustness

**Checker-based verification** runs before each action. It judges whether the current milestone is actually complete from the screenshot, reducing false positives caused by LLM hallucination, such as mistaking a bottom tab for task completion.

**Stuck detection** identifies failures through screen similarity across consecutive frames and repeated operation instructions. When triggered, the system enters a replan flow.

**Replan** diagnoses why the agent is stuck and proposes an alternative strategy, such as using a different navigation path, replacing a scroll with a tap, or escalating to human intervention.

## Capabilities and Limits

**Works well for**

- Fixed mobile workflows: sending messages, placing orders, filling forms, changing settings
- In-app information retrieval: spending summaries, order records, page content extraction
- Cross-app tasks: retrieving content in one app and using it in another app

**Not suitable for**

- Steps that require CAPTCHA, Face ID, or Touch ID
- Real-time interaction scenarios such as games or live streams
- Complex gestures such as 3D Touch or long-press menus
- Screenshots that are blank or black because of screen-protection or DRM restrictions

## Requirements

- macOS Sequoia 15.0 or later, required by iPhone Mirroring
- An iPhone paired with the Mac through iPhone Mirroring
- An OpenAI-compatible LLM provider, such as ModelScope, DashScope, local inference, or a self-hosted service

## Setup

```bash
# Install mirroir-mcp (used in standard mode)
brew tap jfarcand/tap
npx -y mirroir-mcp install

# Install Python dependencies
uv sync
```

Copy `.env` and fill in your API configuration:

```env
API_PROVIDER=modelscope
MODELSCOPE_API_KEY=your_api_key
```

Model assignments for each module are configured in `policy_expr/config.yaml`.

### Screenshot Server

The agent uses `bin/sck_server` (a compiled Swift binary) to take screenshots via ScreenCaptureKit instead of the system `screencapture` command. This avoids triggering the iOS recording indicator on every frame — it fires only once when the stream starts.

The binary is pre-compiled for Apple Silicon (arm64). If you modify the Swift source, recompile:

```bash
swiftc sck/sck_stream_server.swift -o bin/sck_server
```

## Entry Points

### Chat Mode

```bash
bin/chat
```

Interactive multi-turn chat interface. It supports consecutive tasks and maintains session context and task progress.

**In-session commands:**

| Command | Description |
|---------|-------------|
| `/mode [silent\|standard]` | Switch input backend. `silent` = zero-preempt mirror_daemon (default); `standard` = mirroir-mcp original |
| `/model [qwen35\|qwen36]` | Switch model profile (config.yaml `profiles`). `qwen35` = base (default); `qwen36` = qwen3.6 core models |
| `/supervisor` | Toggle between Milestone and Simple supervisor |
| `/clear` | Clear conversation history |
| `/exit` | Exit |

### Runner

Useful for experiments and debugging.

```bash
# Run with defaults (silent mode, qwen35)
bin/runner "open WeChat and go to contacts"

# Switch model or mode via environment variables
AGENT_MODEL=qwen36 bin/runner "open WeChat"
AGENT_MODE=standard bin/runner "open WeChat"
```

For scripted or programmatic use:

```bash
uv run python -m policy_expr.core.runner "open WeChat and go to contacts" \
  --mode agent-loop --supervisor milestone --auto-continue --max-turns 15 --hud
```

**Environment variables:**

| Variable | Values | Default | Description |
|----------|--------|---------|-------------|
| `AGENT_MODE` | `silent`, `standard` | `silent` | Input backend |
| `AGENT_MODEL` | `qwen35`, `qwen36` | `qwen35` | LLM config file |

### Execution Visualization

After each run, an HTML report is automatically generated showing the full task execution trace:

![Execution report](policy_expr/assets/report.png)

- **Milestone decomposition** — sub-goals with names, descriptions, and acceptance criteria
- **Per-milestone thumbnail gallery** — one row of annotated screenshots per sub-goal
- **Action annotations** — tap circles, scroll arrows, type text bubbles, drag start/end points
- **Timing breakdown** — stacked bar per turn showing time spent in checker, planner, action policy, etc.
- **Verification panel** — clicking the verification thumbnail shows the checker's reasoning for why the milestone was marked complete

```bash
# Runner auto-generates report.html in the log directory
bin/runner "open WeChat and send a message"

# Generate report from an existing log
python scripts/report.py runner --run logs/policy_expr/agent-loop/20260528_104755
```

### App Reconnaissance

Generate a reusable knowledge base for an app.

```bash
# Explore an app and generate page knowledge
uv run python -m policy_expr.adapters.iphone.recon_cli --app 微信 --depth 2

# Manually navigate to a new page, then append it to an existing knowledge base
uv run python -m policy_expr.adapters.iphone.recon_cli --app 微信 --mode add --depth 1

# Update knowledge for a specific page
uv run python -m policy_expr.adapters.iphone.recon_cli --app 微信 --mode update \
  --target "WeChat main screen, showing chat list and bottom navigation"
```

`--depth N` controls the DFS exploration depth. Pages from depth `0` to `N-1` are explored and converted into knowledge; depth `N` pages are only recorded.

## Project Structure

```text
policy_expr/
├── chat_cli.py          # Chat mode entry point
├── runner.py            # Experimental/debug runner
├── recon_cli.py         # App reconnaissance CLI
├── executor.py          # Action executor: mirror_daemon / Quartz + MCP
├── perception.py        # Screenshot perception layer
├── output.py            # Final response generation
├── schemas.py           # Core data models
├── config.yaml          # LLM config: base (qwen35) + profiles (AGENT_MODEL) + pricing
├── prefs.py             # User preference memory
├── supervisor/
│   └── milestone.py     # Milestone state machine: decompose -> execute -> verify
├── policies/
│   ├── base.py          # ActionPolicy interface
│   └── structured_output.py  # Vision LLM action policy
├── recon/
│   ├── page_parser.py   # Screenshot -> page identity + interactive elements
│   ├── dfs.py           # Multi-depth DFS app exploration
│   ├── bfs.py           # BFS element probing
│   ├── back_nav.py      # Return navigation from child pages
│   ├── page_identity.py # Page deduplication with visual fingerprints
│   └── cascade_matcher.py  # Cascaded visual/semantic page matching
└── self_learning/
    ├── knowledge.py     # Generate knowledge files from recon results
    └── app_summary.py   # Discover and load app knowledge

bin/
├── chat                 # Launch chat mode
├── runner               # Launch runner (AGENT_MODE / AGENT_MODEL configurable)
└── mirror_daemon        # Zero-preempt input backend binary (silent mode)

knowledge/               # Per-app page knowledge base in Markdown
data/user_preferences.json  # Persistent user preference memory
evals/                   # Module-level evaluation cases and scripts
llm/                     # Structured-output LLM wrappers
models/                  # Local models, such as YOLO icon detection
scripts/                 # Utility scripts for tests and visualization
```

## Knowledge Base

After app reconnaissance, generated page knowledge is stored under `knowledge/{app}/`. Each page contains:

- Page identity description, including title, type, and key visual elements
- Interactive elements and their functional descriptions
- Navigation relationships to reachable child pages

At runtime, the Runner automatically loads relevant knowledge for the target app. This helps the Supervisor understand page structure and helps the Planner choose better navigation steps.

## User Memory

After each completed task, the system extracts user preferences from the conversation, such as commonly used apps, frequent contacts, and habitual options. These preferences are persisted in `data/user_preferences.json` and injected into future tasks when relevant.

## Evaluation

Core modules have independent evaluation suites that do not require a real phone. They call the LLM and assert against expected structured outputs.

```bash
uv run python evals/<module>/test_<module>.py
```

See [`evals/README.md`](evals/README.md) for details.

## Tech Stack

- Python 3.11+
- `mirror_daemon` — Swift binary providing zero-preempt screenshot + touch input for silent mode (SCStream + SkyLight private SPI)
- `mirroir-mcp` — MCP server for standard mode phone control
- `mcp` — MCP client
- `langchain-openai` / `langchain-qwq` — LLM calls through OpenAI-compatible providers
- `onnxruntime` — YOLO icon detection with ONNX inference for coordinate snapping
- `ocrmac` — Native macOS OCR
- `Quartz` — macOS event injection (standard mode)
- `pillow` / `numpy` / `scikit-image` / `imagehash` — Image processing
- `torch` + `transformers` + `sentence-transformers` — CLIP-based visual matching in `cascade_matcher`, loaded on demand
- `pydantic` — Data models
- `rich` + `prompt-toolkit` — Terminal UI
- `uv` — Package management

## TODO

- **Safety gate for risky actions**: add human confirmation before irreversible or sensitive operations, such as payment confirmation, data deletion, or sending messages.

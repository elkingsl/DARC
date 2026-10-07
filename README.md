# DARC — Dynamic Agentic Runtime Coordinator

> **Project specification, architecture document, implementation blueprint, and operational reference**
>
> Target platform: **Arch Linux**
>
> Primary design goal: **a fully local, lightweight, always-available AI assistant for constrained CPU-only hardware**

---

## 0. Document Purpose

This document consolidates the two supplied DARC project documents into one detailed Markdown specification.

It intentionally keeps the original project's technical vocabulary, architecture, model choices, implementation phases, commands, directory structure, and design rationale. Where the supplied documents contain alternatives or differing values, those alternatives are explicitly preserved rather than silently resolved.

This document is intended to serve as the project's working reference while DARC is being implemented.

---

# 1. Project Identity

## 1.1 Name

**DARC**

### Expansion

**Dynamic Agentic Runtime Coordinator**

The four words describe the project's primary architectural responsibilities:

| Term | Meaning in DARC |
|---|---|
| **Dynamic** | The interface adapts in real time to the current interaction state. |
| **Agentic** | Work is divided among specialized models and tools instead of relying on one monolithic model. |
| **Runtime** | The assistant is a persistent local runtime managed by `systemd`. |
| **Coordinator** | The central daemon coordinates models, tools, UI state, and operating-system actions. |

---

## 1.2 Product Definition

DARC is a **fully local, open-source AI assistant designed for Arch Linux**.

The project deliberately avoids:

- cloud-dependent AI inference,
- heavyweight desktop wrappers,
- Electron-based application architecture,
- unnecessarily large multimodal models,
- continuously loading every model into RAM.

Instead, DARC is built around:

- native Linux primitives,
- a lightweight background daemon,
- a minimal graphical overlay,
- Unix-domain-socket IPC,
- specialized sub-3B models,
- local speech recognition,
- local text-to-speech,
- local vision processing,
- controlled operating-system command execution.

The interface is designed as a minimalist **Dynamic Island-style overlay** that provides voice and text interaction without permanently occupying the user's workspace.

---

# 2. Product Philosophy

## 2.1 Local First

DARC is intended to operate without sending user interaction, screen contents, audio, or system data to a cloud service.

The core inference pipeline is local:

```text
User
  │
  ▼
DARC UI
  │
  ▼
Local Coordinator Daemon
  │
  ├── Local LLM
  ├── Local Vision Model
  ├── Local STT
  ├── Local TTS
  └── Local Linux Tools
```

The supplied architecture specifically favors local inference engines such as:

- Ollama,
- `llama.cpp`,
- `whisper.cpp`,
- Piper TTS.

---

## 2.2 Specialized Models Instead of One Large Model

DARC does not attempt to solve every problem with one large multimodal model.

Instead, it uses a collection of smaller components:

```text
                    ┌────────────────────┐
                    │  Coordinator       │
                    │  Router / Runtime  │
                    └─────────┬──────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
          ▼                   ▼                   ▼
      Reasoning             Vision              Audio
     Qwen Coder           Moondream2        Whisper.cpp
          │                                       │
          └───────────────────┬───────────────────┘
                              ▼
                         Piper TTS
```

The purpose is not merely modularity. It is also resource management.

Only the capability required for a particular request should consume significant memory and CPU resources.

---

# 3. Core Pillars

## 3.1 Dynamic

DARC adapts its interface according to the current state.

The Dynamic Island is intended to:

- remain unobtrusive while idle,
- appear when summoned,
- expand while listening,
- display processing state,
- show response text,
- display audio visualization,
- collapse after interaction.

Possible interaction states include:

```text
IDLE
  ↓
LISTENING
  ↓
PROCESSING
  ↓
RESPONDING
  ↓
IDLE
```

The original blueprint explicitly calls for at least:

- `Idle`
- `Listening`
- `Processing`

The second project document also describes expansion while listening or outputting text and a smaller status state while processing or idle.

---

## 3.2 Agentic

DARC uses multiple specialized agents/models rather than a single monolithic model.

The coordinator determines which capability is required.

Examples:

```text
"What is on my screen?"
        │
        ▼
     Vision
        │
        ▼
   Moondream2
```

```text
"Write a shell command to ..."
        │
        ▼
 Logic / Coding
        │
        ▼
 Qwen Coder
```

```text
User speaks
    │
    ▼
  Whisper
    │
    ▼
 Text prompt
```

```text
Text response
      │
      ▼
   Piper TTS
      │
      ▼
   Audio output
```

---

## 3.3 Runtime

DARC is intended to run continuously as a native Linux service.

The coordinator is managed by `systemd`.

The intended model is:

```text
Boot
 │
 ▼
systemd user service
 │
 ▼
DARC daemon
 │
 ├── initialize configuration
 ├── initialize IPC
 ├── initialize model/tool interfaces
 └── wait for requests
```

This allows the assistant to be available without manually launching the application every time the desktop session starts.

---

## 3.4 Coordinator

The coordinator is the central bridge between AI logic and the Linux operating system.

It is responsible for:

- receiving user requests,
- maintaining state,
- managing context,
- routing tasks,
- invoking specialized models,
- invoking system tools,
- executing commands,
- reading command output,
- requesting screenshots,
- passing results between components,
- updating the UI.

The coordinator is therefore the central orchestration layer of DARC.

---

# 4. Hardware Target and Constraints

## 4.1 Target Hardware

The supplied project documents target the following hardware profile:

- Intel Core i7-5557U
- Broadwell generation
- dual-core CPU
- 16 GB RAM
- no discrete GPU
- integrated graphics

This hardware profile is an explicit architectural constraint.

---

## 4.2 Consequences

The architecture is designed around CPU-efficient inference.

The project explicitly avoids relying on:

- large multimodal models,
- GPU-heavy inference,
- excessive simultaneous model loading,
- unnecessarily large background processes.

The intended optimization strategy is:

```text
Do not load everything.
        │
        ▼
Determine required capability.
        │
        ▼
Load/use the smallest suitable model.
        │
        ▼
Process request.
        │
        ▼
Return to low-resource state.
```

---

## 4.3 Memory Strategy

The project emphasizes loading only the necessary specialized models into memory when their capabilities are required.

This is particularly important because the target machine has 16 GB of RAM and no discrete GPU.

The model stack therefore favors approximately sub-3B models and highly quantized variants where appropriate.

---

# 5. High-Level System Architecture

DARC is divided into three major layers.

```text
┌──────────────────────────────────────────────────────────┐
│                    DARC INTERFACE                        │
│              Dynamic Island / GTK Overlay                │
└──────────────────────────┬───────────────────────────────┘
                           │
                    Unix Domain Socket
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│                 DARC COORDINATOR DAEMON                  │
│                                                          │
│  State Management → Router → Execution Engine → IPC      │
└──────────────┬───────────────────────┬───────────────────┘
               │                       │
               ▼                       ▼
┌────────────────────────┐   ┌────────────────────────────┐
│   MODEL / AI LAYER     │   │      LINUX TOOL LAYER      │
│                        │   │                            │
│ Qwen                   │   │ pacman                     │
│ Moondream2             │   │ hyprctl                    │
│ Whisper.cpp             │   │ grim                       │
│ Piper                  │   │ slurp                      │
│ Ollama / llama.cpp     │   │ clipboard / subprocess     │
└────────────────────────┘   └────────────────────────────┘
```

The two supplied documents describe Unix-domain sockets as the primary IPC mechanism. One document also mentions D-Bus as an alternative.

---

# 6. Layer 1 — Interface Layer

## 6.1 Purpose

The interface is DARC's visible component.

It is responsible for:

- presenting the assistant,
- displaying state,
- showing text,
- showing audio activity,
- providing microphone/status controls,
- displaying confirmation requests,
- receiving interaction triggers.

It should not perform the heavy AI work.

---

## 6.2 Framework Options

The supplied documents identify two UI implementation options:

### Option A — Fabric

**Fabric / Python GTK**

Advantages in the project architecture:

- Python-based,
- GTK-based,
- suitable for Linux desktop widgets and overlays,
- convenient integration with the Python daemon ecosystem,
- suitable for building a Dynamic Island-style interface.

The first blueprint specifically describes Fabric as the primary frontend choice.

### Option B — AGS

**AGS / Aylur's GTK Shell**

The second document lists AGS as an alternative.

The architectural requirement is more important than the exact framework:

- borderless overlay,
- always-on-top behavior,
- rounded UI,
- dynamic state,
- low overhead,
- Linux-native rendering.

---

## 6.3 GTK and UI Dependencies

The first blueprint lists:

```text
gtk3
gobject-introspection
cairo
```

The second document additionally lists:

```text
gtk-layer-shell
```

The project therefore expects the UI stack to operate close to the Linux compositor/window-system layer.

---

## 6.4 Visual Design

The supplied DARC visual identity establishes the following design direction:

- very dark background,
- high contrast,
- white primary typography,
- neon blue accents,
- purple accents,
- rounded UI geometry,
- minimal iconography,
- audio waveform visualization,
- compact Dynamic Island form.

The main interface concept is a horizontal rounded capsule that can expand and contract according to state.

---

## 6.5 UI States

A minimum state model should include:

### Idle

Minimal visible state.

```text
[ DARC ]
```

### Listening

The assistant is capturing user speech.

```text
[ DARC | audio waveform | microphone ]
```

### Processing

The assistant is processing the request.

```text
[ DARC | processing indicator ]
```

### Responding

The assistant has generated a response and may display text and/or speak it.

```text
[ DARC | response / waveform ]
```

### Confirmation

A system action requires user approval.

```text
[ Execute command? ] [ Yes ] [ No ]
```

The supplied documents explicitly require confirmation before AI-generated terminal commands are executed.

---

# 7. Layer 2 — Coordinator / Daemon

## 7.1 Purpose

The daemon is the operational core of DARC.

The first blueprint describes it as a lightweight Python `asyncio` background service.

The second document allows either Python or Rust as the implementation language.

The current project blueprint is therefore most directly aligned with:

```text
Python 3.11+
asyncio
systemd
Unix domain sockets
```

---

## 7.2 Responsibilities

The daemon handles:

1. request reception,
2. state management,
3. request queueing,
4. routing,
5. model selection,
6. model communication,
7. audio pipeline coordination,
8. screen capture coordination,
9. system command execution,
10. command confirmation,
11. UI state updates,
12. result processing.

---

## 7.3 State Management

The coordinator maintains:

- current request,
- request queue,
- context history,
- current execution state,
- model/tool state,
- UI state.

The supplied architecture explicitly describes the coordinator as holding context history and managing the queue of user requests.

---

## 7.4 Router

The router analyzes the incoming request and determines which capability is needed.

Conceptually:

```text
Incoming request
      │
      ▼
Intent / capability analysis
      │
      ├── General reasoning → Qwen
      ├── Coding → Qwen
      ├── Vision → Moondream2
      ├── Speech input → Whisper
      ├── Speech output → Piper
      └── System action → Tool / execution engine
```

The router should avoid invoking unnecessary components.

---

# 8. Layer 3 — Agentic Model Layer

## 8.1 Model Philosophy

The model layer consists of lightweight specialized components.

The architecture emphasizes:

- low memory usage,
- CPU compatibility,
- local inference,
- specialization,
- fast startup/response,
- model isolation.

---

## 8.2 Logic and Coding Model

The primary model specified by the first document is:

```text
qwen2.5-coder:1.5b
```

with:

```text
1.5B parameters
Q4_K_M
Ollama
```

Its proposed responsibilities include:

- general reasoning,
- routing support,
- JSON generation,
- command generation,
- coding,
- shell-script generation.

The second document also permits:

```text
qwen2.5-coder:3b
```

as an alternative.

### Important project distinction

The supplied documents therefore contain two intended configurations:

```text
Preferred lightweight configuration:
qwen2.5-coder:1.5b
```

and:

```text
Alternative:
qwen2.5-coder:3b
```

The 1.5B model is the more conservative choice for the target CPU/RAM constraints.

---

## 8.3 Vision Model

The project uses:

```text
moondream2
```

for screen understanding.

The documents list two size descriptions:

- approximately **1.8B** in the first blueprint,
- approximately **1.4B** in the second document.

This discrepancy is preserved because the source documents do not resolve it.

Its intended role is:

- reading screenshots,
- describing screen contents,
- reading UI elements,
- processing OCR-like visual information,
- understanding screen layouts.

---

## 8.4 Speech-to-Text

DARC uses:

```text
whisper.cpp
```

with the lightweight:

```text
tiny.en
```

model as the primary target.

The second document additionally lists:

```text
base.en
```

as an alternative.

The first document estimates the `tiny.en` model at approximately:

```text
~75 MB RAM
```

The purpose is low-latency local transcription on the CPU.

---

## 8.5 Text-to-Speech

DARC uses:

```text
Piper TTS
```

for offline speech generation.

The first document estimates:

```text
<100 MB RAM
```

The intended characteristics are:

- offline operation,
- fast synthesis,
- low resource usage,
- natural-sounding voice output.

---

# 9. Audio Architecture

## 9.1 Input Pipeline

The intended voice pipeline is:

```text
Microphone
    │
    ▼
Audio capture
    │
    ▼
Wake-word detection
    │
    ▼
Audio recording
    │
    ▼
Whisper.cpp
    │
    ▼
Text
    │
    ▼
DARC Coordinator
```

---

## 9.2 Wake Word

The first blueprint specifies:

```text
openwakeword
```

as the wake-word detector.

The goal is to keep the assistant passively available while consuming relatively little CPU.

Example conceptual interaction:

```text
"Hey Assistant"
      │
      ▼
openwakeword
      │
      ▼
DARC enters LISTENING state
```

---

## 9.3 Audio Capture

The first blueprint lists:

- PyAudio
- SoundDevice

The second document lists:

- PipeWire
- `sox`
- `ffmpeg`

The architecture therefore contains several possible audio capture paths.

The project should select one concrete implementation during implementation rather than requiring all of them simultaneously.

---

## 9.4 Speech Output

After the coordinator generates a response:

```text
Response text
      │
      ▼
Piper TTS
      │
      ▼
Audio output
```

The UI may simultaneously show a waveform or response state.

---

# 10. Screen and Vision Architecture

## 10.1 Purpose

DARC can use the user's screen as an input source.

A screen-reading request follows this conceptual path:

```text
User request
     │
     ▼
Coordinator
     │
     ▼
Screenshot tool
     │
     ▼
grim / maim
     │
     ▼
PNG image
     │
     ▼
Moondream2
     │
     ▼
Visual interpretation
     │
     ▼
Qwen refinement
     │
     ▼
UI / TTS output
```

---

## 10.2 Wayland

Recommended Wayland tools:

```text
grim
slurp
wl-clipboard
```

`grim` is used for screenshots.

`slurp` is used for region selection.

`wl-clipboard` is included for clipboard integration.

---

## 10.3 X11 Fallback

The first blueprint lists:

```text
maim
xclip
```

as X11 alternatives.

The project therefore anticipates both:

```text
Wayland
```

and:

```text
X11
```

environments.

---

# 11. System Integration

## 11.1 Command Execution

The coordinator can invoke Linux commands through Python's native subprocess facilities.

Example commands referenced by the project:

```text
pacman
hyprctl
```

The system command tool is responsible for:

- preparing commands,
- requesting confirmation where required,
- executing commands,
- collecting output,
- returning output to the coordinator.

---

## 11.2 Safety / Confirmation Model

AI-generated system commands should not automatically execute without user approval.

The supplied blueprint explicitly describes the sequence:

```text
AI determines command is needed
          │
          ▼
Pause execution
          │
          ▼
Notify Dynamic Island
          │
          ▼
Ask Y/N confirmation
          │
      ┌───┴───┐
      │       │
     YES      NO
      │       │
      ▼       ▼
  Execute   Cancel
```

This confirmation mechanism is a core part of the system integration design.

---

# 12. Inter-Process Communication

## 12.1 Primary IPC

The primary communication mechanism is:

```text
Unix Domain Sockets
```

The UI communicates with the daemon through local IPC.

This avoids requiring a network service for communication between DARC's own components.

---

## 12.2 Alternative IPC

The second project document mentions:

```text
D-Bus
```

as an alternative.

The supplied documents do not define a final D-Bus protocol, so Unix-domain sockets remain the concrete mechanism in the original implementation blueprint.

---

## 12.3 Conceptual Message Flow

```text
UI
 │
 │ request / state event
 ▼
Unix socket
 │
 ▼
Daemon
 │
 │ model/tool operation
 ▼
Model or Linux tool
 │
 │ result
 ▼
Daemon
 │
 │ UI update
 ▼
Unix socket
 │
 ▼
UI
```

---

# 13. Project Directory Structure

The original implementation blueprint proposes the following structure:

```text
arch-ai-assistant/
├── main.py
│
├── ui/
│   ├── __init__.py
│   ├── island.py
│   └── styles.css
│
├── daemon/
│   ├── __init__.py
│   ├── server.py
│   ├── orchestrator.py
│   │
│   └── tools/
│       ├── system_cmd.py
│       └── screen_reader.py
│
├── models/
│   ├── stt.py
│   ├── tts.py
│   ├── wake_word.py
│   └── llm.py
│
├── systemd/
│   └── ai-assistant.service
│
├── requirements.txt
└── config.yaml
```

---

# 14. Directory Responsibilities

## 14.1 `main.py`

Primary entry point.

The original blueprint describes it as launching:

- daemon,
- UI.

---

## 14.2 `ui/`

Contains all frontend code.

### `ui/island.py`

Responsible for:

- Dynamic Island window,
- UI states,
- user-facing display,
- communication with the daemon.

### `ui/styles.css`

Responsible for:

- colors,
- borders,
- rounded corners,
- visual styling.

---

## 14.3 `daemon/`

Contains the backend runtime.

### `daemon/server.py`

Responsible for:

- Unix socket server,
- IPC,
- incoming requests,
- outgoing state updates.

### `daemon/orchestrator.py`

The central coordinator.

Responsible for:

- routing,
- task selection,
- model selection,
- workflow orchestration.

---

## 14.4 `daemon/tools/`

System integration tools.

### `system_cmd.py`

Responsible for:

- subprocess execution,
- command confirmation logic.

### `screen_reader.py`

Responsible for:

- screenshot capture,
- Wayland/X11 tool invocation,
- sending captured images to the vision model.

---

## 14.5 `models/`

Model wrappers.

### `stt.py`

Wrapper for `whisper.cpp`.

### `tts.py`

Wrapper for Piper.

### `wake_word.py`

Wrapper for `openwakeword`.

### `llm.py`

Wrapper for Ollama API calls.

---

## 14.6 `systemd/`

Contains the service definition.

```text
ai-assistant.service
```

The service is intended to provide automatic startup.

---

## 14.7 `requirements.txt`

Contains Python-level dependencies.

---

## 14.8 `config.yaml`

Contains runtime configuration such as:

- wake word,
- model paths,
- UI position.

---

# 15. Dependency Stack

## 15.1 Operating-System Layer

Core packages identified by the project:

```text
systemd
python
pipewire
dbus
```

The first blueprint also requires:

```text
gtk3
gobject-introspection
cairo
```

---

## 15.2 UI Layer

Potential stack:

```text
Fabric
Python
GTK3
Cairo
gobject-introspection
gtk-layer-shell
```

or:

```text
AGS
GJS / GTK
gtk-layer-shell
```

---

## 15.3 AI Inference Layer

```text
Ollama
llama.cpp
whisper.cpp
Piper TTS
openwakeword
```

---

## 15.4 Linux Utility Layer

Wayland:

```text
grim
slurp
wl-clipboard
```

X11:

```text
maim
xclip
```

Audio/media:

```text
sox
ffmpeg
```

---

# 16. Installation Plan

## 16.1 System Packages

The original blueprint proposes:

```bash
sudo pacman -S base-devel python python-pipx gtk3 gobject-introspection cairo sox grim slurp
```

The broader technical stack also identifies PipeWire, D-Bus, GTK layer-shell, and other utilities as relevant project dependencies.

The exact final Arch package list should therefore be treated as an implementation checklist rather than an immutable final manifest.

---

# 17. Ollama Setup

The original blueprint specifies:

```bash
sudo pacman -S ollama
sudo systemctl enable --now ollama
ollama run qwen2.5-coder:1.5b
```

The intended result is a locally available Qwen model through Ollama.

---

# 18. Whisper.cpp Setup

The original blueprint proposes compiling `whisper.cpp` locally:

```bash
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp && make -j4
./models/download-ggml-model.sh tiny.en
```

The rationale is to use a native C++ implementation suitable for CPU-oriented local inference.

---

# 19. Python Environment

The original blueprint uses `uv`:

```bash
pipx install uv
uv venv .venv
source .venv/bin/activate
uv pip install fabric-python openwakeword sounddevice numpy requests pillow
```

The project therefore uses a dedicated virtual environment rather than installing the complete Python application globally.

---

# 20. Systemd Runtime

## 20.1 Purpose

The DARC runtime should start automatically with the user's desktop environment/session.

The original project uses a user-level service:

```text
~/.config/systemd/user/
```

with:

```text
ai-assistant.service
```

The second project document refers to the conceptual service as:

```text
darc-daemon.service
```

The exact final service filename should be chosen consistently during implementation.

---

## 20.2 Intended Startup

Conceptually:

```text
User session starts
        │
        ▼
systemd user instance
        │
        ▼
DARC daemon
        │
        ├── initialize configuration
        ├── initialize IPC
        ├── initialize model interfaces
        └── wait for requests
```

The UI can then connect to the daemon.

---

# 21. End-to-End Request Lifecycle

A normal text request can be represented as:

```text
User
 │
 ▼
Dynamic Island
 │
 ▼
Unix socket
 │
 ▼
DARC daemon
 │
 ▼
Router
 │
 ▼
Qwen
 │
 ▼
Tool/model execution if required
 │
 ▼
Result
 │
 ▼
DARC daemon
 │
 ├──► UI
 │
 └──► Piper TTS
```

---

# 22. Voice Request Lifecycle

```text
Microphone
    │
    ▼
openwakeword
    │
    ▼
Wake detected
    │
    ▼
Dynamic Island → LISTENING
    │
    ▼
Audio capture
    │
    ▼
whisper.cpp
    │
    ▼
Text prompt
    │
    ▼
DARC coordinator
    │
    ▼
Task routing
    │
    ▼
Model/tool execution
    │
    ▼
Response text
    │
    ├──────────────► Dynamic Island
    │
    ▼
Piper TTS
    │
    ▼
Audio output
```

---

# 23. Vision Request Lifecycle

Example request:

> "What am I looking at?"

The supplied project documents define this workflow:

```text
1. User triggers DARC
       │
       ▼
2. Dynamic Island expands
       │
       ▼
3. User speaks
       │
       ▼
4. PipeWire/audio capture
       │
       ▼
5. whisper.cpp transcribes
       │
       ▼
6. Coordinator identifies vision request
       │
       ▼
7. grim captures screen
       │
       ▼
8. Screenshot is passed to Moondream2
       │
       ▼
9. Vision result is produced
       │
       ▼
10. Qwen can refine the result
       │
       ▼
11. DARC displays response
       │
       ▼
12. Piper TTS speaks response
```

This workflow demonstrates the agentic design: different specialized components perform different parts of one user request.

---

# 24. System Command Lifecycle

For an action requiring shell execution:

```text
User request
     │
     ▼
Coordinator
     │
     ▼
Qwen determines required command
     │
     ▼
Command prepared
     │
     ▼
Confirmation request
     │
     ▼
Dynamic Island
     │
     ├──── YES ────► subprocess execution
     │
     └──── NO ─────► cancel
```

If executed, command output is collected and returned to the coordinator for interpretation.

---

# 25. Build Roadmap

The original blueprint divides implementation into five phases.

---

## Phase 1 — Foundation

### Goal

Establish the daemon and local LLM communication.

### Primary work

Create:

```text
daemon/orchestrator.py
```

and connect it to Ollama.

The first milestone is intentionally simple:

```text
CLI prompt
    │
    ▼
Qwen 1.5B
    │
    ▼
streamed response
```

The purpose is to prove the model communication layer before adding UI complexity.

---

# 26. Phase 2 — Dynamic Island UI

## Goal

Build the visual shell.

Tasks:

1. Create the floating GTK window.
2. Give it rounded corners.
3. Anchor it to the screen edge.
4. Implement idle state.
5. Implement listening state.
6. Implement processing state.
7. Connect UI and daemon through Unix sockets.

The original blueprint specifies the top-right position as the initial target.

---

# 27. Phase 3 — Audio Pipeline

## Goal

Enable hands-free voice control.

Pipeline:

```text
openwakeword
      │
      ▼
audio capture
      │
      ▼
whisper.cpp
      │
      ▼
DARC daemon
      │
      ▼
response
      │
      ▼
Piper TTS
      │
      ▼
audio playback
```

Tasks:

1. Run wake-word detection.
2. Detect activation.
3. Record audio.
4. Transcribe audio.
5. Send transcription to the coordinator.
6. Process request.
7. Generate speech.
8. Play speech.
9. Update Dynamic Island states throughout the process.

---

# 28. Phase 4 — System Integration and Vision

## Goal

Give DARC "eyes and hands".

### Vision

Implement:

```text
screen_reader.py
```

and connect it to:

```text
grim / maim
```

and:

```text
moondream2
```

### System execution

Implement:

```text
system_cmd.py
```

with:

- command preparation,
- confirmation,
- execution,
- output parsing.

The Dynamic Island must display a confirmation state before executing AI-generated system commands.

---

# 29. Phase 5 — Systemd Autostart

## Goal

Make DARC persistent and seamless.

Tasks:

1. Create the service definition.
2. Place it in:

```text
~/.config/systemd/user/
```

3. Enable it for the user session.
4. Verify that the daemon starts without manually launching it.
5. Verify that the UI can connect to the daemon after startup.

---

# 30. Configuration Model

The original blueprint proposes:

```text
config.yaml
```

with settings such as:

```yaml
wake_word: ...
model_paths:
  llm: ...
  vision: ...
  stt: ...
  tts: ...

ui:
  position: ...
```

The exact schema is not defined in the supplied documents, so the configuration format should be expanded during implementation.

Potential configuration categories supported by the existing design are:

- wake word,
- model selection,
- model paths,
- UI position,
- runtime behavior.

---

# 31. Model Responsibility Matrix

| Component | Input | Output | Primary responsibility |
|---|---|---|---|
| `openwakeword` | microphone audio | wake event | Detect activation |
| `whisper.cpp` | recorded audio | text | Speech-to-text |
| Qwen Coder | text/context | text/structured instruction | Reasoning, routing support, coding, commands |
| `grim` / `maim` | screen | image | Screen capture |
| Moondream2 | image + prompt | visual description | Vision / screen interpretation |
| Piper | text | audio | Text-to-speech |
| system tools | command/input | command output | OS interaction |
| Dynamic Island | state/data | visual UI | User interaction and status |

---

# 32. Responsibility Boundaries

A major architectural rule is to keep responsibilities separated.

## UI should not:

- run model inference,
- directly execute arbitrary shell commands,
- manage the complete request lifecycle.

## Daemon should:

- coordinate operations,
- maintain state,
- communicate with models,
- control tools,
- authorize execution flow.

## Models should:

- perform specialized inference,
- return structured or textual results.

## Tools should:

- perform explicit system operations,
- return machine-readable results where practical.

This separation keeps the system modular and testable.

---

# 33. Testing Strategy

The supplied architecture explicitly emphasizes modularity so components can be tested individually.

A practical test sequence derived from the project structure is:

```text
1. Test Qwen communication
2. Test daemon routing
3. Test Dynamic Island rendering
4. Test Unix socket IPC
5. Test wake-word detection
6. Test STT
7. Test TTS
8. Test screenshot capture
9. Test vision inference
10. Test command confirmation
11. Test command execution
12. Test systemd startup
13. Test complete end-to-end workflows
```

Each layer should work independently before the next layer is integrated.

---

# 34. Resource-Conscious Design

The project should remain aware of its target hardware at every implementation stage.

Resource-sensitive principles include:

- prefer smaller models,
- avoid unnecessary simultaneous model loading,
- use native C/C++ implementations where already specified,
- keep the UI lightweight,
- use asynchronous daemon operations,
- avoid blocking the UI,
- keep IPC local,
- minimize background work,
- route only the necessary capabilities.

The first blueprint specifically describes an `asyncio` daemon so background work does not freeze the interface.

---

# 35. Failure Isolation

The three-layer design should prevent a failure in one subsystem from freezing the entire assistant.

Conceptually:

```text
UI failure
   │
   └── daemon can remain alive

Model failure
   │
   └── daemon can report an error to UI

Tool failure
   │
   └── command result/error returns to coordinator

Audio failure
   │
   └── text interaction can remain available
```

This follows from the project's decoupled architecture, although detailed recovery policies are not yet specified in the supplied documents.

---

# 36. Security and Command Execution Considerations

The project explicitly requires confirmation before executing terminal commands.

The intended boundary is:

```text
LLM proposes
     │
     ▼
Coordinator validates / prepares
     │
     ▼
User confirms
     │
     ▼
OS executes
```

The documents do not define a complete command allowlist, sandbox, privilege policy, or validation specification. Those details therefore remain implementation work.

The existing design does establish one important rule:

> AI-generated system commands require a user confirmation step before execution.

---

# 37. Linux Environment Integration

DARC is designed around Arch Linux and therefore expects close integration with the host system.

Examples include:

```text
pacman
hyprctl
systemd
PipeWire
GTK
Wayland
X11
```

The architecture is particularly compatible with a tiling/compositor-oriented Linux desktop because it can interact with window-management tools and compositor-specific commands.

---

# 38. Wayland/X11 Compatibility

The project does not hard-code a single display server implementation.

### Wayland path

```text
grim
slurp
wl-clipboard
gtk-layer-shell
```

### X11 path

```text
maim
xclip
```

The UI framework is expected to provide the appropriate compatibility layer, while screen and clipboard tools can be selected according to the active display environment.

---

# 39. Example Interaction

## User asks:

> What am I looking at?

### Step 1 — Trigger

User:

```text
Super + Space
```

or the configured wake word.

### Step 2 — UI

Dynamic Island expands.

```text
LISTENING
```

### Step 3 — Speech

User asks the question.

### Step 4 — STT

`whisper.cpp` converts audio into text.

### Step 5 — Routing

Coordinator identifies a screen-vision request.

### Step 6 — Capture

`grim` captures the screen.

### Step 7 — Vision

Moondream2 interprets the screenshot.

### Step 8 — Refinement

Qwen can refine or structure the visual response.

### Step 9 — Output

DARC:

- displays the response,
- optionally speaks it using Piper.

### Step 10 — Return to idle

Dynamic Island collapses back to its minimal state.

---

# 40. Example Command Interaction

## User asks:

> Install package X.

Conceptual flow:

```text
Voice / text
    │
    ▼
DARC
    │
    ▼
Qwen
    │
    ▼
Generate pacman command
    │
    ▼
Confirmation UI
    │
    ├── No → stop
    │
    └── Yes
          │
          ▼
      subprocess
          │
          ▼
      pacman
          │
          ▼
      command output
          │
          ▼
       Qwen / DARC
          │
          ▼
       UI + TTS
```

The supplied documents do not specify the exact command validation policy or privilege escalation behavior, so those remain to be defined.

---

# 41. API / Communication Abstractions

The architecture implies several internal interfaces.

## 41.1 UI ↔ Daemon

Responsibilities:

- send user input,
- notify state changes,
- receive response text,
- receive confirmation requests,
- receive processing status.

Transport:

```text
Unix domain socket
```

---

## 41.2 Daemon ↔ LLM

Responsibilities:

- submit prompt,
- receive streamed or complete response,
- request structured output where needed.

Transport in the supplied blueprint:

```text
Ollama local API
```

---

## 41.3 Daemon ↔ STT

Responsibilities:

- submit audio,
- receive transcription.

Implementation:

```text
whisper.cpp wrapper
```

---

## 41.4 Daemon ↔ TTS

Responsibilities:

- submit response text,
- receive/generated audio,
- play output.

Implementation:

```text
Piper wrapper
```

---

## 41.5 Daemon ↔ Vision

Responsibilities:

- capture image,
- submit screenshot and prompt,
- receive visual interpretation.

Implementation:

```text
grim / maim
        +
Moondream2
```

---

## 41.6 Daemon ↔ Linux

Responsibilities:

- execute commands,
- read outputs,
- interact with system tools.

Implementation:

```text
Python subprocess
```

---

# 42. Naming Conventions

The current supplied documents use several names for the same conceptual components.

Preferred project-level naming:

```text
DARC
```

Coordinator:

```text
DARC daemon
```

Potential service names appearing in the sources:

```text
ai-assistant.service
darc-daemon.service
```

The final implementation should choose one service name and use it consistently.

The original directory blueprint currently uses:

```text
ai-assistant.service
```

---

# 43. Source-Specified Variants That Need a Final Decision

The supplied documents contain a few deliberate or accidental alternatives. They are recorded here so implementation does not silently choose one.

## 43.1 UI framework

Options:

```text
Fabric
```

or:

```text
AGS
```

The first blueprint is more specific about Fabric.

---

## 43.2 Backend language

Options:

```text
Python
```

or:

```text
Rust
```

The first blueprint defines the daemon as Python `asyncio`, while the second permits Python or Rust.

The current directory structure is Python-based, so Python is the concrete blueprint implementation.

---

## 43.3 IPC

Options:

```text
Unix domain sockets
```

or:

```text
D-Bus
```

Unix sockets are the concrete mechanism used throughout the first blueprint.

---

## 43.4 Qwen size

Options:

```text
qwen2.5-coder:1.5b
```

or:

```text
qwen2.5-coder:3b
```

The 1.5B model is the explicit lightweight target in the installation commands.

---

## 43.5 Moondream2 size

The source documents describe it as approximately:

```text
1.4B
```

and:

```text
1.8B
```

No resolution is provided by the source documents.

---

## 43.6 Whisper model

Options:

```text
tiny.en
```

or:

```text
base.en
```

The first blueprint's concrete setup downloads:

```text
tiny.en
```

---

# 44. Recommended Implementation Order

Based strictly on the supplied roadmap:

```text
Foundation
    ↓
LLM communication
    ↓
Daemon
    ↓
Dynamic Island
    ↓
Unix socket IPC
    ↓
Wake word
    ↓
STT
    ↓
TTS
    ↓
Screen capture
    ↓
Vision
    ↓
Command execution
    ↓
Confirmation UI
    ↓
systemd autostart
```

This sequence minimizes the number of moving pieces introduced at once.

---

# 45. Definition of a Functional Prototype

A first functional DARC prototype should be considered successful when it can:

- run locally on the target Arch Linux machine,
- start the daemon,
- display the Dynamic Island,
- receive a text request,
- send it to the local Qwen model,
- display the response,
- communicate over local IPC,
- accept voice activation,
- transcribe a short request,
- generate local speech,
- capture a screenshot,
- send the screenshot to the vision model,
- request confirmation before executing a system command,
- start through a systemd user service.

This combines the major capabilities specified in the two source documents.

---

# 46. Non-Goals Explicitly Implied by the Architecture

DARC is not being designed as:

- a cloud AI client,
- a generic Electron desktop application,
- a single enormous multimodal model,
- a conventional chatbot window,
- a replacement desktop environment,
- an unrestricted autonomous root process.

Its intended role is a lightweight local system assistant.

---

# 47. Long-Term Architectural Direction

The architecture leaves room for additional specialized agents and tools.

Because the coordinator is separated from both the UI and model layer, additional capabilities can theoretically be added as independent components.

The pattern is:

```text
New capability
      │
      ▼
New specialized model/tool
      │
      ▼
Coordinator routing rule
      │
      ▼
UI status/output integration
```

This is consistent with the project's agentic and modular design.

---

# 48. Complete Architecture Summary

DARC can be summarized as:

```text
                         ┌───────────────────────┐
                         │        USER           │
                         └───────────┬───────────┘
                                     │
                          voice / text / hotkey
                                     │
                                     ▼
                    ┌─────────────────────────────┐
                    │       DARC UI               │
                    │     Dynamic Island          │
                    │                             │
                    │ idle / listening / process  │
                    │ response / confirmation     │
                    └──────────────┬──────────────┘
                                   │
                           Unix Domain Socket
                                   │
                                   ▼
                    ┌─────────────────────────────┐
                    │       DARC DAEMON           │
                    │                             │
                    │ state manager               │
                    │ request queue               │
                    │ router                      │
                    │ execution engine             │
                    └───────┬──────────┬──────────┘
                            │          │
                  ┌─────────┘          └──────────┐
                  ▼                               ▼
       ┌──────────────────────┐        ┌──────────────────────┐
       │    MODEL LAYER       │        │     TOOL LAYER       │
       │                      │        │                      │
       │ Qwen Coder           │        │ grim / maim           │
       │ Moondream2           │        │ slurp                 │
       │ Whisper.cpp          │        │ wl-clipboard / xclip │
       │ Piper TTS            │        │ pacman                │
       │ openwakeword         │        │ hyprctl               │
       └──────────────────────┘        │ subprocess            │
                                       └──────────────────────┘

                    systemd user service
                             │
                             ▼
                       always available
```

---

# 49. Final Project Definition

**DARC — Dynamic Agentic Runtime Coordinator** is a local Arch Linux AI assistant designed around a strict resource-constrained architecture.

Its defining properties are:

1. **Local** — inference and interaction are intended to remain on the machine.
2. **Dynamic** — the interface changes state according to interaction.
3. **Agentic** — specialized models handle specialized capabilities.
4. **Runtime-oriented** — a background daemon provides persistent availability.
5. **Coordinated** — one central service connects models, tools, UI, and Linux.
6. **Resource-conscious** — the architecture targets CPU-only, constrained hardware.
7. **Modular** — each major subsystem has a dedicated implementation boundary.
8. **Linux-native** — the system relies on systemd, GTK, PipeWire, Wayland/X11 tools, and local inference engines.
9. **Confirmable** — potentially consequential system commands require explicit user confirmation.
10. **Incremental** — implementation proceeds from a minimal LLM/daemon foundation toward a complete voice, vision, and system-control assistant.

The project should be implemented in the five stages defined by the original blueprint:

```text
PHASE 1  → Foundation / Daemon / LLM
PHASE 2  → Dynamic Island UI
PHASE 3  → Audio / Wake Word / STT / TTS
PHASE 4  → Vision / System Integration
PHASE 5  → systemd Autostart
```

The resulting system is intended to feel less like a traditional application and more like a small native runtime permanently available inside the Linux desktop.

---

# Appendix A — Original Setup Commands

## System packages

```bash
sudo pacman -S base-devel python python-pipx gtk3 gobject-introspection cairo sox grim slurp
```

## Ollama

```bash
sudo pacman -S ollama
sudo systemctl enable --now ollama
ollama run qwen2.5-coder:1.5b
```

## Whisper.cpp

```bash
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp && make -j4
./models/download-ggml-model.sh tiny.en
```

## Python environment

```bash
pipx install uv
uv venv .venv
source .venv/bin/activate
uv pip install fabric-python openwakeword sounddevice numpy requests pillow
```

---

# Appendix B — Original Directory Blueprint

```text
arch-ai-assistant/
├── main.py
├── ui/
│   ├── __init__.py
│   ├── island.py
│   └── styles.css
├── daemon/
│   ├── __init__.py
│   ├── server.py
│   ├── orchestrator.py
│   └── tools/
│       ├── system_cmd.py
│       └── screen_reader.py
├── models/
│   ├── stt.py
│   ├── tts.py
│   ├── wake_word.py
│   └── llm.py
├── systemd/
│   └── ai-assistant.service
├── requirements.txt
└── config.yaml
```

---

# Appendix C — Component Inventory

| Area | Component | Status in source architecture |
|---|---|---|
| UI | Fabric | Primary blueprint option |
| UI | AGS | Alternative |
| UI | GTK3 | Required UI ecosystem |
| UI | Cairo | UI dependency |
| UI | gtk-layer-shell | Listed for overlay integration |
| Runtime | Python 3.11+ | Primary blueprint runtime |
| Runtime | Rust | Alternative backend language |
| Runtime | asyncio | Primary daemon concurrency model |
| Service | systemd | Persistent runtime |
| IPC | Unix domain sockets | Primary IPC |
| IPC | D-Bus | Alternative mentioned |
| LLM | Qwen 2.5 Coder 1.5B | Primary lightweight model |
| LLM | Qwen 2.5 Coder 3B | Alternative |
| Vision | Moondream2 | Screen/vision model |
| STT | whisper.cpp tiny.en | Primary lightweight STT |
| STT | whisper.cpp base.en | Alternative |
| TTS | Piper | Local speech output |
| Wake word | openwakeword | Local activation |
| Inference | Ollama | Local LLM serving |
| Inference | llama.cpp | Local/native inference option |
| Audio | PipeWire | System audio stack |
| Audio | SoundDevice | Python audio capture option |
| Audio | PyAudio | Python audio capture option |
| Audio | sox | Audio utility |
| Audio | ffmpeg | Alternative audio/media utility |
| Wayland | grim | Screenshot |
| Wayland | slurp | Region selection |
| Wayland | wl-clipboard | Clipboard |
| X11 | maim | Screenshot fallback |
| X11 | xclip | Clipboard fallback |
| System | subprocess | Command execution |
| System | pacman | Package management example |
| System | hyprctl | Window/compositor control example |

---

# Appendix D — Source Reconciliation Notes

This consolidated document does **not** silently resolve source discrepancies.

The supplied sources contain these differences:

1. **Fabric vs AGS** — both are retained as UI options.
2. **Python vs Rust** — Python remains the concrete implementation because the directory structure and daemon blueprint are Python-based.
3. **Unix sockets vs D-Bus** — Unix sockets remain the concrete IPC mechanism because they are used by the detailed blueprint.
4. **Qwen 1.5B vs 3B** — 1.5B remains the primary target because it is used in the installation commands and is more aligned with the stated hardware constraint.
5. **Moondream2 1.4B vs 1.8B** — both source values are retained; the supplied documents do not explain the difference.
6. **Whisper tiny.en vs base.en** — `tiny.en` remains the primary target because it is the model explicitly downloaded by the setup procedure.
7. **`ai-assistant.service` vs `darc-daemon.service`** — both names appear in the sources; the directory blueprint uses `ai-assistant.service`.

These should be finalized before implementation reaches production status.

---

# Appendix E — Project Status Model

The architecture implies the following runtime states:

```text
OFFLINE
   │
   ▼
STARTING
   │
   ▼
IDLE
   │
   ├──────────────► LISTENING
   │                    │
   │                    ▼
   │                PROCESSING
   │                    │
   │                    ▼
   │                RESPONDING
   │                    │
   │                    ▼
   └────────────────── IDLE
```

System-command workflows add:

```text
PROCESSING
    │
    ▼
CONFIRMATION_REQUIRED
    │
    ├── APPROVED → EXECUTING → RESPONDING
    │
    └── DENIED   → IDLE / RESPONDING
```

---

# Appendix F — Implementation Checklist

## Foundation

- [ ] Create project directory.
- [ ] Create Python virtual environment.
- [ ] Install system dependencies.
- [ ] Install Ollama.
- [ ] Install Qwen model.
- [ ] Compile/install whisper.cpp.
- [ ] Install/download Whisper model.
- [ ] Install Piper.
- [ ] Install openwakeword.
- [ ] Verify each dependency independently.

## Daemon

- [ ] Create `daemon/server.py`.
- [ ] Create `daemon/orchestrator.py`.
- [ ] Implement Unix socket.
- [ ] Implement request queue.
- [ ] Implement basic state machine.
- [ ] Implement Ollama wrapper.
- [ ] Implement response streaming.

## UI

- [ ] Create `ui/island.py`.
- [ ] Create `styles.css`.
- [ ] Create borderless GTK window.
- [ ] Implement rounded geometry.
- [ ] Implement idle state.
- [ ] Implement listening state.
- [ ] Implement processing state.
- [ ] Implement response state.
- [ ] Implement confirmation state.
- [ ] Connect UI to daemon.

## Audio

- [ ] Implement wake-word wrapper.
- [ ] Implement microphone capture.
- [ ] Implement STT wrapper.
- [ ] Implement TTS wrapper.
- [ ] Implement playback.
- [ ] Connect audio states to UI.

## Vision

- [ ] Implement screenshot capture.
- [ ] Implement Wayland path.
- [ ] Implement X11 fallback.
- [ ] Implement Moondream2 wrapper.
- [ ] Connect vision requests to router.
- [ ] Return visual results to UI/TTS.

## System control

- [ ] Implement subprocess wrapper.
- [ ] Implement command proposal format.
- [ ] Implement confirmation flow.
- [ ] Capture command stdout/stderr.
- [ ] Return command results to coordinator.
- [ ] Define final command validation policy.

## Runtime

- [ ] Create systemd user service.
- [ ] Enable service.
- [ ] Test startup.
- [ ] Test daemon recovery.
- [ ] Test UI reconnect behavior.

## End-to-end

- [ ] Text → Qwen → UI.
- [ ] Wake word → STT → Qwen → TTS.
- [ ] Voice → screen capture → vision → response.
- [ ] AI command → confirmation → execution.
- [ ] Boot → systemd → DARC ready.

---

# End of DARC Project Specification

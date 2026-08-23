#!/usr/bin/env python3
"""Strip Nova's <thinking> blocks at the CLIENT, not in the prompt.

    python patch_thinking.py

Why
---
Nova Pro emits its deliberation as visible text (<thinking>...</thinking>).
Forbidding that in the system prompt ALSO stops it from calling tools: the
reasoning channel is where it commits to a toolUse block. Measured on this
project, same gateway and harness, only the prompt line differing:

    prompt allows <thinking>  -> [tool call] fires, DynamoDB row created
    prompt forbids <thinking> -> no tool call, model invents a ticket ID

So the model keeps its reasoning channel, and the client hides it. This
patches chat.py (transcripts stay readable) and generate-eval-dataset.py
(the judge scores the customer-facing answer, not the deliberation).

Idempotent: running it twice is harmless.
"""

import re
import sys
from pathlib import Path

HELPER = '''THINKING_RE = re.compile(r"<thinking>.*?</thinking>\\s*", re.DOTALL | re.IGNORECASE)


def strip_thinking(text):
    """Remove Nova's visible reasoning blocks from a reply."""
    text = THINKING_RE.sub("", text)
    # An unclosed block (truncated stream): drop from the tag onward.
    idx = text.lower().find("<thinking>")
    if idx != -1:
        text = text[:idx]
    return text.strip()


'''


def patch(path, edits, marker="def strip_thinking("):
    p = Path(path)
    if not p.exists():
        print(f"SKIP {path}: not found")
        return False
    text = p.read_text(encoding="utf-8")
    if marker in text:
        print(f"SKIP {path}: already patched")
        return False
    for old, new in edits:
        if old not in text:
            sys.exit(f"FAIL {path}: anchor not found:\n{old!r}")
        text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    print(f"OK   {path}")
    return True


# --- chat.py: buffer the stream, print each message with thinking removed ----
CHAT_EDITS = [
    ("import argparse\nimport json",
     "import argparse\nimport json\nimport re"),
    ("def event_stream(response):",
     HELPER + "def event_stream(response):"),
    ('''            if "text" in delta:
                print(delta["text"], end="", flush=True)
                buffer.append(delta["text"])
        elif "messageStop" in event:
            if buffer:
                texts.append("".join(buffer))
                buffer = []
    if buffer:
        texts.append("".join(buffer))
    print()
    return texts[-1] if texts else ""''',
     '''            if "text" in delta:
                buffer.append(delta["text"])
        elif "messageStop" in event:
            if buffer:
                message = "".join(buffer)
                texts.append(message)
                visible = strip_thinking(message)
                if visible:
                    print(visible, flush=True)
                buffer = []
    if buffer:
        message = "".join(buffer)
        texts.append(message)
        visible = strip_thinking(message)
        if visible:
            print(visible, flush=True)
    return strip_thinking(texts[-1]) if texts else ""'''),
]

# --- generate-eval-dataset.py: clean the text written to the JSONL -----------
EVAL_EDITS = [
    ("import argparse\nimport json",
     "import argparse\nimport json\nimport re"),
    ("def _event_stream(response):",
     HELPER + "def _event_stream(response):"),
    ('    return {"final_output_text": texts[-1] if texts else ""}',
     '    return {"final_output_text": strip_thinking(texts[-1]) if texts else ""}'),
]


def main():
    changed = patch("chat.py", CHAT_EDITS)
    changed |= patch("generate-eval-dataset.py", EVAL_EDITS)
    if changed:
        print("\nDone. Replies now print without <thinking>; the model still "
              "reasons (and still calls tools) server-side.")


if __name__ == "__main__":
    main()

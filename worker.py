"""Agent Queue worker: polls Todoist, runs Dispatch Briefs through the Anthropic API."""
import os, re, sys, argparse
import requests

TODOIST = "https://api.todoist.com/api/v1"
PROJECT_ID = "6hwqGFMGXrr369rX"
SEC_QUEUED = "6hwqGFr6GH7CF8QX"
SEC_PROGRESS = "6hwqGFr4J9h4r3mX"
SEC_REVIEW = "6hwqGFpmMQR33xF5"
WIP_LIMIT = 2
REQUIRED_LABEL = "track2"
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 16000
CHUNK = 14000


def parse_brief(text):
    """Return (system_prompt, [(name, prompt_text), ...]) from a Dispatch Brief."""
    sec = re.search(r"## 4\. EXECUTION PROMPTS.*?(?=\n## 5\.)", text, re.S)
    if not sec:
        raise ValueError("Section 4 not found")
    parts = re.split(r"\n### ", "\n" + sec.group(0))
    prompts = {}
    for p in parts[1:]:
        title, _, body = p.partition("\n")
        prompts[title.strip()] = body.strip()
    system = next((v for k, v in prompts.items() if k.startswith("Prompt 0")), None)
    steps = [(k, v) for k, v in prompts.items() if not k.startswith("Prompt 0")]
    if not system or len(steps) < 2:
        raise ValueError("Prompt 0 or steps missing")
    ret = re.search(r"Return Block \(.*?\):\s*```\n(.*?)```", text, re.S)
    template = ret.group(1).strip() if ret else "RETURN BLOCK"
    return system, steps, template


class Todo:
    def __init__(self, token, dry):
        self.h = {"Authorization": f"Bearer {token}"}
        self.dry = dry

    def get(self, path, **params):
        r = requests.get(f"{TODOIST}{path}", headers=self.h, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        if self.dry:
            print(f"[dry-run] POST {path} {str(body)[:120]}")
            return {}
        r = requests.post(f"{TODOIST}{path}", headers=self.h, json=body, timeout=30)
        r.raise_for_status()
        return r.json() if r.text else {}

    def tasks_in(self, section_id):
        out, cursor = [], None
        while True:
            d = self.get("/tasks", section_id=section_id, **({"cursor": cursor} if cursor else {}))
            out += d.get("results", [])
            cursor = d.get("next_cursor")
            if not cursor:
                return out

    def move(self, tid, section_id):
        self.post(f"/tasks/{tid}/move", {"section_id": section_id})

    def comment(self, tid, content):
        for i in range(0, len(content), CHUNK):
            self.post("/comments", {"task_id": tid, "content": content[i:i + CHUNK]})

    def add_label(self, task, label):
        labels = list(task.get("labels", []))
        if label not in labels:
            self.post(f"/tasks/{task['id']}", {"labels": labels + [label]})


def run_steps(client, system, steps):
    msgs, replies = [], []
    for name, prompt in steps:
        msgs.append({"role": "user", "content": prompt})
        with client.messages.stream(model=MODEL, max_tokens=MAX_TOKENS, system=system, messages=msgs) as s:
            reply = s.get_final_message()
        text = "".join(b.text for b in reply.content if b.type == "text")
        msgs.append({"role": "assistant", "content": text})
        replies.append((name, text))
        if name.startswith("Prompt 1") and not all(n in text for n in ("121", "45", "33")):
            return replies, f"Count check failed at {name}"
    return replies, None


def process(todo, client, task):
    tid = task["id"]
    print(f"Running {tid}: {task['content']}")
    todo.move(tid, SEC_PROGRESS)
    try:
        system, steps, template = parse_brief(task.get("description", ""))
        brief = task["description"].replace("this card's Todoist ID", tid)
        full_system = (
            system
            + f"\n\nYour Task ID is {tid}. You cannot save files in this run. The full report you write in chat is the deliverable, so do not list a missing file as a gap and do not mark the run PARTIAL for that reason. Do not wrap the Return Block in code fences."
            + "\n\nDISPATCH BRIEF (your only source of truth):\n" + brief
        )
        replies, err = run_steps(client, full_system, steps)
    except Exception as e:
        todo.add_label(task, "needs-rod")
        todo.comment(tid, f"RETURN BLOCK\nTask ID: {tid}\nStatus: BLOCKED\nGaps: worker error: {e}")
        todo.move(tid, SEC_REVIEW)
        return
    final = replies[-1][1]
    i = final.find("RETURN BLOCK")
    block = final[i:] if i >= 0 else f"RETURN BLOCK\nStatus: PARTIAL\nGaps: final step gave no Return Block"
    block = block.replace("```", "").strip()
    if err:
        block += f"\nWorker stop reason: {err}"
        todo.add_label(task, "needs-rod")
    todo.comment(tid, block)
    report = "\n\n".join(f"### {n}\n{t}" for n, t in replies[:-1])
    if report:
        todo.comment(tid, "FULL REPORT (worker output)\n\n" + report)
    todo.move(tid, SEC_REVIEW)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="read Todoist, parse briefs, no writes, no API calls")
    ap.add_argument("--parse-file", help="parse a local brief file and print the steps")
    a = ap.parse_args()
    if a.parse_file:
        system, steps, _ = parse_brief(open(a.parse_file).read())
        print("SYSTEM:", system[:80], "...")
        for n, p in steps:
            print("STEP:", n, f"({len(p)} chars)")
        return
    todo = Todo(os.environ["TODOIST_API_TOKEN"], a.dry_run)
    if len(todo.tasks_in(SEC_PROGRESS)) >= WIP_LIMIT:
        print("WIP limit reached, skipping")
        return
    queued = [t for t in todo.tasks_in(SEC_QUEUED) if REQUIRED_LABEL in t.get("labels", [])]
    if not queued:
        print("Queue empty")
        return
    task = queued[0]
    if a.dry_run:
        system, steps, _ = parse_brief(task.get("description", ""))
        print(f"[dry-run] would run {task['id']} with {len(steps)} steps, model {MODEL}")
        return
    import anthropic
    process(todo, anthropic.Anthropic(), task)


if __name__ == "__main__":
    main()

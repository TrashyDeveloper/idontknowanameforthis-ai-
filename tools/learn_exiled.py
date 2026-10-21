"""Teach Aria the EXILED plugin framework by reading its source code.

Usage:
    python tools/learn_exiled.py                 # downloads the latest EXILED from GitHub
    python tools/learn_exiled.py path/to/EXILED  # uses a copy you already have

It writes data/packs/exiled.json, a "knowledge pack" Aria loads on startup, with:
  * every event (what it does, what `ev` contains, whether it can be cancelled, how to subscribe)
  * the public API (Player, Server, Map, Room, Door, ...) with each member's description
  * the CustomRoles and CustomItems APIs (making your own roles, abilities and items)
  * enums (RoleTypeId-style lists such as DamageType, EffectType, DoorType, ...)
  * the official example plugin's source files
  * real source of the CustomRoles/CustomItems commands and event handlers, Exiled.Events'
    commands, features and internal handlers, and its config/csproj
  * every Harmony patch: which game method it changes and which events it fires
    (prefix/postfix patches are kept as full Harmony examples)

EXILED is made by the ExMod Team and licensed under CC BY-SA 3.0; the generated pack keeps
that attribution and license.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ZIP_URL = "https://codeload.github.com/ExMod-Team/EXILED/zip/refs/heads/master"
REPO_URL = "https://github.com/ExMod-Team/EXILED"
OUT = Path(__file__).resolve().parent.parent / "data" / "packs" / "exiled.json"
MEMBERS_PER_ENTRY = 25
MAX_SOURCE_CHARS = 12000  # longer files are cut, with a link to the full file
BLOB_URL = REPO_URL + "/blob/master/EXILED/"

# Folders whose real source code Aria keeps as examples (relative to the EXILED folder).
SOURCE_FOLDERS = [
    "Exiled.CustomRoles/Commands", "Exiled.CustomRoles/Events", "Exiled.CustomRoles/CustomRoles.cs",
    "Exiled.CustomRoles/Config.cs", "Exiled.CustomRoles/API/Extensions.cs",
    "Exiled.CustomItems/Commands", "Exiled.CustomItems/Events", "Exiled.CustomItems/Patches",
    "Exiled.CustomItems/CustomItems.cs", "Exiled.CustomItems/Config.cs", "Exiled.CustomItems/API/Extensions.cs",
    "Exiled.Events/Attributes", "Exiled.Events/Commands", "Exiled.Events/Features", "Exiled.Events/Handlers/Internal",
    "Exiled.Events/Config.cs", "Exiled.Events/Events.cs", "Exiled.Events/Exiled.Events.csproj",
]


# ---------- reading C# doc comments ----------

def clean_doc(text: str) -> str:
    """Turn an XML doc comment into plain text."""
    text = re.sub(r'<see(?:also)? cref="(?:[A-Z]:)?([^"]+)"\s*>(.*?)</see(?:also)?>', r"\2", text)
    text = re.sub(r'<see(?:also)? cref="(?:[A-Z]:)?([^"]+)"\s*/>',
                  lambda m: re.sub(r"\(.*\)", "", m.group(1)).split(".")[-1].replace("{", "<").replace("}", ">"), text)
    text = re.sub(r'<see langword="([^"]+)"\s*/>', r"\1", text)
    text = re.sub(r'<(?:paramref|typeparamref) name="([^"]+)"\s*/>', r"\1", text)
    text = re.sub(r"</?(?:c|para|b|i|code|list|item|description|term)>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def doc_blocks(source: str):
    """Yield (summary, declaration line, line number) for every documented declaration."""
    lines = source.splitlines()
    doc: list[str] = []
    for i, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith("///"):
            doc.append(line[3:].strip())
            continue
        if not doc:
            continue
        if line.startswith("[") or not line:
            continue  # attributes between the comment and the declaration
        joined = " ".join(doc)
        doc = []
        j = i
        while line.count("(") > line.count(")") and j + 1 < len(lines):  # declaration spans lines
            j += 1
            line += " " + lines[j].strip()
        line = re.sub(r"\(\s+", "(", line)
        if "<inheritdoc" in joined and "<summary>" not in joined:
            summary = ""
        else:
            m = re.search(r"<summary>(.*?)</summary>", joined)
            summary = clean_doc(m.group(1)) if m else ""
        yield summary, line, i


def signature(decl: str) -> str:
    """Shorten a declaration line to its signature."""
    decl = re.sub(r"\s*//.*$", "", decl)
    depth = 0
    for i, ch in enumerate(decl):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and "=>" in decl[i:] or depth == 0 and decl[i + 1:].lstrip().startswith(("{", ";", "where")):
                return decl[: i + 1].strip()
        elif depth == 0 and decl.startswith(("=>", "{", "= ", ";"), i):
            return decl[:i].strip()
        elif depth == 0 and decl.startswith(" {", i):
            return decl[:i].strip()
    return decl.rstrip(";{ ").strip()


def strip_modifiers(sig: str) -> str:
    return re.sub(r"^(?:public|static|override|virtual|abstract|sealed|readonly|new|partial|extern|unsafe|\s)+", "", sig)


def camel_words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)]


# ---------- events ----------

def parse_event_args(path: Path) -> dict:
    source = path.read_text(encoding="utf-8-sig")
    info = {"summary": "", "interfaces": [], "properties": []}
    m = re.search(r"public (?:sealed |abstract )*class \w+(?:<[^>]+>)?\s*:\s*([^\n{]+)", source)
    if m:
        info["interfaces"] = [s.strip() for s in m.group(1).split(",")]
    for summary, decl, _ in doc_blocks(source):
        if re.match(r"public (?:sealed |abstract )*class ", decl):
            info["summary"] = summary
        elif decl.startswith("public ") and "(" not in decl.split("{")[0] and " class " not in decl:
            sig = strip_modifiers(signature(decl))
            parts = sig.rsplit(" ", 1)
            if len(parts) == 2:
                settable = "set;" in decl or "set {" in decl or summary.startswith("Gets or sets")
                info["properties"].append({"type": parts[0], "name": parts[1], "doc": summary, "settable": settable})
    return info


def learn_events(root: Path, version: str) -> list[dict]:
    handlers = root / "Exiled.Events" / "Handlers"
    args_dir = root / "Exiled.Events" / "EventArgs"
    args_files: dict[str, list[Path]] = {}
    for p in args_dir.rglob("*.cs"):
        args_files.setdefault(p.stem, []).append(p)

    entries = []
    for handler in sorted(handlers.glob("*.cs")):
        category = handler.stem
        source = handler.read_text(encoding="utf-8-sig")
        for summary, decl, _ in doc_blocks(source):
            m = re.match(r"public static Event(?:<(\w+)>)?\s+(\w+)\s*\{", decl)
            if not m:
                continue
            args_name, event = m.group(1), m.group(2)
            text = [summary or f"The {event} event."]
            usings = ["Exiled.Events.EventArgs." + category] if args_name else []
            interfaces, props = [], []
            if args_name:
                candidates = args_files.get(args_name, [])
                preferred = [p for p in candidates if p.parent.name == category] or candidates
                if preferred:
                    info = parse_event_args(preferred[0])
                    interfaces, props = info["interfaces"], info["properties"]
                    usings = ["Exiled.Events.EventArgs." + preferred[0].parent.name]
                if any(i.startswith("IDeniableEvent") for i in interfaces):
                    text.append("It can be cancelled: set `ev.IsAllowed = false;`.")
                if props:
                    text.append("`ev` (" + args_name + ") contains:")
                    for p in props:
                        rw = " (can be changed)" if p["settable"] else ""
                        text.append(f"- `{p['type']} {p['name']}`{rw}" + (f": {p['doc']}" if p["doc"] else ""))
            handler_name = "On" + event
            param = f"{args_name} ev" if args_name else ""
            body = "    // " + ", ".join(f"ev.{p['name']}" for p in props[:4]) if props else "    // your code here"
            code = "\n".join([
                *[f"using {u};" for u in usings],
                "",
                f"// In OnEnabled():  Exiled.Events.Handlers.{category}.{event} += {handler_name};",
                f"// In OnDisabled(): Exiled.Events.Handlers.{category}.{event} -= {handler_name};",
                f"private void {handler_name}({param})",
                "{",
                body,
                "}",
            ])
            entries.append({
                "id": f"exiled-event-{category}.{event}",
                "kind": "code",
                "title": f"EXILED event: {category}.{event}",
                "text": "\n".join(text),
                "code": code,
                "framework": "EXILED",
                "tags": ["exiled", "event", category.lower(), *camel_words(event)],
            })
    return entries


# ---------- API reference ----------

TYPE_DECL = re.compile(r"public (?:static |sealed |abstract |partial |readonly )*(class|struct|enum|interface) (\w+)")


def learn_api(root: Path, folders: list[str]) -> list[dict]:
    entries = []
    for folder in folders:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.cs")):
            source = path.read_text(encoding="utf-8-sig")
            namespace = (re.search(r"namespace ([\w.]+)", source) or [None, ""])[1]
            types: dict[str, dict] = {}
            current = None
            in_enum = False
            for summary, decl, _ in doc_blocks(source):
                t = TYPE_DECL.match(decl)
                if t:
                    current = t.group(2)
                    in_enum = t.group(1) == "enum"
                    types.setdefault(current, {"kind": t.group(1), "summary": summary, "members": []})
                    continue
                if current is None:
                    continue
                if in_enum:
                    m = re.match(r"(\w+)\s*(?:=\s*[^,]+)?,?$", decl)
                    if m:
                        types[current]["members"].append((m.group(1), summary))
                elif decl.startswith("public ") or decl.startswith("protected "):
                    if decl.startswith("protected "):
                        continue
                    types[current]["members"].append((strip_modifiers(signature(decl)), summary))

            for name, info in types.items():
                members = info["members"]
                if not members and not info["summary"]:
                    continue
                chunks = [members[i:i + MEMBERS_PER_ENTRY] for i in range(0, len(members), MEMBERS_PER_ENTRY)] or [[]]
                for n, chunk in enumerate(chunks, 1):
                    part = f" (part {n} of {len(chunks)})" if len(chunks) > 1 else ""
                    lines = [f"{info['kind']} `{namespace}.{name}`: {info['summary']}".rstrip(": ")]
                    if chunk:
                        lines.append("Values:" if info["kind"] == "enum" else "Members:")
                        lines += [f"- `{sig}`" + (f": {doc}" if doc else "") for sig, doc in chunk]
                    extra = ["custom", "role"] if "CustomRoles" in namespace else ["custom", "item"] if "CustomItems" in namespace else []
                    entries.append({
                        "id": f"exiled-api-{namespace}.{name}-{n}",
                        "kind": "fact",
                        "title": f"EXILED API: {name}{part}",
                        "text": "\n".join(lines),
                        "tags": ["exiled", "api", info["kind"], name.lower(), *camel_words(name), *extra],
                    })
    return entries


def learn_examples(root: Path) -> list[dict]:
    entries = []
    base = root / "Exiled.Example"
    for path in sorted(base.rglob("*.cs")):
        source = path.read_text(encoding="utf-8-sig")
        code = re.sub(r"\A(?://[^\n]*\n)+\s*", "", source).strip()  # drop the license header
        rel = path.relative_to(base).as_posix()
        summaries = [s for s, decl, _ in doc_blocks(source) if TYPE_DECL.match(decl)]
        entries.append({
            "id": f"exiled-example-{rel}",
            "kind": "code",
            "title": f"EXILED official example plugin: {rel}",
            "text": (summaries[0] + " " if summaries else "") +
                    "Real, working code from the official Exiled.Example plugin.",
            "code": code,
            "framework": "EXILED",
            "tags": ["exiled", "example", "plugin", *camel_words(path.stem)],
        })
    return entries


# ---------- source files and Harmony patches ----------

def strip_header(source: str) -> str:
    return re.sub(r"\A(?://[^\n]*\n)+\s*", "", source).strip()


def cut(code: str, rel: str) -> str:
    if len(code) <= MAX_SOURCE_CHARS:
        return code
    return code[:MAX_SOURCE_CHARS].rsplit("\n", 1)[0] + f"\n\n// ... (cut here; full file: {BLOB_URL}{rel})"


def learn_sources(root: Path) -> list[dict]:
    entries = []
    for target in SOURCE_FOLDERS:
        path = root / target
        files = [path] if path.is_file() else sorted(path.rglob("*.cs")) if path.is_dir() else []
        for f in files:
            rel = f.relative_to(root).as_posix()
            source = f.read_text(encoding="utf-8-sig")
            summaries = [s for s, decl, _ in doc_blocks(source) if TYPE_DECL.match(decl) or " class " in decl]
            project = rel.split("/")[0]
            extra = ["custom", "role"] if "CustomRoles" in rel else ["custom", "item"] if "CustomItems" in rel else []
            if "/Commands/" in rel:
                extra.append("command")
            entries.append({
                "id": f"exiled-source-{rel}",
                "kind": "code",
                "title": f"EXILED source: {rel}",
                "text": ((summaries[0] + " ") if summaries and summaries[0] else "") +
                        f"Real source code from {project} ({BLOB_URL}{rel}).",
                "code": cut(strip_header(source), rel),
                "framework": "EXILED",
                "tags": ["exiled", "source", *camel_words(f.stem), *[w.lower() for w in rel.split("/")[:-1]], *extra],
            })
    return entries


def patch_info(source: str) -> dict:
    targets = re.findall(r"\[HarmonyPatch\(typeof\(([\w.]+)\)(?:,\s*nameof\(([\w.]+)\)|,\s*\"(\w+)\")?", source)
    methods = []
    for cls, nameof, literal in targets:
        member = (nameof or literal or "").split(".")[-1]
        methods.append(f"{cls.split('.')[-1]}.{member}" if member else cls.split(".")[-1])
    events = [f"{c.split('.')[-1]}.{e.split('.')[-1]}"
              for c, e in re.findall(r"\[EventPatch\(typeof\(([\w.]+)\),\s*nameof\(([\w.]+)\)\)", source)]
    kinds = [k for k in ("Prefix", "Postfix", "Transpiler", "Finalizer") if re.search(rf"\b{k}\(", source)]
    return {"methods": methods, "events": events, "kinds": kinds}


def learn_patches(root: Path, events: list[dict]) -> list[dict]:
    base = root / "Exiled.Events" / "Patches"
    entries = []
    hooks: dict[str, list[str]] = {}
    for f in sorted(base.rglob("*.cs")):
        rel = f.relative_to(root).as_posix()
        short = f.relative_to(base).as_posix()
        source = f.read_text(encoding="utf-8-sig")
        info = patch_info(source)
        summary = next((s for s, decl, _ in doc_blocks(source) if " class " in decl), "")
        lines = [summary or f"Harmony patch {short}."]
        if info["methods"]:
            lines.append("Game method patched: " + ", ".join(f"`{m}`" for m in info["methods"]) + ".")
        if info["events"]:
            lines.append("Fires the EXILED events: " + ", ".join(f"`{e}`" for e in info["events"]) + ".")
        if info["kinds"]:
            lines.append("Harmony patch type: " + ", ".join(info["kinds"]) + ".")
        lines.append(f"Source: {BLOB_URL}{rel}")
        for e in info["events"]:
            hooks.setdefault(e, []).extend(info["methods"])
        readable = info["kinds"] and "Transpiler" not in info["kinds"]
        entry = {
            "id": f"exiled-patch-{short}",
            "kind": "code" if readable else "fact",
            "title": f"EXILED patch: {short}",
            "text": "\n".join(lines),
            "framework": "EXILED",
            "tags": ["exiled", "patch", "harmony", *[k.lower() for k in info["kinds"]], *camel_words(f.stem),
                     *[w.lower() for w in short.split("/")[:-1]]],
        }
        if readable:
            # Prefix/postfix patches are easy to read, so they're kept as Harmony examples.
            entry["code"] = cut(strip_header(source), rel)
            entry["tags"] += ["example", "harmony", "patch"]
        entries.append(entry)
    # Tell each event which game method it hooks into.
    for ev in events:
        name = ev["title"].removeprefix("EXILED event: ")
        if hooks.get(name):
            methods = ", ".join(f"`{m}`" for m in dict.fromkeys(hooks[name]))
            ev["text"] = ev["text"].replace("\n", f"\nHooks into the game method {methods}.\n", 1) \
                if "\n" in ev["text"] else ev["text"] + f"\nHooks into the game method {methods}."
    return entries


# ---------- main ----------

def find_root(path: Path) -> Path:
    """Accept the repo root, the EXILED folder inside it, or an unzipped download."""
    for candidate in [path, path / "EXILED", *path.glob("*/EXILED"), *path.glob("*")]:
        if (candidate / "Exiled.Events").is_dir() and (candidate / "Exiled.API").is_dir():
            return candidate
    sys.exit(f"Couldn't find the EXILED source (Exiled.API and Exiled.Events folders) in {path}")


def download(tmp: Path) -> Path:
    print(f"Downloading EXILED from {REPO_URL} ...")
    with urllib.request.urlopen(ZIP_URL, timeout=120) as resp:
        data = resp.read()
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
    return tmp


def build_pack(root: Path) -> dict:
    props = (root / "EXILED.props").read_text(encoding="utf-8-sig") if (root / "EXILED.props").exists() else ""
    version = (re.search(r"<Version[^>]*>([^<]+)</Version>", props) or [None, "unknown"])[1]
    events = learn_events(root, version)
    entries = [
        {
            "id": "exiled-overview",
            "kind": "fact",
            "title": "EXILED framework overview",
            "text": (f"EXILED (version {version}) is a community plugin framework for SCP: Secret Laboratory servers, "
                     "made by the ExMod Team. Plugins inherit `Plugin<Config>` (Exiled.API.Features) with a config class "
                     "implementing `IConfig` (IsEnabled, Debug). Subscribe to events in OnEnabled() with "
                     "`Exiled.Events.Handlers.<Category>.<Event> += handler;` and unsubscribe in OnDisabled(). "
                     "Events whose args implement IDeniableEvent can be cancelled with `ev.IsAllowed = false`. "
                     "Commands use the game's CommandSystem: `[CommandHandler(typeof(RemoteAdminCommandHandler))]` on a "
                     "class implementing `ICommand`. Custom roles inherit `CustomRole` (Exiled.CustomRoles.API.Features) "
                     "and custom items inherit `CustomItem` (Exiled.CustomItems.API.Features); both need the "
                     "Exiled.CustomRoles / Exiled.CustomItems plugins installed and are registered with "
                     "`CustomRole.RegisterRoles(...)` / `CustomItem.RegisterItems()`. Event categories: " +
                     ", ".join(sorted(p.stem for p in (root / "Exiled.Events" / "Handlers").glob("*.cs"))) + "."),
            "tags": ["exiled", "overview", "framework", "plugin", "event", "category"],
        },
        *events,
        *learn_api(root, ["Exiled.API/Features", "Exiled.API/Enums", "Exiled.API/Extensions",
                          "Exiled.API/Interfaces", "Exiled.CustomRoles/API", "Exiled.CustomItems/API",
                          "Exiled.Events/EventArgs/Interfaces", "Exiled.Events/Features"]),
        *learn_examples(root),
        *learn_sources(root),
        *learn_patches(root, events),
    ]
    seen = set()
    unique = []
    for e in entries:
        if e["id"] not in seen:
            seen.add(e["id"])
            unique.append(e)
    return {
        "name": "EXILED",
        "version": version,
        "source": REPO_URL,
        "license": "CC BY-SA 3.0 (content derived from EXILED by the ExMod Team)",
        "generated": time.strftime("%Y-%m-%d"),
        "entries": unique,
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = find_root(Path(sys.argv[1]) if len(sys.argv) > 1 else download(Path(tmp)))
        pack = build_pack(root)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(pack, indent=1, ensure_ascii=False), encoding="utf-8")
    kinds = {}
    for e in pack["entries"]:
        key = e["id"].split("-")[1]
        kinds[key] = kinds.get(key, 0) + 1
    print(f"Learned EXILED {pack['version']}: " + ", ".join(f"{n} {k}" for k, n in kinds.items()))
    print(f"Saved to {OUT}. Restart Aria to use it.")


if __name__ == "__main__":
    main()

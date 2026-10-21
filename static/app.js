// Aria web page: chat, teaching forms and the knowledge browser.

const $ = (sel, root = document) => root.querySelector(sel);
const STORAGE_KEY = "aria-chat";
let chatHistory = []; // {role, content} pairs sent with each question for context
let saved = []; // messages kept across page reloads: {role, content, sources}

// ---------- tiny, safe Markdown renderer ----------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function inline(s) {
  const codes = [];
  // Inline code is set aside first so nothing inside it is formatted.
  let out = escapeHtml(s).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
  out = out
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_]+)_(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])\*([^*]+)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");
  return out.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}

function renderMarkdown(text) {
  const out = [];
  const lines = text.split("\n");
  let i = 0;
  let para = [];
  const flush = () => {
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    para = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++;
      out.push(`<div class="codeblock"><button class="copy" type="button">Copy</button><pre><code>${escapeHtml(code.join("\n"))}</code></pre></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)/);
    if (heading) {
      flush();
      const level = heading[1].length < 3 ? 3 : 4;
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }
    if (/^\s*([-*]|\d+[.)])\s+/.test(line)) {
      flush();
      const ordered = /^\s*\d/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+[.)])\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*([-*]|\d+[.)])\s+/, ""))}</li>`);
        i++;
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
      continue;
    }
    if (!line.trim()) flush();
    else para.push(line);
    i++;
  }
  flush();
  return out.join("");
}

document.addEventListener("click", (e) => {
  if (!e.target.classList.contains("copy")) return;
  const code = e.target.parentElement.querySelector("code").textContent;
  navigator.clipboard.writeText(code).then(
    () => {
      e.target.textContent = "Copied!";
      setTimeout(() => (e.target.textContent = "Copy"), 1500);
    },
    () => (e.target.textContent = "Select & copy")
  );
});

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// ---------- tabs and status ----------

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
    if (tab.dataset.tab === "knowledge") loadKnowledge();
    if (tab.dataset.tab === "chat") input.focus();
  })
);

async function refreshStatus() {
  const el = $("#status");
  try {
    const s = await (await fetch("/api/status")).json();
    el.textContent = `${s.mode === "claude" ? "● Claude mode" : "○ Offline mode"} · ${s.learned} learned` +
      (s.words ? ` · ${plural(s.words, "word")}` : "");
    el.className = `status ${s.mode}`;
    el.title = s.mode === "claude"
      ? "Using Claude to write answers from what she knows."
      : "No API key found: answers come straight from what she knows. See the README to turn on Claude mode.";
  } catch {
    el.textContent = "⚠ Aria isn't running";
    el.className = "status offline";
    el.title = "Start Aria with: python app.py";
  }
}

// ---------- chat ----------

const messagesEl = $("#messages");
const input = $("#chat-input");

function nearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 120;
}

function addMessage(role, content) {
  const msg = document.createElement("div");
  msg.className = `msg ${role}`;
  msg.innerHTML = `<div class="bubble"></div>`;
  if (role === "user") $(".bubble", msg).textContent = content;
  else $(".bubble", msg).innerHTML = renderMarkdown(content);
  messagesEl.appendChild(msg);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return msg;
}

function addSources(msg, sources) {
  if (!sources.length) return null;
  const meta = document.createElement("div");
  meta.className = "meta";
  for (const s of sources) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `source ${s.source}`;
    chip.textContent = (s.kind === "code" ? "{ } " : "") + s.title;
    chip.title = (s.source === "user" ? "Something you taught me" : s.pack ? `From the ${s.pack} pack` : "Starter knowledge") +
      " · click to ask about it";
    chip.onclick = () => ask(s.title);
    meta.appendChild(chip);
  }
  msg.appendChild(meta);
  return meta;
}

function addFeedback(msg, question, sources) {
  const meta = addSources(msg, sources) || msg.appendChild(Object.assign(document.createElement("div"), { className: "meta" }));
  const up = Object.assign(document.createElement("button"), { type: "button", className: "vote", textContent: "👍", title: "Helpful (I'll remember how you asked)" });
  const down = Object.assign(document.createElement("button"), { type: "button", className: "vote", textContent: "👎", title: "Wrong, let me correct it" });
  meta.append(up, down);

  const ids = sources.map((s) => s.id);
  up.onclick = async () => {
    up.disabled = down.disabled = true;
    up.classList.add("chosen");
    await postJson("/api/feedback", { sources: ids, helpful: true, question }).catch(() => {});
    up.textContent = "👍 Thanks!";
  };
  down.onclick = () => {
    up.disabled = down.disabled = true;
    down.classList.add("chosen");
    const box = document.createElement("form");
    box.className = "correction";
    box.innerHTML = `<textarea rows="3" placeholder="What's the right answer? I'll remember it."></textarea>
      <div><button type="submit">Teach me</button> <button type="button" class="secondary">Skip</button></div>`;
    msg.appendChild(box);
    $("textarea", box).focus();
    const done = (text) => box.replaceWith(Object.assign(document.createElement("p"), { className: "hint", textContent: text }));
    $("button.secondary", box).onclick = async () => {
      await postJson("/api/feedback", { sources: ids, helpful: false }).catch(() => {});
      done("Noted, I'll trust those answers less.");
    };
    box.onsubmit = async (e) => {
      e.preventDefault();
      const correction = $("textarea", box).value.trim();
      if (!correction) return;
      try {
        await postJson("/api/feedback", { sources: ids, helpful: false, question, correction });
        done("✅ Learned! Ask me again any time.");
        refreshStatus();
      } catch (err) {
        done(`⚠️ Couldn't save that: ${err.message}`);
      }
    };
  };
}

function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(saved.slice(-40)));
  } catch {
    /* storage unavailable: the chat just won't survive a reload */
  }
}

async function ask(question) {
  if ($("#send").disabled) return;
  addMessage("user", question);
  const botMsg = addMessage("bot", "");
  const bubble = $(".bubble", botMsg);
  bubble.classList.add("typing");
  $("#send").disabled = true;

  let answer = "";
  let meta = { sources: [] };
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, history: chatHistory }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "meta") meta = event;
        if (event.type === "text") {
          const stick = nearBottom();
          answer += event.text;
          bubble.innerHTML = renderMarkdown(answer);
          if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
        }
      }
    }
  } catch (err) {
    answer = err instanceof TypeError
      ? "⚠️ I can't reach my server. Is the PowerShell window running `python app.py` still open?"
      : `⚠️ Something went wrong: ${err.message}`;
    bubble.innerHTML = renderMarkdown(answer);
  }
  bubble.classList.remove("typing");
  $("#send").disabled = false;

  chatHistory.push({ role: "user", content: question }, { role: "assistant", content: answer });
  chatHistory = chatHistory.slice(-20);
  saved.push({ role: "user", content: question }, { role: "bot", content: answer, sources: meta.sources || [] });
  persist();
  if (meta.learned) refreshStatus();
  else if (!answer.startsWith("⚠️")) addFeedback(botMsg, question, meta.sources || []);
}

function restoreChat() {
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    if (!Array.isArray(saved)) saved = [];
  } catch {
    saved = [];
  }
  for (const m of saved) {
    const msg = addMessage(m.role, m.content);
    if (m.role === "bot") addSources(msg, m.sources || []);
    chatHistory.push({ role: m.role === "bot" ? "assistant" : "user", content: m.content });
  }
  chatHistory = chatHistory.slice(-20);
}

$("#new-chat").addEventListener("click", () => {
  saved = [];
  chatHistory = [];
  persist();
  messagesEl.querySelectorAll(".msg:not(.intro)").forEach((m) => m.remove());
  input.focus();
});

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (!q || $("#send").disabled) return;
  input.value = "";
  input.style.height = "";
  ask(q);
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
});
document.querySelectorAll(".suggest").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));

// ---------- teach forms ----------

function wireForm(form, send) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const msg = $(".form-msg", form);
    try {
      await send(data);
      msg.classList.remove("error");
      msg.textContent = "✅ Saved! Aria will use this from now on.";
      form.reset();
      refreshStatus();
    } catch (err) {
      msg.classList.add("error");
      msg.textContent = `⚠️ ${err.message}`;
    }
  });
}
const splitTags = (tags) => (tags || "").split(",").map((t) => t.trim()).filter(Boolean);
wireForm($("#fact-form"), (d) => postJson("/api/knowledge", { kind: "fact", title: d.title, text: d.text, tags: splitTags(d.tags) }));
wireForm($("#code-form"), (d) =>
  postJson("/api/knowledge", { kind: "code", title: d.title, text: d.text, code: d.code, framework: d.framework, tags: splitTags(d.tags) })
);
wireForm($("#word-form"), (d) => postJson("/api/words", { word: d.word, meaning: d.meaning }));

// ---------- knowledge browser ----------

let filter = "all";
let searchTimer;
$("#kb-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadKnowledge, 250);
});
document.querySelectorAll(".filter").forEach((b) =>
  b.addEventListener("click", () => {
    filter = b.dataset.filter;
    document.querySelectorAll(".filter").forEach((x) => x.classList.toggle("active", x === b));
    loadKnowledge();
  })
);

function badge(text, cls = "") {
  return `<span class="badge ${cls}">${escapeHtml(text)}</span>`;
}

function entryCard(e, hidden) {
  const item = document.createElement("article");
  item.className = "kb-item";
  const kindLabel = { fact: "fact", qa: "correction", code: "code" }[e.kind] || e.kind;
  const origin = e.source === "user" ? badge("learned", "user") : badge(e.pack || "starter");
  item.innerHTML = `
    <div class="kb-head">
      <div>
        <h3></h3>
        <div class="badges">${badge(kindLabel)}${origin}${e.framework ? badge(e.framework) : ""}
          <span class="badge" title="Goes up with 👍 and down with 👎">trust ${e.weight.toFixed(1)}</span></div>
      </div>
      <button type="button" class="secondary small action">${hidden ? "Restore" : e.source === "user" ? "Delete" : "Hide"}</button>
    </div>
    <p></p>`;
  $("h3", item).textContent = e.title;
  $("p", item).textContent = e.text;
  if (e.code) {
    const details = document.createElement("details");
    details.innerHTML = `<summary>Show code</summary>${renderMarkdown("```\n" + e.code + "\n```")}`;
    item.appendChild(details);
  }
  $(".action", item).onclick = async () => {
    if (hidden) {
      await postJson(`/api/knowledge/${encodeURIComponent(e.id)}/restore`, {}).catch(() => {});
    } else {
      const verb = e.source === "user" ? "Delete" : "Hide";
      if (!confirm(`${verb} "${e.title}"?`)) return;
      await fetch(`/api/knowledge/${encodeURIComponent(e.id)}`, { method: "DELETE" });
    }
    loadKnowledge();
    refreshStatus();
  };
  return item;
}

function wordsList(words) {
  const frag = document.createDocumentFragment();
  const names = Object.keys(words).sort();
  if (!names.length) {
    frag.appendChild(Object.assign(document.createElement("p"), {
      className: "muted",
      textContent: 'No words yet. Teach one in the Teach tab, or say "tp means teleport" in the chat.',
    }));
  }
  for (const w of names) {
    const row = document.createElement("article");
    row.className = "kb-item";
    row.innerHTML = `<div class="kb-head"><div><h3></h3><p></p></div>
      <button type="button" class="secondary small action">Forget</button></div>`;
    $("h3", row).textContent = w;
    $("p", row).textContent = `means: ${words[w].join(", ")}`;
    $(".action", row).onclick = async () => {
      await fetch(`/api/words/${encodeURIComponent(w)}`, { method: "DELETE" });
      loadKnowledge();
      refreshStatus();
    };
    frag.appendChild(row);
  }
  return frag;
}

async function loadKnowledge() {
  const q = $("#kb-search").value.trim();
  const params = new URLSearchParams();
  if (filter === "hidden") params.set("hidden", "1");
  else if (q && filter !== "words") params.set("q", q);
  const list = $("#kb-list");
  let data;
  try {
    data = await (await fetch(`/api/knowledge?${params}`)).json();
  } catch {
    list.innerHTML = `<p class="muted">⚠️ Can't reach Aria. Is she running?</p>`;
    return;
  }
  const packs = data.packs.map((p) => `${p.name} (${p.entries})`).join(", ");
  $("#kb-stats").textContent = `${plural(data.total, "entry").replace("entrys", "entries")} · ${data.learned} learned from you · ` +
    `${plural(data.code_examples, "code example")} · ${plural(data.words, "word")} · ${data.hidden} hidden` +
    (packs ? ` · packs: ${packs}` : "") +
    (packs && !q && filter === "all" ? " (search to see pack entries)" : "");
  list.innerHTML = "";
  if (filter === "words") {
    list.appendChild(wordsList(data.word_list));
    return;
  }
  let entries = data.entries;
  if (filter === "learned") entries = entries.filter((e) => e.source === "user");
  if (!entries.length) list.innerHTML = `<p class="muted">Nothing here yet.</p>`;
  for (const e of entries) list.appendChild(entryCard(e, filter === "hidden"));
}

restoreChat();
refreshStatus();
input.focus();

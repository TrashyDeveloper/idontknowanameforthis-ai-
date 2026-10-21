// Aria web page: chat, teaching forms and the knowledge browser.

const $ = (sel, root = document) => root.querySelector(sel);
const chatHistory = []; // {role, content} pairs sent with each question for context

// ---------- tiny, safe Markdown renderer ----------

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function inline(s) {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_]+)_(?=[\s).,!?]|$)/g, "$1<em>$2</em>")
    .replace(/(^|[\s(])\*([^*]+)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>");
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
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      flush();
      const code = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      out.push(`<div class="codeblock"><button class="copy">Copy</button><pre><code>${escapeHtml(code.join("\n"))}</code></pre></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)/);
    if (heading) {
      flush();
      out.push(`<h${heading[1].length < 3 ? 3 : 4}>${inline(heading[2])}</h${heading[1].length < 3 ? 3 : 4}>`);
      i++;
      continue;
    }
    const listMatch = line.match(/^\s*([-*]|\d+[.)])\s+/);
    if (listMatch) {
      flush();
      const ordered = /\d/.test(listMatch[1]);
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
  navigator.clipboard.writeText(code).then(() => {
    e.target.textContent = "Copied!";
    setTimeout(() => (e.target.textContent = "Copy"), 1500);
  });
});

// ---------- tabs and status ----------

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab.dataset.tab}`));
    if (tab.dataset.tab === "knowledge") loadKnowledge();
  })
);

async function refreshStatus() {
  const s = await (await fetch("/api/status")).json();
  const el = $("#status");
  el.textContent = `${s.mode === "claude" ? "● Claude mode" : "○ Offline mode"} · ${s.learned} learned`;
  el.className = `status ${s.mode}`;
  el.title = s.mode === "claude"
    ? "Using Claude to write answers from its knowledge."
    : "No API key found: answers come straight from the knowledge base. See the README to enable Claude mode.";
}

// ---------- chat ----------

const messagesEl = $("#messages");
const input = $("#chat-input");

function addMessage(role, html) {
  const msg = document.createElement("div");
  msg.className = `msg ${role}`;
  msg.innerHTML = `<div class="bubble"></div>`;
  if (role === "user") $(".bubble", msg).textContent = html;
  else $(".bubble", msg).innerHTML = html;
  messagesEl.appendChild(msg);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return msg;
}

function addFeedback(msg, question, sources) {
  const meta = document.createElement("div");
  meta.className = "meta";
  for (const s of sources) {
    const chip = document.createElement("button");
    chip.className = `source ${s.source}`;
    chip.onclick = () => ask(s.title);
    chip.textContent = (s.kind === "code" ? "{ } " : "") + s.title;
    chip.title = (s.source === "user" ? "Something you taught me" : s.pack ? `From the ${s.pack} pack` : "Starter knowledge") +
      " · click to ask about it";
    meta.appendChild(chip);
  }
  const up = Object.assign(document.createElement("button"), { className: "vote", textContent: "👍", title: "Helpful" });
  const down = Object.assign(document.createElement("button"), { className: "vote", textContent: "👎", title: "Wrong, let me correct it" });
  meta.append(up, down);
  msg.appendChild(meta);

  const ids = sources.map((s) => s.id);
  up.onclick = async () => {
    up.disabled = down.disabled = true;
    await sendFeedback({ sources: ids, helpful: true });
    up.textContent = "👍 Thanks!";
    up.classList.add("chosen");
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
    $("button.secondary", box).onclick = async () => {
      await sendFeedback({ sources: ids, helpful: false });
      box.replaceWith(Object.assign(document.createElement("p"), { className: "hint", textContent: "Noted, I'll trust those sources less." }));
    };
    box.onsubmit = async (e) => {
      e.preventDefault();
      const correction = $("textarea", box).value.trim();
      if (!correction) return;
      await sendFeedback({ sources: ids, helpful: false, question, correction });
      box.replaceWith(Object.assign(document.createElement("p"), { className: "hint", textContent: "✅ Learned! Ask me again any time." }));
      refreshStatus();
    };
  };
}

async function sendFeedback(body) {
  await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

async function ask(question) {
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
    if (!res.ok) throw new Error((await res.json()).error || res.statusText);
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
          answer += event.text;
          bubble.innerHTML = renderMarkdown(answer);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
      }
    }
  } catch (err) {
    answer = `⚠️ Something went wrong: ${err.message}`;
    bubble.innerHTML = renderMarkdown(answer);
  }
  bubble.classList.remove("typing");
  $("#send").disabled = false;

  chatHistory.push({ role: "user", content: question }, { role: "assistant", content: answer });
  if (meta.learned) refreshStatus();
  else addFeedback(botMsg, question, meta.sources || []);
}

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (!q || $("#send").disabled) return;
  input.value = "";
  input.style.height = "";
  ask(q);
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "";
  input.style.height = `${input.scrollHeight}px`;
});
document.querySelectorAll(".suggest").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));

// ---------- teach forms ----------

function wireForm(form, kind) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const msg = $(".form-msg", form);
    const body = {
      kind,
      title: data.title,
      text: data.text || "",
      code: data.code || "",
      framework: data.framework || "",
      tags: (data.tags || "").split(",").map((t) => t.trim()).filter(Boolean),
    };
    const res = await fetch("/api/knowledge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const out = await res.json();
    msg.classList.toggle("error", !res.ok);
    msg.textContent = res.ok ? "✅ Saved! The bot will use this from now on." : `⚠️ ${out.error}`;
    if (res.ok) {
      form.reset();
      refreshStatus();
    }
  });
}
wireForm($("#fact-form"), "fact");
wireForm($("#code-form"), "code");

// ---------- knowledge browser ----------

let searchTimer;
$("#kb-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadKnowledge, 250);
});

async function loadKnowledge() {
  const q = $("#kb-search").value.trim();
  const data = await (await fetch(`/api/knowledge?q=${encodeURIComponent(q)}`)).json();
  const packs = data.packs.map((p) => `${p.name} (${p.entries})`).join(", ");
  $("#kb-stats").textContent = `${data.total} entries · ${data.learned} learned from you · ${data.code_examples} code examples` +
    (packs ? ` · packs: ${packs}${q ? "" : ", search to see pack entries"}` : "");
  const list = $("#kb-list");
  list.innerHTML = "";
  if (!data.entries.length) list.innerHTML = `<p class="muted">Nothing found.</p>`;
  for (const e of data.entries) {
    const item = document.createElement("article");
    item.className = "kb-item";
    const kindLabel = { fact: "fact", qa: "correction", code: "code" }[e.kind];
    item.innerHTML = `
      <header>
        <div>
          <h3></h3>
          <div class="badges">
            <span class="badge">${kindLabel}</span>
            <span class="badge ${e.source}">${e.source === "user" ? "learned" : e.pack ? escapeHtml(e.pack) : "starter"}</span>
            ${e.framework ? `<span class="badge">${escapeHtml(e.framework)}</span>` : ""}
            <span class="badge" title="Goes up with 👍 and down with 👎">trust ${e.weight.toFixed(1)}</span>
          </div>
        </div>
        <button class="secondary remove">${e.source === "user" ? "Delete" : "Hide"}</button>
      </header>
      <p></p>`;
    $("h3", item).textContent = e.title;
    $("p", item).textContent = e.text;
    if (e.code) {
      const details = document.createElement("details");
      details.innerHTML = `<summary>Show code</summary>${renderMarkdown("```\n" + e.code + "\n```")}`;
      item.appendChild(details);
    }
    $(".remove", item).onclick = async () => {
      const verb = e.source === "user" ? "Delete" : "Hide";
      if (!confirm(`${verb} "${e.title}"?`)) return;
      await fetch(`/api/knowledge/${encodeURIComponent(e.id)}`, { method: "DELETE" });
      loadKnowledge();
      refreshStatus();
    };
    list.appendChild(item);
  }
}

refreshStatus();

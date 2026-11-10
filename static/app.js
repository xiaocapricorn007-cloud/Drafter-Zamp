"use strict";

const view = document.getElementById("view");
const SVG = "http://www.w3.org/2000/svg";

const STATUS = {
  finding: "Looking",
  waiting: "Needs you",
  working: "Working",
  hold: "Not enough to go on",
  ready: "Ready to read",
  flagged: "Please check",
  approved: "Done",
  sending: "Sending",
  scheduled: "Scheduled",
  sent: "Sent",
  discarded: "Set aside",
  stopped: "Stopped",
  failed: "Didn't work",
};
const STILL_GOING = new Set(["finding", "working", "sending"]);

// ---- small helpers -----------------------------------------------------

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) node.append(kid);
  return node;
}

function svg(markup, attrs) {
  const holder = document.createElementNS(SVG, "svg");
  for (const [k, v] of Object.entries(attrs)) holder.setAttribute(k, v);
  holder.innerHTML = markup; // only ever our own fixed drawings
  return holder;
}

const logo = (draw) => {
  const c = draw ? ' class="draw" pathLength="1"' : "";
  const d = (n) => (draw ? ` style="--d:${n}"` : "");
  return svg(
    `<path${c}${d(0)} d="M32 4v7"/><circle${c}${d(1)} cx="32" cy="14" r="4"/>` +
      `<path${c}${d(2)} d="M30 17 16 54l-1.5 5"/><path${c}${d(3)} d="m34 17 12 33 3 8"/>` +
      `<path${c}${d(4)} d="M21.5 42a22 22 0 0 0 21 0"/>`,
    { viewBox: "0 0 64 64", class: "logo", "aria-hidden": "true" }
  );
};

const compass = () =>
  svg(
    `<circle class="trace" cx="60" cy="60" r="40"/>` +
      `<g class="arm"><path d="M60 60 80 18l20 42"/><path d="M80 18V7"/><path d="M68.5 43a26 26 0 0 0 23 0" stroke-width="2.4"/>` +
      `<circle cx="80" cy="18" r="5" fill="var(--beige)"/><path d="M97 53.7 100 60" stroke-width="5.5"/>` +
      `<circle class="pin" cx="60" cy="60" r="2.2"/></g>`,
    { viewBox: "0 0 120 120", class: "spinner", role: "img", "aria-label": "Working" }
  );

const safeUrl = (url) => (/^https?:\/\//i.test(url || "") ? url : null);
function sourceLink(title, url) {
  const href = safeUrl(url);
  return href ? el("a", { href, target: "_blank", rel: "noopener noreferrer" }, title || "See the page") : null;
}

function problemText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg.replace("Value error, ", "");
  return "Something went wrong. Please try again.";
}

async function api(path, body, method = body === undefined ? "GET" : "POST") {
  const res = await fetch("/api" + path, {
    method,
    ...(body === undefined ? {} : { headers: { "content-type": "application/json" }, body: JSON.stringify(body) }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !["/me", "/login", "/signup"].includes(path)) location.reload(); // logged out elsewhere
  if (!res.ok) {
    const err = new Error(problemText(data.detail));
    err.detail = data.detail;
    err.status = res.status;
    throw err;
  }
  return data;
}

function show(...nodes) {
  view.replaceChildren(...nodes);
  window.scrollTo({ top: 0 });
}

function toast(text) {
  const t = el("div", { class: "toast", role: "status" }, text);
  document.body.append(t);
  requestAnimationFrame(() => t.classList.add("on"));
  setTimeout(() => { t.classList.remove("on"); setTimeout(() => t.remove(), 400); }, 1900);
}

async function changeView(render) {
  const current = view.firstElementChild;
  if (current) {
    current.classList.add("leaving");
    await new Promise((done) => setTimeout(done, 200));
  }
  render();
}

const when = (ts) =>
  new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });

// ---- first screen ------------------------------------------------------

function field(name, label, type = "text", autocomplete = "off") {
  const input = el("input", { id: name, name, type, placeholder: " ", autocomplete });
  const error = el("div", { class: "err", id: name + "-err" }, el("span"));
  input.addEventListener("input", () => error.classList.remove("on"));
  return el("div", {}, el("div", { class: "field" }, input, el("label", { for: name }, label)), error);
}

function setError(name, text) {
  const box = document.getElementById(name + "-err");
  if (!box) return;
  box.firstChild.textContent = text;
  box.classList.add("on");
}

function homeView() {
  const extras = el("div", { class: "more" }, el("div", {},
    field("linkedin", "Their LinkedIn link"),
    field("email", "Their email", "email"),
  ));
  const moreBtn = el("button", {
    type: "button", class: "more-btn enter", style: "--i:7", "aria-expanded": "false",
    onclick: () => {
      const open = extras.classList.toggle("open");
      moreBtn.setAttribute("aria-expanded", String(open));
      moreBtn.textContent = open ? "Hide these" : "I also have their LinkedIn or email";
    },
  }, "I also have their LinkedIn or email");

  const problem = el("p", { class: "form-problem", role: "alert" });
  const label = el("span", {}, "Find them");
  const button = el("button", { class: "btn main wide enter", style: "--i:8", type: "submit" }, label);

  const form = el("form", {
    novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      problem.textContent = "";
      const values = Object.fromEntries(new FormData(form));
      const need = { name: "Please add their name.", title: "Please add their job.", company: "Please add their company." };
      let ok = true;
      for (const [key, text] of Object.entries(need)) {
        if (!values[key].trim()) { setError(key, text); ok = false; }
      }
      if (!ok) return;
      button.disabled = true;
      label.textContent = "One moment";
      try {
        const { id } = await api("/runs", values);
        location.hash = "#/run/" + id;
        loadSidebar();
      } catch (err) {
        button.disabled = false;
        label.textContent = "Find them";
        if (Array.isArray(err.detail)) {
          let shown = false;
          for (const item of err.detail) {
            const key = item.loc?.[1];
            if (key) {
              setError(key, (item.msg || "").replace("Value error, ", ""));
              if (["linkedin", "email"].includes(key)) extras.classList.add("open");
              shown = true;
            }
          }
          if (!shown) problem.textContent = "Please check the details above.";
        } else {
          problem.textContent = err.message;
        }
      }
    },
  },
    el("div", { class: "enter", style: "--i:3" }, field("name", "Their name")),
    el("div", { class: "enter", style: "--i:4" }, field("title", "Their job")),
    el("div", { class: "enter", style: "--i:5" }, field("company", "Their company")),
    extras, moreBtn, button, problem,
  );

  show(el("div", { class: "home-wrap" }, el("div", { class: "home-box" },
    logo(true),
    el("h1", { class: "enter", style: "--i:1" }, "Who do you want to write to?"),
    form,
  )));
}

// ---- one piece of work -------------------------------------------------

let live = null;

function stopLive() {
  if (live?.stream) live.stream.close();
  clearTimeout(live?.timer);
  live = null;
}

async function runView(id) {
  stopLive();
  const state = { id, shown: null, stream: null, timer: null, notes: new Map(), stepNodes: new Map(), run: null };
  live = state;
  await refresh(state);
  if (live !== state) return;
  state.stream = new EventSource(`/api/runs/${id}/events`);
  state.stream.onmessage = () => {
    clearTimeout(state.timer);
    state.timer = setTimeout(() => refresh(state), 120);
  };
}

async function refresh(state) {
  let run;
  try {
    run = await api("/runs/" + state.id);
  } catch (err) {
    if (live === state && err.status === 423) {
      stopLive();
      show(lockedScreen(state.id));
    } else if (live === state && !state.run) {
      show(el("div", { class: "page" }, el("p", { class: "empty" }, err.message)));
      stopLive();
    }
    return;
  }
  if (live !== state) return;
  state.run = run;
  if (run.status !== state.shown) {
    const first = state.shown === null;
    state.shown = run.status;
    state.setArt = null;
    loadSidebar();
    state.stepNodes = new Map();
    const render = () => { if (live === state) drawRun(state, run); };
    if (first) render(); else await changeView(render);
  }
  patchSteps(state, state.run);
  if (state.setArt && run.data.draft && run.data.draft.art !== state.art) state.setArt(run.data.draft.art);
  if (state.artWait) state.artWait.hidden = !drawingCard(run);
}

function drawingCard(run) {
  const step = run.timeline.find((s) => s.key === "art");
  return step?.state === "running" && Date.now() / 1000 - run.updated_at < 300;
}

function drawRun(state, run) {
  const p = run.prospect;
  const heading = el("p", { class: "who enter" }, `${run.data.sample ? "Test draft · " : ""}${run.title ? run.title + " · " : ""}${p.name} · ${p.company}`);
  const screens = {
    finding: working, working,
    waiting: confirmScreen,
    hold: endScreen, stopped: endScreen, failed: endScreen, discarded: endScreen,
    ready: reviewScreen, flagged: reviewScreen,
    approved: approvedScreen,
    sending: sendingScreen, scheduled: scheduledScreen, sent: sentScreen,
  };
  show(screens[run.status](state, run, heading));
}

function stepsList(state) {
  const list = el("ol", { class: "steps" });
  state.stepList = list;
  return list;
}

function patchSteps(state, run) {
  const list = state.stepList;
  if (!list || !list.isConnected) return;
  run.timeline.forEach((step) => {
    let node = state.stepNodes.get(step.key);
    if (!node) {
      const notes = el("ul", { class: "step-notes" });
      const row = el("li", { class: "step" }, el("span", { class: "mark" }), el("div", {}, el("div", { class: "step-label" }, step.label), notes));
      node = { row, notes, count: 0 };
      state.stepNodes.set(step.key, node);
      list.append(row);
    }
    node.row.className = "step " + step.state;
    step.notes.slice(node.count).forEach((text) => node.notes.append(el("li", {}, text)));
    node.count = step.notes.length;
  });
  const now = state.nowNode;
  if (now?.isConnected) {
    const active = run.timeline.find((s) => s.state === "running");
    const text = active ? active.label : "Just a moment";
    if (now.textContent !== text) {
      now.replaceChildren(el("span", { class: "swap", style: "display:inline-block" }, text));
    }
  }
}

function lockedScreen(id) {
  const box = field("lock-code", "Your code", "password");
  const input = box.querySelector("input");
  const problem = el("p", { class: "form-problem", role: "alert" });
  const button = el("button", { class: "btn main wide enter", style: "--i:3", type: "submit" }, "Unlock");
  const form = el("form", {
    novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      button.disabled = true;
      problem.textContent = "";
      try {
        await api(`/runs/${id}/unlock`, { code: input.value });
        toast("Unlocked");
        await refreshMe();
        await loadSidebar();
        route();
      } catch (err) {
        problem.textContent = err.message;
        button.disabled = false;
        input.select();
      }
    },
  }, el("div", { class: "enter", style: "--i:2" }, box), button, problem);
  requestAnimationFrame(() => input.focus());
  return el("div", { class: "home-wrap" }, el("div", { class: "home-box" },
    el("div", { class: "lock-mark enter" }, icon("lock")),
    el("h1", { class: "enter", style: "--i:1" }, "This draft is private"),
    form,
  ));
}

function working(state, run, heading) {
  state.nowNode = el("h1", { class: "now" }, "Just a moment");
  return el("div", { class: "page" }, heading,
    el("div", { class: "working-top enter", style: "--i:1" }, compass(), state.nowNode),
    el("div", { class: "enter", style: "--i:2" }, stepsList(state)),
  );
}

function confirmScreen(state, run, heading) {
  const people = run.data.people;
  const answer = async (person) => {
    document.querySelectorAll(".btn").forEach((b) => (b.disabled = true));
    try {
      await api(`/runs/${run.id}/confirm`, { person });
    } catch (err) {
      toast(err.message);
      document.querySelectorAll(".btn").forEach((b) => (b.disabled = false));
    }
  };
  return el("div", { class: "page" }, heading,
    el("h1", { class: "title enter", style: "--i:1;margin-top:20px" }, people.length > 1 ? "Which one is the right person?" : "Is this the right person?"),
    el("p", { class: "sub enter", style: "--i:2" }, "We won't start until you say so."),
    people.map((person, i) => el("article", { class: "card person enter", style: `--i:${i + 3}` },
      person.matches_link && el("span", { class: "tag" }, "Matches the link you gave"),
      person.different_company && el("span", { class: "tag" }, `You wrote ${person.different_company}, but they seem to be at ${person.company} now`),
      el("h2", {}, person.name),
      el("p", { class: "role" }, [person.title, person.company].filter(Boolean).join(" at ")),
      el("p", {}, person.about),
      el("div", { class: "sources" }, person.sources.map((s) => sourceLink(s.title, s.url))),
      el("button", { class: "btn main", onclick: () => answer(i) }, "Yes, this is them"),
    )),
    el("div", { class: "center enter", style: `--i:${people.length + 3}` },
      el("button", { class: "btn quiet", onclick: () => answer(null) }, "None of these. Write from what I gave you")),
  );
}

function endScreen(state, run, heading) {
  const titles = {
    hold: "There isn't enough to write something real",
    stopped: "We stopped here",
    failed: "That didn't work",
    discarded: "You set this one aside",
  };
  const details = {
    hold: "A message with nothing true behind it isn't worth sending, so we've held off.",
  };
  const searched = run.data.searched || [];
  const pages = run.data.pages || [];
  const { draft, email } = run.data;
  return el("div", { class: "page" }, heading,
    el("div", { class: "working-top enter", style: "--i:1;margin-bottom:10px" }, logo(true)),
    el("h1", { class: "title enter", style: "--i:2" }, titles[run.status]),
    el("p", { class: "sub enter", style: "--i:3" }, run.data.problem || details[run.status] || ""),
    run.status === "hold" && el("p", { class: "sub enter", style: "--i:3" }, details.hold),
    draft && composeWindow({
      to: email ? email.address : "Add their email yourself",
      subject: el("span", { class: "compose-subject" }, draft.subject),
      body: el("p", { class: "plain" }, draft.body || draft.lines.map((l) => l.text).join("\n\n")),
      card: cardCanvas(run, draft.brief.sticky_line, draft.brief.card_line),
    }),
    (searched.length > 0 || pages.length > 0) && el("div", { class: "card enter", style: "--i:4" },
      el("details", {}, el("summary", {}, `See where we looked (${searched.length} searches, ${pages.length} pages)`),
        el("ul", { class: "found" }, searched.map((q) => el("li", { class: "small" }, q)),
          pages.map((p) => el("li", {}, sourceLink(p.title, p.url)))))),
    el("div", { class: "center enter", style: "--i:5" }, el("a", { class: "btn main", href: "#/" }, "Write to someone else")),
  );
}

function grow(area) {
  area.style.height = "auto";
  area.style.height = area.scrollHeight + "px";
}

function currentHook(run) {
  const hooks = run.data.hooks || [];
  return hooks[Math.min(run.data.hook_pos || 0, hooks.length - 1)];
}

function testRow(passed, title, detail, i) {
  const mark = passed
    ? svg(`<path class="pathdraw" pathLength="1" style="--i:${i}" d="m4 12 5 5L20 6"/>`, { viewBox: "0 0 24 24" })
    : svg(`<path class="pathdraw" pathLength="1" style="--i:${i}" d="M6 6l12 12M18 6 6 18"/>`, { viewBox: "0 0 24 24" });
  return el("li", {}, mark, el("div", {}, el("div", {}, title), detail && el("div", { class: "small" }, detail)));
}

function wrapText(ctx, text, width) {
  const lines = [];
  let line = "";
  for (const word of text.split(/\s+/)) {
    const next = line ? `${line} ${word}` : word;
    if (ctx.measureText(next).width > width && line) {
      lines.push(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  return lines;
}

async function loadArt(markup) {
  const picture = new Image();
  picture.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(markup);
  try {
    await picture.decode();
    return picture;
  } catch {
    return null;
  }
}

async function drawCard(canvas, { headline, line, first, company, seller, art }) {
  await Promise.allSettled([document.fonts.load("400 80px Newsreader"), document.fonts.load("400 28px Inter")]);
  const picture = art ? await loadArt(art) : null;
  const W = 1200, H = 628, left = 88, textWidth = picture ? 580 : 930;
  canvas.width = W;
  canvas.height = H;
  const c = canvas.getContext("2d");
  c.fillStyle = "#f2e9da";
  c.fillRect(0, 0, W, H);

  // circles drawn by a compass, off the corner
  c.strokeStyle = "rgba(110, 20, 35, .2)";
  c.lineWidth = 2;
  if (!picture) {
    for (const r of [120, 220, 320, 420]) {
      c.beginPath();
      c.arc(W - 30, 40, r, 0, Math.PI * 2);
      c.stroke();
    }
  }
  c.fillStyle = "#6e1423";
  c.fillRect(0, 0, 16, H);
  if (picture) {
    c.fillRect(W - 480, 0, 480, H);
    c.drawImage(picture, W - 480, 0, 480, H);
  }

  c.textBaseline = "alphabetic";
  c.font = '500 25px Inter, sans-serif';
  c.fillText(`For ${first} at ${company}`, left, 92);

  c.font = '400 29px Inter, sans-serif';
  const small = wrapText(c, line, textWidth).slice(0, picture ? 3 : 2);
  // Shrink the headline until everything fits above the company name.
  let size = 86, rows;
  do {
    c.font = `400 ${size}px Newsreader, Georgia, serif`;
    rows = wrapText(c, headline, textWidth);
    const lastLine = 150 + size + rows.length * size * 1.1 + 14 + (small.length - 1) * 40;
    if (rows.length <= 3 && lastLine <= H - 94) break;
    size -= 4;
  } while (size > 40);
  const leading = size * 1.1;
  let y = 150 + size;
  c.fillStyle = "#4a0e19";
  for (const row of rows) {
    c.fillText(row, left, y);
    y += leading;
  }

  c.font = '400 29px Inter, sans-serif';
  c.fillStyle = "rgba(74, 14, 25, .78)";
  let below = y + 14;
  for (const row of small) {
    c.fillText(row, left, below);
    below += 40;
  }

  c.font = "500 34px Newsreader, Georgia, serif";
  c.fillStyle = "#6e1423";
  c.fillText(seller, left, H - 52);
}

function confidenceBlock(sure) {
  const fill = el("div", { class: "sure-fill" });
  requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = sure.score + "%"; }));
  return el("section", { class: "sure", style: "--i:0" },
    el("h3", {}, "How sure we are"),
    el("div", { class: "sure-top" }, el("span", { class: "sure-num" }, sure.score + "%"), el("span", { class: "pill" }, sure.label)),
    el("div", { class: "sure-bar" }, fill),
    sure.label === "Low" && el("p", { class: "sure-warn" }, "We wouldn't send this as it is. Here's why:"),
    sure.bad.length > 0 && el("ul", { class: "reasons" }, sure.bad.map((text) => el("li", {}, "– ", text))),
    sure.good.length > 0 && el("ul", { class: "reasons" }, sure.good.map((text) => el("li", {}, "+ ", text))),
    el("p", { class: "small", style: "margin-top:8px" }, "This shows how real and well-fitted the email is. It can't promise a reply."),
  );
}

function composeWindow({ to, subject, body, card, below, actions }) {
  return el("div", { class: "compose enter", style: "--i:2" },
    el("div", { class: "compose-bar" }, "New message"),
    el("div", { class: "compose-row" }, el("span", { class: "compose-key" }, "To"), el("span", { class: "compose-to" }, to)),
    el("div", { class: "compose-row" }, el("span", { class: "compose-key" }, "Subject"), subject),
    el("div", { class: "compose-body" }, body, card, below),
    actions && el("div", { class: "compose-foot" }, actions),
  );
}

const firstName = (run) => run.prospect.name.split(/\s+/)[0];

function cardCanvas(run, headline, line) {
  const canvas = el("canvas", { class: "card-image", role: "img", "aria-label": "The card sent with this email" });
  drawCard(canvas, { headline: headline || " ", line: line || "", first: firstName(run), company: run.prospect.company, seller: run.data.seller_name || "", art: run.data.draft?.art });
  return canvas;
}

// Words typed but not finished yet, so clicking another draft doesn't lose them.
const unfinished = new Map();

function reviewScreen(state, run, heading) {
  const { draft, tests, problems, rules_ok: rulesOk, facts = [], set_aside: aside = [], email, used_saved: saved, hooks = [], hook_pos: pos = 0, confidence: sure, seller_name: seller = "" } = run.data;
  const hook = currentHook(run);
  const factMap = Object.fromEntries(facts.map((f) => [f.id, f]));
  const usedIds = [...new Set(draft.lines.flatMap((l) => l.facts))];

  const kept = unfinished.get(run.id);
  const subject = el("input", { class: "compose-input", value: kept ? kept.subject : draft.subject, "aria-label": "Subject" });
  const body = el("textarea", { class: "compose-text", "aria-label": "Email" });
  body.value = kept ? kept.body : draft.body || draft.lines.map((l) => l.text).join("\n\n");
  const toBox = el("input", { class: "compose-input", type: "email", value: kept ? kept.to : run.data.to_override || (email ? email.address : ""), placeholder: "Add their email", "aria-label": "To" });
  const remember = () => unfinished.set(run.id, { to: toBox.value, subject: subject.value, body: body.value, words: words.value });
  toBox.addEventListener("input", remember);
  subject.addEventListener("input", remember);
  body.addEventListener("input", () => { grow(body); remember(); });
  requestAnimationFrame(() => grow(body));

  const canvas = el("canvas", { class: "card-image", role: "img", "aria-label": "The card sent with this email" });
  const words = el("input", { class: "card-words", value: kept ? kept.words : draft.brief.sticky_line || "", "aria-label": "Words on the card" });
  const paint = () => { painted = drawCard(canvas, { headline: words.value || " ", line: draft.brief.card_line || "", first: firstName(run), company: run.prospect.company, seller, art: draft.art }); };
  let painted;
  words.addEventListener("input", () => { paint(); remember(); });
  paint();
  state.art = draft.art;
  state.setArt = (picture) => { state.art = picture; draft.art = picture; paint(); };
  state.artWait = el("p", { class: "small card-wait", hidden: !drawingCard(run) }, "Drawing the picture. It will appear on the card in about a minute.");
  const sends = sendActions(run, async () => {
    await painted;
    return { to: toBox.value, subject: subject.value, body: body.value, card_headline: words.value, card_png: pngOf(canvas) };
  });

  const act = async (payload) => {
    document.querySelectorAll(".compose-foot .btn").forEach((b) => (b.disabled = true));
    try {
      await api(`/runs/${run.id}/review`, payload);
      unfinished.delete(run.id);
    } catch (err) {
      toast(err.message);
      document.querySelectorAll(".compose-foot .btn").forEach((b) => (b.disabled = false));
    }
  };

  const finish = () => act({ action: "approve", subject: subject.value, body: body.value, card_headline: words.value });
  const steps = stepsList(state);
  let i = 1;
  const sections = [
    sure && confidenceBlock(sure),
    saved && el("div", { class: "notice" }, saved),
    run.data.send_problem && el("div", { class: "notice" }, el("strong", {}, "This didn't go out"), el("div", { class: "small" }, run.data.send_problem)),
    run.status === "flagged" && el("div", { class: "notice" }, el("strong", {}, "This one needs a closer look"),
      el("div", { class: "small" }, "We tried a few times but couldn't get it fully right:"),
      el("ul", {}, problems.map((text) => el("li", {}, text)))),
    el("section", { style: `--i:${i++}` }, el("h3", {}, "The big idea"), el("p", { class: "big-idea" }, draft.brief.one_idea)),
    hook && el("section", { style: `--i:${i++}` }, el("h3", {}, "Why it could matter to them"),
      el("p", {}, hook.pressure), el("p", { class: "small" }, hook.why_they_care),
      el("p", { class: "small", style: "margin-top:8px" }, "This is our best guess from public clues.")),
    usedIds.length > 0 && el("section", { style: `--i:${i++}` }, el("h3", {}, "What we found"),
      el("ul", { class: "found" }, usedIds.filter((id) => factMap[id]).map((id) => el("li", {}, factMap[id].what, sourceLink(factMap[id].source, factMap[id].url))))),
    el("section", { style: `--i:${i++}` }, el("h3", {}, "Our checks"),
      el("ul", { class: "tests" },
        testRow(rulesOk, "Facts are real and it reads well", rulesOk ? "Every fact matches where we found it." : null, 0),
        tests && testRow(tests.only_them, "Written just for them", tests.only_them ? "A reader could pick them out with names hidden." : "It could fit other people too.", 1),
        tests && testRow(tests.same_idea, "Easy to remember", `A reader remembered: “${tests.remembered}”`, 2),
        tests && testRow(tests.would_open, "Worth opening", null, 3),
        tests && testRow(tests.kind, "Kind and fair", null, 4))),
    el("section", { style: `--i:${i++}` }, el("details", {}, el("summary", {}, "Why each sentence is there"),
      el("ul", { class: "found" }, draft.lines.map((l) => el("li", {}, l.text, el("div", { class: "small" }, l.why)))))),
    aside.length > 0 && el("section", { style: `--i:${i++}` }, el("details", {}, el("summary", {}, `Ideas we left out (${aside.length})`),
      el("ul", { class: "found" }, aside.map((a) => el("li", {}, a.summary, el("div", { class: "small" }, a.reason)))))),
    el("section", { style: `--i:${i++}` }, el("details", {}, el("summary", {}, "See how we got here"), steps)),
  ];

  return el("div", { class: "page wide" }, heading,
    el("h1", { class: "title enter", style: "--i:1;margin:18px 0 30px" }, run.status === "flagged" ? "Have a look at this one" : "Here's your email"),
    el("div", { class: "review" },
      composeWindow({
        to: toBox,
        subject, body, card: canvas,
        below: [state.artWait, el("label", { class: "card-edit" }, "Words on the card", words)],
        actions: [
          me.gmail && el("button", { class: "btn main", onclick: sends.now }, "Send"),
          me.gmail && el("button", { class: "btn", onclick: sends.later }, "Schedule"),
          !me.gmail && el("button", { class: "btn main", onclick: finish }, "Looks good"),
          pos + 1 < hooks.length && el("button", { class: "btn", onclick: () => act({ action: "next" }) }, "Try another opening"),
          me.gmail && el("button", { class: "btn quiet", onclick: finish }, "Finish without sending"),
          el("button", { class: "btn quiet", onclick: () => act({ action: "discard" }) }, "Set aside"),
        ],
      }),
      el("aside", { class: "side" }, sections),
    ),
  );
}

const pngOf = (canvas) => canvas.toDataURL("image/png").split(",")[1];

function pad(n) {
  return String(n).padStart(2, "0");
}

function tomorrowMorning() {
  const d = new Date(Date.now() + 86400000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T09:00`;
}

function sendActions(run, message) {
  const artNote = () => (drawingCard(live?.run || run) ? " The picture is still being drawn, so the card will go with words only." : "");
  const gmailIsReady = () => {
    if (me.gmail) return true;
    askDialog({
      title: "Connect your Gmail first",
      text: "We need your permission to send from your Gmail. Your email stays here while you do that.",
      ok: "Go to Settings",
      submit: async () => { location.hash = "#/settings"; },
    });
    return false;
  };
  const post = async (m, extra) => {
    await api(`/runs/${run.id}/send`, { ...m, ...extra });
    unfinished.delete(run.id);
  };
  const askForAddress = (m) => (m.to.trim() ? undefined : { type: "email", label: "Their email" });
  return {
    now: async () => {
      if (!gmailIsReady()) return;
      const m = await message();
      const to = m.to.trim();
      askDialog({
        title: "Send this email?",
        text: `${to ? `To ${to}, from` : "From"} ${me.gmail}. Once it's sent, it can't be taken back.${artNote()}`,
        input: askForAddress(m),
        ok: "Send",
        submit: (value) => post({ ...m, to: to || value || "" }),
      });
    },
    later: async () => {
      if (!gmailIsReady()) return;
      const m = await message();
      if (!m.to.trim()) { toast("Please add their email first."); return; }
      askDialog({
        title: "When should it go?",
        text: `Drafter has to be running at that time. If it's more than an hour late, we won't send it, and we'll tell you.${artNote()}`,
        input: { type: "datetime-local", label: "Day and time", value: tomorrowMorning() },
        ok: "Schedule",
        submit: async (value) => {
          const sendAt = new Date(value).getTime() / 1000;
          if (!sendAt) throw new Error("Please pick a day and time.");
          await post(m, { send_at: sendAt });
          toast("Scheduled");
        },
      });
    },
  };
}

function sendingScreen(state, run, heading) {
  return el("div", { class: "page" }, heading,
    el("div", { class: "working-top enter", style: "--i:1" }, compass(), el("h1", { class: "now" }, "Sending your email")));
}

function readOnlyEmail(run, { to, subject, body, headline }) {
  return composeWindow({
    to,
    subject: el("span", { class: "compose-subject" }, subject),
    body: el("p", { class: "plain" }, body),
    card: cardCanvas(run, headline, run.data.draft.brief.card_line),
  });
}

function scheduledScreen(state, run, heading) {
  const o = run.data.outbox;
  const cancel = async (event) => {
    event.currentTarget.disabled = true;
    try {
      await api(`/runs/${run.id}/cancel-send`, {});
    } catch (err) {
      toast(err.message);
    }
  };
  return el("div", { class: "page" }, heading,
    logo(true),
    el("h1", { class: "title enter", style: "--i:1" }, "It's scheduled"),
    el("p", { class: "sub enter", style: "--i:2" }, `It will go out on ${when(o.send_at)}. Please keep Drafter running until then.`),
    readOnlyEmail(run, { to: o.to, subject: o.subject, body: o.body, headline: o.card_headline }),
    el("div", { class: "center enter", style: "--i:3" }, el("button", { class: "btn", onclick: cancel }, "Cancel and edit")),
  );
}

function sentScreen(state, run, heading) {
  const { sent, final } = run.data;
  return el("div", { class: "page" }, heading,
    logo(true),
    el("h1", { class: "title enter", style: "--i:1" }, "Sent"),
    el("p", { class: "sub enter", style: "--i:2" }, `Sent to ${sent.to} from ${sent.from}, on ${when(sent.at)}.`),
    readOnlyEmail(run, { to: sent.to, subject: final.subject, body: final.body, headline: final.card_headline }),
    el("div", { class: "center enter", style: "--i:3" }, el("a", { class: "btn main", href: "#/" }, "Write to someone else")),
  );
}

function approvedScreen(state, run, heading) {
  const { final, email, confidence: sure, draft } = run.data;
  const text = final.body || (final.lines || []).join("\n\n");
  const canvas = cardCanvas(run, final.card_headline || draft.brief.sticky_line, final.card_line || draft.brief.card_line);
  const to = run.data.to_override || (email ? email.address : "");
  const sends = sendActions(run, async () => ({ to, subject: final.subject, body: text, card_headline: final.card_headline || draft.brief.sticky_line || "", card_png: pngOf(canvas) }));
  const gmail = `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(to)}&su=${encodeURIComponent(final.subject)}&body=${encodeURIComponent(text)}`;
  const copyEmail = async () => {
    try {
      await navigator.clipboard.writeText(`${final.subject}\n\n${text}`);
      toast("Copied");
    } catch {
      toast("We couldn't copy that. Please select it by hand.");
    }
  };
  const blob = () => new Promise((done) => canvas.toBlob(done, "image/png"));
  const copyCard = async () => {
    try {
      await navigator.clipboard.write([new ClipboardItem({ "image/png": await blob() })]);
      toast("Card copied. Paste it into your email.");
    } catch {
      toast("We couldn't copy the card. Please use Save card.");
    }
  };
  const saveCard = async () => {
    const url = URL.createObjectURL(await blob());
    el("a", { href: url, download: `card-${firstName(run).toLowerCase()}.png` }).click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  };
  return el("div", { class: "page" }, heading,
    logo(true),
    el("h1", { class: "title enter", style: "--i:1" }, "All done"),
    el("p", { class: "sub enter", style: "--i:2" }, sure ? `Nothing has been sent. It's yours to send. We're ${sure.score}% sure of this one.` : "Nothing has been sent. It's yours to send."),
    composeWindow({
      to: to || "Add their email yourself",
      subject: el("span", { class: "compose-subject" }, final.subject),
      body: el("p", { class: "plain" }, text),
      card: canvas,
    }),
    el("div", { class: "center enter", style: "--i:4" },
      me.gmail && el("button", { class: "btn main", onclick: sends.now }, "Send"),
      me.gmail && el("button", { class: "btn", onclick: sends.later }, "Schedule"),
      el("a", { class: me.gmail ? "btn" : "btn main", href: gmail, target: "_blank", rel: "noopener noreferrer" }, "Open in Gmail"),
      el("button", { class: "btn", onclick: copyCard }, "Copy card"),
      el("button", { class: "btn", onclick: saveCard }, "Save card"),
      el("button", { class: "btn", onclick: copyEmail }, "Copy email"),
      el("a", { class: "btn quiet", href: "#/" }, "Write to someone else")),
  );
}

// ---- past work ---------------------------------------------------------

const GROUPS = [
  { key: "all", label: "Everything", has: () => true },
  { key: "you", label: "Needs you", has: (s) => ["waiting", "ready", "flagged"].includes(s) },
  { key: "done", label: "Done", has: (s) => ["approved", "scheduled", "sent"].includes(s) },
  { key: "held", label: "Held back", has: (s) => ["hold", "stopped", "discarded", "failed"].includes(s) },
];

async function pastView() {
  stopLive();
  let items;
  try {
    items = await api("/runs");
  } catch (err) {
    show(el("div", { class: "page" }, el("p", { class: "empty" }, err.message)));
    return;
  }
  if (location.hash !== "#/past") return;
  let active = "all";
  const list = el("div", {});
  const chips = GROUPS.map((group) => {
    const chip = el("button", { class: "chip", onclick: () => { active = group.key; draw(); } },
      el("b", {}, String(items.filter((r) => group.has(r.status)).length)), el("span", {}, group.label));
    chip.dataset.key = group.key;
    return chip;
  });
  const pillClass = (s) => (["approved", "sent"].includes(s) ? "pill fill" : ["hold", "stopped", "discarded", "failed"].includes(s) ? "pill dim" : "pill");

  function draw() {
    chips.forEach((c) => c.classList.toggle("on", c.dataset.key === active));
    const group = GROUPS.find((g) => g.key === active);
    const rows = items.filter((r) => group.has(r.status));
    list.replaceChildren(...(rows.length
      ? rows.map((r, i) => el("a", { class: "row enter", style: `--i:${Math.min(i, 10)}`, href: "#/run/" + r.id },
          el("div", {}, el("b", {}, draftName(r)), !r.locked && el("small", {}, `${r.prospect.title}, ${r.prospect.company}`)),
          el("div", { class: "opening" }, r.subject || r.opening || "", el("small", {}, when(r.created_at) + (r.confidence != null ? ` · ${r.confidence}% sure` : ""))),
          el("span", { class: pillClass(r.status) }, STATUS[r.status])))
      : [el("p", { class: "empty" }, "Nothing here yet.")]));
  }
  draw();
  show(el("div", { class: "page wide" },
    el("h1", { class: "past-head enter" }, "Past work"),
    el("div", { class: "filters enter", style: "--i:1" }, chips), list));
}

// ---- the sidebar -------------------------------------------------------

const ICONS = {
  panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  dots: '<circle cx="5" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="19" cy="12" r="1.2" fill="currentColor"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  pin: '<path d="M12 16v6M8.5 3h7l-1 7 3.5 4H6l3.5-4z"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  unlock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7.6-1.7"/>',
  redo: '<path d="M20 12a8 8 0 1 1-2.6-5.9L20 8"/><path d="M20 3v5h-5"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6.5 7l1 13h9l1-13"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  back: '<path d="m15 6-6 6 6 6"/>',
  pencil: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 21a7.5 7.5 0 0 1 15 0"/>',
  gear: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
  door: '<path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4"/><path d="M15 8l4 4-4 4M19 12H9"/>',
  swap: '<path d="M4 8h14l-3.5-3.5M20 16H6l3.5 3.5"/>',
};
const icon = (name) => svg(ICONS[name], { viewBox: "0 0 24 24", class: "ico", "aria-hidden": "true" });

const stored = {
  get(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private window */ }
  },
};

const sideList = document.getElementById("side-list");
const side = { runs: [], folders: [], closed: new Set(stored.get("drafter.closedFolders", [])), seen: null, last: "", timer: null, ask: 0 };
const openRunId = () => (location.hash.match(/^#\/run\/([a-f0-9]+)$/) || [])[1];
const draftName = (r) => (r.locked ? "Private draft" : r.title || r.prospect.name);

async function loadSidebar() {
  const ask = ++side.ask;
  let runs, folders;
  try {
    [runs, folders] = await Promise.all([api("/runs"), api("/folders")]);
  } catch {
    clearTimeout(side.timer);
    side.timer = setTimeout(loadSidebar, 8000);
    return;
  }
  if (ask !== side.ask) return;
  clearTimeout(side.timer);
  if (runs.some((r) => STILL_GOING.has(r.status))) side.timer = setTimeout(loadSidebar, 4000);
  else if (runs.some((r) => r.status === "scheduled")) side.timer = setTimeout(loadSidebar, 15000);
  const snapshot = JSON.stringify([runs, folders]);
  if (snapshot === side.last) return;
  side.last = snapshot;
  side.runs = runs;
  side.folders = folders;
  drawSidebar();
}

function statusMark(status) {
  const kind = STILL_GOING.has(status) ? "busy" : ["waiting", "ready", "flagged"].includes(status) ? "needs" : "";
  return el("span", { class: "s-dot " + kind, title: STATUS[status] });
}

function sideRow(r) {
  const fresh = side.seen && !side.seen.has(r.id);
  const name = draftName(r);
  return el("div", { class: `s-row${r.id === openRunId() ? " on" : ""}${fresh ? " s-new" : ""}`, "data-id": r.id },
    el("a", { class: "s-link", href: "#/run/" + r.id, title: r.locked ? name : r.subject || `${r.prospect.name}, ${r.prospect.company}` },
      r.locked ? icon("lock") : statusMark(r.status),
      el("span", { class: "s-name" }, name, !r.locked && !r.title && el("small", {}, ` · ${r.prospect.company}`)),
    ),
    el("button", { class: "icon-btn s-more", type: "button", "aria-label": `Options for ${name}`, "aria-haspopup": "menu", onclick: (e) => openMenu(e.currentTarget, rowMenu(r)) }, icon("dots")),
  );
}

function folderBlock(folder, rows) {
  const open = !side.closed.has(folder.id);
  const toggle = () => {
    if (open) side.closed.add(folder.id); else side.closed.delete(folder.id);
    stored.set("drafter.closedFolders", [...side.closed]);
    drawSidebar();
  };
  return el("div", { class: "s-folder" },
    el("div", { class: "s-row" },
      el("button", { class: `s-link${open ? " open" : ""}`, type: "button", "aria-expanded": String(open), onclick: toggle },
        icon("chevron"), icon("folder"),
        el("span", { class: "s-name" }, folder.name),
        el("span", { class: "s-count" }, String(rows.length)),
      ),
      el("button", { class: "icon-btn s-more", type: "button", "aria-label": `Options for folder ${folder.name}`, "aria-haspopup": "menu", onclick: (e) => openMenu(e.currentTarget, folderMenu(folder)) }, icon("dots")),
    ),
    open && el("div", { class: "s-inside" }, rows.length ? rows.map(sideRow) : el("p", { class: "s-hint" }, "Nothing in here yet.")),
  );
}

function drawSidebar() {
  const top = sideList.scrollTop;
  const { runs, folders } = side;
  const pinned = runs.filter((r) => r.pinned);
  const loose = runs.filter((r) => !r.pinned && !r.folder_id);
  sideList.replaceChildren(
    ...(pinned.length ? [el("div", { class: "s-head" }, "Pinned"), ...pinned.map(sideRow)] : []),
    el("div", { class: "s-head" }, "Folders",
      el("button", { class: "icon-btn tiny", type: "button", "aria-label": "New folder", title: "New folder", onclick: () => newFolder() }, icon("plus"))),
    ...(folders.length ? folders.map((f) => folderBlock(f, runs.filter((r) => r.folder_id === f.id))) : [el("p", { class: "s-hint" }, "Make a folder to group your drafts.")]),
    el("div", { class: "s-head" }, "Drafts"),
    ...(loose.length ? loose.map(sideRow) : [el("p", { class: "s-hint" }, "Your drafts will show up here.")]),
  );
  sideList.scrollTop = top;
  side.seen = new Set(runs.map((r) => r.id));
}

function markOpenRow() {
  const id = openRunId();
  sideList.querySelectorAll(".s-row[data-id]").forEach((row) => row.classList.toggle("on", row.dataset.id === id));
}

function afterChange() {
  if (location.hash === "#/past") route();
  return loadSidebar();
}

async function attempt(work, message) {
  try {
    await work();
    if (message) toast(message);
    await afterChange();
    return true;
  } catch (err) {
    toast(err.message);
    return false;
  }
}

// ---- small menus and questions ---------------------------------------

let menu = null;

function closeMenu(giveFocusBack = true) {
  if (!menu) return;
  const { box, anchor } = menu;
  menu = null;
  box.remove();
  if (giveFocusBack && anchor.isConnected) anchor.focus();
}

function placeMenu() {
  const { box, anchor } = menu;
  const at = anchor.getBoundingClientRect();
  const size = box.getBoundingClientRect();
  box.style.left = Math.max(8, Math.min(at.left, innerWidth - size.width - 8)) + "px";
  const below = at.bottom + 4;
  box.style.top = (below + size.height > innerHeight - 8 ? Math.max(8, at.top - size.height - 4) : below) + "px";
}

function fillMenu(items) {
  menu.box.replaceChildren(...items.filter(Boolean).map((item) => item === "line"
    ? el("hr", {})
    : el("button", {
        class: "menu-item" + (item.danger ? " danger" : ""), type: "button", role: "menuitem",
        onclick: () => { if (!item.stay) closeMenu(); item.act(); },
      }, icon(item.icon), el("span", {}, item.label), item.ticked && icon("check"))));
  placeMenu();
  menu.box.querySelector("button")?.focus();
}

function openMenu(anchor, items) {
  const again = menu && menu.anchor === anchor;
  closeMenu(false);
  if (again) { anchor.focus(); return; }
  const box = el("div", { class: "menu", role: "menu", onkeydown: (event) => {
    const buttons = [...box.querySelectorAll("button")];
    const at = buttons.indexOf(document.activeElement);
    if (event.key === "ArrowDown") { event.preventDefault(); buttons[(at + 1) % buttons.length].focus(); }
    if (event.key === "ArrowUp") { event.preventDefault(); buttons[(at - 1 + buttons.length) % buttons.length].focus(); }
    if (event.key === "Tab") closeMenu(false);
  } });
  menu = { box, anchor };
  document.body.append(box);
  fillMenu(items);
}

document.addEventListener("pointerdown", (event) => {
  if (menu && !menu.box.contains(event.target) && !menu.anchor.contains(event.target)) closeMenu(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (menu) closeMenu();
  else document.body.classList.remove("side-shown");
});
window.addEventListener("resize", () => closeMenu(false));
sideList.addEventListener("scroll", () => closeMenu(false));

function askDialog({ title, text, input: wanted, ok, submit }) {
  return new Promise((done) => {
    let answered = false;
    const input = wanted && el("input", { class: "dlg-input", type: wanted.type || "text", "aria-label": wanted.label, placeholder: wanted.label, value: wanted.value || "", maxlength: wanted.max || 60, autocomplete: "off" });
    const problem = el("p", { class: "dlg-problem", role: "alert" });
    const yes = el("button", { class: "btn main", type: "submit" }, ok);
    const box = el("dialog", { class: "dlg", onclose: () => { box.remove(); done(answered); } },
      el("form", {
        novalidate: true,
        onsubmit: async (event) => {
          event.preventDefault();
          yes.disabled = true;
          problem.textContent = "";
          try {
            await submit(input?.value);
            answered = true;
            box.close();
          } catch (err) {
            problem.textContent = err.message;
            yes.disabled = false;
            input?.select();
          }
        },
      },
        el("h2", {}, title),
        text && el("p", { class: "small" }, text),
        input, problem,
        el("div", { class: "dlg-foot" }, el("button", { class: "btn quiet", type: "button", onclick: () => box.close() }, "Cancel"), yes),
      ),
    );
    document.body.append(box);
    box.showModal();
  });
}

// ---- what each option does -------------------------------------------

function newFolder(thenPut) {
  return askDialog({
    title: "New folder",
    input: { label: "Folder name", max: 40 },
    ok: "Make folder",
    submit: async (name) => {
      const { id } = await api("/folders", { name });
      if (thenPut) await api(`/runs/${thenPut.id}/folder`, { folder_id: id });
      await afterChange();
    },
  });
}

function moveMenu(r) {
  return [
    { icon: "back", label: "Back", stay: true, act: () => fillMenu(rowMenu(r)) },
    "line",
    ...side.folders.map((f) => ({
      icon: "folder", label: f.name, ticked: f.id === r.folder_id,
      act: () => f.id !== r.folder_id && attempt(() => api(`/runs/${r.id}/folder`, { folder_id: f.id }), `Moved to ${f.name}`),
    })),
    r.folder_id && { icon: "trash", label: "Take out of folder", act: () => attempt(() => api(`/runs/${r.id}/folder`, { folder_id: null }), "Taken out of the folder") },
    side.folders.length > 0 && "line",
    { icon: "plus", label: "New folder", act: () => newFolder(r) },
  ];
}

function reopenIfShowing(id) {
  if (openRunId() === id) route();
}

async function lockDraft(r) {
  const first = !me.has_code;
  const done = await askDialog({
    title: first ? "Choose a code" : "Lock this draft",
    text: first
      ? "We'll scramble this draft with your code, so nobody can read it without the code. Not even us. If you forget the code, the draft is lost for good."
      : "Enter your code. We'll scramble this draft so nobody can read it without that code.",
    input: { type: "password", label: first ? "At least 8 characters" : "Your code" },
    ok: "Lock it",
    submit: (code) => api(`/runs/${r.id}/lock`, { code }),
  });
  if (!done) return;
  unfinished.delete(r.id);
  toast("Locked");
  await refreshMe();
  await afterChange();
  reopenIfShowing(r.id);
}

async function unlockDraft(r) {
  const done = await askDialog({
    title: "Unlock this draft",
    text: "It will be saved as normal text again until you lock it.",
    input: { type: "password", label: "Your code" },
    ok: "Unlock",
    submit: (code) => api(`/runs/${r.id}/unlock`, { code }),
  });
  if (done) {
    toast("Unlocked");
    await refreshMe();
    await afterChange();
    reopenIfShowing(r.id);
  }
}

async function redoDraft(r) {
  try {
    const { id } = await api(`/runs/${r.id}/redo`, {});
    toast("Starting again. Your old draft is still here.");
    location.hash = "#/run/" + id;
    await loadSidebar();
  } catch (err) {
    toast(err.message);
  }
}

async function deleteDraft(r) {
  const done = await askDialog({
    title: "Delete this draft?",
    text: r.locked ? "This private draft will be gone for good." : `The email to ${r.prospect.name} will be gone for good.`,
    ok: "Delete",
    submit: () => api(`/runs/${r.id}`, undefined, "DELETE"),
  });
  if (!done) return;
  unfinished.delete(r.id);
  toast("Deleted");
  if (openRunId() === r.id) location.hash = "#/";
  await afterChange();
}

function renameDraft(r) {
  return askDialog({
    title: "Rename this draft",
    input: { label: "Draft name", value: draftName(r), max: 80 },
    ok: "Save",
    submit: async (title) => {
      await api(`/runs/${r.id}/rename`, { title });
      await afterChange();
      reopenIfShowing(r.id);
    },
  });
}

function rowMenu(r) {
  return [
    !r.locked && { icon: "pencil", label: "Rename", act: () => renameDraft(r) },
    { icon: "pin", label: r.pinned ? "Unpin" : "Pin", act: () => attempt(() => api(`/runs/${r.id}/pin`, { pinned: !r.pinned }), r.pinned ? "Unpinned" : "Pinned") },
    { icon: "folder", label: "Move to folder", stay: true, act: () => fillMenu(moveMenu(r)) },
    r.locked
      ? { icon: "unlock", label: "Unlock", act: () => unlockDraft(r) }
      : { icon: "lock", label: "Lock", act: () => lockDraft(r) },
    "line",
    !r.locked && { icon: "redo", label: "Redo", act: () => redoDraft(r) },
    { icon: "trash", label: "Delete", danger: true, act: () => deleteDraft(r) },
  ];
}

function folderMenu(folder) {
  return [
    {
      icon: "pencil", label: "Rename",
      act: () => askDialog({
        title: "Rename folder",
        input: { label: "Folder name", value: folder.name, max: 40 },
        ok: "Save",
        submit: async (name) => { await api(`/folders/${folder.id}`, { name }, "PATCH"); await afterChange(); },
      }),
    },
    {
      icon: "trash", label: "Delete folder", danger: true,
      act: () => askDialog({
        title: "Delete this folder?",
        text: "The drafts inside won't be deleted. They'll go back to your list.",
        ok: "Delete folder",
        submit: async () => { await api(`/folders/${folder.id}`, undefined, "DELETE"); await afterChange(); },
      }),
    },
  ];
}

// ---- you: logging in, the bottom bar, settings ------------------------

let me = null;

async function refreshMe() {
  try {
    me = await api("/me");
    drawFoot();
  } catch { /* the next call will send them to log in */ }
}

const knownNames = () => {
  const names = stored.get("drafter.accounts", []);
  return Array.isArray(names) ? names : [];
};

function rememberName(name) {
  stored.set("drafter.accounts", [name, ...knownNames().filter((n) => n.toLowerCase() !== name.toLowerCase())].slice(0, 6));
}

function freshStart() {
  location.hash = "#/";
  location.reload();
}

function authForm({ headingTag, names, startJoining, after }) {
  let joining = startJoining;
  const nameBox = field("auth-name", "Your name", "text", "username");
  const passBox = field("auth-pass", "Password", "password", "current-password");
  const nameInput = nameBox.querySelector("input");
  const passInput = passBox.querySelector("input");
  const heading = el(headingTag, {});
  const problem = el("p", { class: "form-problem", role: "alert" });
  const button = el("button", { class: "btn main wide", type: "submit" });
  const swap = el("button", { type: "button", class: "more-btn", onclick: () => { joining = !joining; paint(); } });
  const paint = () => {
    heading.textContent = joining ? "Make your account" : "Welcome back";
    button.textContent = joining ? "Make account" : "Log in";
    swap.textContent = joining ? "I already have an account" : "New here? Make an account";
    passInput.autocomplete = joining ? "new-password" : "current-password";
    problem.textContent = "";
  };
  paint();
  const chips = names.length > 0 && el("div", { class: "name-chips" }, names.map((name) =>
    el("button", { type: "button", class: "name-chip", onclick: () => { joining = false; paint(); nameInput.value = name; passInput.focus(); } }, icon("user"), name)));
  const form = el("form", {
    novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      button.disabled = true;
      problem.textContent = "";
      try {
        const account = await api(joining ? "/signup" : "/login", { name: nameInput.value, password: passInput.value });
        rememberName(account.name);
        after();
      } catch (err) {
        problem.textContent = err.message;
        button.disabled = false;
      }
    },
  }, chips, nameBox, passBox, button, problem, el("div", { class: "auth-swap" }, swap));
  return { heading, form };
}

function signInScreen() {
  const names = knownNames();
  const { heading, form } = authForm({ headingTag: "h1", names, startJoining: names.length === 0, after: freshStart });
  view.replaceChildren(el("div", { class: "home-wrap" }, el("div", { class: "home-box enter" }, logo(true), heading, form)));
}

function changeUser() {
  const names = knownNames().filter((n) => n.toLowerCase() !== me.name.toLowerCase());
  const { heading, form } = authForm({ headingTag: "h2", names, startJoining: false, after: freshStart });
  const box = el("dialog", { class: "dlg", onclose: () => box.remove() }, heading, form,
    el("div", { class: "auth-swap" }, el("button", { type: "button", class: "more-btn", onclick: () => box.close() }, "Cancel")));
  document.body.append(box);
  box.showModal();
}

async function logOut() {
  try {
    await api("/logout", {});
    freshStart();
  } catch (err) {
    toast(err.message);
  }
}

function drawFoot() {
  document.getElementById("side-foot").replaceChildren(
    el("div", { class: "user-chip", title: me.name },
      el("span", { class: "user-badge" }, icon("user")),
      el("span", { class: "user-name" }, me.name)),
    el("a", { class: "icon-btn", href: "#/settings", "aria-label": "Settings", title: "Settings" }, icon("gear")),
    el("button", {
      class: "icon-btn", type: "button", "aria-label": "Change user or log out", title: "Change user or log out", "aria-haspopup": "menu",
      onclick: (e) => openMenu(e.currentTarget, [
        { icon: "swap", label: "Change user", act: changeUser },
        { icon: "door", label: "Log out", act: logOut },
      ]),
    }, icon("door")),
  );
}

function settingsCard(position, { title, text, fields, button, submit }) {
  const boxes = fields.map((f) => {
    const box = field(f.name, f.label, f.type || "text", f.auto || "off");
    box.querySelector("input").value = f.value || "";
    return box;
  });
  const problem = el("p", { class: "form-problem", role: "alert" });
  const save = el("button", { class: "btn main", type: "submit" }, button);
  const form = el("form", {
    novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      save.disabled = true;
      problem.textContent = "";
      try {
        await submit(Object.fromEntries(new FormData(form)));
        toast("Saved");
        boxes.forEach((box, i) => { if (fields[i].type === "password") box.querySelector("input").value = ""; });
      } catch (err) {
        problem.textContent = err.message;
      }
      save.disabled = false;
    },
  }, boxes, problem, el("div", { class: "set-foot" }, save));
  return el("section", { class: "card set-card enter", style: `--i:${position}` }, el("h2", {}, title), text && el("p", { class: "small" }, text), form);
}

function gmailCard(position) {
  const body = !me.gmail_ready
    ? [el("p", { class: "small" }, "Gmail isn't set up on this app yet.")]
    : me.gmail
      ? [el("p", { class: "small" }, `Emails you send here go out from ${me.gmail}. We can only send, and can never read your inbox.`),
         el("div", { class: "set-foot" }, el("button", { class: "btn", onclick: disconnectGmail }, "Disconnect Gmail"))]
      : [el("p", { class: "small" }, "Connect it to send and schedule your emails from here. We can only send, and can never read your inbox."),
         el("div", { class: "set-foot" }, el("a", { class: "btn main", href: "/api/gmail/connect" }, "Connect Gmail"))];
  return el("section", { class: "card set-card enter", style: `--i:${position}` }, el("h2", {}, "Your Gmail"), body);
}

function disconnectGmail() {
  askDialog({
    title: "Disconnect Gmail?",
    text: "Anything you've scheduled won't be able to send until you connect again.",
    ok: "Disconnect",
    submit: async () => {
      await api("/gmail/disconnect", {});
      route();
    },
  });
}

async function settingsView() {
  stopLive();
  await refreshMe();
  if (location.hash !== "#/settings" || !me) return;
  const codeCard = me.has_code
    ? settingsCard(3, {
        title: "Your private code",
        text: me.has_locked
          ? "Your locked drafts are scrambled with this code. We can't open them without it, and we can't get it back for you."
          : "None of your drafts are locked right now, so you can set a new code without the old one.",
        fields: [
          ...(me.has_locked ? [{ name: "code-current", label: "Current code", type: "password" }] : []),
          { name: "code-new", label: "New code, at least 8 characters", type: "password" },
        ],
        button: "Change code",
        submit: async (v) => {
          await api("/settings/code", { current: v["code-current"] || "", new: v["code-new"] });
          await refreshMe();
        },
      })
    : el("section", { class: "card set-card enter", style: "--i:3" }, el("h2", {}, "Your private code"),
        el("p", { class: "small" }, "You'll choose a code the first time you lock a draft."));
  show(el("div", { class: "page" },
    el("h1", { class: "title enter", style: "margin-bottom:26px" }, "Settings"),
    settingsCard(1, {
      title: "You",
      fields: [
        { name: "name", label: "Your name", value: me.name, auto: "username" },
        { name: "sender", label: "Name at the end of your emails", value: me.sender },
      ],
      button: "Save",
      text: "If you leave the second one empty, we'll use your name. It's used on new emails.",
      submit: async (v) => {
        await api("/settings/profile", { name: v.name, sender: v.sender });
        await refreshMe();
        rememberName(me.name);
      },
    }),
    me.pitching && productCard(2),
    gmailCard(2),
    settingsCard(3, {
      title: "Your password",
      fields: [
        { name: "pass-current", label: "Current password", type: "password", auto: "current-password" },
        { name: "pass-new", label: "New password, at least 8 characters", type: "password", auto: "new-password" },
      ],
      button: "Change password",
      submit: (v) => api("/settings/password", { current: v["pass-current"], new: v["pass-new"] }),
    }),
    codeCard,
  ));
}

// ---- what you're selling ----------------------------------------------

let setupTurn = 0;
const SETUP_BUSY = new Set(["finding", "researching"]);

async function setupView() {
  stopLive();
  const turn = ++setupTurn;
  let shown = "";
  while (turn === setupTurn) {
    let setup;
    try {
      setup = await api("/setup");
    } catch (err) {
      if (turn === setupTurn) show(el("div", { class: "page" }, el("p", { class: "empty" }, err.message)));
      return;
    }
    if (turn !== setupTurn) return;
    const snapshot = JSON.stringify(setup);
    if (snapshot !== shown) {
      const sameScreen = shown && JSON.parse(shown).status === setup.status;
      shown = snapshot;
      if (setup.status === "researching" && sameScreen) patchSetupSteps(setup);
      else drawSetup(setup);
    }
    if (!SETUP_BUSY.has(setup.status)) return;
    await new Promise((done) => setTimeout(done, 1200));
  }
}

function setupBack() {
  return me.pitching
    ? el("a", { class: "btn quiet", href: "#/settings" }, "Back to Settings")
    : el("button", { class: "btn quiet", type: "button", onclick: logOut }, "Log out");
}

function setupForm(setup) {
  const site = field("setup-website", "Its website");
  const extras = el("div", { class: "more" }, el("div", {}, site));
  const moreBtn = el("button", {
    type: "button", class: "more-btn enter", style: "--i:5", "aria-expanded": "false",
    onclick: () => {
      const open = extras.classList.toggle("open");
      moreBtn.setAttribute("aria-expanded", String(open));
      moreBtn.textContent = open ? "Hide this" : "I also have its website";
    },
  }, "I also have its website");
  const problem = el("p", { class: "form-problem", role: "alert" }, setup.status === "failed" ? setup.problem : "");
  const button = el("button", { class: "btn main wide enter", style: "--i:6", type: "submit" }, "Find it");
  const form = el("form", {
    novalidate: true,
    onsubmit: async (event) => {
      event.preventDefault();
      const v = Object.fromEntries(new FormData(form));
      let ok = true;
      if (!v["setup-company"].trim()) { setError("setup-company", "Please add your company."); ok = false; }
      if (!v["setup-product"].trim()) { setError("setup-product", "Please add the product."); ok = false; }
      if (!ok) return;
      button.disabled = true;
      problem.textContent = "";
      try {
        await api("/setup", { company: v["setup-company"], product: v["setup-product"], website: v["setup-website"] || "" });
        setupView();
      } catch (err) {
        problem.textContent = err.message;
        button.disabled = false;
      }
    },
  },
    el("div", { class: "enter", style: "--i:3" }, field("setup-company", "Your company")),
    el("div", { class: "enter", style: "--i:4" }, field("setup-product", "The product you want to pitch")),
    extras, moreBtn, button, problem,
  );
  return el("div", { class: "home-wrap" }, el("div", { class: "home-box" },
    logo(true),
    el("h1", { class: "enter", style: "--i:1" }, "What are you selling?"),
    form,
    el("div", { class: "center enter", style: "--i:7" }, setupBack()),
  ));
}

async function answerSetup(body, problem) {
  document.querySelectorAll("#view .btn").forEach((b) => (b.disabled = true));
  try {
    await api("/setup/confirm", body);
    setupView();
  } catch (err) {
    document.querySelectorAll("#view .btn").forEach((b) => (b.disabled = false));
    if (problem) problem.textContent = err.message; else toast(err.message);
  }
}

function describeBox(setup, startOpen) {
  const area = el("textarea", { class: "setup-text", rows: 3, placeholder: "Tell us what it does and who it's for", "aria-label": "What it does" });
  const problem = el("p", { class: "form-problem", role: "alert" });
  const box = el("div", { class: `more${startOpen ? " open" : ""}` }, el("div", {},
    area,
    el("div", { class: "center" }, el("button", { class: "btn main", type: "button", onclick: () => answerSetup({ choice: null, description: area.value }, problem) }, "Use this")),
    problem,
  ));
  return { box, open: () => { box.classList.add("open"); area.focus(); } };
}

function chooseProduct(setup) {
  const none = setup.candidates.length === 0;
  const describe = describeBox(setup, none);
  return el("div", { class: "page" },
    el("h1", { class: "title enter", style: "--i:1;margin-top:20px" }, none ? "We couldn't find it online" : setup.candidates.length > 1 ? "Which one is your product?" : "Is this your product?"),
    el("p", { class: "sub enter", style: "--i:2" }, none ? `Tell us about ${setup.product} and we'll work from that.` : "We won't start until you say so."),
    setup.candidates.map((c, i) => el("article", { class: "card person enter", style: `--i:${i + 3}` },
      el("h2", {}, c.product),
      el("p", { class: "role" }, c.company),
      el("p", {}, c.about),
      el("div", { class: "sources" }, c.sources.map((s) => sourceLink(s.title, s.url))),
      el("button", { class: "btn main", onclick: () => answerSetup({ choice: i }) }, "Yes, this is it"),
    )),
    !none && el("div", { class: "center enter", style: `--i:${setup.candidates.length + 3}` },
      el("button", { class: "btn quiet", onclick: describe.open }, "None of these. I'll describe it")),
    el("div", { class: "enter", style: `--i:${setup.candidates.length + 4}` }, describe.box),
    el("div", { class: "center" }, el("button", { class: "btn quiet", onclick: startSetupAgain }, "Start again")),
  );
}

async function startSetupAgain() {
  try {
    await api("/setup/reset", {});
    setupView();
  } catch (err) {
    toast(err.message);
  }
}

let setupSteps = null;

function patchSetupSteps(setup) {
  if (!setupSteps?.list?.isConnected) return;
  setup.steps.forEach((step) => {
    let node = setupSteps.nodes.get(step.key);
    if (!node) {
      const notes = el("ul", { class: "step-notes" });
      const row = el("li", { class: "step" }, el("span", { class: "mark" }), el("div", {}, el("div", { class: "step-label" }, step.label), notes));
      node = { row, notes, count: 0 };
      setupSteps.nodes.set(step.key, node);
      setupSteps.list.append(row);
    }
    node.row.className = "step " + step.state;
    step.notes.slice(node.count).forEach((text) => node.notes.append(el("li", {}, text)));
    node.count = step.notes.length;
  });
  const active = setup.steps.find((s) => s.state === "running");
  if (active && setupSteps.now.textContent !== active.label) setupSteps.now.replaceChildren(el("span", { class: "swap", style: "display:inline-block" }, active.label));
}

function productSummary(p) {
  return [
    el("p", { class: "setup-about" }, p.about),
    el("ul", { class: "found" }, p.offerings.map((o) => el("li", {},
      el("b", {}, o.name),
      el("div", { class: "small" }, o.plain),
      o.proof.map((claim) => el("div", { class: "small proof" }, "“", claim, "”")),
    ))),
  ];
}

async function finishSetup() {
  try {
    await api("/setup/reset", {});
  } catch { /* nothing left to clear */ }
  await refreshMe();
  document.body.classList.remove("signed-out");
  loadSidebar();
  const before = location.hash;
  location.hash = "#/";
  if (before === "#/" || before === "") route();
}

async function drawSetupReady() {
  await refreshMe();
  const p = me.pitching;
  show(el("div", { class: "page" },
    logo(true),
    el("h1", { class: "title enter", style: "--i:1" }, `Ready to pitch ${p.product}`),
    el("p", { class: "sub enter", style: "--i:2" }, `Here's what we learned about ${p.company}. Every new draft will use this.`),
    el("section", { class: "card set-card enter", style: "--i:3" }, productSummary(p)),
    el("div", { class: "center enter", style: "--i:4" },
      el("button", { class: "btn main", onclick: finishSetup }, "Start writing"),
      el("button", { class: "btn quiet", onclick: startSetupAgain }, "That's not right. Start again")),
  ));
}

function drawSetup(setup) {
  if (setup.status === "ready") { drawSetupReady(); return; }
  if (setup.status === "choosing") { show(chooseProduct(setup)); return; }
  if (setup.status === "finding" || setup.status === "researching") {
    const now = el("h1", { class: "now" }, setup.status === "finding" ? `Looking up ${setup.product}` : "Learning about your product");
    const list = el("ol", { class: "steps" });
    setupSteps = { list, now, nodes: new Map() };
    show(el("div", { class: "page" },
      el("p", { class: "who enter" }, `${setup.product} · ${setup.company}`),
      el("div", { class: "working-top enter", style: "--i:1" }, compass(), now),
      el("div", { class: "enter", style: "--i:2" }, list),
    ));
    if (setup.status === "researching") patchSetupSteps(setup);
    return;
  }
  show(setupForm(setup));
}

function productCard(position) {
  const p = me.pitching;
  return el("section", { class: "card set-card enter", style: `--i:${position}` },
    el("h2", {}, "What you're pitching"),
    el("p", { class: "small" }, `${p.product} from ${p.company}`),
    productSummary(p),
    el("div", { class: "set-foot" }, el("button", { class: "btn", onclick: changeProduct }, "Change product")),
    el("p", { class: "small", style: "margin-top:10px" }, "Drafts you've already made stay as they are."),
  );
}

async function changeProduct() {
  await api("/setup/reset", {}).catch(() => {});
  location.hash = "#/setup";
}

// ---- moving between screens --------------------------------------------

const narrowScreen = window.matchMedia("(max-width: 820px)");

function showSidebar(open) {
  if (narrowScreen.matches) {
    document.body.classList.toggle("side-shown", open);
  } else {
    document.body.classList.toggle("side-hidden", !open);
    stored.set("drafter.sideHidden", !open);
  }
}

let routeTurn = 0;

function route() {
  if (!me || !me.pitching) return;
  setupTurn++;
  const turn = ++routeTurn;
  const hash = location.hash || "#/";
  const run = hash.match(/^#\/run\/([a-f0-9]+)$/);
  markOpenRow();
  document.body.classList.remove("side-shown");
  changeView(() => {
    if (turn !== routeTurn) return;
    if (run) runView(run[1]);
    else if (hash === "#/past") pastView();
    else if (hash === "#/settings") settingsView();
    else if (hash === "#/setup") setupView();
    else { stopLive(); homeView(); }
  });
}

document.getElementById("brand").append(logo(false), "Drafter");
document.getElementById("new-draft").append(icon("plus"), "New draft");
document.getElementById("side-close").append(icon("panel"));
document.getElementById("side-open").append(icon("panel"));
document.getElementById("side-close").addEventListener("click", () => showSidebar(false));
document.getElementById("side-open").addEventListener("click", () => showSidebar(true));
document.getElementById("scrim").addEventListener("click", () => showSidebar(false));
document.getElementById("sidebar").addEventListener("click", (event) => {
  if (event.target.closest("a")) document.body.classList.remove("side-shown");
});
if (stored.get("drafter.sideHidden", false)) document.body.classList.add("side-hidden");
window.addEventListener("hashchange", route);

(async function start() {
  try {
    me = await api("/me");
  } catch {
    signInScreen();
    return;
  }
  drawFoot();
  if (!me.pitching) {
    setupView();
    return;
  }
  document.body.classList.remove("signed-out");
  const gmailResult = new URLSearchParams(location.search).get("gmail");
  if (gmailResult) {
    toast(gmailResult === "connected" ? "Gmail connected" : "We couldn't connect your Gmail. Please try again.");
    history.replaceState(null, "", location.pathname + location.hash);
  }
  route();
  loadSidebar();
})();

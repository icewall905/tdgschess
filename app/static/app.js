"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(k));
  return el;
};
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json", "X-Client-Id": CID }, ...opts });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
};
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

// one id per browser: it owns the human seats this device plays (network games)
const CID = (() => {
  let c = store.get("cid", null);
  if (!c) { c = crypto.randomUUID?.() || `${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`; store.set("cid", c); }
  return c;
})();
let profiles = [];
const profileById = (id) => profiles.find((p) => p.id === id);
async function loadProfiles() { try { profiles = await api("/api/profiles"); } catch {} }

const FILES = "abcdefgh";
const VALUES = { p: 1, n: 3, b: 3, r: 5, q: 9, k: 0 };
const avatar = (side) => h("span", { class: `av ${side}` }, h("img", { src: pieceImg(side === "white" ? "K" : "k"), alt: "" }));
// players with a profile show their emoji instead of the king
const playerAvatar = (side, p) => {
  const emoji = p?.profile && (profileById(p.profile)?.emoji);
  return emoji ? h("span", { class: `av emoji ${side}` }, emoji) : avatar(side);
};
const TYPES = { human: ["🧒", "Human"], llm: ["🤖", "LLM"], stockfish: ["🐟", "Stockfish"], lc0: ["🦁", "Lc0"] };  // lc0: old games only
const pieceImg = (p) => `/static/pieces/${p === p.toUpperCase() ? "w" : "b"}${p.toUpperCase()}.svg`;

let config = { endpoints: [] };
let game = null;          // full state of the displayed game
let currentId = store.get("currentId", null);
let viewPly = null;       // null = follow live
let orientation = "white";
let manualFlip = false;
let selected = null;
let animFrom = null;
let skipAnim = false;     // human dropped the piece by drag, it is already on the target square      // {id, ply} shown before the next render; animate if it advances by one

// ------------------------------------------------------------ setup form
const DEFAULTS = {
  human: { type: "human", profile: null, remote: false },
  llm: { type: "llm", endpoint: "", model: "", temperature: 0.6, max_tokens: 8192, retries: 3, show_legal: true, hints: true, vision: false, chat: true, persona: "", on_fail: "random", extra: {} },
  stockfish: { type: "stockfish", auto: true, elo: 1500, movetime: 0.5, commentary: false, persona: "" },
};
let setup = store.get("setup", { white: { ...DEFAULTS.human }, black: { ...DEFAULTS.llm } });
for (const side of ["white", "black"]) if (!DEFAULTS[setup[side]?.type]) setup[side] = { ...DEFAULTS.stockfish };  // Lc0 was removed

function firstModel(idx = 0) {
  const online = config.endpoints.filter((e) => e.online && e.models?.length);
  const pool = online.length ? online : config.endpoints;
  const ep = pool[Math.min(idx, pool.length - 1)];
  return ep ? { endpoint: ep.name, model: ep.models?.[0] || "" } : {};
}

// engines can get a chat line per move written by an LLM
function commentaryFields(s, side, box) {
  const rerender = () => { store.set("setup", setup); renderSide(side); };
  box.append(h("label", { class: "chk" }, h("input", {
    type: "checkbox", checked: !!s.commentary,
    onchange: (e) => {
      s.commentary = e.target.checked;
      if (s.commentary && !s.comment_model) {
        const ep = config.endpoints.find((x) => x.online && x.models?.length) || config.endpoints[0];
        if (ep) { s.comment_endpoint = ep.name; s.comment_model = ep.models?.[0] || ""; }
      }
      rerender();
    },
  }), "Chat comments by an LLM 💬"));
  if (!s.commentary) return;
  const ep = config.endpoints.find((x) => x.name === s.comment_endpoint);
  box.append(
    h("div", { class: "grid2" },
      h("label", {}, "Endpoint", h("select", {
        onchange: (e) => { s.comment_endpoint = e.target.value; const x = config.endpoints.find((y) => y.name === s.comment_endpoint); if (x?.models?.length) s.comment_model = x.models[0]; rerender(); },
      }, ...config.endpoints.map((x) => h("option", { value: x.name, selected: x.name === s.comment_endpoint }, `${x.name}${x.online ? "" : " (offline)"}`)))),
      h("label", {}, "Model", h("input", { value: s.comment_model || "", list: `cmodels-${side}`, oninput: (e) => { s.comment_model = e.target.value; store.set("setup", setup); } }),
        h("datalist", { id: `cmodels-${side}` }, ...(ep?.models || []).map((m) => h("option", { value: m }))))),
    h("label", {}, "Chat character (optional)", h("input", { value: s.persona || "", placeholder: "e.g. a friendly robot fish", oninput: (e) => { s.persona = e.target.value; store.set("setup", setup); } })));
}

function renderSide(side) {
  const box = $(`.side-config[data-side=${side}]`);
  const s = setup[side];
  const upd = (key, conv = (v) => v) => (e) => {
    s[key] = e.target.type === "checkbox" ? e.target.checked : conv(e.target.value);
    store.set("setup", setup);
  };
  const num = (v) => (v === "" ? 0 : Number(v));
  box.replaceChildren();
  const types = ["human", "llm", "stockfish"];
  const typeSeg = h("div", { class: "type-seg", style: `--n:${types.length}` }, ...types.map((t) =>
    h("button", {
      class: s.type === t ? "active" : "", type: "button", title: TYPES[t][1],
      onclick: () => {
        if (s.type === t) return;
        setup[side] = { ...DEFAULTS[t] };
        if (t === "llm") Object.assign(setup[side], firstModel(side === "white" ? 0 : 1));
        store.set("setup", setup);
        renderSide(side);
      },
    }, h("b", {}, TYPES[t][0]), TYPES[t][1])));
  box.append(h("div", { class: "head" }, avatar(side), side === "white" ? "White" : "Black"), typeSeg);

  if (s.type === "llm") {
    const epSel = h("select", {
      onchange: (e) => {
        s.endpoint = e.target.value;
        const ep = config.endpoints.find((x) => x.name === s.endpoint);
        if (ep?.models?.length) s.model = ep.models[0];
        store.set("setup", setup);
        renderSide(side);
      },
    }, ...config.endpoints.map((ep) => h("option", { value: ep.name, selected: ep.name === s.endpoint }, `${ep.name}${ep.online ? "" : " (offline)"}`)));
    if (!config.endpoints.some((e) => e.name === s.endpoint)) epSel.prepend(h("option", { value: s.endpoint, selected: true }, s.endpoint || "—"));
    const ep = config.endpoints.find((x) => x.name === s.endpoint);
    const mi = (m) => ep?.model_info?.[m];
    const dl = h("datalist", { id: `models-${side}` }, ...(ep?.models || []).map((m) => h("option", { value: m, label: mi(m) ? `⭐ ${mi(m).rating}${mi(m).key !== m ? ` · ${mi(m).key}` : ""}` : "" })));
    const ratingNote = h("div", { class: "hint rating-note" });
    const showRating = () => {
      const info = mi(s.model);
      ratingNote.textContent = !s.model ? "" : info
        ? (info.games ? `⭐ ${info.rating} · ${info.games} rated game${info.games === 1 ? "" : "s"}` : `⭐ ${info.rating} · new model, not rated yet`) + (info.key !== s.model ? ` · plays as ${info.key}` : "")
        : "Unknown model id — it will be rated as it plays";
    };
    showRating();
    box.append(
      h("label", {}, "Endpoint", epSel),
      h("label", {}, "Model", h("input", { value: s.model || "", list: `models-${side}`, oninput: (e) => { s.model = e.target.value; store.set("setup", setup); showRating(); } }), dl),
      ratingNote,
      h("div", { class: "grid2" },
        h("label", {}, "Temperature", h("input", { type: "number", step: "0.1", min: "0", max: "2", value: s.temperature, oninput: upd("temperature", num) })),
        h("label", {}, "Max tokens", h("input", { type: "number", step: "256", value: s.max_tokens, oninput: upd("max_tokens", num) }))),
      h("label", { class: "chk" }, h("input", { type: "checkbox", checked: s.show_legal, onchange: upd("show_legal") }), "Give legal move list in prompt"),
      h("label", { class: "chk" }, h("input", { type: "checkbox", checked: s.hints ?? true, onchange: upd("hints") }), "Give position facts (material, hanging pieces, checks, captures)"),
      h("label", { class: "chk" }, h("input", { type: "checkbox", checked: !!s.vision, onchange: upd("vision") }), "Send board image (vision models only)"),
      h("label", { class: "chk" }, h("input", { type: "checkbox", checked: s.chat ?? true, onchange: upd("chat") }), "Chat message with each move 💬"),
      h("label", {}, "Chat character (optional)", h("input", { value: s.persona || "", placeholder: "e.g. a friendly pirate, a sleepy cat", oninput: upd("persona") })),
      h("details", {}, h("summary", {}, "Advanced"),
        h("div", { class: "grid2" },
          h("label", {}, "Retries on illegal", h("input", { type: "number", min: "0", max: "10", value: s.retries, oninput: upd("retries", num) })),
          h("label", {}, "When out of retries", h("select", { onchange: upd("on_fail") },
            h("option", { value: "random", selected: s.on_fail === "random" }, "random move"),
            h("option", { value: "forfeit", selected: s.on_fail === "forfeit" }, "forfeit game")))),
        h("label", {}, "Display name (optional)", h("input", { value: s.name || "", oninput: upd("name") })),
        h("label", {}, "Extra request JSON", h("textarea", {
          rows: 2, placeholder: '{"chat_template_kwargs": {"enable_thinking": false}}',
          oninput: (e) => { try { s.extra = e.target.value.trim() ? JSON.parse(e.target.value) : {}; e.target.style.borderColor = ""; store.set("setup", setup); } catch { e.target.style.borderColor = "var(--bad)"; } },
        }, Object.keys(s.extra || {}).length ? JSON.stringify(s.extra) : ""))),
    );
  } else if (s.type === "human") {
    if (s.profile && !profileById(s.profile)) s.profile = null;
    const sel = h("select", {
      onchange: (e) => { s.profile = e.target.value || null; store.set("setup", setup); renderSide(side); },
    }, h("option", { value: "", selected: !s.profile }, "🙂 Guest"),
      ...profiles.map((p) => h("option", { value: p.id, selected: s.profile === p.id }, `${p.emoji} ${p.name} · ⭐ ${p.rating}`)));
    box.append(h("label", {}, s.remote ? "Who's playing? (they can pick when joining)" : "Who's playing?", sel));
    if (!s.profile) box.append(h("label", {}, "Name", h("input", { value: s.name || "", placeholder: "e.g. Emma", maxlength: "40", oninput: upd("name") })));
    const where = (remote, label) => h("button", {
      type: "button", class: !!s.remote === remote ? "active" : "",
      onclick: () => { s.remote = remote; store.set("setup", setup); renderSide(side); },
    }, label);
    box.append(h("label", {}, "Plays on"), h("div", { class: "seg full" }, where(false, "💻 This device"), where(true, "📱 Another device")));
    if (s.remote) box.append(h("div", { class: "hint" }, "You'll get a 4-digit code. On the other phone or laptop open this page and tap 🔑 Join."));
  } else if (s.type === "stockfish") {
    if (s.auto === undefined) { s.auto = true; store.set("setup", setup); }  // setups saved before auto existed
    const lbl = h("span", {}, s.elo ? `${s.elo} Elo` : "full strength");
    box.append(
      h("label", { class: "chk" }, h("input", { type: "checkbox", checked: s.auto ?? false, onchange: (e) => { s.auto = e.target.checked; store.set("setup", setup); renderSide(side); } }),
        "Auto strength"),
      s.auto ? h("div", { class: "hint" }, `Plays a bit below a human's rating (eases off when far ahead) and even with a rated LLM. The slider is only a fallback.`) : null,
    );
    box.append(
      h("label", {}, "Strength: ", lbl, h("input", {
        type: "range", min: "300", max: "3200", step: "50", value: s.elo || 3200,
        oninput: (e) => { const v = Number(e.target.value); s.elo = v >= 3200 ? 0 : Math.max(300, v); lbl.textContent = s.elo ? `${s.elo} Elo` : "full strength"; store.set("setup", setup); },
      })),
      h("label", {}, "Seconds per move", h("input", { type: "number", step: "0.1", min: "0.05", value: s.movetime, oninput: upd("movetime", num) })),
    );
    commentaryFields(s, side, box);
  }
}

function renderSetup() {
  renderSide("white");
  renderSide("black");
  $("#endpoints").replaceChildren(h("div", {}, "Endpoints:"), ...config.endpoints.map((ep) =>
    h("div", {}, h("span", { class: "dot", style: `background:${ep.online ? "var(--good)" : "var(--bad)"}` }),
      `${ep.name} — ${ep.url} ${ep.online ? `(${ep.models.length} model${ep.models.length === 1 ? "" : "s"})` : "(offline)"}`)));
}

function applyPreset(p) {
  const llm = (i) => ({ ...DEFAULTS.llm, ...firstModel(i) });
  setup = {
    "me-llm": { white: { ...DEFAULTS.human }, black: llm(0) },
    "llm-llm": { white: llm(0), black: llm(1) },
    "llm-sf": { white: llm(0), black: { ...DEFAULTS.stockfish, elo: 1400 } },
    "me-sf": { white: { ...DEFAULTS.human }, black: { ...DEFAULTS.stockfish } },
  }[p];
  $("#opt-games").value = p === "llm-llm" || p === "llm-sf" ? 2 : 1;
  store.set("setup", setup);
  renderSetup();
}

async function startGame() {
  $("#setup-err").textContent = "";
  try {
    const body = {
      white: setup.white, black: setup.black,
      games: Number($("#opt-games").value) || 1,
      swap_colors: $("#opt-swap").checked,
      move_delay: Number($("#opt-delay").value) || 0,
      analysis: $("#opt-analysis").checked,
      start_fen: $("#opt-fen").value.trim() || null,
    };
    const r = await api("/api/games", { method: "POST", body: JSON.stringify(body) });
    openGame(r.ids[0]);
  } catch (e) { $("#setup-err").textContent = e.message; }
}

// ------------------------------------------------------------ board
function parseFen(fen) {
  const map = {};
  fen.split(" ")[0].split("/").forEach((row, i) => {
    let f = 0;
    for (const c of row) {
      if (/\d/.test(c)) f += Number(c);
      else { map[FILES[f] + (8 - i)] = c; f++; }
    }
  });
  return map;
}

const shownPly = () => (viewPly == null ? game.san.length : viewPly);
const isLive = () => viewPly == null || viewPly === game?.san.length;
const canMove = () => game && game.human_turn && isLive();

function renderBoard() {
  const board = $("#board");
  board.replaceChildren();
  const fen = game ? game.fens[shownPly()] : "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const pos = parseFen(fen);
  const ply = game ? shownPly() : 0;
  const last = game && ply > 0 ? game.uci[ply - 1] : null;
  const toMove = fen.split(" ")[1];
  const inCheck = game && ply === game.san.length ? game.check : false;
  const targets = selected && canMove() ? game.legal.filter((u) => u.startsWith(selected)).map((u) => u.slice(2, 4)) : [];
  const ranks = orientation === "white" ? [8, 7, 6, 5, 4, 3, 2, 1] : [1, 2, 3, 4, 5, 6, 7, 8];
  const files = orientation === "white" ? [...FILES] : [...FILES].reverse();
  ranks.forEach((r, ri) => files.forEach((f, fi) => {
    const sq = f + r;
    const light = (FILES.indexOf(f) + r) % 2 === 1;
    const p = pos[sq];
    const el = h("div", { class: `sq ${light ? "l" : "d"}`, "data-sq": sq });
    if (last && (last.slice(0, 2) === sq || last.slice(2, 4) === sq)) el.classList.add("last");
    if (sq === selected) el.classList.add("sel");
    if (targets.includes(sq)) el.classList.add("target", ...(p ? ["occ"] : []));
    if (inCheck && p && p.toLowerCase() === "k" && (p === "K") === (toMove === "w")) el.classList.add("check");
    if (p) el.append(h("img", { src: pieceImg(p), draggable: "false" }));
    if (fi === 0) el.append(h("span", { class: "coord r" }, r));
    if (ri === 7) el.append(h("span", { class: "coord f" }, f));
    board.append(el);
  }));
  if (animFrom && last && animFrom.id === game.id && animFrom.ply === ply - 1) {
    const img = $(`.sq[data-sq=${last.slice(2, 4)}] img`, board);
    const a = $(`.sq[data-sq=${last.slice(0, 2)}]`, board).getBoundingClientRect();
    const b = $(`.sq[data-sq=${last.slice(2, 4)}]`, board).getBoundingClientRect();
    img?.animate([{ transform: `translate(${a.left - b.left}px, ${a.top - b.top}px)` }, { transform: "none" }], { duration: 180, easing: "ease-out" });
  }
  animFrom = null;
  // hint arrow
  const ev = game?.evals?.[ply];
  if ($("#show-best").checked && ev?.best) {
    board.append(arrowSvg(ev.best));
  }
  renderEval();
}

function sqXY(sq) {
  let x = FILES.indexOf(sq[0]), y = 8 - Number(sq[1]);
  if (orientation === "black") { x = 7 - x; y = 7 - y; }
  return [x * 100 + 50, y * 100 + 50];
}

function arrowSvg(uci) {
  const [x1, y1] = sqXY(uci.slice(0, 2)), [x2, y2] = sqXY(uci.slice(2, 4));
  const ang = Math.atan2(y2 - y1, x2 - x1), len = Math.hypot(x2 - x1, y2 - y1) - 30;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "arrows");
  svg.setAttribute("viewBox", "0 0 800 800");
  const g = document.createElementNS(ns, "g");
  g.setAttribute("transform", `translate(${x1},${y1}) rotate(${(ang * 180) / Math.PI})`);
  g.setAttribute("fill", "rgba(21,120,27,.75)");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", `M0,-9 L${len},-9 L${len},-22 L${len + 30},0 L${len},22 L${len},9 L0,9 Z`);
  g.append(path);
  svg.append(g);
  return svg;
}

function evalToFrac(ev, ply) {
  if (!ev) return 0.5;
  if (ev.mate === 0 && game && ply != null) return game.fens[ply].split(" ")[1] === "w" ? 0 : 1;  // side to move is mated
  if (ev.mate != null) return ev.mate > 0 ? 1 : 0;
  return 1 / (1 + Math.exp(-ev.cp / 250));
}
const evalText = (ev) => (!ev ? "" : ev.mate != null ? `M${Math.abs(ev.mate)}` : (Math.abs(ev.cp) / 100).toFixed(1));

function renderEval() {
  const ev = game?.evals?.[game ? shownPly() : 0];
  const frac = evalToFrac(ev, game ? shownPly() : 0);
  const bar = $("#evalbar");
  bar.classList.toggle("flipped", orientation === "black");
  $("#evalfill").style.height = `${frac * 100}%`;
  const t = $("#evaltext");
  t.textContent = evalText(ev);
  const whiteAhead = frac >= 0.5;
  const atBottom = whiteAhead === (orientation === "white");
  t.style.top = atBottom ? "auto" : "3px";
  t.style.bottom = atBottom ? "3px" : "auto";
  t.style.color = whiteAhead ? "#222" : "#eee";
  // graph
  const svg = $("#evalgraph");
  const evs = game?.evals || [];
  if (evs.length < 2) { svg.innerHTML = ""; return; }
  const n = Math.max(evs.length - 1, 1);
  const pts = evs.map((e, i) => `${(i / n) * 400},${60 - evalToFrac(e, i) * 60}`);
  const cx = (shownPly() / n) * 400;
  svg.innerHTML = `<rect width="400" height="60" style="fill:var(--graph-bg)"/>` +
    `<polygon points="0,60 ${pts.join(" ")} 400,60" style="fill:var(--graph-fill)"/>` +
    `<line x1="0" y1="30" x2="400" y2="30" stroke="#888" stroke-width=".5" vector-effect="non-scaling-stroke"/>` +
    `<line x1="${cx}" y1="0" x2="${cx}" y2="60" style="stroke:var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke"/>` +
    annotations().filter((a) => a && a.sym !== "?!").map((a) =>
      `<circle cx="${((a.ply + 1) / n) * 400}" cy="${60 - evalToFrac(evs[a.ply + 1], a.ply + 1) * 60}" r="2.5" fill="${a.cls === "blunder" ? "var(--bad)" : "var(--warn)"}"/>`).join("");
}

// pointer interaction: click-click or drag
let drag = null;
$("#board").addEventListener("pointerdown", (e) => {
  const sqEl = e.target.closest(".sq");
  if (!sqEl || !canMove()) return;
  const sq = sqEl.dataset.sq;
  const pos = parseFen(game.fen);
  const p = pos[sq];
  const mine = p && (p === p.toUpperCase()) === (game.turn === "white");
  if (selected && selected !== sq && !mine) { tryMove(selected, sq); return; }
  if (mine) {
    selected = sq;
    renderBoard();
    const img = h("img", { class: "ghost", src: pieceImg(p) });
    const size = sqEl.getBoundingClientRect().width;
    img.style.width = img.style.height = `${size}px`;
    img.style.left = `${e.clientX}px`; img.style.top = `${e.clientY}px`;
    drag = { from: sq, img, moved: false };
    document.body.append(img);
    const origin = $(`.sq[data-sq=${sq}] img`);
    if (origin) origin.style.opacity = ".35";
  } else { selected = null; renderBoard(); }
});
window.addEventListener("pointermove", (e) => {
  if (!drag) return;
  drag.moved = true;
  drag.img.style.left = `${e.clientX}px`; drag.img.style.top = `${e.clientY}px`;
});
window.addEventListener("pointerup", (e) => {
  if (!drag) return;
  drag.img.remove();
  const el = document.elementFromPoint(e.clientX, e.clientY)?.closest(".sq");
  const from = drag.from;
  const moved = drag.moved;
  drag = null;
  if (el && el.dataset.sq !== from && moved) { skipAnim = true; tryMove(from, el.dataset.sq); }
  else renderBoard();
});

async function tryMove(from, to) {
  const cands = game.legal.filter((u) => u.slice(0, 4) === from + to);
  selected = null;
  if (!cands.length) { renderBoard(); return; }
  let uci = cands[0];
  if (cands.length > 1) {
    uci = await pickPromotion(from + to, game.turn);
    if (!uci) { renderBoard(); return; }
  }
  // optimistic local update so the piece moves immediately
  game.human_turn = false;
  try {
    await api(`/api/games/${game.id}/move`, { method: "POST", body: JSON.stringify({ uci }) });
  } catch (e) { alert(e.message); }
  poll(true);
}

function pickPromotion(base, turn) {
  return new Promise((resolve) => {
    const box = $("#promo-box");
    box.replaceChildren(...["q", "r", "b", "n"].map((p) =>
      h("button", { onclick: () => { $("#promo").hidden = true; resolve(base + p); } },
        h("img", { src: pieceImg(turn === "white" ? p.toUpperCase() : p) }))));
    $("#promo").hidden = false;
    $("#promo").onclick = (e) => { if (e.target.id === "promo") { $("#promo").hidden = true; resolve(null); } };
  });
}

// ------------------------------------------------------------ game panels
function material(fen) {
  let w = 0, b = 0;
  for (const p of Object.values(parseFen(fen))) {
    if (p === p.toUpperCase()) w += VALUES[p.toLowerCase()]; else b += VALUES[p];
  }
  return w - b;
}

// win-probability lost by the mover, from the Stockfish evals (white POV)
const NAGS = [[0.3, "??", "blunder"], [0.18, "?", "mistake"], [0.1, "?!", "inaccuracy"]];
let annCache = { key: "", list: [] };
function annotations() {
  if (!game?.evals?.length) return [];
  const key = `${game.id}:${game.evals.length}`;
  if (annCache.key === key) return annCache.list;
  const list = game.san.map((_, i) => {
    const a = game.evals[i], b = game.evals[i + 1];
    if (!a || !b) return null;
    const whiteMoved = game.fens[i].split(" ")[1] === "w";
    const loss = (evalToFrac(a, i) - evalToFrac(b, i + 1)) * (whiteMoved ? 1 : -1);
    const nag = NAGS.find(([t]) => loss >= t);
    return nag ? { ply: i, side: whiteMoved ? "white" : "black", sym: nag[1], cls: nag[2], loss } : null;
  });
  annCache = { key, list };
  return list;
}

function countPieces(fen) {
  const c = {};
  for (const p of Object.values(parseFen(fen))) c[p] = (c[p] || 0) + 1;
  return c;
}

// pieces captured BY each side, relative to the game's start position (promotions offset missing pawns)
function captured(fen) {
  const start = countPieces(game.fens[0]), now = countPieces(fen);
  const out = { white: [], black: [] };
  for (const [victim, taker] of [["w", "black"], ["b", "white"]]) {
    const k = (t) => (victim === "w" ? t.toUpperCase() : t);
    const promoted = ["q", "r", "b", "n"].reduce((n, t) => n + Math.max(0, (now[k(t)] || 0) - (start[k(t)] || 0)), 0);
    for (const t of ["q", "r", "b", "n", "p"]) {
      const miss = (start[k(t)] || 0) - (now[k(t)] || 0) - (t === "p" ? promoted : 0);
      for (let i = 0; i < miss; i++) out[taker].push(k(t));
    }
  }
  return out;
}

let audio = null;
function sound(kind) {
  if (!$("#sound").checked) return;
  try {
    audio ||= new AudioContext();
    if (audio.state === "suspended") audio.resume();
    const t = audio.currentTime;
    const tones = { move: [[520, 0]], capture: [[330, 0], [240, 0.05]], check: [[660, 0], [880, 0.08]], end: [[440, 0], [554, 0.12], [659, 0.24]] }[kind];
    for (const [f, dt] of tones) {
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = "triangle"; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + dt);
      g.gain.exponentialRampToValueAtTime(0.25, t + dt + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dt + 0.14);
      o.connect(g).connect(audio.destination);
      o.start(t + dt); o.stop(t + dt + 0.16);
    }
  } catch {}
}

function thinkingText(p, side) {
  const secs = Math.round(game.thinking_for + (performance.now() - (game._recv || performance.now())) / 1000);
  if (p.type !== "human") return `thinking ${secs}s`;
  if (game.open_seats?.includes(side)) return "waiting to join…";
  return game.my_sides?.includes(side) ? `your move · ${secs}s` : `thinking ${secs}s`;
}

function renderBars() {
  const sides = orientation === "white" ? ["black", "white"] : ["white", "black"];
  [["#bar-top", sides[0]], ["#bar-bottom", sides[1]]].forEach(([sel, side]) => {
    const el = $(sel);
    if (!game) { el.replaceChildren(avatar(side), h("span", { class: "muted" }, side === "white" ? "White" : "Black")); el.classList.remove("turn"); return; }
    const p = game[side], st = game.stats[side];
    const fen = game.fens[shownPly()];
    const diff = material(fen) * (side === "white" ? 1 : -1);
    const caps = captured(fen)[side];
    const marks = {};
    for (const a of annotations()) if (a && a.side === side && a.ply < shownPly()) marks[a.sym] = (marks[a.sym] || 0) + 1;
    const toMove = game.status === "running" && game.turn === side;
    el.classList.toggle("turn", toMove);
    const meta = [];
    if (p.type === "llm") {
      meta.push(`${st.llm_calls} calls`, `${st.tokens} tok`);
      if (st.illegal) meta.push(`${st.illegal} illegal`);
      if (st.random_moves) meta.push(`${st.random_moves} random`);
      if (st.errors) meta.push(`${st.errors} errors`);
    }
    if (p.type !== "human" && st.seconds) meta.push(`${Math.round(st.seconds)}s total`);
    if (p.type === "stockfish" && p.auto && game.engine_now?.[side]) meta.unshift(`≈ ${game.engine_now[side]} Elo`);
    const rt = game.ratings?.[side];
    if (rt) meta.unshift(rt.delta != null ? `⭐ ${rt.after} (${rt.delta >= 0 ? "+" : ""}${rt.delta})` : `⭐ ${rt.before}`);
    const markEls = NAGS.filter(([, sym]) => marks[sym]).map(([, sym, cls]) => h("span", { class: `nag ${cls}`, title: `${marks[sym]} ${cls}${marks[sym] > 1 ? "s" : ""}` }, `${marks[sym]}${sym}`));
    el.replaceChildren(...[
      playerAvatar(side, p),
      h("div", { class: "who" },
        h("div", { class: "row" }, h("span", { class: "name", title: p.label }, p.label), h("span", { class: `pill ${p.type}` }, `${TYPES[p.type]?.[0] || ""} ${TYPES[p.type]?.[1] || p.type}`)),
        h("div", { class: "row" },
          h("span", { class: "captured" }, ...caps.map((c, i) => h("img", { src: pieceImg(c), class: i && caps[i - 1] === c ? "same" : "", alt: c })),
            diff > 0 ? h("span", { class: "diff" }, `+${diff}`) : null),
          markEls.length ? h("span", { class: "marks" }, ...markEls) : null)),
      toMove && game.thinking_for != null ? h("span", { class: "thinking" }, thinkingText(p, side)) : null,
      h("span", { class: "meta" }, meta.join(" · ")),
    ].filter((x) => x != null && x !== false));
  });
}

function renderStatus() {
  const el = $("#status");
  el.className = "status";
  if (!game) { el.replaceChildren(h("span", { class: "big" }, "👋 Welcome!"), h("span", { class: "sub" }, "Pick two players and press Start.")); return; }
  const match = game.opts?.games > 1 ? `Game ${game.index} of ${game.opts.games}` : "";
  const line = (big, sub) => el.replaceChildren(h("span", { class: "big" }, big), ...(sub ? [h("span", { class: "sub" }, sub)] : []));
  if (game.status === "finished") {
    const winner = game.result === "1-0" ? "white" : game.result === "0-1" ? "black" : null;
    el.classList.add("win");
    line(winner ? `🏆 ${game[winner].label} wins!` : "🤝 It's a draw!", [game.result, game.termination, match].filter(Boolean).join(" · "));
  } else if (game.open_seats?.length && (game.status === "running" || game.status === "queued")) {
    el.classList.add("live", "code");
    const side = game.open_seats[0];
    el.replaceChildren(h("span", { class: "sub" }, "Join code"), h("span", { class: "code-digits" }, game.code),
      h("span", { class: "sub" }, `Waiting for ${side === "white" ? "⚪ White" : "⚫ Black"} — on the other device tap 🔑 Join`));
  } else if (game.status === "running") {
    el.classList.add("live");
    const p = game[game.turn];
    line(`${game.turn === "white" ? "⚪" : "⚫"} ${game.human_turn ? "Your move!" : `${p.label} to move`}${game.check ? " — check! ⚡" : ""}`,
      [`Move ${Math.floor(game.san.length / 2) + 1}`, match, game.code ? `code ${game.code}` : ""].filter(Boolean).join(" · "));
  } else line(game.status === "queued" ? "⏳ Waiting to start" : `⏹ ${game.status}`,
    [game.termination && game.termination !== game.status ? game.termination : "", match].filter(Boolean).join(" · "));
  const live = game.status === "running" || game.status === "queued";
  $("#abort").hidden = !live;
  $("#resign").hidden = !(game.human_turn);
  const taken = (game.remote_sides || []).filter((s) => !game.open_seats?.includes(s));
  $("#free-seat").hidden = !(live && game.is_host && taken.length);
}

function renderMoves() {
  const el = $("#moves");
  const bad = new Set(game ? game.log.filter((e) => e.error).map((e) => e.ply) : []);
  const ann = annotations();
  const cur = game ? shownPly() : 0;
  el.replaceChildren();
  if (!game) return;
  const startBlack = game.fens[0].split(" ")[1] === "b";
  const startNo = Number(game.fens[0].split(" ")[5]) || 1;
  const cells = [];
  const sans = startBlack ? [null, ...game.san] : game.san;
  for (let i = 0; i < sans.length; i += 2) {
    cells.push(h("span", { class: "n" }, `${startNo + i / 2}.`));
    for (const j of [i, i + 1]) {
      const realPly = startBlack ? j - 1 : j;
      if (j >= sans.length || sans[j] == null) { cells.push(h("span", {}, j < sans.length ? "…" : "")); continue; }
      cells.push(h("span", {
        class: `m${realPly + 1 === cur ? " cur" : ""}${bad.has(realPly) ? " bad" : ""}`,
        title: [bad.has(realPly) ? "LLM needed retries for this move" : "", ann[realPly] ? `${ann[realPly].cls} (−${Math.round(ann[realPly].loss * 100)}% win chance)` : ""].filter(Boolean).join(" · "),
        onclick: () => setView(realPly + 1),
      }, sans[j], ann[realPly] ? h("span", { class: `nag ${ann[realPly].cls}` }, ann[realPly].sym) : null));
    }
  }
  el.append(...cells);
  const c = $(".m.cur", el);
  if (c && isLive()) c.scrollIntoView({ block: "nearest" });
}

let logSig = "";
function renderLog(force) {
  const el = $("#log");
  if (!game) { el.replaceChildren(); logSig = ""; return; }
  const showR = $("#show-reasoning").checked;
  const sig = `${game.id}:${game.log.length}:${showR}`;
  if (sig === logSig && !force) return;
  logSig = sig;
  el.replaceChildren(...game.log.filter((e) => e.kind !== "chat" && e.kind !== "comment").map((e) => {
    const moveNo = `${Math.floor(e.ply / 2) + 1}${e.side === "white" ? "." : "…"}`;
    return h("div", { class: `entry ${e.side}${e.error ? " error" : ""}` },
      h("div", { class: "h" },
        h("span", {}, `${moveNo} ${game[e.side].label}${e.attempt > 1 ? ` · attempt ${e.attempt}` : ""}`),
        h("span", {}, [e.tokens != null ? `${e.tokens} tok` : null, e.seconds != null ? `${e.seconds}s` : null].filter(Boolean).join(" · "))),
      e.move ? h("div", {}, "played ", h("span", { class: "mv" }, e.move)) : null,
      e.error ? h("div", { class: "e" }, e.error) : null,
      showR && e.prompt ? h("details", { class: "prompt" }, h("summary", {}, "prompt"), h("pre", {}, e.prompt)) : null,
      showR && e.reasoning ? h("pre", { class: "reason" }, e.reasoning) : null,
      e.content ? h("pre", {}, e.content) : null);
  }));
}

// ------------------------------------------------------------ chat
let chatSig = "";
const spoken = new Set();
const moverSide = (ply) => {
  const blackFirst = game.fens[0].split(" ")[1] === "b";
  return (ply % 2 === 0) !== blackFirst ? "white" : "black";
};

function speak(text, side) {
  if (!$("#speak").checked || !window.speechSynthesis) return;
  const clean = text.replace(/[\p{Extended_Pictographic}\uFE0F\u200D]/gu, "").trim();
  if (!clean) return;
  const u = new SpeechSynthesisUtterance(clean);
  u.pitch = side === "white" ? 1.4 : 0.8;
  u.rate = 1.05;
  speechSynthesis.speak(u);
}

function renderChat(force) {
  const el = $("#chat");
  if (!game) { el.replaceChildren(h("div", { class: "empty" }, h("b", {}, "💬"), "Chat messages from the players show up here.")); chatSig = ""; return; }
  const toMove = game.status === "running" && game.thinking_for != null ? game.turn : null;
  const sig = `${game.id}:${game.san.length}:${game.log.length}:${game.status}:${toMove}:${shownPly()}`;
  if (sig === chatSig && !force) return;
  const firstLoad = !chatSig.startsWith(game.id + ":");
  chatSig = sig;
  const says = {};
  for (const e of game.log) if (e.move) says[e.ply] = e.say || "";
  const chats = game.log.map((e, i) => ({ ...e, i })).filter((e) => e.kind === "chat");
  let ci = 0;
  const bubble = (side, who, chip, text, cls = "") => h("div", { class: `msg ${side} ${cls}` }, playerAvatar(side, game[side]),
    h("div", { class: "bubble" }, h("div", { class: "who" }, who, chip ? h("span", { class: "chip" }, chip) : null), h("div", { class: "txt" }, text)));
  const pushChats = (upto) => {
    while (ci < chats.length && chats[ci].ply <= upto) {
      const e = chats[ci++];
      const key = `${game.id}:c${e.i}`;
      if (!spoken.has(key)) { spoken.add(key); if (!firstLoad) speak(e.say, e.side); }
      items.push(bubble(e.side, game[e.side].label, null, e.say, "human"));
    }
  };
  const cur = shownPly();
  const items = [h("div", { class: "note start" }, `🎉 Game on!  ${game.white.label} (White) vs ${game.black.label} (Black)`)];
  game.san.forEach((san, ply) => {
    pushChats(ply);
    const side = moverSide(ply);
    const p = game[side];
    const key = `${game.id}:${ply}`;
    const onclick = () => setView(ply + 1);
    const moveNo = `${Math.floor(ply / 2) + 1}${side === "white" ? "." : "…"}`;
    if (says[ply]) {
      if (!spoken.has(key)) { spoken.add(key); if (!firstLoad) speak(says[ply], side); }
      items.push(h("div", { class: `msg ${side}${ply + 1 === cur ? " cur" : ""}`, onclick },
        playerAvatar(side, p),
        h("div", { class: "bubble" },
          h("div", { class: "who" }, p.name || p.model || p.label, h("span", { class: "chip" }, `${moveNo} ${san}`)),
          h("div", { class: "txt" }, says[ply]))));
    } else {
      if (firstLoad) spoken.add(key);  // a late engine comment for a move seen live is still read aloud
      items.push(h("div", { class: `note mv${ply + 1 === cur ? " cur" : ""}`, onclick },
        `${p.type === "human" ? p.name || "You" : p.label} played ${moveNo} ${san}`));
    }
  });
  pushChats(Infinity);
  if (toMove) {
    const p = game[toMove];
    if (p.type !== "human") items.push(h("div", { class: `msg ${toMove} typing` },
      playerAvatar(toMove, p),
      h("div", { class: "bubble" }, h("div", { class: "who" }, p.name || p.model || p.label),
        h("div", { class: "dots" }, h("i"), h("i"), h("i")))));
  }
  if (game.status === "finished") {
    items.push(h("div", { class: "note end" }, `🏁 ${game.result} — ${game.termination}`));
    for (const [side, r] of Object.entries(game.ratings || {})) if (r.delta != null)
      items.push(h("div", { class: `note rating ${r.delta >= 0 ? "up" : "down"}` }, `${game[side].label}: ⭐ ${r.before} → ${r.after} (${r.delta >= 0 ? "+" : ""}${r.delta})`));
  }
  else if (game.status !== "running" && game.status !== "queued") items.push(h("div", { class: "note end" }, game.termination || game.status));
  renderReact();
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.replaceChildren(...items);
  if (atBottom || firstLoad) el.scrollTop = el.scrollHeight;
}

const REACTIONS = ["👏", "😮", "😂", "🤔", "👍", "❤️", "😱", "🎉"];
let reactSig = "";
function renderReact() {
  const el = $("#react");
  const mine = game?.my_sides?.length && game.status !== "aborted" && game.status !== "error";
  const show = !!mine && store.get("infoTab", "chat") === "chat";
  el.hidden = !show;
  if (!show) { reactSig = ""; return; }
  if (reactSig === game.id) return;  // keep the text box as the user types
  reactSig = game.id;
  const say = async (text) => {
    if (!text.trim()) return;
    try { await api(`/api/games/${game.id}/say`, { method: "POST", body: JSON.stringify({ text }) }); } catch (e) { alert(e.message); }
  };
  const input = h("input", { placeholder: "Say something…", maxlength: "140" });
  el.replaceChildren(
    h("div", { class: "emojis" }, ...REACTIONS.map((r) => h("button", { type: "button", onclick: () => say(r) }, r))),
    h("form", { onsubmit: (e) => { e.preventDefault(); say(input.value); input.value = ""; } }, input, h("button", { type: "submit" }, "Send")));
}

function setTab(tab) {
  store.set("infoTab", tab);
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tabs label[data-for]").forEach((l) => { l.hidden = l.dataset.for !== tab; });
  $("#chat").hidden = tab !== "chat";
  if (game) renderReact();
  $("#log").hidden = tab !== "log";
  if (tab === "chat") renderChat(true); else renderLog(true);
}

function renderAll() {
  renderBoard();
  renderBars();
  renderStatus();
  renderMoves();
  renderLog();
  renderChat();
}

function setView(ply) {
  if (!game) return;
  const before = shownPly();
  viewPly = ply >= game.san.length ? null : Math.max(0, ply);
  if (shownPly() === before + 1) animFrom = { id: game.id, ply: before };
  selected = null;
  renderAll();
}

function autoOrient() {
  if (manualFlip || !game) return;
  if (game.my_sides?.length === 1) orientation = game.my_sides[0];
  else orientation = game.black.type === "human" && game.white.type !== "human" ? "black" : "white";
}

async function openGame(id) {
  currentId = id;
  store.set("currentId", id);
  game = null; viewPly = null; selected = null; manualFlip = false; logSig = "";
  showView("play");
  await poll(true);
}

async function poll(force = false) {
  if (!currentId) { renderAll(); return; }
  try {
    const since = !force && game && game.id === currentId ? game.version : -1;
    const want = currentId;
    const s = await api(`/api/games/${currentId}?since=${since}`);
    if (s.unchanged || want !== currentId || (!s.unchanged && s.id !== currentId)) return;
    const first = !game || game.id !== s.id;
    const prev = first ? null : game;
    if (!first && s.version < prev.version) return;  // an older long-poll answer arriving late
    s._recv = performance.now();
    game = s;
    if (prev && !prev.human_turn && game.human_turn && game.my_sides?.length) navigator.vibrate?.(120);
    if (prev && prev.open_seats?.length && !game.open_seats?.length) sound("check");
    if (prev && viewPly == null && game.san.length === prev.san.length + 1) {
      if (!skipAnim) animFrom = { id: game.id, ply: prev.san.length };
      const san = game.san[game.san.length - 1];
      sound(san.includes("#") || game.status === "finished" ? "end" : san.includes("+") ? "check" : san.includes("x") ? "capture" : "move");
    } else if (prev && prev.status === "running" && game.status === "finished") sound("end");
    skipAnim = false;
    if (first) autoOrient();
    // when a match game ends, jump to the next one in the same match
    if (game.status === "finished" || game.status === "aborted") maybeFollowMatch();
    renderAll();
  } catch (e) {
    if (String(e.message).includes("not found")) { currentId = null; game = null; store.set("currentId", null); renderAll(); }
  }
}

let followTimer = null;
async function maybeFollowMatch() {
  if (followTimer || game.opts?.games <= 1 || game.status !== "finished") return;
  const g = game;
  followTimer = setTimeout(async () => {
    followTimer = null;
    if (currentId !== g.id) return;
    const list = await api("/api/games");
    const next = list.find((x) => x.match_id === g.match_id && x.index === g.index + 1);
    if (next && (next.status === "running" || next.status === "queued")) openGame(next.id);
  }, 4000);
}

// the server holds the request until the game changes (long poll), so moves from other devices appear instantly
async function loop() {
  await poll();
  const live = game && (game.status === "running" || game.status === "queued");
  setTimeout(loop, live ? 30 : 1500);
}
setInterval(() => { if (game?.thinking_for != null && game.status === "running") renderBars(); }, 1000);

// ------------------------------------------------------------ lists
const fmtTime = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

async function renderGames() {
  const list = await api("/api/games");
  const t = $("#games-table");
  t.replaceChildren(h("tr", {}, ...["When", "White", "Black", "Result", "Plies", "Status", "Termination", ""].map((x) => h("th", {}, x))),
    ...list.map((g) => h("tr", { class: "click", onclick: () => openGame(g.id) },
      h("td", {}, fmtTime(g.created)),
      h("td", {}, h("span", { class: "who-cell" }, avatar("white"), g.white.label, g.result === "1-0" ? " 🏆" : "")),
      h("td", {}, h("span", { class: "who-cell" }, avatar("black"), g.black.label, g.result === "0-1" ? " 🏆" : "")),
      h("td", { class: "res" }, g.result), h("td", {}, g.plies),
      h("td", {}, h("span", { class: `pill ${g.status}` }, g.status)),
      h("td", { class: "muted", title: g.termination || "" }, g.termination || ""),
      h("td", {}, ["finished", "aborted", "error"].includes(g.status) ? h("button", {
        onclick: async (e) => { e.stopPropagation(); if (confirm("Delete this game?")) { await api(`/api/games/${g.id}`, { method: "DELETE" }); renderGames(); } },
      }, "✕") : ""))));
  if (!list.length) t.append(h("tr", {}, h("td", { class: "muted" }, "No games yet.")));
}

async function renderStandings() {
  const rows = await api("/api/standings");
  const t = $("#standings-table");
  t.replaceChildren(h("tr", {}, ...["#", "Player", "Type", "Rating", "Games", "W", "D", "L", "Score %", "Illegal", "Random"].map((x) => h("th", {}, x))),
    ...rows.map((r, i) => h("tr", {},
      h("td", { class: "medal" }, ["🥇", "🥈", "🥉"][i] || i + 1), h("td", {}, h("b", {}, r.emoji ? `${r.emoji} ${r.label}` : r.label), r.settings ? h("div", { class: "muted small" }, r.settings) : null),
      h("td", {}, h("span", { class: `pill ${r.type}` }, `${TYPES[r.type]?.[0] || ""} ${TYPES[r.type]?.[1] || r.type}`)),
      h("td", {}, r.rating != null ? h("b", {}, `⭐ ${r.rating}`) : ""),
      h("td", {}, r.games), h("td", {}, r.w), h("td", {}, r.d), h("td", {}, r.l),
      h("td", {}, h("b", {}, r.score), h("span", { class: "bar" }, h("i", { style: `width:${r.score}%` }))), h("td", {}, r.illegal), h("td", {}, r.random_moves))));
  if (!rows.length) t.append(h("tr", {}, h("td", { class: "muted", colspan: 11 }, "No finished games yet.")));
}

function showView(v) {
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((s) => s.classList.toggle("active", s.id === `view-${v}`));
  if (v === "games") renderGames();
  if (v === "standings") renderStandings();
  if (v === "players") renderProfiles();
}

// ------------------------------------------------------------ players (profiles)
const EMOJIS = ["🧔", "👩", "🌸", "🌟", "🦄", "🐼", "🦊", "🐯", "🐸", "🐱", "🐶", "🦖", "🚀", "⚽", "🎨", "👑", "🐙", "🦋"];
let editing = null;

function sparkline(hist) {
  const pts = hist.map((x) => x.rating);
  if (pts.length < 2) return h("div", { class: "spark empty" }, "no games yet");
  const lo = Math.min(...pts) - 10, hi = Math.max(...pts) + 10;
  const xy = pts.map((v, i) => `${(i / (pts.length - 1)) * 200},${50 - ((v - lo) / (hi - lo)) * 46 - 2}`).join(" ");
  const el = h("div", { class: "spark" });
  el.innerHTML = `<svg viewBox="0 0 200 50" preserveAspectRatio="none"><polyline points="${xy}" fill="none" stroke-width="2.5" vector-effect="non-scaling-stroke" style="stroke:var(--accent)"/></svg>`;
  return el;
}

function profileForm(p) {
  const f = { name: p?.name || "", emoji: p?.emoji || EMOJIS[profiles.length % EMOJIS.length], rating: p?.rating || 600 };
  const emo = h("div", { class: "emoji-pick" });
  const drawEmo = () => emo.replaceChildren(...EMOJIS.map((e) => h("button", { type: "button", class: e === f.emoji ? "active" : "", onclick: () => { f.emoji = e; drawEmo(); } }, e)));
  drawEmo();
  const save = async () => {
    try {
      if (p) await api(`/api/profiles/${p.id}`, { method: "PATCH", body: JSON.stringify(f) });
      else await api("/api/profiles", { method: "POST", body: JSON.stringify(f) });
      editing = null; await loadProfiles(); renderProfiles(); renderSetup();
    } catch (e) { alert(e.message); }
  };
  return h("div", { class: "profile-card editing" },
    h("label", {}, "Name", h("input", { value: f.name, maxlength: "40", oninput: (e) => (f.name = e.target.value) })),
    h("label", {}, "Picture"), emo,
    h("label", {}, "Rating", h("input", { type: "number", min: "100", max: "3000", step: "10", value: f.rating, oninput: (e) => (f.rating = Number(e.target.value)) })),
    h("div", { class: "row" }, h("button", { class: "primary", type: "button", onclick: save }, p ? "Save" : "Add player"),
      h("button", { type: "button", onclick: () => { editing = null; renderProfiles(); } }, "Cancel")));
}

async function renderProfiles() {
  await loadProfiles();
  const el = $("#profiles");
  el.replaceChildren(...profiles.map((p) => editing === p.id ? profileForm(p) : h("div", { class: "profile-card" },
    h("div", { class: "top" }, h("span", { class: "big-emoji" }, p.emoji),
      h("div", {}, h("div", { class: "pname" }, p.name), h("div", { class: "muted stats" }, `${p.games} game${p.games === 1 ? "" : "s"}`), h("div", { class: "muted stats" }, `${p.w} won · ${p.d} drawn · ${p.l} lost`)),
      h("div", { class: "prating" }, h("small", {}, "rating"), `⭐ ${p.rating}`)),
    sparkline(p.history || []),
    h("div", { class: "row" },
      h("button", { type: "button", onclick: () => { editing = p.id; renderProfiles(); } }, "✏️ Edit"),
      h("button", { type: "button", class: "danger", onclick: async () => {
        if (!confirm(`Delete ${p.name}? Their rating history is lost.`)) return;
        await api(`/api/profiles/${p.id}`, { method: "DELETE" }); await loadProfiles(); renderProfiles(); renderSetup();
      } }, "🗑")))),
    editing === "new" ? profileForm(null) : h("button", { class: "profile-card add", type: "button", onclick: () => { editing = "new"; renderProfiles(); } }, h("span", {}, "＋"), "Add player"));
  let models = [];
  try { models = await api("/api/llm-ratings"); } catch {}
  $("#models").replaceChildren(...(models.length ? models.map((m) => h("div", { class: "profile-card" },
    h("div", { class: "top" }, h("span", { class: "big-emoji" }, "🤖"),
      h("div", {}, h("div", { class: "pname", title: m.key }, m.name), h("div", { class: "muted stats" }, `${m.games} game${m.games === 1 ? "" : "s"}`), h("div", { class: "muted stats" }, `${m.w} won · ${m.d} drawn · ${m.l} lost`)),
      h("div", { class: "prating" }, h("small", {}, "rating"), `⭐ ${m.rating}`)),
    sparkline(m.history || []),
    h("div", { class: "row" },
      h("button", { type: "button", onclick: async () => {
        const v = prompt(`Set ${m.name}'s rating`, m.rating);
        if (v && !isNaN(Number(v))) { await api("/api/llm-ratings/update", { method: "POST", body: JSON.stringify({ key: m.key, rating: Number(v) }) }); renderProfiles(); }
      } }, "✏️ Rating"),
      h("button", { type: "button", class: "danger", title: "Forget this model's rating", onclick: async () => {
        if (!confirm(`Reset ${m.name}? It starts again at 600 next game.`)) return;
        await api("/api/llm-ratings/update", { method: "POST", body: JSON.stringify({ key: m.key, delete: true }) }); renderProfiles();
      } }, "🗑")))) : [h("div", { class: "muted" }, "No rated models yet — they get a rating after their first finished game against a rated opponent.")]));
}

// ------------------------------------------------------------ join (network play)
let joinTimer = null;
let me = store.get("me", { profile: null, name: "" });

function renderJoinWho() {
  const pick = (profile) => { me = { ...me, profile }; store.set("me", me); renderJoinWho(); };
  $("#join-who").replaceChildren(
    ...profiles.map((p) => h("button", { type: "button", class: me.profile === p.id ? "active" : "", onclick: () => pick(p.id) },
      h("b", {}, p.emoji), p.name)),
    h("button", { type: "button", class: !me.profile ? "active" : "", onclick: () => pick(null) }, h("b", {}, "🙂"), "Guest"),
    ...(!me.profile ? [h("input", { class: "guest", placeholder: "Your name", value: me.name || "", maxlength: "40",
      oninput: (e) => { me.name = e.target.value; store.set("me", me); } })] : []));
}

async function refreshJoinList() {
  let list = [];
  try { list = await api("/api/open"); } catch {}
  const el = $("#join-list");
  el.replaceChildren(...(list.length ? list.map((g) => h("button", { type: "button", class: "open-game", onclick: () => doJoin(g.code) },
    h("span", { class: "pc" }, g.side === "white" ? "⚪" : "⚫"),
    h("span", {}, h("b", {}, `Play ${g.side === "white" ? "White" : "Black"} vs ${g.opponent}`), h("small", {}, `code ${g.code}${g.mine ? " · started here" : ""}`)),
    h("span", { class: "go" }, "Join ▶"))) : [h("div", { class: "muted empty-list" }, "No games are waiting right now. Ask the other player to start one with 📱 Another device.")]));
}

async function doJoin(code) {
  $("#join-err").textContent = "";
  try {
    const r = await api("/api/join", { method: "POST", body: JSON.stringify({ code, profile: me.profile, name: me.profile ? null : me.name }) });
    closeJoin();
    manualFlip = false;
    openGame(r.id);
  } catch (e) { $("#join-err").textContent = e.message; }
}

async function openJoin(code) {
  await loadProfiles();
  renderJoinWho();
  $("#join-code").value = code || "";
  $("#join-err").textContent = "";
  $("#join-modal").hidden = false;
  refreshJoinList();
  clearInterval(joinTimer);
  joinTimer = setInterval(refreshJoinList, 2000);
}
function closeJoin() { $("#join-modal").hidden = true; clearInterval(joinTimer); }

// auto-detect: games waiting for a player, and running games to watch
async function refreshLive() {
  let open = [], games = [];
  try { [open, games] = await Promise.all([api("/api/open"), api("/api/games")]); } catch { return; }
  const el = $("#live-strip");
  const waiting = open.filter((g) => !g.mine && g.id !== currentId);
  const watch = games.filter((g) => g.status === "running" && g.id !== currentId && !open.some((o) => o.id === g.id)).slice(0, 4);
  el.hidden = !waiting.length && !watch.length;
  el.replaceChildren(
    ...waiting.map((g) => h("button", { type: "button", class: "strip-join", onclick: () => openJoin(g.code) },
      `🎲 ${g.opponent} is waiting for a player!`, h("b", {}, "Join"))),
    ...watch.map((g) => h("button", { type: "button", class: "strip-watch", onclick: () => openGame(g.id) },
      `👀 ${g.white.label} vs ${g.black.label}`)));
}

// ------------------------------------------------------------ wiring
$("#join-open").onclick = () => openJoin();
$("#join-close").onclick = closeJoin;
$("#join-modal").addEventListener("click", (e) => { if (e.target.id === "join-modal") closeJoin(); });
$("#join-go").onclick = () => doJoin($("#join-code").value.trim());
$("#join-code").addEventListener("keydown", (e) => { if (e.key === "Enter") doJoin($("#join-code").value.trim()); });
$("#free-seat").onclick = async () => {
  if (!game) return;
  const side = (game.remote_sides || []).find((s) => !game.open_seats?.includes(s));
  if (side && confirm(`Let another device take the ${side} seat? The current device can join again with the code.`)) {
    await api(`/api/games/${game.id}/free/${side}`, { method: "POST" }).catch((e) => alert(e.message));
    poll(true);
  }
};
$$("nav button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
$$("[data-preset]").forEach((b) => b.addEventListener("click", () => applyPreset(b.dataset.preset)));
$("#swap-sides").onclick = () => { setup = { white: setup.black, black: setup.white }; store.set("setup", setup); renderSetup(); };
$("#start").onclick = startGame;
function setTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  $("#theme").textContent = dark ? "☀️" : "🌙";
}
$("#theme").onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  store.set("theme", next);
  setTheme(next);
};
function collapseSetup(on) {
  store.set("setupCollapsed", on);
  document.body.classList.toggle("setup-collapsed", on);
  $("#setup").hidden = on;
  $("#expand-setup").hidden = !on;
  renderBoard();
}
$("#collapse-setup").onclick = () => collapseSetup(true);
$("#expand-setup").onclick = () => collapseSetup(false);
$("#nav-first").onclick = () => setView(0);
$("#nav-prev").onclick = () => game && setView(shownPly() - 1);
$("#nav-next").onclick = () => game && setView(shownPly() + 1);
$("#nav-last").onclick = () => setView(Infinity);
$("#flip").onclick = () => { orientation = orientation === "white" ? "black" : "white"; manualFlip = true; renderAll(); };
$("#sound").onchange = () => { store.set("sound", $("#sound").checked); if ($("#sound").checked) sound("move"); };
$("#show-best").onchange = () => { store.set("showBest", $("#show-best").checked); renderBoard(); };
$$(".tabs button").forEach((b) => { b.onclick = () => setTab(b.dataset.tab); });
$("#speak").onchange = () => { store.set("speak", $("#speak").checked); if (!$("#speak").checked) window.speechSynthesis?.cancel(); };
$("#show-reasoning").onchange = () => { store.set("showReasoning", $("#show-reasoning").checked); renderLog(true); };
$("#resign").onclick = async () => { if (game && confirm("Resign this game?")) { await api(`/api/games/${game.id}/resign`, { method: "POST" }).catch((e) => alert(e.message)); poll(true); } };
$("#abort").onclick = async () => { if (game && confirm("Abort this game (and the rest of its match)?")) { await api(`/api/games/${game.id}/abort`, { method: "POST" }); poll(true); } };
$("#pgn").onclick = () => {
  if (!game) return;
  $("#pgn-text").value = game.pgn;
  $("#pgn-dl").href = `/api/games/${game.id}/pgn`;
  $("#pgn-dl").download = `${game.white.label} vs ${game.black.label}.pgn`.replace(/[^\w .@-]+/g, "_");
  $("#pgn-modal").hidden = false;
};
$("#pgn-close").onclick = () => ($("#pgn-modal").hidden = true);
$("#pgn-copy").onclick = () => navigator.clipboard?.writeText($("#pgn-text").value);
$("#evalgraph").addEventListener("click", (e) => {
  if (!game?.evals?.length) return;
  const r = e.currentTarget.getBoundingClientRect();
  setView(Math.round(((e.clientX - r.left) / r.width) * (game.evals.length - 1)));
});
document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) || !game) return;
  if (e.key === "ArrowLeft") setView(shownPly() - 1);
  else if (e.key === "ArrowRight") setView(shownPly() + 1);
  else if (e.key === "Home") setView(0);
  else if (e.key === "End") setView(Infinity);
  else if (e.key === "f") $("#flip").click();
  else return;
  e.preventDefault();
});

(async function init() {
  $("#show-best").checked = store.get("showBest", false);
  $("#sound").checked = store.get("sound", true);
  $("#show-reasoning").checked = store.get("showReasoning", false);
  $("#speak").checked = store.get("speak", false);
  setTheme(store.get("theme", null));
  if (store.get("setupCollapsed", false)) collapseSetup(true);
  setTab(store.get("infoTab", "chat"));
  try { config = await api("/api/config"); } catch {}
  await loadProfiles();
  // fill in endpoint/model for stored LLM setups that have none
  for (const [i, side] of ["white", "black"].entries()) {
    if (setup[side].type === "llm" && !setup[side].model) Object.assign(setup[side], firstModel(i));
  }
  renderSetup();
  renderAll();
  loop();
  refreshLive();
  setInterval(refreshLive, 4000);
})();

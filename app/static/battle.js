"use strict";
// Battle mode: pieces waddle square by square and have a cartoon fight when they capture.
// Everything plays on an overlay above #board, so a board rebuild mid-show does not cut it off.
const Battle = (() => {
  const FILES = "abcdefgh";
  const WORDS = ["POW!", "BAM!", "BONK!", "ZAP!", "WHAM!", "BOOM!"];
  let key = "", hide = new Set(), gen = 0, layer = null, g = null;

  const ok = () => typeof Element.prototype.animate === "function" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  const T = (x, y, more = "") => `translate(${x}px, ${y}px) ${more}`;
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // the board calls this on every render: a new position ends any show, and the squares
  // returned must keep their board piece hidden (an overlay piece stands in for it)
  function sync(k) {
    if (k !== key) { key = k; hide = new Set(); gen++; layer?.replaceChildren(); }
    return hide;
  }

  function setup(wrap, board, orient) {
    if (!layer || layer.parentNode !== wrap) { layer = document.createElement("div"); layer.className = "battle-layer"; wrap.append(layer); }
    const s = board.clientWidth / 8;
    Object.assign(layer.style, { left: `${board.offsetLeft}px`, top: `${board.offsetTop}px`, width: `${s * 8}px`, height: `${s * 8}px` });
    const xy = (sq) => {
      let x = FILES.indexOf(sq[0]), y = 8 - Number(sq[1]);
      if (orient === "black") { x = 7 - x; y = 7 - y; }
      return [x * s + s * 0.05, y * s + s * 0.05];
    };
    g = { s, xy, board, wrap };
  }

  // run keyframes on an overlay piece and keep its end state; a cancelled show skips it
  async function go(el, frames, ms, easing = "ease-in-out") {
    if (el._gen !== gen) return;
    const a = el.animate(frames, { duration: ms, easing, fill: "forwards" });
    await a.finished.catch(() => {});
    try { a.commitStyles(); } catch {}
    a.cancel();
  }

  function actor(src, sq) {
    const el = document.createElement("img");
    el.className = "battle-piece"; el.src = src; el.draggable = false; el._gen = gen;
    el.style.width = el.style.height = `${g.s * 0.9}px`;
    [el._x, el._y] = g.xy(sq);
    el.style.transform = T(el._x, el._y);
    layer.append(el);
    return el;
  }

  function hop(el, x, y, ms, lift, mid = "") {
    const f = [{ transform: T(el._x, el._y) }, { transform: T((el._x + x) / 2, (el._y + y) / 2 - lift, mid), offset: 0.5 }, { transform: T(x, y) }];
    el._x = x; el._y = y;
    return go(el, f, ms);
  }
  function slide(el, x, y, ms, a = "", b = "", easing) {
    const f = [{ transform: T(el._x, el._y, a) }, { transform: T(x, y, b) }];
    el._x = x; el._y = y;
    return go(el, f, ms, easing);
  }
  const squash = (el) => go(el, [{ transform: T(el._x, el._y) }, { transform: T(el._x, el._y + g.s * 0.06, "scale(1.18, .78)") }, { transform: T(el._x, el._y) }], 220);
  const wiggle = (el, deg = 8, ms = 360) => go(el, [0, -deg, deg, -deg, 0].map((d) => ({ transform: T(el._x, el._y, `rotate(${d}deg)`) })), ms);
  const center = (el) => [el._x + g.s * 0.45, el._y + g.s * 0.45];

  // squares a sliding piece steps on, not counting where it starts
  function path(from, to) {
    const f0 = FILES.indexOf(from[0]), r0 = Number(from[1]);
    const df = FILES.indexOf(to[0]) - f0, dr = Number(to[1]) - r0;
    const n = Math.max(Math.abs(df), Math.abs(dr));
    return Array.from({ length: n }, (_, i) => FILES[f0 + Math.sign(df) * (i + 1)] + (r0 + Math.sign(dr) * (i + 1)));
  }

  async function walk(el, from, to, type, stopShort = false) {
    if (type === "n") { const [x, y] = g.xy(to); await hop(el, x, y, 520, g.s * 1.1); await squash(el); return; }
    const steps = path(from, to).slice(0, stopShort ? -1 : undefined);
    const ms = Math.min(200, 1100 / Math.max(steps.length, 1));
    for (const [i, sq] of steps.entries()) {
      const [x, y] = g.xy(sq);
      await hop(el, x, y, ms, g.s * 0.14, `rotate(${i % 2 ? -7 : 7}deg)`);
    }
  }

  // ---------------------------------------------------------- effects
  function fx(cls, text, x, y, size) {
    const el = document.createElement("div");
    el.className = cls; el.textContent = text;
    el.style.left = `${x}px`; el.style.top = `${y}px`; el.style.fontSize = `${size}px`;
    layer.append(el);
    return el;
  }
  const fade = (el, frames, ms, easing = "ease-out") =>
    el.animate(frames, { duration: ms, easing, fill: "forwards" }).finished.then(() => el.remove(), () => {});

  function pow(x, y) {
    const el = fx("battle-pow", WORDS[Math.floor(Math.random() * WORDS.length)], x, y, g.s * 0.34);
    const tr = (sc, dy = -50) => `translate(-50%, ${dy}%) scale(${sc}) rotate(-8deg)`;
    fade(el, [{ transform: tr(0.2), opacity: 1 }, { transform: tr(1.2), opacity: 1, offset: 0.2 }, { transform: tr(1), opacity: 1, offset: 0.75 },
      { transform: tr(1.1, -90), opacity: 0 }], 950);
  }
  function burst(x, y, n, char, cls, dist) {
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2 + Math.random() * 0.5, d = dist * (0.7 + Math.random() * 0.5);
      const el = fx(cls, char, x, y, g.s * 0.22);
      fade(el, [{ transform: "translate(-50%, -50%) scale(.4)", opacity: 1 },
        { transform: `translate(calc(-50% + ${Math.cos(a) * d}px), calc(-50% + ${Math.sin(a) * d}px)) scale(1) rotate(${Math.random() * 360}deg)`, opacity: 0 }],
      650 + Math.random() * 250, "cubic-bezier(.2, .7, .3, 1)");
    }
  }
  const shake = (amt) => g.wrap.animate([0, -amt, amt, -amt / 2, amt / 2, 0].map((d, i) => ({ transform: `translate(${d}px, ${i % 2 ? d / 2 : -d / 2}px)` })), { duration: 320 });

  // ---------------------------------------------------------- the fight
  // each piece attacks its own way; returns true if the victim got stomped flat
  async function attack(att, vic, type) {
    const s = g.s, vx = vic._x, vy = vic._y;
    const dx = vx - att._x, dy = vy - att._y, len = Math.hypot(dx, dy) || 1;
    const hx = vx - (dx / len) * s * 0.42, hy = vy - (dy / len) * s * 0.42;  // touching the victim
    await Promise.all([wiggle(att), wiggle(vic, 12, 420)]);  // face-off
    if (type === "n") { await hop(att, vx, vy - s * 0.3, 480, s * 1.0); return true; }
    if (type === "p") await hop(att, hx, hy, 300, s * 0.35);
    else if (type === "b") await slide(att, hx, hy, 480, "rotate(0deg)", "rotate(720deg)", "ease-in");
    else if (type === "r") {
      await slide(att, att._x, att._y - s * 0.45, 300, "", "scale(1.15)", "ease-out");
      await slide(att, hx, hy, 170, "scale(1.15)", "", "ease-in");
    } else if (type === "q") {
      burst(...center(att), 8, "✦", "battle-spark", s * 0.7);
      await wait(260);
      await slide(att, hx, hy, 200, "", "", "ease-in");
    } else {  // king: slow wind-up, then a bonk
      await slide(att, att._x - (dx / len) * s * 0.15, att._y - (dy / len) * s * 0.15, 380, "", "rotate(-14deg)");
      await slide(att, hx, hy, 260, "rotate(-14deg)", "rotate(8deg)", "ease-in");
    }
    return false;
  }

  async function knockout(att, vic, type, stomped, sound) {
    const s = g.s, [cx, cy] = center(vic);
    sound("capture");
    pow(cx, cy - s * 0.2);
    burst(cx, cy, 7, "★", "battle-star", s * 0.8);
    shake(type === "r" || type === "n" ? 9 : 4);
    if (stomped) {
      await go(vic, [{ transform: T(vic._x, vic._y) }, { transform: T(vic._x, vic._y + s * 0.38, "scale(1.35, .12)") }], 160, "ease-in");
      await go(vic, [{ transform: T(vic._x, vic._y + s * 0.38, "scale(1.35, .12)"), opacity: 1 }, { transform: T(vic._x, vic._y + s * 0.38, "scale(1.5, .05)"), opacity: 0 }], 380);
      return;
    }
    const dx = vic._x - att._x, dy = vic._y - att._y, len = Math.hypot(dx, dy) || 1;
    burst(cx, cy, 5, "", "battle-puff", s * 0.45);
    await go(vic, [{ transform: T(vic._x, vic._y), opacity: 1 },
      { transform: T(vic._x + (dx / len) * s * 1.3, vic._y + (dy / len) * s * 1.3 - s * 0.8, "rotate(540deg) scale(.2)"), opacity: 0 }], 650, "ease-out");
  }

  // ---------------------------------------------------------- one move
  // o: {key, wrap, board, orient, from, to, piece, victim, victimSq, castle, promo, kingSq, san, end, dragged, img, sound}
  async function play(o) {
    sync(o.key);
    setup(o.wrap, o.board, o.orient);
    const my = gen, alive = () => my === gen;
    const boardImg = (sq) => o.board.querySelector(`.sq[data-sq=${sq}] img`);
    const hideSq = (sq) => { hide.add(sq); const im = boardImg(sq); if (im) im.style.visibility = "hidden"; };
    const showSq = (sq) => { if (!alive()) return; hide.delete(sq); const im = boardImg(sq); if (im) im.style.visibility = ""; };
    const type = o.piece.toLowerCase();

    hideSq(o.to);
    if (o.castle) hideSq(o.castle.to);
    const att = actor(o.img(o.piece), o.dragged ? o.to : o.from);
    const vic = o.victim ? actor(o.img(o.victim), o.victimSq) : null;

    if (vic) {
      let stomped = false;
      if (!o.dragged) {
        if (type !== "n") await walk(att, o.from, o.to, type, true);
        if (!alive()) return;
        stomped = await attack(att, vic, type);
      }
      if (!alive()) return;
      await knockout(att, vic, type, stomped, o.sound);
      if (!alive()) return;
      vic.remove();
      const [x, y] = g.xy(o.to);
      if (stomped) { await slide(att, x, y, 150, "", "", "ease-in"); await squash(att); }
      else if (!o.dragged) await hop(att, x, y, 260, g.s * 0.15);
      await hop(att, x, y, 300, g.s * 0.3);  // victory hop
    } else {
      if (!o.dragged) await walk(att, o.from, o.to, type);
      if (!alive()) return;
      o.sound("move");
      if (o.castle) {
        const rook = actor(o.img(o.castle.piece), o.castle.from);
        await walk(rook, o.castle.from, o.castle.to, "r");
        showSq(o.castle.to);
        rook.remove();
      }
    }
    if (!alive()) return;
    if (o.promo) {
      burst(...center(att), 10, "✦", "battle-spark", g.s * 0.75);
      await wait(200);
      att.src = o.img(o.promo);
      await squash(att);
    }
    showSq(o.to);
    att.remove();
    if (!alive() || !o.kingSq) { if (o.end) o.sound("end"); return; }

    const [kx, ky] = g.xy(o.kingSq), s = g.s;
    if (o.san.includes("#")) {  // checkmate: the king wobbles and falls over (and stays down)
      o.sound("end");
      hideSq(o.kingSq);
      const king = actor(boardImg(o.kingSq)?.src || "", o.kingSq);
      await wiggle(king, 14, 600);
      await go(king, [{ transform: T(kx, ky) }, { transform: T(kx + s * 0.12, ky + s * 0.22, "rotate(90deg)") }], 650, "cubic-bezier(.5, 0, .9, .6)");
      await go(king, [{ transform: T(kx + s * 0.12, ky + s * 0.22, "rotate(90deg)") }, { transform: T(kx + s * 0.12, ky + s * 0.16, "rotate(84deg)"), offset: 0.4 },
        { transform: T(kx + s * 0.12, ky + s * 0.22, "rotate(90deg)") }], 260);
    } else if (o.san.includes("+")) {  // check: the king gets a fright
      o.sound(o.end ? "end" : "check");
      const bang = fx("battle-bang", "!", kx + s * 0.62, ky - s * 0.02, s * 0.5);
      fade(bang, [{ transform: "translate(-50%, -50%) scale(.2)", opacity: 1 }, { transform: "translate(-50%, -50%) scale(1.2)", opacity: 1, offset: 0.2 },
        { transform: "translate(-50%, -50%) scale(1)", opacity: 1, offset: 0.8 }, { transform: "translate(-50%, -70%) scale(1)", opacity: 0 }], 1100);
      boardImg(o.kingSq)?.animate([0, -10, 10, -10, 10, 0].map((d) => ({ transform: `rotate(${d}deg)` })), { duration: 500 });
      await wait(500);
    } else if (o.end) o.sound("end");
  }

  return { ok, sync, play };
})();

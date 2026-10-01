"""Game recap: a move-by-move review of a finished game. Stockfish grades every move and works out what was
better and why (app/coach.py); an LLM writes one or two calm, child-friendly paragraphs per move from those facts,
plus a summary with the turning points and lessons. Recaps are stored per game and language."""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import chess
import httpx

from app import coach, danish

JOBS: dict[tuple[str, str], asyncio.Task] = {}
PARALLEL_LLM = 3  # LLM calls in flight at once (Stockfish analysis itself is sequential)
LABELS = {"best": "best move", "good": "good move", "inaccuracy": "inaccuracy", "mistake": "mistake", "blunder": "blunder"}

MOVE_PROMPT = """You are a calm, clear chess coach going over a finished game with a child (about 6-12 years old). \
Write about ONE move. Use 1-2 short paragraphs (3-6 sentences in total) in simple words:
- what the move did on the board and what the engine thinks of it (the verdict),
- WHY, concretely (what it took, attacked, defended, left unprotected, or allowed),
- if it wasn't the best: what was better and why, and what could have happened instead,
- one small lesson, if it fits.
Explain chess words (fork, pin, castle) in a few words. {reader} Start directly with the move - no heading, no \
"here is the explanation", no role-play, no greetings, at most one emoji. Use only the ENGINE FACTS - never invent \
moves or pieces. {lang}"""

SUMMARY_PROMPT = """You are a calm, clear chess coach summing up a finished game for a child (about 6-12 years old). \
Write 1-2 short paragraphs about how the game went (the opening, the turning points, how it was decided), then \
exactly three short lessons as a list starting with "- ". Simple words, encouraging, no role-play, at most one emoji. \
{reader} Use only the facts. {lang}"""


def main():
    return sys.modules["app.main"]


def reader_side(types: dict) -> Optional[str]:
    return next((s for s in ("white", "black") if types.get(s) == "human"), None)


def reader_line(names: dict, types: dict) -> str:
    """Who "you" is: the (first) human player; everyone else is called by name."""
    humans = [s for s in ("white", "black") if types.get(s) == "human"]
    if not humans:
        return f"Call the players by name: {names['white']} (White) and {names['black']} (Black)."
    me = humans[0]
    other = "black" if me == "white" else "white"
    return (f"The reader is {names[me]}, who played {me.capitalize()}: talk to them as \"you\" and call the other "
            f"player {names[other]} ({other.capitalize()}) by name, never \"you\".")


def lang_line(lang: str) -> str:
    return coach.TEXT.get(lang, coach.TEXT["en"])["lang"]


def recap_path(gid: str, lang: str) -> Path:
    d = main().DATA_DIR / "recaps"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{gid}-{lang}.json"


def load(gid: str, lang: str) -> Optional[dict]:
    try:
        return json.loads(recap_path(gid, lang).read_text())
    except FileNotFoundError:
        return None


def save(gid: str, lang: str, data: dict):
    p = recap_path(gid, lang)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(p)


def game_data(gid: str) -> Optional[dict]:
    m = main()
    if gid in m.GAMES:
        return m.GAMES[gid].state()
    return m.ARCHIVE.get(gid)


def parse_line(board: chess.Board, sans: list[str]) -> list[chess.Move]:
    b, out = board.copy(), []
    for san in sans:
        try:
            mv = b.parse_san(san)
        except ValueError:
            break
        out.append(mv)
        b.push(mv)
    return out


async def move_facts(board: chess.Board, mv: chess.Move, names: dict, reader: Optional[str] = None) -> dict:
    """Engine facts for one move of the game (played by the side to move in `board`)."""
    color = board.turn
    who = names["white" if color else "black"]
    other = names["black" if color else "white"]
    r = await coach.review(board, mv, color)
    reasons, _ = coach.move_reasons(board, mv, color)
    side = "white" if color else "black"
    voice = ("This move was played by the reader - write \"you\"." if reader == side else
             f"This move was played by {who}, not the reader - write \"{who}\" (third person), never \"you\"."
             if reader else "")
    lines = [f"Move {board.fullmove_number}{'.' if color else '...'} {who} ({'White' if color else 'Black'}) "
             f"played {r['words']}. {voice}".strip(),
             f"Engine verdict: {LABELS[r['cls']]} (evaluation for {who} before: {r['eval_before']}, after: {r['eval_after']})"]
    if reasons:
        lines.append("What the move did: " + "; ".join(reasons))
    if r["motifs"]:
        lines.append("Problems with it: " + "; ".join(r["motifs"]))
    if r["reply"] and r["cls"] in ("inaccuracy", "mistake", "blunder"):
        lines.append(f"What {other} could do now: {r['reply']['words']} (line: {' '.join(r['reply']['line'])})")
    best = None
    if r["better"]:
        b0 = r["better"][0]
        pv = parse_line(board, b0["line"])
        best = pv[0] if pv else None
        if best is not None and r["cls"] not in ("best",):
            why, _ = coach.move_reasons(board, best, color)
            lines.append(f"Better was {b0['words']} ({b0['eval']})" + (f": {'; '.join(why)}" if why else ""))
            nxt = coach.follow_up(board, pv, color)
            if nxt:
                lines.append(f"What could have happened then: {nxt}")
    after = board.copy()
    after.push(mv)
    if after.is_checkmate():
        lines.append("This move is checkmate - the game is over.")
    opening = main().opening_of(after)
    arrows = [a for a in r["arrows"] if a["color"] != "green"][:2]
    if best is not None and r["cls"] in ("inaccuracy", "mistake", "blunder"):
        arrows.append({"from": chess.square_name(best.from_square), "to": chess.square_name(best.to_square),
                       "color": "green"})
    return {"ply": len(board.move_stack), "side": "white" if color else "black", "san": r["san"], "cls": r["cls"],
            "facts": "\n".join(f"- {l}" for l in lines), "arrows": arrows, "squares": r["squares"],
            "better": r["better"][0]["san"] if r["better"] and r["cls"] != "best" else None,
            "opening": opening, "swing": r["loss"]}


async def llm(endpoint: str, model: str, system: str, user: str, max_tokens: int) -> Optional[str]:
    m = main()
    base = m.ENDPOINTS.get(endpoint, endpoint)
    body = {"model": model, "temperature": 0.4, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10)) as client:
                r = await client.post(base.rstrip("/") + "/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {m.API_KEY}"})
                r.raise_for_status()
                text = m.THINK_RE.sub("", r.json()["choices"][0]["message"].get("content") or "").strip()
                if text:
                    return danish.fix(text) if coach.TEXT["da"]["lang"] in system else text
        except Exception as e:
            print(f"recap LLM call failed: {e!r}", file=sys.stderr, flush=True)
            await asyncio.sleep(2)
    return None


def pick_llm() -> tuple[str, str]:
    """The first endpoint that is online (the no-think one is listed first), with its first model."""
    m = main()
    for ep in m.ENDPOINTS:
        mids = [mid for (e, mid) in m.MODEL_KEYS if e == ep]
        if mids:
            return ep, mids[0]
    return next(iter(m.ENDPOINTS), ""), "currentmodel"


async def build(gid: str, lang: str):
    d = game_data(gid)
    board = chess.Board(d["opts"].get("start_fen") or chess.STARTING_FEN)
    moves = [chess.Move.from_uci(u) for u in d["uci"]]
    names = {"white": d["white"]["label"], "black": d["black"]["label"]}
    types = {"white": d["white"]["type"], "black": d["black"]["type"]}
    reader, rside = reader_line(names, types), reader_side(types)
    await asyncio.gather(*(main().probe_endpoint(n, u) for n, u in main().ENDPOINTS.items()))
    endpoint, model = pick_llm()
    data = load(gid, lang) or {}
    have = data.get("moves") or []
    data.update(id=gid, lang=lang, status="running", total=len(moves), summary=None, created=time.time(), model=model,
                moves=[(have[i] if i < len(have) and have[i] and have[i].get("text") else None) for i in range(len(moves))])
    save(gid, lang, data)
    sem = asyncio.Semaphore(PARALLEL_LLM)
    system = MOVE_PROMPT.format(lang=lang_line(lang), reader=reader)

    async def write(i: int, f: dict):
        async with sem:
            text = await llm(endpoint, model, system, "ENGINE FACTS:\n" + f["facts"] + "\n\nWrite about this move.", 450)
        entry = {k: v for k, v in f.items() if k != "facts"}
        entry["text"] = text or ""
        data["moves"][i] = entry
        save(gid, lang, data)

    facts, writers = [], []
    for i, mv in enumerate(moves):
        if data["moves"][i]:  # already written during the game
            facts.append(data["moves"][i])
        else:
            f = await move_facts(board, mv, names, rside)  # sequential: one Stockfish
            facts.append(f)
            writers.append(asyncio.create_task(write(i, f)))
        board.push(mv)
    await asyncio.gather(*writers)

    # summary: opening, turning points, mistake counts, result
    counts = {s: {c: 0 for c in ("blunder", "mistake", "inaccuracy")} for s in ("white", "black")}
    for f in facts:
        if f["cls"] in counts[f["side"]]:
            counts[f["side"]][f["cls"]] += 1
    turning = sorted([f for f in facts if f["cls"] in ("mistake", "blunder")], key=lambda f: -f["swing"])[:3]
    opening = next((f["opening"] for f in reversed(facts[:20]) if f["opening"]), None)
    lines = [f"Result: {d['result']} ({d['termination']})", f"White: {names['white']}, Black: {names['black']}"]
    if opening:
        lines.append(f"Opening: {opening}")
    for s in ("white", "black"):
        c = counts[s]
        lines.append(f"{names[s]}: {c['blunder']} blunders, {c['mistake']} mistakes, {c['inaccuracy']} inaccuracies")
    for f in sorted(turning, key=lambda f: f["ply"]):
        lines.append(f"Turning point: move {f['ply'] // 2 + 1}, {names[f['side']]} played {f['san']} ({f['cls']})"
                     + (f", {f['better']} was better" if f["better"] else ""))
    summary = await llm(endpoint, model, SUMMARY_PROMPT.format(lang=lang_line(lang), reader=reader),
                        "FACTS:\n" + "\n".join(f"- {l}" for l in lines), 600)
    data.update(summary=summary or "", counts=counts, opening=opening, status="done",
                turning=[f["ply"] for f in turning])
    save(gid, lang, data)


# ---- written during the game: each move's entry is prepared in a quiet moment, so at the end the recap only
# has to fill in what's missing (and the summary)

LIVE: dict[str, asyncio.Task] = {}


def on_move(game):
    """Called after every move of a live game with a human player."""
    if "human" not in (game.white.type, game.black.type):
        return
    task = LIVE.get(game.id)
    if not task or task.done():
        LIVE[game.id] = coach.background(live_worker(game))


async def live_worker(game):
    names = {"white": game.white.label(), "black": game.black.label()}
    types = {"white": game.white.type, "black": game.black.type}
    reader, rside = reader_line(names, types), reader_side(types)
    endpoint, model = pick_llm()
    quiet = main().QUIET_BEFORE_LLM
    while game.status == "running":
        await asyncio.sleep(1)
        if time.time() - game.last_move_at < quiet:
            continue  # the players are moving: don't compete with the chat / teacher
        # quiet: catch up on as many moves as possible until someone moves again
        while game.status == "running" and time.time() - game.last_move_at >= quiet:
            lang = game.opts.lang if game.opts.lang in ("en", "da") else "en"
            data = load(game.id, lang) or {"id": game.id, "lang": lang, "status": "live", "moves": [], "summary": None}
            stack = list(game.board.move_stack)
            moves = (data.get("moves") or []) + [None] * max(0, len(stack) - len(data.get("moves") or []))
            missing = [i for i in range(len(stack)) if not (moves[i] and moves[i].get("text"))]
            if not missing:
                return  # all caught up; the next move starts a new worker
            i = missing[0]
            board = chess.Board(game.opts.start_fen) if game.opts.start_fen else chess.Board()
            for mv in stack[:i]:
                board.push(mv)
            f = await move_facts(board, stack[i], names, rside)
            text = await llm(endpoint, model, MOVE_PROMPT.format(lang=lang_line(lang), reader=reader),
                             "ENGINE FACTS:\n" + f["facts"] + "\n\nWrite about this move.", 450)
            if not text:
                return
            data = load(game.id, lang) or data  # re-read: a finished-game build may have started meanwhile
            data.setdefault("moves", [])
            data["moves"] += [None] * (i + 1 - len(data["moves"]))
            if not (data["moves"][i] and data["moves"][i].get("text")):
                data["moves"][i] = {**{k: v for k, v in f.items() if k != "facts"}, "text": text}
                data.setdefault("status", "live")
                save(game.id, lang, data)


def start(gid: str, lang: str) -> dict:
    existing = load(gid, lang)
    job = JOBS.get((gid, lang))
    if existing and (existing.get("status") == "done" or (job and not job.done())):
        return existing
    live = LIVE.get(gid)
    if live and not live.done():
        live.cancel()  # the full build takes over (it keeps what the live worker wrote)
    placeholder = {**(existing or {}), "id": gid, "lang": lang, "status": "running",
                   "total": len(game_data(gid)["uci"]), "moves": (existing or {}).get("moves") or [], "summary": None}
    save(gid, lang, placeholder)  # so the page sees "running" right away
    JOBS[(gid, lang)] = coach.background(build(gid, lang))
    return placeholder

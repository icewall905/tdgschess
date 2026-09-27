"""Learning mode: Stockfish works out the facts, an LLM teacher explains them to a child (Danish or English).

Every chess claim the teacher makes comes from the ENGINE FACTS block built here; the LLM only phrases it.
"""

import asyncio
import math
import re
import sys
import time
from typing import Optional

import chess
import chess.engine
import httpx

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook",
         chess.QUEEN: "queen", chess.KING: "king"}
BLUNDER, MISTAKE, INACCURACY = 0.30, 0.18, 0.10  # win-probability loss, same thresholds as the move list marks
ANALYSE_SECONDS = 0.3

TEXT = {
    "en": {"undone": "↩️ Move taken back — try again!", "kept": "OK, we keep that move. Let's see what happens!",
           "away": "The teacher is taking a short break — try again in a moment! ☕",
           "lang": "Always answer in English."},
    "da": {"undone": "↩️ Trækket er taget tilbage — prøv igen!", "kept": "OK, vi beholder trækket. Lad os se, hvad der sker!",
           "away": "Læreren holder en lille pause — prøv igen om lidt! ☕",
           "lang": "Svar altid på dansk (Danish). Brug danske skaknavne: bonde, springer, løber, tårn, dronning, konge."},
}


def main():
    """app.main, imported lazily because it imports this module."""
    return sys.modules["app.main"]


def win_frac(cp: Optional[int], mate: Optional[int]) -> float:
    if mate is not None:
        return 1.0 if mate > 0 else 0.0
    return 1 / (1 + math.exp(-(cp or 0) / 250))


def piece_on(board: chess.Board, sq: int) -> str:
    p = board.piece_at(sq)
    return f"{NAMES[p.piece_type]} on {chess.square_name(sq)}" if p else chess.square_name(sq)


def move_words(board: chess.Board, mv: chess.Move) -> str:
    return main().move_words(board, mv)


def eval_words(cp: Optional[int], mate: Optional[int]) -> str:
    return main().eval_words(cp or 0, mate)


def in_danger(board: chess.Board, color: bool, min_value: int = 1) -> list[dict]:
    """Pieces of `color` that are attacked and undefended, or attacked by a cheaper piece."""
    out = []
    for sq, p in board.piece_map().items():
        if p.color != color or p.piece_type == chess.KING or VALUES[p.piece_type] < min_value:
            continue
        attackers = board.attackers(not color, sq)
        if not attackers:
            continue
        defended = bool(board.attackers(color, sq))
        cheapest = min(VALUES[board.piece_type_at(a)] or 100 for a in attackers)
        if not defended or cheapest < VALUES[p.piece_type]:
            out.append({"square": chess.square_name(sq), "piece": NAMES[p.piece_type], "value": VALUES[p.piece_type],
                        "by": [piece_on(board, a) for a in attackers], "defended": defended})
    return sorted(out, key=lambda d: -d["value"])


class CoachEngine:
    """Its own Stockfish (full strength) for learning mode; one shared process, calls queue on a lock."""

    def __init__(self):
        self.lazy = None  # main.LazyEngine, created on first use (main imports this module)
        self.lock = asyncio.Lock()

    async def top(self, board: chess.Board, n: int = 3, seconds: float = ANALYSE_SECONDS) -> list[dict]:
        """Best moves for the side to move, scores from that side's point of view."""
        if board.is_game_over():
            return []
        async with self.lock:
            self.lazy = self.lazy or main().LazyEngine({"Threads": 2, "Hash": 32})
            try:
                engine = await self.lazy.acquire()
                infos = await engine.analyse(board, chess.engine.Limit(time=seconds),
                                             multipv=min(n, board.legal_moves.count()))
                self.lazy.release()
            except Exception:
                await self.lazy.close()
                return []
        out = []
        for info in infos:
            pv = info.get("pv") or []
            if not pv:
                continue
            b, line = board.copy(), []
            for mv in pv[:5]:
                line.append(b.san(mv))
                b.push(mv)
            score = info["score"].pov(board.turn)
            out.append({"move": pv[0], "san": line[0], "words": move_words(board, pv[0]), "line": line,
                        "cp": score.score(mate_score=100000), "mate": score.mate()})
        return out


ENGINE = CoachEngine()


async def analyse(board: chess.Board, color: bool) -> dict:
    """Everything the teacher may say about the position, for the learner playing `color`."""
    kid_to_move = board.turn == color
    top = await ENGINE.top(board)
    ev = top[0] if top else {"cp": 0, "mate": None}
    cp, mate = (ev["cp"], ev["mate"]) if kid_to_move else (-(ev["cp"] or 0), -ev["mate"] if ev["mate"] else None)
    threat = None
    if kid_to_move and not board.is_check():
        nb = board.copy(stack=False)
        nb.push(chess.Move.null())
        t = await ENGINE.top(nb, 1, 0.15)
        if t:
            gain = (t[0]["cp"] or 0) + (cp or 0)  # opponent's score after a free move vs the kid's current score
            if t[0]["mate"] and t[0]["mate"] > 0 or gain > 150:
                threat = {"san": t[0]["san"], "words": t[0]["words"], "mate": t[0]["mate"]}
    mat = {c: sum(VALUES[p.piece_type] for p in board.piece_map().values() if p.color == c) for c in (True, False)}
    return {
        "kid_to_move": kid_to_move, "cp": cp, "mate": mate, "eval": eval_words(cp, mate),
        "top": top if kid_to_move else [], "opponent_plan": top[0]["line"] if top and not kid_to_move else None,
        "threat": threat, "danger": in_danger(board, color), "targets": in_danger(board, not color),
        "material": mat[color] - mat[not color], "overview": main().game_overview(board, color),
    }


async def review(board: chess.Board, move: chess.Move, color: bool) -> dict:
    """How good was `move` (played by `color` in `board`)? Includes what it allows and better options."""
    best = await ENGINE.top(board)
    after = board.copy()
    after.push(move)
    reply = await ENGINE.top(after, 1)
    b_cp, b_mate = (best[0]["cp"], best[0]["mate"]) if best else (0, None)
    if after.is_checkmate():
        a_cp, a_mate = 100000, 1
    elif reply:
        a_cp, a_mate = -(reply[0]["cp"] or 0), (-reply[0]["mate"] if reply[0]["mate"] else None)
    else:
        a_cp, a_mate = 0, None  # stalemate / draw
    loss = max(0.0, win_frac(b_cp, b_mate) - win_frac(a_cp, a_mate))
    is_best = bool(best) and best[0]["move"] == move
    cls = ("best" if is_best else "blunder" if loss >= BLUNDER else "mistake" if loss >= MISTAKE
           else "inaccuracy" if loss >= INACCURACY else "good")

    motifs, squares = [], []
    lost = [d for d in in_danger(after, color, 3)]
    if lost and cls in ("blunder", "mistake"):
        d = lost[0]
        motifs.append(f"the {d['piece']} on {d['square']} can be taken ({'undefended' if not d['defended'] else 'attacked by a cheaper piece'})")
        squares.append(d["square"])
    r = reply[0] if reply else None
    if r:
        if r["mate"] and r["mate"] > 0:
            motifs.append(f"the opponent can checkmate in {r['mate']} (starting with {r['words']})")
        rb = after.copy()
        rb.push(r["move"])
        forked = [sq for sq in rb.attacks(r["move"].to_square)
                  if rb.piece_at(sq) and rb.piece_at(sq).color == color
                  and (VALUES[rb.piece_type_at(sq)] >= 3 or rb.piece_type_at(sq) == chess.KING)]
        if len(forked) >= 2:
            motifs.append(f"the reply {r['san']} attacks two pieces at once (a fork): "
                          + " and ".join(piece_on(rb, s) for s in forked))
            squares += [chess.square_name(s) for s in forked]
    if b_mate and b_mate > 0 and not (a_mate and a_mate > 0):
        motifs.append(f"there was a checkmate: {best[0]['words']}")

    arrows = [{"from": chess.square_name(move.from_square), "to": chess.square_name(move.to_square),
               "color": "red" if cls in ("blunder", "mistake") else "blue"}]
    if r and cls in ("blunder", "mistake"):
        arrows.append({"from": chess.square_name(r["move"].from_square), "to": chess.square_name(r["move"].to_square),
                       "color": "orange"})
    better = [b for b in best if b["move"] != move][:2] if cls != "best" else []
    if cls in ("blunder", "mistake", "inaccuracy"):
        arrows += [{"from": chess.square_name(b["move"].from_square), "to": chess.square_name(b["move"].to_square),
                    "color": "green"} for b in better]
    return {
        "san": board.san(move), "words": move_words(board, move), "cls": cls, "loss": round(loss, 2),
        "eval_before": eval_words(b_cp, b_mate), "eval_after": eval_words(a_cp, a_mate),
        "cp_before": b_cp, "reply": {"san": r["san"], "words": r["words"], "line": r["line"]} if r else None,
        "better": [{"san": b["san"], "words": b["words"], "eval": eval_words(b["cp"], b["mate"]), "line": b["line"]}
                   for b in better],
        "motifs": motifs, "arrows": arrows, "squares": sorted(set(squares)),
    }


def facts_text(a: dict, who: str) -> str:
    m = a["material"]
    material = "even" if m == 0 else f"{who} is {abs(m)} points {'ahead' if m > 0 else 'behind'}"
    lines = ["ENGINE FACTS (from Stockfish; true — base every chess claim on these, never guess):",
             f"- Evaluation for {who}: {a['eval']}",
             f"- Material: {material}"]
    if a["top"]:
        lines.append(f"- Best moves for {who} now: " + "; ".join(
            f"{i + 1}. {t['words']} ({eval_words(t['cp'], t['mate'])}), plan: {' '.join(t['line'])}"
            for i, t in enumerate(a["top"])))
    if a.get("opponent_plan"):
        lines.append(f"- The computer's plan: {' '.join(a['opponent_plan'])}")
    if a["threat"]:
        lines.append(f"- Threat: if {who} ignores it, the opponent would play {a['threat']['words']}")
    if a["danger"]:
        lines.append(f"- {who}'s pieces in danger: " + "; ".join(
            f"{d['piece']} on {d['square']} (attacked by {', '.join(d['by'])}{'' if d['defended'] else ', not defended'})"
            for d in a["danger"]))
    if a["targets"]:
        lines.append(f"- Opponent pieces {who} could win: " + "; ".join(
            f"{d['piece']} on {d['square']}" for d in a["targets"]))
    lines.append(a["overview"])
    return "\n".join(lines)


def review_text(r: dict, who: str) -> str:
    lines = [f"- {who} played {r['words']}. Engine verdict: {r['cls']} (eval before {r['eval_before']}, after {r['eval_after']})"]
    if r["reply"] and r["cls"] in ("blunder", "mistake", "inaccuracy"):
        lines.append(f"- The opponent's best answer: {r['reply']['words']} (line: {' '.join(r['reply']['line'])})")
    for m in r["motifs"]:
        lines.append(f"- Why: {m}")
    if r["better"]:
        lines.append("- Better moves were: " + "; ".join(f"{b['words']} ({b['eval']})" for b in r["better"]))
    return "\n".join(lines)


SYSTEM = """You are a warm, patient chess teacher for a child about 6-12 years old{name}. The child plays {color} \
against a computer. You explain what is happening and WHY, in simple words: threats, keeping pieces safe, the \
centre, developing pieces, king safety, making good trades.

Rules:
- Every claim about moves, threats or who is better must come from the ENGINE FACTS you are given. If the facts \
don't say it, don't claim it. Never invent moves or pieces.
- Name pieces and squares in words ("your knight on f3"), not chess notation soup.
- Short, friendly sentences. Encourage the child; mistakes are how we learn. No scary or mean words.
- {lang}"""


def teacher_of(game) -> tuple[str, str, str]:
    o = game.opts
    ep = o.teacher_endpoint or next(iter(main().ENDPOINTS), "")
    return main().ENDPOINTS.get(ep, ep), o.teacher_model or "currentmodel", o.lang if o.lang in TEXT else "en"


def learner(game) -> tuple[bool, str]:
    """The learner's colour and a label for prompts ("the child (White)")."""
    color = chess.WHITE if game.white.type == "human" else chess.BLACK
    spec = game.white if color else game.black
    return color, f"{spec.name or 'the child'} ({'White' if color else 'Black'})"


async def llm(game, user: str, max_tokens: int = 350) -> Optional[str]:
    """The teacher's answer, or None if the endpoint is unavailable (callers decide what the child sees)."""
    base, model, lang = teacher_of(game)
    color, who = learner(game)
    spec = game.white if color else game.black
    system = SYSTEM.format(name=f" named {spec.name}" if spec.name else "", color="White" if color else "Black",
                           lang=TEXT[lang]["lang"])
    # thinking off: the facts are already worked out, and reasoning models otherwise spend the whole budget thinking
    body = {"model": model, "temperature": 0.5, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    text = ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            for attempt in range(2):
                r = await client.post(base.rstrip("/") + "/chat/completions", json=body,
                                      headers={"Authorization": f"Bearer {main().API_KEY}"})
                r.raise_for_status()
                text = main().THINK_RE.sub("", r.json()["choices"][0]["message"].get("content") or "").strip()
                if text:
                    break
                body["max_tokens"] = max_tokens * 4  # still empty: the model must have thought anyway
    except Exception as e:
        print(f"teacher LLM unavailable: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return None
    return text or None


def away(game) -> str:
    return TEXT[teacher_of(game)[2]]["away"]


def conversation(game, n: int = 6) -> str:
    items = [e for e in game.log if e.get("kind") in ("ask", "teach") and e.get("say")][-n:]
    if not items:
        return ""
    return "Recent conversation:\n" + "\n".join(
        f"{'Child' if e['kind'] == 'ask' else 'Teacher'}: {e['say']}" for e in items)


def add_teach(game, text: str, arrows=None, squares=None, **extra):
    game.add_log({"kind": "teach", "ply": len(game.board.move_stack), "say": text, "arrows": arrows or [],
                  "squares": squares or [], "t": time.time(), **extra})


TASKS: set = set()


def background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    TASKS.add(task)

    def done(t):
        TASKS.discard(t)
        if not t.cancelled() and t.exception():
            print(f"learning mode task failed: {t.exception()!r}", file=sys.stderr, flush=True)

    task.add_done_callback(done)
    return task


def explain_latest(game, coro):
    """Per-move explanations never queue up: a newer one replaces one that hasn't finished, so a fast player
    always gets the teacher's view of the current position and the LLM endpoint gets at most one call per game."""
    old = getattr(game, "teach_task", None)
    if old and not old.done():
        old.cancel()

    async def run():
        game.teach_busy = True
        game.touch()
        try:
            await coro
        finally:
            game.teach_busy = False
            game.touch()

    game.teach_task = background(run())


async def explain_turn(game, kid: Optional[tuple], comp: Optional[tuple]):
    """One explanation per turn: the child's move (board, move, review) and the computer's reply (board, move)."""
    color, who = learner(game)
    facts, parts, about, arrows = [], [], [], []
    kid_rev = None
    if kid:
        kb, kmv, kid_rev = kid
        kid_rev = kid_rev or await review(kb, kmv, color)
        facts.append(review_text(kid_rev, who))
        about.append(kid_rev["san"])
        arrows += [a for a in kid_rev["arrows"] if a["color"] == "green"][:2]
        parts.append(f"say briefly whether the child's move {kid_rev['words']} was good (engine verdict: "
                     f"{kid_rev['cls']}; if not the best, gently name one better move)")
    if comp:
        cb, cmv = comp
        words = move_words(cb, cmv)
        facts.append(f"- The computer answered {words}")
        about.append(cb.san(cmv))
        parts.append(f"explain what the computer's answer {words} is trying to do")
    a = await analyse(game.board, color)
    parts.append("say what the child should look out for now")
    user = f"{chr(10).join(facts)}\n{facts_text(a, who)}\n\nIn 2-3 short sentences: " + "; then ".join(parts) + "."
    text = await llm(game, user, 300)
    if not text:
        return  # teacher unreachable: skip this turn quietly, the next one will try again
    add_teach(game, text, arrows, ply=len(game.board.move_stack), about=" · ".join(about),
              cls=kid_rev["cls"] if kid_rev else None, mover="turn")


async def explain_blunder(game, board: chess.Board, rev: dict):
    color, who = learner(game)
    a = await analyse(board, color)
    user = (f"{review_text(rev, who)}\n{facts_text(a, who)}\n\nThe child's move is a big mistake and the game is "
            "paused so they can take it back. In at most 3 short sentences: say kindly what goes wrong (what the opponent "
            "can now do), then suggest the better move(s) and why they are better.")
    text = await llm(game, user, 350) or away(game)  # the card still shows the arrows and better moves
    if game.review and game.review.get("ply") == len(board.move_stack):
        game.review["text"] = text
        game.touch()


async def hint(game) -> dict:
    color, who = learner(game)
    a = await analyse(game.board, color)
    user = (f"{facts_text(a, who)}\n\nThe child asks for a hint. First give a gentle nudge about what to look for "
            "(a threat, a piece in danger, a good plan). Then suggest 1-2 of the engine's best moves and explain "
            "simply why they are good. At most 3 short sentences.")
    text = await llm(game, user, 350) or away(game)
    arrows = [{"from": chess.square_name(t["move"].from_square), "to": chess.square_name(t["move"].to_square),
               "color": "green"} for t in a["top"][:2]]
    squares = [d["square"] for d in a["danger"][:2]]
    add_teach(game, text, arrows, squares, kind_detail="hint")
    return {"ok": True}


MOVE_TOKEN = re.compile(r"\b([KQRBNSDTLkqrbn]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBNqrbn])?|O-O(?:-O)?|0-0(?:-0)?)\b")
DA_PIECES = str.maketrans({"S": "N", "L": "B", "T": "R", "D": "Q"})  # Danish piece letters -> English


async def answer(game, question: str) -> dict:
    color, who = learner(game)
    game.add_log({"kind": "ask", "ply": len(game.board.move_stack), "say": question, "t": time.time()})
    a = await analyse(game.board, color)
    # "what if I play Nf3?": analyse moves the child mentions, so the teacher never has to judge them itself
    checked, board = [], game.board
    if board.turn == color:
        for tok in MOVE_TOKEN.findall(question)[:3]:
            for cand in (tok, tok.translate(DA_PIECES)):
                try:
                    mv = board.parse_san(cand.replace("0", "O"))
                except ValueError:
                    try:
                        mv = chess.Move.from_uci(cand.lower())
                        if mv not in board.legal_moves:
                            continue
                    except ValueError:
                        continue
                checked.append(await review(board, mv, color))
                break
    extra = "\n".join(review_text(r, who) for r in checked)
    user = (f"{facts_text(a, who)}\n{('Moves the child asked about (engine-checked):' + chr(10) + extra) if extra else ''}\n"
            f"{conversation(game)}\n\nThe child asks: \"{question}\"\nAnswer kindly and simply in 2-5 short sentences, "
            "using only the engine facts.")
    text = await llm(game, user, 450) or away(game)
    arrows = [x for r in checked for x in r["arrows"]][:4]
    add_teach(game, text, arrows, [s for r in checked for s in r["squares"]], reply_to=question)
    return {"ok": True}

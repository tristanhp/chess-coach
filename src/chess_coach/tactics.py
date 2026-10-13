"""Static tactics helpers: exchange simulation and en-prise detection (no engine)."""
import chess

VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
PIECE_WORD = {1: "pawn", 2: "knight", 3: "bishop", 4: "rook", 5: "queen", 6: "king"}
SEE_VALUE = {**VALUES, chess.KING: 100}


def _see(board: chess.Board, sq: int, depth: int = 0):
    """Net material the side to move wins by capturing on `sq` (exchange simulation). (net, [(san, victim)])."""
    caps = [m for m in board.legal_moves if m.to_square == sq and board.piece_type_at(sq)]
    if not caps or depth > 8:
        return 0, []
    m = min(caps, key=lambda m: SEE_VALUE[board.piece_type_at(m.from_square)])
    victim = board.piece_type_at(sq)
    san = board.san(m)
    b = board.copy(stack=False)
    b.push(m)
    sub, line = _see(b, sq, depth + 1)
    net = SEE_VALUE[victim] - sub
    if net <= 0:
        return 0, []
    return net, [(san, victim)] + (line if sub > 0 else [])


def _letters(types):
    return "+".join(sorted((chess.piece_symbol(t).upper() for t in types), key=lambda c: -"PNBRQ".index(c))) or "nothing"


def en_prise(board: chess.Board, owner: bool):
    """Pieces of `owner` that the side to move (the opponent) wins material on. board.turn must be the opponent."""
    out = []
    for sq, p in board.piece_map().items():
        if p.color != owner or p.piece_type == chess.KING:
            continue
        net, line = _see(board, sq)
        if net <= 0:
            continue
        att = board.attackers(not owner, sq)
        dfn = board.attackers(owner, sq)
        low_att = min(att, key=lambda s: SEE_VALUE[board.piece_type_at(s)])
        desc = (f"{chess.piece_symbol(p.piece_type).upper()} on {chess.square_name(sq)} attacked by "
                f"{PIECE_WORD[board.piece_type_at(low_att)]} {chess.square_name(low_att)} ({len(att)}x), defended {len(dfn)}x")
        if dfn:
            low_def = min(dfn, key=lambda s: SEE_VALUE[board.piece_type_at(s)])
            desc += f" (lowest: {chess.piece_symbol(board.piece_type_at(low_def)).upper()}{chess.square_name(low_def)})"
        lost = [v for i, (_, v) in enumerate(line) if i % 2 == 0]
        back = [v for i, (_, v) in enumerate(line) if i % 2 == 1]
        out.append({"sq": sq, "net": net,
                    "text": f"{desc}: loses {_letters(lost)} for {_letters(back)} after {line[0][0]} (net -{net})"})
    return sorted(out, key=lambda e: -e["net"])


def en_prise_after(board_before: chess.Board, move: chess.Move):
    """Pieces left en prise by `move`, each tagged if it was already hanging before the move."""
    mover = board_before.turn
    after = board_before.copy(stack=False)
    after.push(move)
    already = set()
    if not board_before.is_check():
        nb = board_before.copy(stack=False)
        nb.push(chess.Move.null())
        already = {e["sq"] for e in en_prise(nb, mover)}
    res = []
    for e in en_prise(after, mover):
        was = e["sq"] in already and e["sq"] != move.to_square
        piece = chess.piece_symbol(after.piece_type_at(e["sq"])).upper()
        res.append({**e, "was": was, "piece": piece, "name": piece + chess.square_name(e["sq"]),
                    "full": e["text"] + (" [was already hanging before this move]" if was else "")})
    return res

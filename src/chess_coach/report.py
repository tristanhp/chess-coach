"""Plain-text rendering (compact, token-light)."""
from collections import defaultdict

from .analysis import fmt_eval

MARK = {"inaccuracy": "?!", "mistake": "?", "blunder": "??", None: ""}
SHORT = {"opening": "open", "middlegame": "mid", "endgame": "end"}


def mmss(sec):
    if sec is None:
        return "?"
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def error_line(e, with_fen=True):
    flags = ",".join(e["flags"])
    s = (f"{e['n']}{MARK[e['cls']]} best {e['best'] or '-'} | "
         f"{fmt_eval(e['before'])}→{fmt_eval(e['after'])} | {SHORT[e['phase']]} | clk {mmss(e['clock'])}"
         + (f" | think {e['think']:.0f}s" if e.get("think") is not None else ""))
    if flags:
        s += f" | {flags}"
    return s + (f" | {e['fen']}" if with_fen else "")


def game_analysis(info, res, depth, max_errors=12):
    c = res["counts"]
    lines = [
        f"{info.date} {info.color[0].upper()} vs {info.opp} ({info.opp_rating}) "
        f"{info.result[0].upper()} {info.how} | {info.opening} | {info.moves} moves | depth {depth}",
        f"Inacc {c['inaccuracy']} | Mistakes {c['mistake']} | Blunders {c['blunder']} | "
        f"ACPL {res['acpl']} | missed mate {res['missed_mate']} | missed material {res['missed_material']}"
        f" | hung pieces {res['hung']}",
    ]
    bp = res["by_phase"]
    lines.append("Mistakes+blunders by phase: " + ", ".join(
        f"{SHORT[p]} {bp[p]['mistake'] + bp[p]['blunder']}" for p in bp))
    d = res["decisive"]
    lines.append("Decisive moment: " + (error_line(d, False) if d else "none (no mistakes)"))
    serious = sorted((e for e in res["errors"] if e["cls"] in ("mistake", "blunder")),
                     key=lambda e: -e["drop"])
    lines.append("Mistakes/blunders (move | eval mine before→after | phase | clock | FEN before move):")
    # chronological, capped
    keep = sorted(serious[:max_errors], key=res["errors"].index)
    lines += [error_line(e) for e in keep]
    if len(serious) > max_errors:
        lines.append(f"... +{len(serious) - max_errors} smaller mistakes omitted")
    if not serious:
        lines.append("(none)")
    if res.get("adv_moves"):
        k = res.get("adv_unflagged", 0)
        lines.append(f"Large advantage: you were above +5 for {res['adv_moves']} moves; material-losing moves may not be "
                     f"flagged ({k} unflagged drop(s) of 1.5+ pawns); use review_moves.")
    return "\n".join(lines)


def _pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "n/a"


def coaching_report(username, time_class, pairs, all_infos, depth, pending, skipped_short):
    """pairs: [(GameInfo, analysis)] analysed; all_infos: every game in the window."""
    n = len(pairs)
    w = sum(i.result == "win" for i in all_infos)
    l = sum(i.result == "loss" for i in all_infos)
    d = len(all_infos) - w - l
    rated = [i for i in reversed(all_infos) if i.my_rating]
    head = f"Coaching report {username} {time_class}: {len(all_infos)} games ({w}W {l}L {d}D)"
    if rated:
        head += f", rating {rated[0].my_rating}→{rated[-1].my_rating} ({rated[-1].my_rating - rated[0].my_rating:+d})"
    accs = [i.accuracy for i in all_infos if i.accuracy]
    if accs:
        head += f", avg accuracy {sum(accs) / len(accs):.1f}"
    lines = [head, f"Analysed {n}/{len(all_infos)} at depth {depth}"
             + (f" ({pending} left: call again to continue, results are cached)" if pending else "")
             + (f"; {skipped_short} too short to analyse" if skipped_short else "")]
    if not n:
        return "\n".join(lines)
    res = [r for _, r in pairs]
    tot = lambda k: sum(r["counts"][k] for r in res)  # noqa: E731
    lines.append(f"Per game: inaccuracies {tot('inaccuracy') / n:.1f}, mistakes {tot('mistake') / n:.1f}, "
                 f"blunders {tot('blunder') / n:.1f}, ACPL {sum(r['acpl'] for r in res) / n:.0f}")
    ph = []
    for p in ("opening", "middlegame", "endgame"):
        m = sum(r["by_phase"][p]["mistake"] for r in res) / n
        b = sum(r["by_phase"][p]["blunder"] for r in res) / n
        ph.append(f"{SHORT[p]} {m:.1f}m/{b:.1f}b")
    lines.append("Mistakes/blunders per game by phase: " + ", ".join(ph))
    hung = sum(r["hung"] for r in res)
    lines.append(f"Hung a piece in one move: {hung} ({sum(1 for r in res if r['hung'])}/{n} games) | "
                 f"missed mates {sum(r['missed_mate'] for r in res)} | "
                 f"missed material wins {sum(r['missed_material'] for r in res)}")
    serious = [e for r in res for e in r["errors"] if e["cls"] in ("mistake", "blunder")]
    if serious:
        cnt = lambda f: sum(1 for e in serious if f in e["flags"])  # noqa: E731
        other = sum(1 for e in serious if not set(e["flags"]) & {"ignored_threat", "left_hanging", "allowed_mate", "missed_mate", "missed_material"})
        lines.append(f"Causes of {len(serious)} mistakes/blunders: ignored opponent's threat {cnt('ignored_threat')} | "
                     f"moved into/left a piece hanging {cnt('left_hanging')} | allowed mate {cnt('allowed_mate')} | "
                     f"missed mate {cnt('missed_mate')} | missed free material {cnt('missed_material')} | other {other}")
    thinks = [t for r in res for t in r.get("thinks", []) if t is not None]
    if thinks:
        fast = lambda xs: sum(1 for t in xs if t < 5) / len(xs)  # noqa: E731
        bt = [e["think"] for e in serious if e.get("think") is not None]
        tl = f"Think time: avg {sum(thinks) / len(thinks):.0f}s/move, median {sorted(thinks)[len(thinks) // 2]:.0f}s"
        if bt:
            tl += (f" | mistakes/blunders avg {sum(bt) / len(bt):.0f}s; {100 * fast(bt):.0f}% under 5s "
                   f"vs {100 * fast(thinks):.0f}% of all moves")
            if fast(bt) >= 0.5 and fast(bt) > 1.3 * fast(thinks):
                tl += " → most errors are quick moves: slow down and check the opponent's last move"
        lines.append(tl)
    for color in ("white", "black"):
        agg = defaultdict(lambda: [0, 0.0])
        for i in all_infos:
            if i.color == color:
                agg[i.family][0] += 1
                agg[i.family][1] += 1 if i.result == "win" else 0.5 if i.result == "draw" else 0
        top = sorted(agg.items(), key=lambda kv: -kv[1][0])[:5]
        lines.append(f"As {color.title()}: " + ("; ".join(
            f"{f} {g}g {_pct(s, g)}" for f, (g, s) in top) or "no games"))
    losses = [i for i in all_infos if i.result == "loss"]
    if losses:
        kinds = defaultdict(int)
        for i in losses:
            kinds[i.loss_kind] += 1
        lines.append(f"Losses ({len(losses)}): " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
    blunders = [(e, r) for _, r in pairs for e in r["errors"] if e["cls"] == "blunder"]
    timed = [(e, r) for e, r in blunders if e["clock"] is not None and r["init_clock"]]
    tt = sum(1 for e, r in timed if e["clock"] < 0.1 * r["init_clock"])
    c20 = [(r["clock_at_20"], r["init_clock"]) for r in res if r["clock_at_20"] is not None and r["init_clock"]]
    time_line = f"Time trouble: {_pct(tt, len(timed))} of blunders with <10% clock ({tt}/{len(timed)})"
    if c20:
        avg = sum(c for c, _ in c20) / len(c20)
        time_line += f" | avg clock at move 20: {mmss(avg)} ({100 * sum(c / s for c, s in c20) / len(c20):.0f}% of start, {len(c20)} games)"
    lines.append(time_line)
    worst = sorted(((e, i) for i, r in pairs for e in [r["decisive"]] if e), key=lambda t: -t[0]["drop"])[:3]
    if worst:
        lines.append("Biggest swings (review these):")
        lines += [f"{i.date} {i.url} {error_line(e, False)}" for e, i in worst]
    return "\n".join(lines)


def review_table(info, rows, total, depth, side, done):
    lines = [f"Review {info.date} {info.color[0].upper()} vs {info.opp} ({info.opp_rating}) "
             f"{info.result[0].upper()} {info.how} | {side} | depth {depth} | {len(rows)}/{total} moves",
             "move | eval after (mover view) | best if different (= none) | cp loss | ?!/?/?? by win% drop, * = loss >=200cp not flagged, hangs = piece left en prise (net material)"]
    for r in rows:
        mark = MARK[r["cls"]] or ("*" if r["loss"] >= 200 else "")
        hang = "".join(f" | hangs {h['name']}(-{h['net']})" for h in r.get("hangs", []))
        lines.append(f"{r['n']} | {fmt_eval(r['after'])} | {r['best'] or '='} | {r['loss']}{mark}{hang}")
    if rows:
        lines.append(f"avg cp loss {sum(r['loss'] for r in rows) / len(rows):.0f} | "
                     + ", ".join(f"{k} {sum(1 for r in rows if r['cls'] == k)}" for k in ("inaccuracy", "mistake", "blunder")))
    if not done:
        lines.append(f"Time budget reached after {len(rows)}/{total} moves: call again to continue (progress is cached).")
    return "\n".join(lines)


METRICS = [("blunders", "blunders/game"), ("mistakes", "mistakes/game"), ("hung", "hung pieces/game"),
           ("ignored", "ignored threats/game"), ("missed_mate", "missed mates/game"), ("acpl", "ACPL"),
           ("think", "avg think s")]


def compute_metrics(pairs, infos):
    n = len(pairs)
    if not n:
        return None
    res = [r for _, r in pairs]
    th = [t for r in res for t in r.get("thinks", []) if t is not None]
    serious = [e for r in res for e in r["errors"] if e["cls"] in ("mistake", "blunder")]
    rated = [i.my_rating for i in infos if i.my_rating]
    return {"n": n, "rating": rated[0] if rated else None,
            "blunders": sum(r["counts"]["blunder"] for r in res) / n,
            "mistakes": sum(r["counts"]["mistake"] for r in res) / n,
            "hung": sum(r["hung"] for r in res) / n,
            "ignored": sum(1 for e in serious if "ignored_threat" in e["flags"]) / n,
            "missed_mate": sum(r["missed_mate"] for r in res) / n,
            "acpl": sum(r["acpl"] for r in res) / n,
            "think": sum(th) / len(th) if th else None}


def metrics_line(m, prev=None):
    parts = []
    for key, label in METRICS:
        if m.get(key) is None:
            continue
        s = f"{label} {m[key]:.1f}" if key != "acpl" else f"{label} {m[key]:.0f}"
        if prev and prev.get(key) is not None:
            s = s.replace(f" {m[key]:.1f}", f" {prev[key]:.1f}→{m[key]:.1f}") if key != "acpl" else f"{label} {prev[key]:.0f}→{m[key]:.0f}"
        parts.append(s)
    return " | ".join(parts)

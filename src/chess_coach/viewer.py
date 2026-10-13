"""Local web viewer: a chess.com-analysis-style page that fills in as Stockfish evaluates the game."""
import io
import json
import logging
import math
import queue
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chess
import chess.pgn

from .analysis import MATE_THRESHOLD, classify, win_pct
from .config import DEFAULT_USER

log = logging.getLogger("chess_coach.viewer")
PAGE = Path(__file__).with_name("viewer.html")
PORTS = range(8765, 8785)
LINE_PLIES = 6


def classify_ply(prev_cp: int, cp: int, white_moved: bool, played_uci: str, best_uci: str | None) -> dict:
    """Judge one move from evals given in White's view. Labels follow the chess.com style."""
    sign = 1 if white_moved else -1
    before, after = sign * prev_cp, sign * cp
    best = played_uci == best_uci
    loss = 0 if best else max(0, max(-3000, min(3000, before)) - max(-3000, min(3000, after)))
    drop = 0.0 if best else max(0.0, win_pct(before) - win_pct(after))
    cls = classify(drop)
    if not cls:
        cls = "best" if best else "excellent" if loss <= 25 else "good"
        if loss >= 200:  # far ahead: win% barely moves but material was probably given away
            cls = "inaccuracy"
    if before >= MATE_THRESHOLD and after < MATE_THRESHOLD and not best:
        cls = "miss"
    return {"cls": cls, "loss": loss, "drop": round(drop, 1), "best": best_uci}


def accuracy(drops: list[float]) -> float | None:
    """Lichess per-move accuracy formula, averaged."""
    if not drops:
        return None
    return round(sum(103.1668 * math.exp(-0.04354 * d) - 3.1669 for d in drops) / len(drops), 1)


class Job:
    def __init__(self, gid, info, depth):
        self.id, self.info, self.depth = gid, info, depth
        game = chess.pgn.read_game(io.StringIO(info.pgn))
        self.headers = dict(game.headers)
        board = game.board()
        self.fens, self.plies, self.boards = [board.fen()], [], [board.copy()]
        for node in game.mainline():
            san = board.san(node.move)
            self.plies.append({"san": san, "uci": node.move.uci(), "from": chess.square_name(node.move.from_square),
                               "to": chess.square_name(node.move.to_square), "clock": node.clock(),
                               "white": board.turn})
            board.push(node.move)
            self.fens.append(board.fen())
            self.boards.append(board.copy())
        n = len(self.fens)
        self.evals: list = [None] * n
        self.moves: list = [None] * n  # moves[i] judges ply i (the move leading to position i)
        self.done, self.error = False, None

    def game_json(self):
        h, i = self.headers, self.info
        return {"id": self.id, "url": i.url, "white": h.get("White", "?"), "black": h.get("Black", "?"),
                "white_elo": h.get("WhiteElo"), "black_elo": h.get("BlackElo"), "result": h.get("Result", "*"),
                "how": i.how, "outcome": i.result, "date": i.date, "opening": i.opening, "mine": i.color,
                "time_control": h.get("TimeControl"), "cc_accuracy": i.accuracy, "depth": self.depth,
                "fens": self.fens, "plies": self.plies, "init_clock": i.init_clock}

    def progress_json(self):
        done = sum(e is not None for e in self.evals)
        drops = {True: [], False: []}
        counts = {True: {}, False: {}}
        for m, p in zip(self.moves[1:], self.plies):
            if m:
                drops[p["white"]].append(m["drop"])
                counts[p["white"]][m["cls"]] = counts[p["white"]].get(m["cls"], 0) + 1
        summary = {("white" if w else "black"): {"accuracy": accuracy(drops[w]), "counts": counts[w]} for w in (True, False)}
        return {"done": self.done, "error": self.error, "n_done": done, "total": len(self.evals),
                "evals": self.evals, "moves": self.moves, "summary": summary}


class Viewer:
    def __init__(self, coach, port=None):
        self.coach, self.jobs, self.q = coach, {}, queue.Queue()
        self.lock = threading.Lock()
        self.server = None
        self.preferred = port

    # -- jobs ------------------------------------------------------------
    def ensure_job(self, gid, depth=14, username=DEFAULT_USER):
        with self.lock:
            job = self.jobs.get(gid)
            if job and job.depth >= depth:
                return job
        info, err = self.coach._resolve(f"https://www.chess.com/game/live/{gid}", username, time.time() + 25)
        if err:
            raise LookupError(err)
        job = Job(gid, info, depth)
        with self.lock:
            self.jobs[gid] = job
        self.q.put(job)
        return job

    def _worker(self):
        while True:
            job = self.q.get()
            try:
                evaluate = self.coach._game_evaluator(job.depth, pv_len=LINE_PLIES)
                for i, board in enumerate(job.boards):
                    ev = evaluate(board, chess.WHITE)
                    pv = [chess.Move.from_uci(u) for u in ev.pv]
                    job.evals[i] = {"cp": ev.cp, "best": board.san(pv[0]) if pv else None,
                                    "uci": ev.pv[0] if ev.pv else None,
                                    "line": board.variation_san(pv[:LINE_PLIES]) if pv else ""}
                    if i >= 1:
                        prev = job.evals[i - 1]
                        job.moves[i] = {**classify_ply(prev["cp"], ev.cp, job.plies[i - 1]["white"],
                                                       job.plies[i - 1]["uci"], prev["uci"]),
                                        "best_san": prev["best"]}
                job.done = True
            except Exception as e:  # keep the worker alive for the next game
                log.exception("viewer analysis failed")
                job.error = f"{type(e).__name__}: {e}"
                job.done = True

    # -- http ------------------------------------------------------------
    def start(self):
        if self.server:
            return self.port
        viewer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost"):  # blocks DNS-rebinding style access
                    return self._send(403, "forbidden", "text/plain")
                path = self.path.split("?")[0]
                try:
                    if path == "/":
                        return self._send(200, INDEX, "text/html")
                    if path == "/api/games":
                        return self._send(200, json.dumps(viewer.recent_games()))
                    m = re.fullmatch(r"/(game|api/game|api/evals)/(\d+)", path)
                    if not m:
                        return self._send(404, "not found", "text/plain")
                    kind, gid = m.groups()
                    # only the page itself (re)starts analysis; the polling API reuses the running job
                    job = (viewer.ensure_job(gid, viewer.depth_for(self.path)) if kind == "game"
                           else viewer.jobs.get(gid) or viewer.ensure_job(gid))
                    if kind == "game":
                        return self._send(200, PAGE.read_text(encoding="utf-8"), "text/html")
                    return self._send(200, json.dumps(job.game_json() if kind == "api/game" else job.progress_json()))
                except LookupError as e:
                    return self._send(404, json.dumps({"error": str(e)}))
                except Exception as e:
                    log.exception("viewer request failed")
                    return self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))

        ports = [self.preferred] if self.preferred is not None else list(PORTS)
        for port in ports:
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
                break
            except OSError:
                continue
        if not self.server:
            raise OSError("no free port for the viewer")
        self.port = self.server.server_address[1]
        threading.Thread(target=self._worker, daemon=True, name="viewer-worker").start()
        threading.Thread(target=self.server.serve_forever, daemon=True, name="viewer-http").start()
        log.info("viewer on http://127.0.0.1:%d/", self.port)
        return self.port

    def depth_for(self, raw_path):
        m = re.search(r"[?&]depth=(\d+)", raw_path)
        return max(6, min(int(m[1]), 30)) if m else 14

    def recent_games(self, username=DEFAULT_USER, n=20):
        return [{"id": re.search(r"(\d{6,})", i.url)[1], "date": i.date, "color": i.color, "opp": i.opp,
                 "opp_rating": i.opp_rating, "result": i.result, "how": i.how, "opening": i.opening,
                 "moves": i.moves} for i in self.coach._infos(username, "all", None, n)]

    def url(self, gid=None, depth=14):
        base = f"http://127.0.0.1:{self.port}/"
        return base if gid is None else f"{base}game/{gid}" + (f"?depth={depth}" if depth != 14 else "")


def open_in_browser(url: str) -> str:
    """Open `url` in the default (Windows, when under WSL) browser. Returns the method used or ''."""
    for cmd in (["wslview", url], ["explorer.exe", url], ["cmd.exe", "/c", "start", "", url], ["xdg-open", url]):
        if shutil.which(cmd[0]):
            try:  # explorer.exe exits 1 even on success; output must never reach our stdout
                subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10)
                return cmd[0]
            except (OSError, subprocess.TimeoutExpired):
                continue
    return ""


INDEX = """<!doctype html><meta charset=utf-8><title>chess-coach games</title>
<style>body{background:#262421;color:#bababa;font:15px system-ui,sans-serif;max-width:760px;margin:30px auto;padding:0 16px}
a{color:#8fbc4a;text-decoration:none}tr:hover{background:#2f2d2a}td{padding:6px 10px}h1{color:#fff;font-size:20px}
.W{color:#8fbc4a}.L{color:#ca3431}.D{color:#999}</style>
<h1>Recent games</h1><table id=t></table><script>
fetch('/api/games').then(r=>r.json()).then(g=>{document.getElementById('t').innerHTML=g.map(x=>
`<tr><td>${x.date}</td><td>${x.color[0].toUpperCase()}</td><td><a href="/game/${x.id}">vs ${x.opp} (${x.opp_rating})</a></td>
<td class="${x.result[0].toUpperCase()}">${x.result[0].toUpperCase()} ${x.how}</td><td>${x.opening}</td><td>${x.moves} moves</td></tr>`).join('')})
</script>"""

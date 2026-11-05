"""Offline Elle-style checker for recorded client read/write histories.

Follows the approach of Kingsbury & Alvaro, "Elle: Inferring Isolation Anomalies from
Experimental Observations", PVLDB 14(3), 2020: infer write-write (ww), write-read (wr)
and read-write anti-dependency (rw) edges between committed transactions from what
clients observed, then look for cycles (Adya's G0, G1c, G-single, G2-item) and for
non-cyclic anomalies (G1a, G1b, lost update).

Elle needs a recoverable version order. LedgerOps balance rows carry a `version` column
that each write increments by exactly one, so every (key, version) pair names one write
and versions sort in installation order. That plays the role of Elle's list-append or
unique register values.

History format: JSON Lines, one completed transaction attempt per line:

    {"index": 0, "process": 4711, "status": "ok", "ops": [["r", "acct", 1], ["w", "acct", 2]]}

status: "ok" (committed), "fail" (definitely aborted) or "info" (outcome unknown).
ops: ["r", key, version] for a read, ["w", key, version] for the version a write installed.
Optional fields such as "label" or "xid" are carried into reports unchanged.

Usage:  python -m tests.history.checker HISTORY.jsonl   (exit status 1 if anomalous)
"""

import json
import sys
from collections import defaultdict, deque
from pathlib import Path

CYCLE_ANOMALIES = ("G0", "G1c", "G-single", "G2-item")


def load_history(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def dump_history(history, path):
    with Path(path).open("w", encoding="utf-8") as stream:
        for entry in history:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")


def _validate(history):
    for position, txn in enumerate(history):
        if txn.get("status") not in {"ok", "fail", "info"}:
            raise ValueError(f"entry {position}: status must be ok, fail or info")
        for op in txn.get("ops", []):
            if (
                not isinstance(op, list)
                or len(op) != 3
                or op[0] not in {"r", "w"}
                or not isinstance(op[1], str)
                or type(op[2]) is not int
            ):
                raise ValueError(f"entry {position}: malformed micro-op {op!r}")


def _final_writes(txn):
    """The version each key ends at inside one transaction (later writes win)."""
    final = {}
    for kind, key, version in txn["ops"]:
        if kind == "w":
            final[key] = version
    return final


def check(history):
    """Return {"valid": bool, "anomalies": {name: [evidence, ...]}, "stats": {...}}."""
    _validate(history)
    anomalies = defaultdict(list)
    by_index = {txn["index"]: txn for txn in history}
    if len(by_index) != len(history):
        raise ValueError("transaction indexes must be unique")

    # Who wrote each (key, version)? Intermediate writes are versions a transaction
    # overwrote itself before it ended.
    final_writers = defaultdict(list)  # (key, version) -> [index] of final writes
    intermediate = {}  # (key, version) -> index
    for txn in history:
        final = _final_writes(txn)
        for kind, key, version in txn["ops"]:
            if kind == "w" and final[key] != version:
                intermediate[(key, version)] = txn["index"]
        for key, version in final.items():
            final_writers[(key, version)].append(txn["index"])

    committed_reads = defaultdict(set)  # (key, version) -> {index}
    for txn in history:
        if txn["status"] != "ok":
            continue
        seen_own = {}
        for kind, key, version in txn["ops"]:
            if kind == "w":
                seen_own[key] = version
                continue
            if key in seen_own:
                continue  # reading its own write is not a dependency
            committed_reads[(key, version)].add(txn["index"])
            writers = final_writers.get((key, version), [])
            if (key, version) in intermediate and intermediate[(key, version)] != txn["index"]:
                anomalies["G1b"].append(
                    {"reader": txn["index"], "key": key, "version": version,
                     "writer": intermediate[(key, version)]}
                )  # fmt: skip
            if writers and all(by_index[w]["status"] == "fail" for w in writers):
                anomalies["G1a"].append(
                    {"reader": txn["index"], "key": key, "version": version, "writers": writers}
                )

    # A transaction with unknown outcome ("info") counts as committed only if a
    # committed transaction observed one of its writes, as in Elle.
    def counts(index):
        txn = by_index[index]
        if txn["status"] == "ok":
            return True
        if txn["status"] == "info":
            return any(committed_reads.get(kv) for kv in _final_writes(txn).items())
        return False

    graph_nodes = {txn["index"] for txn in history if counts(txn["index"])}

    # Version order per key: recorded committed writes plus versions only ever read
    # (written by setup code outside the history; modelled as external writers).
    versions = defaultdict(set)
    for (key, version), writers in final_writers.items():
        if any(w in graph_nodes for w in writers):
            versions[key].add(version)
    for key, version in committed_reads:
        versions[key].add(version)

    def writers_of(key, version):
        return [w for w in final_writers.get((key, version), []) if w in graph_nodes]

    # Duplicate versions: two committed transactions both installed the same version.
    for (key, version), writers in sorted(final_writers.items()):
        live = [w for w in writers if w in graph_nodes]
        if len(live) > 1:
            anomalies["duplicate-version"].append({"key": key, "version": version, "writers": live})

    edges = defaultdict(set)  # (src, dst) -> {"ww", "wr", "rw"}

    def add(src, dst, kind):
        if src != dst and src in graph_nodes and dst in graph_nodes:
            edges[(src, dst)].add(kind)

    for key, installed in versions.items():
        ordered = sorted(installed)
        for previous, current in zip(ordered, ordered[1:], strict=False):
            for a in writers_of(key, previous):
                for b in writers_of(key, current):
                    add(a, b, "ww")
            for reader in committed_reads.get((key, previous), ()):
                for b in writers_of(key, current):
                    add(reader, b, "rw")
        for version in ordered:
            for a in writers_of(key, version):
                for reader in committed_reads.get((key, version), ()):
                    add(a, reader, "wr")

    # Lost update (Adya P4, Elle's lost-update): two committed transactions read the
    # same version of a key and both wrote that key.
    for (key, version), readers in sorted(committed_reads.items()):
        writers = sorted(r for r in readers if key in _final_writes(by_index[r]))
        if len(writers) > 1:
            anomalies["lost-update"].append({"key": key, "read_version": version, "txns": writers})

    # Read-modify-write registers: a committed transaction that read version v and then
    # installed anything but v+1 had another write slip in between its read and write.
    for index in sorted(graph_nodes):
        txn = by_index[index]
        first_read = {}
        for kind, key, version in txn["ops"]:
            if kind == "r":
                first_read.setdefault(key, version)
            elif key in first_read and version != first_read[key] + 1:
                anomalies["non-adjacent-rmw"].append(
                    {"txn": index, "key": key, "read": first_read[key], "wrote": version}
                )
                first_read[key] = version

    # Search the narrowest edge set first, as Elle does: a ww-only cycle is G0, a
    # ww/wr cycle is G1c, and a cycle that needs anti-dependency edges is G-single
    # (exactly one rw edge) or G2-item (two or more). All of them are G2 in Adya's sense
    # once an rw edge is involved.
    for search, allowed in (("G0", {"ww"}), ("G1c", {"ww", "wr"}), ("G2", {"ww", "wr", "rw"})):
        for cycle in _cycles(graph_nodes, edges, allowed):
            rw = sum(1 for step in cycle if step["kinds"] == ["rw"])
            if search == "G1c" and all(step["kinds"] == ["ww"] for step in cycle):
                continue  # already reported as G0
            if search == "G2" and rw == 0:
                continue  # already reported as G0 or G1c
            found = search if search != "G2" else ("G-single" if rw == 1 else "G2-item")
            anomalies[found].append({"cycle": cycle})

    return {
        "valid": not anomalies,
        "anomalies": {name: anomalies[name] for name in sorted(anomalies)},
        "stats": {
            "transactions": len(history),
            "committed": sum(1 for t in history if t["status"] == "ok"),
            "failed": sum(1 for t in history if t["status"] == "fail"),
            "indeterminate": sum(1 for t in history if t["status"] == "info"),
            "graph_nodes": len(graph_nodes),
            "edges": {
                kind: sum(1 for kinds in edges.values() if kind in kinds)
                for kind in ("ww", "wr", "rw")
            },
        },
    }


def _cycles(nodes, edges, allowed):
    """One shortest witness cycle per strongly connected component of the subgraph."""
    adjacency = defaultdict(list)
    kinds_of = {}
    for (src, dst), kinds in edges.items():
        usable = kinds & allowed
        if usable:
            adjacency[src].append(dst)
            kinds_of[(src, dst)] = sorted(usable)
    witnesses = []
    for component in _strongly_connected(nodes, adjacency):
        if len(component) < 2:
            continue
        best = None
        for start in sorted(component):
            path = _shortest_cycle(start, adjacency, component)
            if path and (best is None or len(path) < len(best)):
                best = path
        if best:
            witnesses.append(
                [
                    {"from": a, "to": b, "kinds": kinds_of[(a, b)]}
                    for a, b in zip(best, best[1:], strict=False)
                ]
            )
    return witnesses


def _shortest_cycle(start, adjacency, component):
    parents = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in sorted(adjacency[node]):
            if nxt not in component:
                continue
            if nxt == start:
                back = [node]  # node, parent(node), ..., start
                while parents[back[-1]] is not None:
                    back.append(parents[back[-1]])
                return [*reversed(back), start]
            if nxt not in parents:
                parents[nxt] = node
                queue.append(nxt)
    return None


def _strongly_connected(nodes, adjacency):
    """Iterative Tarjan; returns a list of components (sets)."""
    index_of, low, on_stack, stack, components = {}, {}, set(), [], []
    counter = 0
    for root in sorted(nodes):
        if root in index_of:
            continue
        work = [(root, iter(sorted(adjacency[root])))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in nodes:
                    continue
                if child not in index_of:
                    index_of[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(adjacency[child]))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index_of[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index_of[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                components.append(component)
    return components


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: python -m tests.history.checker HISTORY.jsonl", file=sys.stderr)
        return 2
    report = check(load_history(arguments[0]))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

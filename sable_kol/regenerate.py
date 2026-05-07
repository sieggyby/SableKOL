"""End-to-end deliverable refresh for one client.

Orchestrates the *non-SocialData* steps that turn current bank state into
fresh outreach deliverables and a fresh network graph:

  1. Classify any unclassified candidates (Anthropic Haiku — cheap)
  2. Recompute kol_strength_score for everyone
  3. Build the outreach plan (writes filtered + _full variants + symlinks)
  4. Build the network graph (reads latest plan, writes GEXF + HTML)

**Does NOT touch SocialData.** Per audit finding #1 / Codex P1, the
regenerate path is forbidden from spending the SocialData budget. All new
audience pulls / following pulls are operator-initiated via the dedicated
bulk-fetch CLI commands. Regenerate only refreshes deliverables based on
the bank state we already have.

Wired into a Hetzner systemd timer at ``deploy/regenerate/`` (one timer
per client, fires daily 03:00 UTC). Operator can also run it ad-hoc:

    sable-kol regenerate solstitch
    sable-kol regenerate solstitch --skip-classify
    sable-kol regenerate solstitch --output-dir /tmp/test_run/

Returns a structured summary so the systemd service log captures what
actually changed.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sable_kol.client_config import (
    ClientConfig,
    load_client_config,
    outreach_output_dir,
)


logger = logging.getLogger(__name__)


@dataclass
class RegenerateSummary:
    client_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    classify_skipped: bool
    classify_classified: int = 0
    classify_dropped: int = 0
    classify_cost_usd: float = 0.0
    score_rescored: int = 0
    outreach_files_written: list[str] = field(default_factory=list)
    network_files_written: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _run_step(
    name: str,
    args: list[str],
    *,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run a subprocess step. Returns (returncode, stdout, stderr).

    Each step is a separate process so failures stay isolated and the
    classify path can use Anthropic's batched code path naturally.
    """
    logger.info("step=%s cmd=%s", name, " ".join(shlex.quote(a) for a in args))
    proc = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        logger.warning(
            "step=%s rc=%d stderr=%s", name, proc.returncode, proc.stderr[-500:]
        )
    return proc.returncode, proc.stdout, proc.stderr


def _python_executable() -> str:
    """Return the absolute path to the SableKOL venv's Python.

    We're either running INSIDE that venv (sys.executable points at it),
    or being invoked from elsewhere — in which case the operator should
    pass ``--venv`` (TODO) or invoke us via the venv's sable-kol script.
    """
    return sys.executable


def _parse_classify_summary(stdout: str) -> dict:
    """Extract counts from the classify CLI's last line.

    Format (sable_kol/cli.py:classify): 'classify: {n} rows, {dropped} dropped, ${cost:.2f} spent'
    """
    out = {"classified": 0, "dropped": 0, "cost_usd": 0.0}
    for line in stdout.strip().splitlines()[::-1]:
        if line.startswith("classify:"):
            try:
                tokens = line.split()
                # 'classify:', '{n}', 'rows,', ...
                out["classified"] = int(tokens[1])
                # find ' dropped,' index
                for i, t in enumerate(tokens):
                    if t.endswith("dropped,"):
                        out["dropped"] = int(tokens[i - 1])
                # cost is '$X.XX'
                for t in tokens:
                    if t.startswith("$"):
                        out["cost_usd"] = float(t[1:].rstrip(","))
                        break
            except (ValueError, IndexError):
                pass
            break
    return out


def _parse_score_summary(stdout: str) -> int:
    """Extract `rescored N rows` from `enrich --score-only` output."""
    for line in stdout.strip().splitlines()[::-1]:
        if line.startswith("enrich --score-only:"):
            try:
                # 'enrich --score-only: rescored {n} rows'
                tokens = line.split()
                for i, t in enumerate(tokens):
                    if t == "rescored":
                        return int(tokens[i + 1])
            except (ValueError, IndexError):
                pass
            break
    return 0


def run_regenerate(
    client_id: str,
    *,
    skip_classify: bool = False,
    skip_score: bool = False,
    output_dir: Path | None = None,
    network_max_nodes: int = 5000,
    network_suffix: str = "interactive",
) -> RegenerateSummary:
    """Refresh deliverables for one client end-to-end.

    Args:
        client_id: Loaded via :func:`load_client_config` — must have a YAML.
        skip_classify: Skip the Haiku classification step. Useful when the
            cron has already classified everything recently.
        skip_score: Skip the kol_strength rescoring.
        output_dir: Override the outreach output dir.
        network_max_nodes: Hard cap on embedded nodes in the network HTML.
        network_suffix: Suffix on the generated GEXF/HTML filenames.

    Returns:
        :class:`RegenerateSummary` with timestamps, counts, costs, and the
        list of files actually written. Errors don't raise — they're
        appended to ``summary.errors`` so the cron log captures partial
        success.
    """
    config: ClientConfig = load_client_config(client_id)
    started = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()
    summary = RegenerateSummary(
        client_id=config.client_id,
        started_at=started_iso,
        finished_at="",
        duration_seconds=0.0,
        classify_skipped=skip_classify,
    )

    py = _python_executable()
    out_dir = output_dir or outreach_output_dir(client_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 1. Classify any unclassified candidates
    # ------------------------------------------------------------------
    if not skip_classify:
        rc, stdout, stderr = _run_step(
            "classify",
            [py, "-m", "sable_kol.cli", "classify"],
        )
        if rc != 0:
            summary.errors.append(f"classify: rc={rc} {stderr[-200:]}")
        else:
            parsed = _parse_classify_summary(stdout)
            summary.classify_classified = parsed["classified"]
            summary.classify_dropped = parsed["dropped"]
            summary.classify_cost_usd = parsed["cost_usd"]

    # ------------------------------------------------------------------
    # 2. Recompute kol_strength_score
    # ------------------------------------------------------------------
    if not skip_score:
        rc, stdout, stderr = _run_step(
            "score",
            [py, "-m", "sable_kol.cli", "enrich", "--score-only"],
        )
        if rc != 0:
            summary.errors.append(f"score: rc={rc} {stderr[-200:]}")
        else:
            summary.score_rescored = _parse_score_summary(stdout)

    # ------------------------------------------------------------------
    # 3. Build outreach plan (writes both filtered and _full variants)
    # ------------------------------------------------------------------
    plan_args = [
        py,
        str(Path(__file__).parent.parent / "scripts" / "build_outreach_plan.py"),
        "--client",
        client_id,
        "--output-dir",
        str(out_dir),
    ]
    rc, stdout, stderr = _run_step("outreach_plan", plan_args)
    if rc != 0:
        summary.errors.append(f"outreach_plan: rc={rc} {stderr[-300:]}")
    else:
        for line in stdout.splitlines():
            if line.strip().startswith("wrote "):
                summary.outreach_files_written.append(
                    line.strip().removeprefix("wrote ").strip()
                )

    # ------------------------------------------------------------------
    # 4. Build network graph (reads the plan we just wrote)
    # ------------------------------------------------------------------
    graph_args = [
        py,
        str(Path(__file__).parent.parent / "scripts" / "build_network_graph.py"),
        "--client",
        client_id,
        "--output-dir",
        str(out_dir),
        "--max-nodes",
        str(network_max_nodes),
        "--suffix",
        network_suffix,
    ]
    rc, stdout, stderr = _run_step("network_graph", graph_args)
    if rc != 0:
        summary.errors.append(f"network_graph: rc={rc} {stderr[-300:]}")
    else:
        for line in stdout.splitlines():
            if "wrote" in line and ("/" in line or line.strip().endswith((".gexf", ".html"))):
                summary.network_files_written.append(line.strip())

    finished = time.monotonic()
    summary.finished_at = datetime.now(timezone.utc).isoformat()
    summary.duration_seconds = round(finished - started, 2)
    return summary


def main() -> int:
    """CLI entry point — also wired through `sable-kol regenerate`."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Refresh deliverables for one client (no SocialData spend)."
    )
    ap.add_argument("client_id")
    ap.add_argument("--skip-classify", action="store_true")
    ap.add_argument("--skip-score", action="store_true")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--network-max-nodes", type=int, default=5000)
    ap.add_argument("--network-suffix", default="interactive")
    ap.add_argument("--json", action="store_true",
                    help="Print summary as JSON (for systemd / logs).")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )

    summary = run_regenerate(
        args.client_id,
        skip_classify=args.skip_classify,
        skip_score=args.skip_score,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        network_max_nodes=args.network_max_nodes,
        network_suffix=args.network_suffix,
    )

    if args.json:
        print(json.dumps(asdict(summary), indent=2))
    else:
        print(f"client       {summary.client_id}")
        print(f"duration     {summary.duration_seconds}s")
        print(f"classify     classified={summary.classify_classified} "
              f"dropped={summary.classify_dropped} "
              f"cost=${summary.classify_cost_usd:.2f}")
        print(f"score        rescored={summary.score_rescored}")
        print(f"outreach     {len(summary.outreach_files_written)} files")
        for f in summary.outreach_files_written[:8]:
            print(f"  - {f}")
        print(f"network      {len(summary.network_files_written)} files")
        for f in summary.network_files_written[:4]:
            print(f"  - {f}")
        if summary.errors:
            print(f"errors       {len(summary.errors)}")
            for e in summary.errors:
                print(f"  ! {e}")

    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())

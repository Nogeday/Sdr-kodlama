"""``eds measure`` — run KTR quantitative-spec measurements and write the jury report."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..measure.base import MeasurementResult
from .util import load_app, make_prompt

#: Exit code when at least one measurement failed (skipped ones do not count).
EXIT_FAILED = 2


def cmd_measure(args: argparse.Namespace) -> int:
    from ..measure.report import console_table
    from ..measure.session import resolve_spec_ids, run_suite

    app = load_app(args)
    ids = resolve_spec_ids(args.specs or ["all"])
    hardware = any(d.backend != "sim" for d in app.devices)
    prompt = make_prompt(bool(args.yes)) if hardware else None

    def progress(res: MeasurementResult) -> None:
        print(f"  {res.spec_id}: {res.status} — {res.summary}", file=sys.stderr, flush=True)

    kw: dict[str, Any] = {"quick": bool(args.quick), "out_dir": args.out, "prompt": prompt}
    if args.source:
        kw["source_kind"] = args.source
    print(f"Ölçümler: {', '.join(ids)}{' (hızlı)' if args.quick else ''}", file=sys.stderr, flush=True)
    suite = run_suite(app, ids, write=not args.no_report, publish=bool(args.publish), title=args.title,
                      on_result=progress, **kw)
    print(console_table(suite.results))
    if suite.report_dir is not None:
        print(f"Rapor: {suite.report_dir}")
    failed = [r.spec_id for r in suite.results if r.status == "failed"]
    if failed:
        print(f"Başarısız ölçüm(ler): {', '.join(failed)}", file=sys.stderr)
        return EXIT_FAILED
    return 0

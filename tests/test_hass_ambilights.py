#!/usr/bin/env python3
"""Run the HASS Ambilights test suite section by section.

The suite is integration-shaped: sections share one imported module, one fake Home
Assistant and one fake screen, and mutating that state per section is the point. So
sections run in order in one namespace, but each is compiled and executed on its own,
which means a failure names the section that broke and does not hide the fifty after it.

    python tests/test_hass_ambilights.py            # everything
    python tests/test_hass_ambilights.py --list     # section names
    python tests/test_hass_ambilights.py -k zones   # sections matching "zones"
    python tests/test_hass_ambilights.py --failfast

`pytest tests/test_hass_ambilights.py` collects one test per section, in order.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
BODY = os.path.join(HERE, "suite_body.py")
MARKER = re.compile(r"^# --- (?P<name>.+?) -+ *$", re.MULTILINE)
HEADER_NAME = "(setup)"


def split_sections(source: str) -> list[tuple[str, str]]:
    """Header plus one chunk per `# --- name ---` marker."""
    marks = list(MARKER.finditer(source))
    if not marks:
        return [(HEADER_NAME, source)]
    sections = [(HEADER_NAME, source[: marks[0].start()])]
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(source)
        sections.append((mark.group("name").strip(), source[mark.start():end]))
    return sections


def load() -> list[tuple[str, str]]:
    with open(BODY, encoding="utf-8") as handle:
        return split_sections(handle.read())


class Section:
    def __init__(self, name: str, code: str):
        self.name = name
        self.code = code
        self.status = "pending"
        self.seconds = 0.0
        self.error = ""

    def run(self, namespace: dict) -> None:
        started = time.perf_counter()
        try:
            exec(compile(self.code, f"<section: {self.name}>", "exec"), namespace)
        except BaseException as exc:                  # a section may raise anything
            self.status = "failed"
            self.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            if not isinstance(exc, AssertionError):
                self.error += "\n" + traceback.format_exc(limit=8)
        else:
            self.status = "passed"
        self.seconds = time.perf_counter() - started


def run_sections(sections: list[Section], failfast: bool = False) -> dict:
    namespace = {"__name__": "__main__", "__file__": BODY}
    for section in sections:
        section.run(namespace)
        if section.status == "failed":
            print(f"  ✖ {section.name}: {section.error.splitlines()[0]}")
            if section.name == HEADER_NAME:
                # nothing after this can be meaningful, and every later section
                # would fail with a NameError that hides the real cause
                print("\nthe setup section failed - the rest cannot run")
                for later in sections[sections.index(section) + 1:]:
                    later.status = "blocked"
                break
            if failfast:
                break
    return namespace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print section names and exit")
    parser.add_argument("-k", default="", help="only sections whose name contains this")
    parser.add_argument("--failfast", action="store_true", help="stop at the first failure")
    parser.add_argument("--quiet", action="store_true", help="only the summary")
    args = parser.parse_args(argv)

    wanted = [(name, code) for name, code in load()
              if not args.k or args.k.lower() in name.lower()]
    sections = [Section(name, code) for name, code in wanted]

    if args.list:
        for section in sections:
            print(section.name)
        return 0

    print(f"running {len(sections)} section(s) from {os.path.basename(BODY)}")
    started = time.perf_counter()
    run_sections(sections, failfast=args.failfast)
    elapsed = time.perf_counter() - started

    failed = [s for s in sections if s.status == "failed"]
    skipped = [s for s in sections if s.status in ("pending", "blocked")]
    passed = [s for s in sections if s.status == "passed"]

    if not args.quiet:
        print()
        for section in sorted(passed, key=lambda s: -s.seconds)[:3]:
            print(f"  slowest: {section.seconds * 1000:6.0f} ms  {section.name}")
    print(f"\n{len(passed)} passed, {len(failed)} failed"
          + (f", {len(skipped)} not run" if skipped else "")
          + f" in {elapsed:.1f}s")
    if failed:
        print("\nfailed sections:")
        for section in failed:
            print(f"  ✖ {section.name}\n    {section.error.splitlines()[0]}")
        return 1
    if skipped:
        print("sections after the failure were not run (--failfast)")
        return 1
    print("ALL CHECKS PASSED")
    return 0


# ---- pytest: one test per section, in file order -----------------------------
_SECTIONS = load()
_SHARED: dict = {}
_RAN: list[Section] = []


def _run_upto(index: int) -> None:
    """Sections share state, so the prefix runs once and each test checks its own."""
    if not _SHARED:
        _SHARED.update({"__name__": "__main__", "__file__": BODY})
    while len(_RAN) < index:
        name, code = _SECTIONS[len(_RAN)]
        section = Section(name, code)
        section.run(_SHARED)          # one namespace for the whole session
        _RAN.append(section)


def _make_test(index: int, name: str):
    def test_section() -> None:
        _run_upto(index)
        section = _RAN[index - 1]
        if section.status != "passed":
            raise AssertionError(f"{name}: {section.error.splitlines()[0]}")
    test_section.__name__ = f"test_{index:02d}_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}"
    return test_section


for _index, (_name, _code) in enumerate(_SECTIONS[1:], start=2):
    globals()[_make_test(_index, _name).__name__] = _make_test(_index, _name)


if __name__ == "__main__":
    sys.exit(main())

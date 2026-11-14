"""One command to check everything: self-tests, the test suite, the attack
showcase and a quick benchmark, each on scratch data (never the live chain).

    py -3 check.py            # about 30 s
    py -3 check.py --quick    # skip the benchmark
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def step(name, args, parse=None):
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable] + args, cwd=HERE, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    out = (p.stdout + p.stderr).strip()
    detail = parse(out) if parse and p.returncode == 0 else (out.splitlines() or [""])[-1][:100]
    print(f"{'PASS' if p.returncode == 0 else 'FAIL'}  {name:<28} {dt:5.1f}s  {detail}")
    if p.returncode:
        print("      " + "\n      ".join(out.splitlines()[-15:]))
    return p.returncode == 0


def main():
    quick = "--quick" in sys.argv
    results = [
        step("wallets self-test", ["wallets.py"], lambda o: o.splitlines()[-1][:80]),
        step("chain self-test", ["chain.py"], lambda o: o.splitlines()[-1][:80]),
        step("paywall self-test", ["paywall.py"], lambda o: o.splitlines()[-1][:80]),
        step("test suite", ["-m", "unittest", "discover", "-s", "tests"],
             lambda o: next((ln for ln in o.splitlines() if ln.startswith("Ran ")), "")),
        step("attack showcase", ["attacks.py"], lambda o: o.splitlines()[-1]),
    ]
    if not quick:
        def bench(o):
            r = json.loads(o[o.index("{"):])
            return (f"on-chain {r['on_chain']['calls_per_s']}/s, channel {r['channel']['calls_per_s']}/s, "
                    f"8 agents {r['parallel_channels']['calls_per_s']}/s")
        results.append(step("benchmark", ["bench.py", "50", "1000"], bench))
    ok = all(results)
    print(f"\n{'ALL PASSED' if ok else 'FAILED'} ({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

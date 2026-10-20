"""Checks every built test part must pass, on any drawing: KOMPAS marks no feature with an error
(«Тело с нарушенной топологией» and the like), and no operation was dropped for such an error."""


def clean(res: dict) -> list[str]:
    """What is wrong with a build_part result in this sense (empty when clean)."""
    bad = [f"операция {x['n']} с ошибкой КОМПАСа: {x.get('comment') or x.get('op')}" for x in res.get("invalid") or []]
    bad += [f"операция {x['n']} убрана из-за ошибки КОМПАСа: {x.get('comment') or x.get('op')}"
            for x in res.get("skipped") or [] if "КОМПАС пометил" in x.get("error", "")]
    f = res.get("failed") or {}
    if "КОМПАС пометил" in f.get("error", ""):
        bad.append(f"построение остановлено ошибкой КОМПАСа на операции {f['n']}")
    return bad


def check(res: dict) -> bool:
    bad = clean(res)
    for b in bad:
        print("KOMPAS ERROR:", b)
    return not bad

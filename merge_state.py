#!/usr/bin/env python3
"""겹쳐 실행된 워크플로의 state.json 을 병합한다.

    python merge_state.py <우리것.json> <원격것.json>
    -> 원격것.json 자리에 병합 결과를 쓴다.

왜 필요한가
-----------
cron-job.org 트리거와 시간별 안전망 cron 이 겹치면 두 실행이 각각
state.json 을 고친 뒤 동시에 push 하려 한다. git rebase 는 이걸
자동으로 못 풀고 충돌로 죽는다.

단순히 한쪽을 버리면 안 된다. 먼저 끝난 실행이 알림을 보내고 seen 에
넣어둔 회차를, 나중 실행이 모르는 상태로 덮어쓰면 그 회차를 다시
알리게 된다. 그래서 seen 은 반드시 합집합으로 합친다.
"""

import json
import sys


def merge(ours, theirs):
    skip = ("seen", "gates", "baselined")
    out = dict(theirs)
    out.update({k: v for k, v in ours.items() if k not in skip})

    # 알림을 보낸 기록은 어느 쪽도 잃으면 안 된다. 합집합.
    out["seen"] = sorted(set(ours.get("seen", [])) | set(theirs.get("seen", [])))

    # 기준선을 잡은 대상도 합집합. 한쪽이라도 잡았으면 잡은 것이다.
    # 잃어버리면 그 대상을 새로 추가한 것으로 오해해 다시 기준선을 잡는데,
    # 그 사이에 열린 회차를 놓치게 된다.
    out["baselined"] = sorted(
        set(ours.get("baselined", [])) | set(theirs.get("baselined", [])))

    # 게이트 스냅샷은 지점별로 최신 쪽을 쓴다. 값이 다르면 다음 실행에서
    # 변화로 판단해 전체 스캔이 한 번 더 도는 것뿐이라 안전하다.
    gates = dict(theirs.get("gates", {}))
    gates.update(ours.get("gates", {}))
    out["gates"] = gates

    # 단조 증가해야 하는 값들
    for key in ("run_no", "tg_offset", "last_block_warn"):
        vals = [v for v in (ours.get(key), theirs.get(key)) if v is not None]
        if vals:
            out[key] = max(vals)

    out["initialized"] = bool(ours.get("initialized") or theirs.get("initialized"))

    last = [v for v in (ours.get("last_run"), theirs.get("last_run")) if v]
    if last:
        out["last_run"] = max(last)

    return out


def load(path):
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def main():
    ours_path, theirs_path = sys.argv[1], sys.argv[2]
    ours, theirs = load(ours_path), load(theirs_path)
    merged = merge(ours, theirs)

    with open(theirs_path, "w", encoding="utf-8") as fp:
        json.dump(merged, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")

    print("state 병합: 우리 seen {} + 원격 seen {} -> {}".format(
        len(ours.get("seen", [])), len(theirs.get("seen", [])),
        len(merged["seen"])))


if __name__ == "__main__":
    main()
